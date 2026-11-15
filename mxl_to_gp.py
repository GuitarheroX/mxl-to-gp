#!/usr/bin/env python3
"""Convert Audiveris/MusicXML .mxl or .xml files to Guitar Pro 8 .gp files.

The converter is score-agnostic: it discovers parts and measures, merges
non-overlapping page/system fragments that share a part name, reads time/key/
clef/tempo metadata, and writes a GP8 (GPIF) package using the supplied .gp as
a formatting template.  Tracks without a Guitar Pro equivalent default to a
nylon-guitar track so they can be re-instrumented in Guitar Pro.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import permutations
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

# Guitar Pro stores String 0 = lowest string, 5 = highest string.
TUNING = [40, 45, 50, 55, 59, 64]  # E2 A2 D3 G3 B3 E4
NUM_STRINGS = 6
HIGH_E = 5

TRACK_COLORS = [
    "237 116 116", "116 160 237", "116 200 140", "237 190 116",
    "190 116 237", "116 220 220", "237 140 190", "160 160 160",
    "200 180 100", "130 180 237", "180 220 130", "237 150 100",
]
GENERIC_WORDS = {"guitar", "gtr", "gt", "part", "track", "voice", "staff", "stave", "instrument"}
ROMAN = {"1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v", "6": "vi"}
ROMAN_UPPER = {"i": "I", "ii": "II", "iii": "III", "iv": "IV", "v": "V", "vi": "VI"}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class SourceNote:
    step: str
    alter: int
    octave: int
    duration: int
    voice: str
    note_type: str | None
    dots: int
    tuplet: tuple[int, int] | None
    tie_start: bool
    tie_stop: bool
    chord: bool = False
    rest: bool = False


@dataclass
class ChordEvent:
    onset: int
    duration: int
    voice: str
    notes: list[SourceNote] = field(default_factory=list)
    rest: bool = False
    gp_notes: list["GpNote"] = field(default_factory=list)


@dataclass
class MeasureData:
    number: int
    divisions: int
    events: list[ChordEvent]
    raw_rest_total: int = 0
    clef_octave: int = 0
    time_beats: int = 4
    time_beat_type: int = 4
    key_fifths: int = 0
    key_mode: str = "Major"
    clef: str = "G2"

    @property
    def time_signature(self) -> str:
        return f"{self.time_beats}/{self.time_beat_type}"

    @property
    def duration(self) -> int:
        return self.divisions * self.time_beats * 4 // self.time_beat_type


@dataclass
class TrackSpec:
    name: str
    is_guitar: bool
    sources: dict[int, str]  # 1-based relative measure -> part id
    parts: list[str] = field(default_factory=list)


@dataclass
class GpNote:
    step: str
    alter: int
    octave: int
    string: int
    fret: int
    midi: int
    concert_octave: int
    transposed_octave: int
    tie_origin: bool = False
    tie_dest: bool = False
    id: int = -1


@dataclass(frozen=True)
class RhythmSpec:
    value: str
    beats: Fraction
    dots: int = 0
    num: int = 1
    den: int = 1

    @property
    def key(self) -> tuple[str, int, int, int]:
        return (self.value, self.dots, self.num, self.den)


@dataclass
class BeatOut:
    rhythm: RhythmSpec
    notes: list[GpNote] = field(default_factory=list)
    dynamic: str = "MF"
    id: int = -1


@dataclass
class VoiceOut:
    beats: list[BeatOut] = field(default_factory=list)
    id: int = -1


@dataclass
class BarOut:
    track: int
    index: int
    voices: list[VoiceOut] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MusicXML parsing
# ---------------------------------------------------------------------------
STEP_TO_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def written_midi(step: str, alter: int, octave: int) -> int:
    return 12 * (octave + 1) + STEP_TO_SEMITONE[step] + alter


def read_mxl(path: Path) -> ET.Element:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            rootfile = None
            if "META-INF/container.xml" in names:
                c = ET.fromstring(z.read("META-INF/container.xml"))
                for rf in c.iter():
                    if rf.tag.endswith("rootfile"):
                        rootfile = rf.attrib.get("full-path")
                        break
            if rootfile is None:
                rootfile = next((n for n in names if n.lower().endswith(".xml") and not n.startswith("META-INF")), None)
            if rootfile is None:
                raise SystemExit("no MusicXML rootfile in archive")
            return ET.fromstring(z.read(rootfile))
    return ET.parse(path).getroot()


def gp_clef(sign: str | None, line: str | None) -> str:
    sign = (sign or "G").upper()
    line = (line or "2").strip()
    if sign == "G":
        return "G2" if line != "1" else "G1"
    if sign == "F":
        return "F4" if line != "3" else "F3"
    if sign == "C":
        return f"C{line}"
    return "G2"


def parse_parts(root: ET.Element) -> dict[str, list[MeasureData]]:
    parts: dict[str, list[MeasureData]] = {}
    for part in root.findall("part"):
        pid = part.get("id")
        div = 1
        clef_oct = 0
        time_beats, time_beat_type = 4, 4
        key_fifths, key_mode = 0, "Major"
        clef = "G2"
        measures: list[MeasureData] = []
        for idx, measure in enumerate(part.findall("measure"), 1):
            attrs = measure.find("attributes")
            if attrs is not None:
                if attrs.findtext("divisions"):
                    div = int(attrs.findtext("divisions"))
                time = attrs.find("time")
                if time is not None and time.findtext("beats"):
                    time_beats = int(time.findtext("beats"))
                    time_beat_type = int(time.findtext("beat-type") or 4)
                key = attrs.find("key")
                if key is not None and key.findtext("fifths") is not None:
                    key_fifths = int(key.findtext("fifths"))
                    key_mode = key.findtext("mode") or "Major"
                clef_node = attrs.find("clef")
                if clef_node is not None:
                    clef_oct = int(clef_node.findtext("clef-octave-change") or 0)
                    clef = gp_clef(clef_node.findtext("sign"), clef_node.findtext("line"))
            try:
                number = int(measure.get("number"))
            except (TypeError, ValueError):
                number = idx
            events: list[ChordEvent] = []
            cursor = 0
            last_onset = 0
            raw_rest_total = 0
            for child in list(measure):
                tag = child.tag
                if tag == "backup":
                    cursor -= int(child.findtext("duration") or 0)
                    continue
                if tag == "forward":
                    cursor += int(child.findtext("duration") or 0)
                    continue
                if tag != "note":
                    continue
                dur = int(child.findtext("duration") or 0)
                is_chord = child.find("chord") is not None
                is_rest = child.find("rest") is not None
                onset = last_onset if is_chord else cursor
                if not is_chord:
                    cursor += dur
                last_onset = onset
                voice = child.findtext("voice") or "1"
                dots = len(child.findall("dot"))
                tm = child.find("time-modification")
                tuplet = None
                if tm is not None:
                    try:
                        tuplet = (int(tm.findtext("actual-notes")), int(tm.findtext("normal-notes")))
                    except (TypeError, ValueError):
                        tuplet = None
                tie_start = any(t.get("type") == "start" for t in child.findall("tie"))
                tie_stop = any(t.get("type") == "stop" for t in child.findall("tie"))
                if is_rest:
                    raw_rest_total += dur
                    events.append(ChordEvent(onset, dur, voice, [], rest=True))
                    continue
                pitch = child.find("pitch")
                if pitch is None:
                    continue
                step = pitch.findtext("step") or "C"
                alter = int(pitch.findtext("alter") or 0)
                octave = int(pitch.findtext("octave") or 4)
                sn = SourceNote(step, alter, octave, dur, voice, child.findtext("type"), dots, tuplet,
                                tie_start, tie_stop)
                if is_chord and events:
                    target = events[-1]
                    if not target.rest and target.onset == onset:
                        target.notes.append(sn)
                        continue
                events.append(ChordEvent(onset, dur, voice, [sn]))
            measures.append(MeasureData(number, div, events, raw_rest_total, clef_oct,
                                        time_beats, time_beat_type, key_fifths, key_mode, clef))
        parts[pid] = measures
    return parts


def normalize_part_name(name: str) -> str:
    raw = (name or "").lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", raw) if t]
    meaningful = [t for t in tokens if t not in GENERIC_WORDS]
    out: list[str] = []
    for t in meaningful:
        if t == "11":
            out.append("ii")
        elif t == "111":
            out.append("iii")
        elif t in ROMAN:
            out.append(ROMAN[t])
        elif t == "8":
            out.append("s")
        else:
            out.append(t)
    return " ".join(out) or " ".join(tokens) or "track"


def _display_track_name(key: str, names: list[str]) -> str:
    lower = [n.lower() for n in names]
    if key in ROMAN_UPPER and any(("gtr" in n or "guitar" in n) for n in lower):
        return f"Guitar {ROMAN_UPPER[key]}"
    cleaned = [n.strip() for n in names if n and n.strip()]
    if not cleaned:
        return key.title()
    value = Counter(cleaned).most_common(1)[0][0]
    cleaned_value = value.rstrip(".") or value
    if cleaned_value.lower() in ("s", "a", "t", "b"):
        return cleaned_value.upper()
    return cleaned_value


def _compress_measures(measures: Iterable[int]) -> list[list[int]]:
    values = sorted(measures)
    if not values:
        return []
    ranges = [[values[0], values[0]]]
    for value in values[1:]:
        if value == ranges[-1][1] + 1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ranges


def resolve_tracks(parts: dict[str, list[MeasureData]], part_names: dict[str, str],
                   layout: str = "auto") -> tuple[list[TrackSpec], int, int]:
    """Return (tracks, number_of_measures, source_base_measure_number)."""
    all_numbers = [m.number for ms in parts.values() for m in ms]
    if not all_numbers:
        raise SystemExit("MusicXML has no measures")
    base = min(all_numbers)
    num_measures = max(all_numbers) - base + 1
    lookups: dict[str, dict[int, MeasureData]] = {}
    coverage: dict[str, set[int]] = {}
    for pid, measures in parts.items():
        by_number = {m.number - base + 1: m for m in measures}
        lookups[pid] = by_number
        coverage[pid] = {
            n for n, m in by_number.items() if any(not ev.rest for ev in m.events)
        }

    if layout == "parts":
        tracks: list[TrackSpec] = []
        used_names: Counter[str] = Counter()
        for pid in parts:
            cov = coverage[pid]
            if not cov:
                continue
            name = part_names.get(pid) or pid
            used_names[name] += 1
            if used_names[name] > 1:
                name = f"{name} {used_names[name]}"
            is_guitar = any(s in (part_names.get(pid) or "").lower() for s in ("gtr", "guitar"))
            tracks.append(TrackSpec(name, is_guitar, {m: pid for m in cov}, [pid]))
        return tracks, num_measures, base

    # Group parts by normalized part name, then merge fragments of one group
    # whose measure coverage does not overlap.  Overlapping parts with the same
    # name are kept as separate tracks (they are usually different voices).
    groups: dict[str, list[str]] = {}
    for pid in parts:
        if not coverage[pid]:
            continue
        groups.setdefault(normalize_part_name(part_names.get(pid) or pid), []).append(pid)

    tracks = []
    for key, pids in groups.items():
        group_tracks: list[dict] = []
        for pid in pids:
            cov = coverage[pid]
            placed = False
            for tr in group_tracks:
                if not (tr["coverage"] & cov):
                    tr["coverage"] |= cov
                    tr["sources"].update({m: pid for m in cov})
                    tr["parts"].append(pid)
                    placed = True
                    break
            if not placed:
                group_tracks.append({
                    "coverage": set(cov),
                    "sources": {m: pid for m in cov},
                    "parts": [pid],
                })
        for idx, tr in enumerate(group_tracks):
            names = [part_names.get(pid, pid) for pid in tr["parts"]]
            name = _display_track_name(key, names)
            if len(group_tracks) > 1:
                name = f"{name} {idx + 1}"
            is_guitar = any(any(s in n.lower() for s in ("gtr", "guitar")) for n in names)
            tracks.append(TrackSpec(name, is_guitar, tr["sources"], tr["parts"]))
    return tracks, num_measures, base


# ---------------------------------------------------------------------------
# Rhythm decomposition
# ---------------------------------------------------------------------------
BASE_VALUES: list[tuple[str, Fraction]] = [
    ("Whole", Fraction(4)),
    ("Half", Fraction(2)),
    ("Quarter", Fraction(1)),
    ("Eighth", Fraction(1, 2)),
    ("16th", Fraction(1, 4)),
    ("32nd", Fraction(1, 8)),
    ("64th", Fraction(1, 16)),
]
TYPE_TO_VALUE = {
    "whole": "Whole", "half": "Half", "quarter": "Quarter", "eighth": "Eighth",
    "16th": "16th", "32nd": "32nd", "64th": "64th",
}
DOT_FACTORS = [Fraction(1), Fraction(3, 2), Fraction(7, 4)]
TUPLETS = [(1, 1), (3, 2), (5, 4), (6, 4), (7, 4), (9, 8)]


def _all_specs() -> list[RhythmSpec]:
    specs: dict[Fraction, RhythmSpec] = {}
    for name, base in BASE_VALUES:
        for dots, factor in enumerate(DOT_FACTORS):
            for num, den in TUPLETS:
                beats = base * factor * Fraction(den, num)
                if beats <= 0:
                    continue
                # Keep the first/cleanest spelling for a duration.
                spec = RhythmSpec(name, beats, dots, num, den)
                old = specs.get(beats)
                if old is None:
                    specs[beats] = spec
                elif dots == 0 and num == 1 and den == 1:
                    specs[beats] = spec
    return sorted(specs.values(), key=lambda s: (-s.beats, s.dots, s.num, s.den))


ALL_SPECS = _all_specs()
SINGLE_BY_BEATS: dict[Fraction, list[RhythmSpec]] = {}
for _s in ALL_SPECS:
    SINGLE_BY_BEATS.setdefault(_s.beats, []).append(_s)


def _build_search_specs() -> list[RhythmSpec]:
    specs: dict[Fraction, RhythmSpec] = {}
    # Prefer plain values, then dotted values, then common tuplets.
    for name, base in BASE_VALUES:
        for dots in (0, 1):
            beats = base * DOT_FACTORS[dots]
            specs.setdefault(beats, RhythmSpec(name, beats, dots, 1, 1))
    for name, base in BASE_VALUES:
        for num, den in ((3, 2), (5, 4), (7, 4)):
            beats = base * Fraction(den, num)
            specs.setdefault(beats, RhythmSpec(name, beats, 0, num, den))
    return sorted(specs.values(), key=lambda s: (s.num != 1 or s.den != 1, -s.beats, s.dots))


SEARCH_SPECS = _build_search_specs()


_DURATION_CACHE: dict[tuple[int, int, str | None], tuple[RhythmSpec, ...]] = {}


def _spec_score(spec: RhythmSpec) -> tuple:
    return (0 if (spec.num == 1 and spec.den == 1) else 1, spec.dots, -spec.beats)


def _sequence_score(sequence: tuple[RhythmSpec, ...]) -> tuple:
    tuplets = sum(1 for s in sequence if s.num != 1 or s.den != 1)
    dots = sum(s.dots for s in sequence)
    return (len(sequence), tuplets, dots)


def _best_order(sequence: tuple[RhythmSpec, ...]) -> tuple[RhythmSpec, ...]:
    # Plain/large values first; this mainly affects rests and tie-split notes.
    return tuple(sorted(sequence, key=_spec_score))


def _find_decomposition(target: Fraction) -> tuple[RhythmSpec, ...] | None:
    exact = SINGLE_BY_BEATS.get(target)
    if exact:
        return (min(exact, key=lambda s: (1 if s.num != 1 or s.den != 1 else 0, s.dots)),)
    candidates = [s for s in SEARCH_SPECS if s.beats <= target]
    best: tuple[RhythmSpec, ...] | None = None
    frontier: dict[Fraction, tuple[RhythmSpec, ...]] = {Fraction(0): ()}
    for _level in range(1, 6):
        level_map: dict[Fraction, tuple[RhythmSpec, ...]] = {}
        for partial, seq in frontier.items():
            for spec in candidates:
                total = partial + spec.beats
                if total > target:
                    continue
                new_seq = seq + (spec,)
                if total == target:
                    ordered = _best_order(new_seq)
                    if best is None or _sequence_score(ordered) < _sequence_score(best):
                        best = ordered
                    continue
                old = level_map.get(total)
                if old is None or _sequence_score(new_seq) < _sequence_score(old):
                    level_map[total] = new_seq
        if best is not None:
            return best
        frontier = level_map
    return None


def decompose_duration(ticks: int, divisions: int, note_type: str | None = None) -> list[RhythmSpec]:
    """Return rhythm specs whose durations sum exactly to ticks/divisions."""
    if ticks <= 0:
        return []
    cache_key = (ticks, divisions, note_type)
    cached = _DURATION_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    target = Fraction(ticks, divisions)
    exact = SINGLE_BY_BEATS.get(target)
    if exact:
        preferred = TYPE_TO_VALUE.get((note_type or "").lower())
        if preferred:
            for s in exact:
                if s.value == preferred:
                    _DURATION_CACHE[cache_key] = (s,)
                    return [s]
        exact = sorted(exact, key=lambda s: (s.num != 1 or s.den != 1, -s.beats, s.dots))
        _DURATION_CACHE[cache_key] = (exact[0],)
        return [exact[0]]

    result = _find_decomposition(target)
    if result is None:
        # Last resort: fill with the largest values that fit.
        pieces: list[RhythmSpec] = []
        rem = target
        while rem > 0:
            choices = [s for s in ALL_SPECS if s.beats <= rem]
            if not choices:
                break
            pieces.append(choices[0])
            rem -= choices[0].beats
        if rem > 0:
            pieces.append(RhythmSpec("64th", Fraction(1, 16)))
        result = tuple(pieces)
    _DURATION_CACHE[cache_key] = tuple(result)
    return list(result)


# ---------------------------------------------------------------------------
# Fret assignment
# ---------------------------------------------------------------------------
def _fret_penalty(fret: int) -> float:
    p = 0.0
    if fret < -2:
        p += 80.0 * (-2 - fret)
    elif fret < 0:
        p += 4.0 * (-fret)
    if fret > 15:
        p += 8.0 * (fret - 15)
    if fret > 24:
        p += 80.0 * (fret - 24)
    if 0 <= fret <= 12:
        p += 0.15 * abs(fret - 5)
    return p


def _assign_chord(strings: list[int], midis_sorted: list[int]) -> tuple[list[int], float]:
    """Assign sorted strings (low..high) to sorted pitches (low..high)."""
    score = 0.0
    frets = []
    for s, midi in zip(strings, midis_sorted):
        fret = midi - TUNING[s]
        frets.append(fret)
        score += _fret_penalty(fret)
    span = strings[-1] - strings[0] if strings else 0
    score += 0.5 * span
    return frets, score


def _iter_assignments(n: int):
    """Yield tuples of n distinct string indices (0..5)."""
    for perm in permutations(range(NUM_STRINGS), n):
        yield perm


def _choose_playable_string(sounding: int) -> tuple[int, int]:
    """Pick a guitar string/fret for a non-guitar track, keeping the fret in a sane range."""
    for s in range(NUM_STRINGS - 1, -1, -1):
        fret = sounding - TUNING[s]
        if 0 <= fret <= 24:
            return s, fret
    best_s = min(range(NUM_STRINGS), key=lambda s: _fret_penalty(sounding - TUNING[s]) + (NUM_STRINGS - 1 - s) * 0.01)
    return best_s, sounding - TUNING[best_s]


def assign_strings(event: ChordEvent, tie_map: dict[int, tuple[int, int]], pitch_mode: str,
                   clef_octave: int = 0, is_guitar: bool = True) -> None:
    notes = event.notes
    if not notes:
        return
    # Written MIDI is the stable identity for ties.
    written = [written_midi(n.step, n.alter, n.octave) for n in notes]
    if pitch_mode == "auto":
        if is_guitar:
            # Audiveris stores sounding pitch when it recognised an 8vb clef;
            # plain G2 is treated as guitar written pitch (sounds an octave lower).
            delta = -12 if clef_octave == 0 else 0
        else:
            # Voices, piano etc. are non-transposing in MusicXML.
            delta = 0
    elif pitch_mode == "written":
        delta = -12
    else:
        delta = 0
    sounding = [w + delta for w in written]

    if len(notes) == 1:
        n = notes[0]
        key = written[0]
        if key in tie_map and n.tie_stop:
            s, f = tie_map[key]
        elif is_guitar:
            s, f = HIGH_E, sounding[0] - TUNING[HIGH_E]
        else:
            s, f = _choose_playable_string(sounding[0])
        # Guitar sounds an octave below its written pitch; non-guitar tracks
        # keep TransposedPitch at concert pitch so the note displays correctly.
        tp_oct = (sounding[0] // 12) + (1 if is_guitar else 0)
        event.gp_notes = [GpNote(n.step, n.alter, n.octave, s, f, sounding[0],
                                 (sounding[0] // 12), tp_oct,
                                 n.tie_start, n.tie_stop)]
        _update_ties(event.gp_notes, written, tie_map)
        return

    # Chord: search injective string assignments.
    order = sorted(range(len(notes)), key=lambda i: sounding[i])
    sorted_notes = [notes[i] for i in order]
    best: tuple[float, tuple[int, ...], list[int]] | None = None
    for perm in _iter_assignments(len(notes)):
        perm_sorted = tuple(sorted(perm))
        frets, score = _assign_chord(list(perm_sorted), [sounding[order[i]] for i in range(len(order))])
        # Penalise changing string on a tied continuation.
        for idx, (n, s) in enumerate(zip(sorted_notes, perm_sorted)):
            key = written[order[idx]]
            if n.tie_stop and key in tie_map:
                want_s, want_f = tie_map[key]
                if s != want_s or frets[idx] != want_f:
                    score += 1000.0
        if best is None or score < best[0]:
            best = (score, perm_sorted, frets)
    if best is None:
        # More notes than strings: fall back to all on high E.
        event.gp_notes = [GpNote(n.step, n.alter, n.octave, HIGH_E, snd - TUNING[HIGH_E], snd,
                                 snd // 12, (snd // 12) + (1 if is_guitar else 0),
                                 n.tie_start, n.tie_stop)
                          for n, snd in zip(notes, sounding)]
        _update_ties(event.gp_notes, written, tie_map)
        return
    _, strings, frets = best
    gp: list[GpNote] = []
    for idx, s in enumerate(strings):
        n = sorted_notes[idx]
        snd = sounding[order[idx]]
        gp.append(GpNote(n.step, n.alter, n.octave, s, frets[idx], snd,
                         snd // 12, (snd // 12) + (1 if is_guitar else 0),
                         n.tie_start, n.tie_stop))
    # Restore the original note order for the beat's note list.
    by_original: dict[int, GpNote] = {}
    for idx, s in enumerate(strings):
        by_original[order[idx]] = gp[idx]
    event.gp_notes = [by_original[i] for i in range(len(notes))]
    _update_ties(event.gp_notes, written, tie_map)


def _update_ties(gp_notes: list[GpNote], written: list[int], tie_map: dict[int, tuple[int, int]]) -> None:
    for g, key in zip(gp_notes, written):
        if g.tie_dest:
            tie_map.pop(key, None)
        if g.tie_origin:
            tie_map[key] = (g.string, g.fret)


# ---------------------------------------------------------------------------
# Lane packing and beat construction
# ---------------------------------------------------------------------------
@dataclass
class Lane:
    events: list[ChordEvent] = field(default_factory=list)
    end: int = 0
    voices: set[str] = field(default_factory=set)


def pack_lanes(events: list[ChordEvent]) -> tuple[list[Lane], list[str]]:
    warnings: list[str] = []
    note_events = [e for e in events if not e.rest and e.notes]
    if not note_events:
        return [], warnings
    lanes: list[Lane] = []
    for ev in sorted(note_events, key=lambda e: (e.onset, -e.duration)):
        placed = False
        for lane in lanes:
            if lane.end <= ev.onset and ev.voice in lane.voices:
                lane.events.append(ev); lane.end = ev.onset + ev.duration; lane.voices.add(ev.voice)
                placed = True
                break
        if not placed:
            for lane in lanes:
                if lane.end <= ev.onset:
                    lane.events.append(ev); lane.end = ev.onset + ev.duration; lane.voices.add(ev.voice)
                    placed = True
                    break
        if not placed:
            if len(lanes) < 4:
                lane = Lane([ev], ev.onset + ev.duration, {ev.voice})
                lanes.append(lane)
            else:
                lane = min(lanes, key=lambda l: l.end)
                warnings.append(f"lane overflow at onset {ev.onset}; forcing into voice")
                lane.events.append(ev)
                lane.end = max(lane.end, ev.onset + ev.duration)
                lane.voices.add(ev.voice)
    return lanes, warnings


def make_rest_beats(total_ticks: int, divisions: int) -> list[BeatOut]:
    beats: list[BeatOut] = []
    for spec in decompose_duration(total_ticks, divisions):
        beats.append(BeatOut(spec))
    return beats


def build_lane_beats(lane: Lane, bar_duration: int, divisions: int) -> list[BeatOut]:
    beats: list[BeatOut] = []
    cursor = 0
    for ev in sorted(lane.events, key=lambda e: e.onset):
        if ev.onset > cursor:
            beats.extend(make_rest_beats(ev.onset - cursor, divisions))
        elif ev.onset < cursor:
            # Should not happen after packing; keep the note rather than drop it.
            ev.onset = cursor
        specs = decompose_duration(ev.duration, divisions, ev.notes[0].note_type)
        if not specs:
            continue
        for si, spec in enumerate(specs):
            notes = []
            for g in ev.gp_notes:
                n = GpNote(g.step, g.alter, g.octave, g.string, g.fret, g.midi,
                           g.concert_octave, g.transposed_octave,
                           tie_origin=(g.tie_origin or si < len(specs) - 1),
                           tie_dest=(g.tie_dest or si > 0))
                notes.append(n)
            beats.append(BeatOut(spec, notes))
        cursor = ev.onset + ev.duration
    if cursor < bar_duration:
        beats.extend(make_rest_beats(bar_duration - cursor, divisions))
    return beats


# ---------------------------------------------------------------------------
# GPIF writing
# ---------------------------------------------------------------------------
def _text(parent: ET.Element, tag: str, value: object) -> ET.Element:
    e = ET.SubElement(parent, tag)
    e.text = str(value)
    return e


def _cdata_text(parent: ET.Element, tag: str, value: str) -> ET.Element:
    e = ET.SubElement(parent, tag)
    e.text = value
    return e


def gp_rhythm_element(parent: ET.Element, spec: RhythmSpec, rid: int) -> None:
    r = ET.SubElement(parent, "Rhythm", {"id": str(rid)})
    _text(r, "NoteValue", spec.value)
    if spec.dots:
        ET.SubElement(r, "AugmentationDot", {"count": str(spec.dots)})
    if spec.num != 1 or spec.den != 1:
        ET.SubElement(r, "PrimaryTuplet", {"num": str(spec.num), "den": str(spec.den)})


def gp_note_element(parent: ET.Element, n: GpNote) -> None:
    node = ET.SubElement(parent, "Note", {"id": str(n.id)})
    if n.tie_origin or n.tie_dest:
        ET.SubElement(node, "Tie", {
            "origin": "true" if n.tie_origin else "false",
            "destination": "true" if n.tie_dest else "false",
        })
    _text(node, "InstrumentArticulation", "0")
    props = ET.SubElement(node, "Properties")
    cp = ET.SubElement(props, "Property", {"name": "ConcertPitch"})
    pitch = ET.SubElement(cp, "Pitch")
    _text(pitch, "Step", n.step)
    _text(pitch, "Accidental", accidental_text(n.alter))
    _text(pitch, "Octave", n.concert_octave)
    fp = ET.SubElement(props, "Property", {"name": "Fret"})
    _text(fp, "Fret", n.fret)
    mp = ET.SubElement(props, "Property", {"name": "Midi"})
    _text(mp, "Number", n.midi)
    sp = ET.SubElement(props, "Property", {"name": "String"})
    _text(sp, "String", n.string)
    tp = ET.SubElement(props, "Property", {"name": "TransposedPitch"})
    pitch = ET.SubElement(tp, "Pitch")
    _text(pitch, "Step", n.step)
    _text(pitch, "Accidental", accidental_text(n.alter))
    _text(pitch, "Octave", n.transposed_octave)


def accidental_text(alter: int) -> str:
    return {0: "", 1: "#", -1: "b", 2: "x", -2: "bb"}.get(alter, "")


def build_gpif(root: ET.Element, template: ET.Element, bars: list[BarOut],
               rhythm_specs: dict[RhythmSpec, int], track_specs: list[TrackSpec],
               master_info: list[dict], bar_clefs: list[list[str]],
               title: str, artist: str, tempo: int) -> ET.Element:
    num_tracks = len(track_specs)
    num_measures = len(master_info)
    # --- Score metadata -------------------------------------------------
    score = root.find("Score")
    if score is not None:
        for tag, value in (("Title", title), ("Artist", artist)):
            node = score.find(tag)
            if node is not None:
                node.text = value
        sdl = score.find("ScoreSystemsDefaultLayout")
        if sdl is not None:
            sdl.text = "4"
        ssl = score.find("ScoreSystemsLayout")
        if ssl is not None:
            ssl.text = "4"

    # --- Master track / tempo ------------------------------------------
    mt = root.find("MasterTrack")
    if mt is not None:
        tracks = mt.find("Tracks")
        if tracks is not None:
            tracks.text = " ".join(str(i) for i in range(num_tracks))
        for auto in mt.iter("Automation"):
            if auto.findtext("Type") == "Tempo":
                val = auto.find("Value")
                if val is not None:
                    val.text = f"{tempo} 2"

    # --- Tracks ---------------------------------------------------------
    track0 = template.find("Tracks/Track")
    if track0 is None:
        raise SystemExit("template has no Track")
    tracks_node = root.find("Tracks")
    if tracks_node is None:
        raise SystemExit("template has no Tracks")
    for child in list(tracks_node):
        tracks_node.remove(child)
    for idx, spec in enumerate(track_specs):
        t = copy.deepcopy(track0)
        t.set("id", str(idx))
        name = spec.name
        short = name if len(name) <= 10 else name[:10]
        for tag, value in (("Name", name), ("ShortName", short),
                           ("Color", TRACK_COLORS[idx % len(TRACK_COLORS)])):
            node = t.find(tag)
            if node is not None:
                node.text = value
        # Guitar tracks transpose down an octave (written pitch); all other
        # tracks keep concert pitch until the user changes the instrument.
        octave = t.find("Transpose/Octave")
        if octave is not None:
            octave.text = "-1" if spec.is_guitar else "0"
        chromatic = t.find("Transpose/Chromatic")
        if chromatic is not None:
            chromatic.text = "0"
        mconn = t.find("MidiConnection")
        if mconn is not None:
            pc = mconn.find("PrimaryChannel")
            sc = mconn.find("SecondaryChannel")
            if pc is not None: pc.text = str(idx % 16)
            if sc is not None: sc.text = str((idx + 1) % 16)
        tracks_node.append(t)

    # --- Remove old music sections -------------------------------------
    for tag in ("MasterBars", "Bars", "Voices", "Beats", "Notes", "Rhythms", "ScoreViews"):
        node = root.find(tag)
        if node is not None:
            root.remove(node)

    # --- MasterBars -----------------------------------------------------
    mbs = ET.SubElement(root, "MasterBars")
    for i in range(num_measures):
        mb = ET.SubElement(mbs, "MasterBar")
        info = master_info[i]
        key = ET.SubElement(mb, "Key")
        fifths = int(info.get("fifths", 0))
        _text(key, "AccidentalCount", fifths)
        _text(key, "Mode", info.get("mode") or "Major")
        _text(key, "TransposeAs", "Flats" if fifths < 0 else "Sharps")
        _text(mb, "Time", info.get("time") or "4/4")
        _text(mb, "Bars", " ".join(str(i * num_tracks + t) for t in range(num_tracks)))

    # --- Bars -----------------------------------------------------------
    bars_node = ET.SubElement(root, "Bars")
    for i in range(num_measures):
        for track in range(num_tracks):
            b = ET.SubElement(bars_node, "Bar", {"id": str(i * num_tracks + track)})
            _text(b, "Clef", bar_clefs[track][i])
            bar = bars[i * num_tracks + track]
            slots = ["-1"] * 4
            for vi, v in enumerate(bar.voices[:4]):
                if v.beats:
                    slots[vi] = str(v.id)
            _text(b, "Voices", " ".join(slots))

    # --- Voices / Beats / Notes ----------------------------------------
    voices_node = ET.SubElement(root, "Voices")
    beats_node = ET.SubElement(root, "Beats")
    notes_node = ET.SubElement(root, "Notes")
    rhythm_ids = {spec.key: rid for spec, rid in rhythm_specs.items()}
    for bar in bars:
        for voice in bar.voices:
            if not voice.beats:
                continue
            v = ET.SubElement(voices_node, "Voice", {"id": str(voice.id)})
            _text(v, "Beats", " ".join(str(b.id) for b in voice.beats))
            for beat in voice.beats:
                bn = ET.SubElement(beats_node, "Beat", {"id": str(beat.id)})
                _text(bn, "Dynamic", beat.dynamic)
                ET.SubElement(bn, "Rhythm", {"ref": str(rhythm_ids[beat.rhythm.key])})
                _text(bn, "TransposedPitchStemOrientation", "Upward")
                _text(bn, "ConcertPitchStemOrientation", "Undefined")
                if beat.notes:
                    _text(bn, "Notes", " ".join(str(n.id) for n in beat.notes))
                props = ET.SubElement(bn, "Properties")
                for pname, pval in (("PrimaryPickupVolume", "0.500000"), ("PrimaryPickupTone", "0.500000")):
                    p = ET.SubElement(props, "Property", {"name": pname})
                    _text(p, "Float", pval)
                for n in beat.notes:
                    gp_note_element(notes_node, n)

    # --- Rhythms --------------------------------------------------------
    rhythms_node = ET.SubElement(root, "Rhythms")
    for spec, rid in sorted(rhythm_specs.items(), key=lambda kv: kv[1]):
        gp_rhythm_element(rhythms_node, spec, rid)

    # --- ScoreViews -----------------------------------------------------
    sv = ET.SubElement(root, "ScoreViews")
    ET.SubElement(sv, "ScoreView", {"id": "0"})
    ET.SubElement(sv, "ScoreView", {"id": "1"})
    return root


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------
def validate_gpif(gpif: bytes, report: dict) -> list[str]:
    errors: list[str] = []
    root = ET.fromstring(gpif)
    num_tracks = report.get("num_tracks", 1)
    num_measures = report.get("num_measures", 1)
    notes = {n.get("id"): n for n in root.findall("Notes/Note")}
    if len(notes) != report["gp_notes"]:
        errors.append(f"note count mismatch: {len(notes)} vs {report['gp_notes']}")
    beats = {b.get("id"): b for b in root.findall("Beats/Beat")}
    voices = {v.get("id"): v for v in root.findall("Voices/Voice")}
    bars = {b.get("id"): b for b in root.findall("Bars/Bar")}
    rhythms = {r.get("id") for r in root.findall("Rhythms/Rhythm")}
    if len(bars) != num_tracks * num_measures:
        errors.append(f"bar count {len(bars)} != {num_tracks * num_measures}")
    if len(root.findall("MasterBars/MasterBar")) != num_measures:
        errors.append("master bar count")
    referenced: set[str] = set()
    for bar in bars.values():
        for vid in bar.findtext("Voices").split():
            if vid == "-1":
                continue
            if vid not in voices:
                errors.append(f"bar {bar.get('id')} references missing voice {vid}")
    for v in voices.values():
        for bid in v.findtext("Beats").split():
            if bid not in beats:
                errors.append(f"voice {v.get('id')} references missing beat {bid}")
                continue
            if beats[bid].find("Rhythm") is None or beats[bid].find("Rhythm").get("ref") not in rhythms:
                errors.append(f"beat {bid} has bad rhythm")
            refs = beats[bid].findtext("Notes")
            if refs:
                for nid in refs.split():
                    if nid not in notes:
                        errors.append(f"beat {bid} references missing note {nid}")
                    referenced.add(nid)
    if set(notes) - referenced:
        errors.append(f"{len(set(notes) - referenced)} notes are not referenced by any beat")
    for nid, n in notes.items():
        props = {p.get("name"): p for p in n.findall("Properties/Property")}
        try:
            midi = int(props["Midi"].findtext("Number"))
            string = int(props["String"].findtext("String"))
            fret = int(props["Fret"].findtext("Fret"))
            cp_oct = int(props["ConcertPitch"].findtext("Pitch/Octave"))
            tp_oct = int(props["TransposedPitch"].findtext("Pitch/Octave"))
            step = props["ConcertPitch"].findtext("Pitch/Step")
            acc = props["ConcertPitch"].findtext("Pitch/Accidental") or ""
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"note {nid} malformed: {exc}")
            continue
        if not (0 <= string < NUM_STRINGS):
            errors.append(f"note {nid} bad string {string}")
        elif TUNING[string] + fret != midi:
            errors.append(f"note {nid} string/fret/midi mismatch: {TUNING[string]}+{fret} != {midi}")
        if cp_oct != midi // 12 or tp_oct not in (cp_oct, cp_oct + 1):
            errors.append(f"note {nid} octave mismatch")
        acc_map = {"": 0, "#": 1, "b": -1, "x": 2, "bb": -2}
        if step not in STEP_TO_SEMITONE or acc not in acc_map:
            errors.append(f"note {nid} bad spelling {step}{acc}")
        elif (STEP_TO_SEMITONE[step] + acc_map[acc]) % 12 != midi % 12:
            errors.append(f"note {nid} spelling does not match midi")
    return errors


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------
def _clean_source_title(stem: str) -> str:
    # Audiveris source names often carry a date prefix or a version suffix:
    #   "20250516_Sis puella magica!_PBRevisedV2"
    #   "想你的365天 260428v1"
    #   "利兹与青鸟_古典吉他二重奏_120-167"
    stem = re.sub(r"^\d{8}[_\s-]+", "", stem)
    stem = re.sub(r"\s+\d{6,}\w*$", "", stem)
    stem = re.sub(r"\s+v\d+$", "", stem, flags=re.IGNORECASE)
    if "_" in stem:
        stem = stem.split("_", 1)[0]
    return stem.strip()


def _extract_title(root: ET.Element, mxl_path: Path) -> str:
    source = root.findtext(".//source") or ""
    if source:
        stem = _clean_source_title(Path(source).stem)
        if stem:
            return stem
    for tag in ("movement-title", "work/work-title"):
        value = root.findtext(tag)
        if value and value.strip():
            return value.strip()
    return mxl_path.stem


def _extract_artist(root: ET.Element) -> str:
    for creator in root.findall("identification/creator"):
        if creator.get("type") == "composer" and creator.text:
            return creator.text.strip()
    return ""


def _extract_tempo(root: ET.Element) -> int:
    for direction in root.iter("direction"):
        sound = direction.find("sound")
        if sound is not None and sound.get("tempo"):
            try:
                return int(round(float(sound.get("tempo"))))
            except ValueError:
                pass
        per_minute = direction.find(".//per-minute")
        if per_minute is not None and per_minute.text:
            try:
                return int(round(float(per_minute.text)))
            except ValueError:
                pass
    return 120


def _rest_measure(number: int, master: MeasureData) -> MeasureData:
    return MeasureData(
        number=number,
        divisions=master.divisions,
        events=[],
        raw_rest_total=master.duration,
        clef_octave=master.clef_octave,
        time_beats=master.time_beats,
        time_beat_type=master.time_beat_type,
        key_fifths=master.key_fifths,
        key_mode=master.key_mode,
        clef=master.clef,
    )


def convert(mxl_path: Path, template_path: Path, out_path: Path,
            pitch_mode: str = "auto", report_path: Path | None = None,
            layout: str = "auto", title_override: str | None = None,
            artist_override: str | None = None, tempo_override: int | None = None,
            all_guitar: bool = False) -> dict:
    rhythm_specs: dict[RhythmSpec, int] = {}

    root = read_mxl(mxl_path)
    parts = parse_parts(root)
    part_names = {
        sp.get("id"): (sp.findtext("part-name") or sp.get("id") or "Track")
        for sp in root.findall("part-list/score-part")
    }
    tracks, num_measures, base = resolve_tracks(parts, part_names, layout)
    if not tracks:
        raise SystemExit("no non-empty parts found in MusicXML")
    if all_guitar:
        for spec in tracks:
            spec.is_guitar = True
    num_tracks = len(tracks)

    lookups: dict[str, dict[int, MeasureData]] = {
        pid: {m.number - base + 1: m for m in ms} for pid, ms in parts.items()
    }

    # Align every track to the global measure grid.
    raw_sequences: list[list[MeasureData | None]] = []
    for spec in tracks:
        seq: list[MeasureData | None] = []
        for mi in range(1, num_measures + 1):
            pid = spec.sources.get(mi)
            seq.append(lookups[pid].get(mi) if pid else None)
        raw_sequences.append(seq)

    # Master time/key comes from the first track that has the measure.
    master_info: list[dict] = []
    master_measures: list[MeasureData] = []
    warnings: list[str] = []
    for mi in range(num_measures):
        candidates = [seq[mi] for seq in raw_sequences if seq[mi] is not None]
        if not candidates:
            master = MeasureData(mi + 1, 4, [], raw_rest_total=16)
        else:
            master = candidates[0]
            for other in candidates[1:]:
                if (other.time_beats, other.time_beat_type, other.key_fifths, other.key_mode) != (
                        master.time_beats, master.time_beat_type, master.key_fifths, master.key_mode):
                    warnings.append(
                        f"measure {mi + 1}: tracks disagree on time/key; using "
                        f"{master.time_signature} fifths={master.key_fifths}"
                    )
                    break
        master_measures.append(master)
        master_info.append({
            "time": master.time_signature,
            "fifths": master.key_fifths,
            "mode": master.key_mode,
        })

    track_sequences: list[list[MeasureData]] = []
    for seq in raw_sequences:
        filled: list[MeasureData] = []
        for mi, measure in enumerate(seq):
            filled.append(measure if measure is not None else _rest_measure(mi + 1, master_measures[mi]))
        track_sequences.append(filled)

    # Assign string/fret in time order to keep ties on the same string.
    for ti, spec in enumerate(tracks):
        tie_map: dict[int, tuple[int, int]] = {}
        for measure in track_sequences[ti]:
            for ev in sorted([e for e in measure.events if not e.rest and e.notes], key=lambda e: e.onset):
                assign_strings(ev, tie_map, pitch_mode, measure.clef_octave, spec.is_guitar)

    # Build bars in per-track order, then reorder to bar-major.
    bars_by_track: list[list[BarOut]] = []
    for ti, spec in enumerate(tracks):
        track_bars: list[BarOut] = []
        for mi, measure in enumerate(track_sequences[ti]):
            lanes, w = pack_lanes(measure.events)
            warnings.extend(w)
            bar = BarOut(ti, mi, [])
            if not lanes:
                rest_total = measure.raw_rest_total or measure.duration
                if rest_total <= 0:
                    rest_total = measure.duration
                bar.voices.append(VoiceOut(make_rest_beats(rest_total, measure.divisions)))
            else:
                for lane in lanes:
                    beats = build_lane_beats(lane, measure.duration, measure.divisions)
                    if beats:
                        bar.voices.append(VoiceOut(beats))
            track_bars.append(bar)
        bars_by_track.append(track_bars)
    bars: list[BarOut] = [bars_by_track[ti][mi] for mi in range(num_measures) for ti in range(num_tracks)]

    # Assign global IDs.
    voice_id = beat_id = note_id = 0
    all_notes: list[GpNote] = []
    gp_notes_by_track = [0] * num_tracks
    for bar in bars:
        for voice in bar.voices:
            if not voice.beats:
                continue
            voice.id = voice_id; voice_id += 1
            for beat in voice.beats:
                beat.id = beat_id; beat_id += 1
                for n in beat.notes:
                    n.id = note_id; note_id += 1
                    all_notes.append(n)
                    gp_notes_by_track[bar.track] += 1

    # Register rhythms in first-use order.
    for bar in bars:
        for voice in bar.voices:
            for beat in voice.beats:
                if beat.rhythm not in rhythm_specs:
                    rhythm_specs[beat.rhythm] = len(rhythm_specs)

    title = title_override or _extract_title(root, mxl_path)
    artist = artist_override if artist_override is not None else _extract_artist(root)
    tempo = tempo_override or _extract_tempo(root)

    with zipfile.ZipFile(template_path) as z:
        template_gpif = ET.fromstring(z.read("Content/score.gpif"))
    template = copy.deepcopy(template_gpif)
    out_root = copy.deepcopy(template_gpif)
    bar_clefs = [[measure.clef for measure in seq] for seq in track_sequences]
    build_gpif(out_root, template, bars, rhythm_specs, tracks, master_info, bar_clefs,
               title, artist, tempo)
    ET.indent(out_root, space="")
    gpif_bytes = ET.tostring(out_root, encoding="utf-8", xml_declaration=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(template_path) as zin, zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zout:
        written = set()
        for info in zin.infolist():
            name = info.filename
            if name == "Content/score.gpif":
                continue
            if name.startswith("Content/ScoreViews/"):
                continue
            if name.endswith("/"):
                zout.writestr(name, b"")
                written.add(name)
                continue
            zout.writestr(name, zin.read(name))
            written.add(name)
        zout.writestr("Content/score.gpif", gpif_bytes)
        if "Content/ScoreViews/" not in written:
            zout.writestr("Content/ScoreViews/", b"")

    # Validate before reporting success.
    errors = validate_gpif(gpif_bytes, {
        "gp_notes": len(all_notes),
        "num_tracks": num_tracks,
        "num_measures": num_measures,
    })
    if errors:
        raise SystemExit("generated GPIF failed validation:\n" + "\n".join(errors[:20]))

    frets = [n.fret for n in all_notes]
    source_notes_by_track = [
        sum(1 for measure in seq for ev in measure.events if not ev.rest for _ in ev.notes)
        for seq in track_sequences
    ]
    fret_by_track = [[n.fret for bar in bars_by_track[ti] for v in bar.voices for beat in v.beats for n in beat.notes]
                     for ti in range(num_tracks)]
    anomalies = []
    for ti, spec in enumerate(tracks):
        for mi, measure in enumerate(track_sequences[ti]):
            if spec.sources.get(mi + 1) is None:
                continue
            total = sum(ev.duration for ev in measure.events if not ev.rest)
            if total != measure.duration:
                delta = total - measure.duration
                anomalies.append({
                    "track": spec.name, "measure": mi + 1, "ticks": total,
                    "expected": measure.duration, "delta_ticks": delta,
                    "beats": round(total / measure.divisions, 3),
                    "kind": "over" if delta > 0 else "under",
                })
    report = {
        "source": str(mxl_path),
        "title": title,
        "artist": artist,
        "tempo": tempo,
        "pitch_mode": pitch_mode,
        "layout": layout,
        "num_tracks": num_tracks,
        "num_measures": num_measures,
        "source_notes": sum(source_notes_by_track),
        "gp_notes": len(all_notes),
        "split_notes": len(all_notes) - sum(source_notes_by_track),
        "bars": len(bars),
        "voices": voice_id,
        "beats": beat_id,
        "rhythms": len(rhythm_specs),
        "min_fret": min(frets) if frets else None,
        "max_fret": max(frets) if frets else None,
        "negative_fret_notes": sum(1 for f in frets if f < 0),
        "tracks": [
            {
                "index": ti,
                "name": spec.name,
                "is_guitar": spec.is_guitar,
                "parts": spec.parts,
                "source_ranges": [
                    {"part": pid, "ranges": _compress_measures(
                        [m for m, p in spec.sources.items() if p == pid])}
                    for pid in spec.parts
                ],
                "source_notes": source_notes_by_track[ti],
                "gp_notes": gp_notes_by_track[ti],
                "min_fret": min(fret_by_track[ti]) if fret_by_track[ti] else None,
                "max_fret": max(fret_by_track[ti]) if fret_by_track[ti] else None,
                "negative_frets": sum(1 for f in fret_by_track[ti] if f < 0),
            }
            for ti, spec in enumerate(tracks)
        ],
        "measure_anomalies": anomalies,
        "warnings": warnings,
    }
    if report_path:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mxl", type=Path, default=Path("liz_GPT.mxl"))
    ap.add_argument("--template", type=Path,
                    default=Path(__file__).resolve().parent / "template.gp")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--pitch-mode", choices=("auto", "written", "sounding"), default="auto",
                    help="auto: guitar tracks use clef-aware written pitch, other tracks use sounding pitch")
    ap.add_argument("--layout", choices=("auto", "parts"), default="auto",
                    help="auto: merge non-overlapping fragments grouped by part name; parts: one track per part")
    ap.add_argument("--title", default=None)
    ap.add_argument("--artist", default=None)
    ap.add_argument("--tempo", type=int, default=None)
    ap.add_argument("--all-guitar", action="store_true",
                    help="treat every track as a guitar track (transpose -1, guitar pitch rules)")
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing output file (default: refuse)")
    args = ap.parse_args(argv)
    if not args.mxl.exists():
        raise SystemExit(f"input MusicXML not found: {args.mxl}")
    if not args.template.exists():
        raise SystemExit(f"GP template not found: {args.template}")
    out_path = args.out or Path(f"{args.mxl.stem}-转换.gp")
    report_path = args.report or out_path.with_name(out_path.stem + "-report.json")
    if out_path.exists() and not args.force:
        raise SystemExit(
            f"output file already exists: {out_path}\n"
            "Use --force to overwrite it, or choose another --out path."
        )
    report = convert(
        args.mxl, args.template, out_path, args.pitch_mode, report_path,
        args.layout, args.title, args.artist, args.tempo, args.all_guitar,
    )
    summary = {k: report[k] for k in (
        "title", "tempo", "pitch_mode", "layout", "num_tracks", "num_measures",
        "source_notes", "gp_notes", "split_notes", "bars", "voices", "beats",
        "rhythms", "min_fret", "max_fret", "negative_fret_notes",
    )}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("tracks:")
    for track in report["tracks"]:
        print(f"  {track['index'] + 1:2d}. {track['name']}  "
              f"notes={track['gp_notes']}  parts={','.join(track['parts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

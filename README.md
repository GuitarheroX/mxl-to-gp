# mxl → gp 通用转换器

把 Audiveris / MusicXML 的 `.mxl` 转换成 Guitar Pro 8 的 `.gp`。脚本只使用 Python 标准库，不依赖第三方包（除了运行环境）。

当前已在两份差异很大的谱子上验证：

| 输入 | 结果 |
|---|---|
| `liz_GPT.mxl` | 2 轨（Guitar I/II）、48 小节、1025 个音符 |
| `365.mxl` | 13 轨（人声/合唱/钢琴）、101 小节、2930 个音符 |

## 转换另一首 mxl

```bash
cd /Users/Xiao/projects/mxl-to-gp
source ~/miniconda3/etc/profile.d/conda.sh
conda activate base

python mxl_to_gp.py --mxl "另一首.mxl" --out "另一首.gp"
```

默认会生成：

- `另一首.gp`
- `另一首-report.json`（转换统计、每轨来源、时值异常等）

常用参数：

```bash
python mxl_to_gp.py \
  --mxl "另一首.mxl" \
  --template "template.gp" \
  --out "另一首.gp" \
  --pitch-mode auto \
  --layout auto
```

- `--pitch-mode auto`（默认）：吉他轨按谱号/8vb 判断记谱音高与实际发声音高；非吉他轨按 MusicXML 的实际发声音高。
- `--pitch-mode written`：所有轨都按“记谱音高 - 12”处理。
- `--pitch-mode sounding`：所有轨都直接使用 `<pitch>`。
- `--layout auto`（默认）：按 part 名称分组，把同一组里**小节不重叠**的碎片合并成一条轨；同一组里小节重叠的 part 保留为多条轨（通常是不同声部）。
- `--layout parts`：每个 `<part>` 单独一条轨，不做任何合并。
- `--template` 默认使用**脚本所在目录**的 `template.gp`，所以可以从任意目录运行；需要别的模板时传绝对路径。
- `-H` / `--help`：打印用法和所有参数说明。
- 默认**拒绝覆盖已存在的输出文件**；确认覆盖请加 `--force`。
- `--title` / `--artist` / `--tempo`：覆盖自动提取的标题/作者/速度。
- `--all-guitar`：把所有轨都按吉他处理（`Transpose -1`、按吉他记谱音高）。适合多吉他合奏、且 part 名称里没有 “Gtr/Guitar” 的谱。

## part 合并规则

Audiveris 经常把一页/一个系统拆成多个 `<part>`，所以不能简单地“一个 part 一条轨”。默认规则：

1. 把 part 名称规范化，例如：
   - `Gtr. I` / `Gtr.I` / `Guitar I` → 同一组
   - `Gtr. 11`（OCR 把 II 识别成 11）→ 与 `Gtr. II` 同组
   - `S.` / `s.` / `8.` → 同一组
   - `A.` / `A` / `A.solo` 等按名称分组
2. 同一组内，如果两个 part 的非空小节**不重叠**，视为同一轨的碎片并合并。
3. 如果同一组内的小节重叠，视为不同声部/不同轨，保留为多条轨。
4. 没有任何音符的 part 会被跳过。

`liz_GPT.mxl` 因此自动得到 2 轨：P5+P4+P2+P1 → Guitar I，P6+P3 → Guitar II。  
`365.mxl` 得到 13 轨，其中 P1–P18 的碎片按声部合并，P19–P22 的 Soprano/Alto/Tenor/Bass 与 P23 Piano 各自成轨。

如果自动结果不符合预期，用 `--layout parts` 保留所有 part，之后在 Guitar Pro 里手动删/并轨。

## 写入 GP 的策略

- **模板**：以 `template.gp` 为 GP8 模板，克隆 `Score`、`MasterTrack`、`Track`、样式表、`BinaryStylesheet`、`LayoutConfiguration`、`PartConfiguration`、`Preferences.json`。
- **轨道**：part 数动态，最多不限制；每条轨默认都是 Nylon Guitar 音色（因为 Guitar Pro 没有人声/钢琴轨时，先统一成吉他，之后在 GP 里改乐器）。
- **移调**：
  - 名称含 `gtr` / `guitar` 的轨：`Transpose Octave = -1`，按吉他记谱处理，1 弦负数品合法。
  - 其他轨（人声、钢琴等）：`Transpose Octave = 0`，音高按 MusicXML 实际发声音高写，避免整体低八度。
- **单音**：吉他轨默认放在 1 弦（GPIF `String=5`），允许负数品；非吉他轨会自动选一根品数在 0–24 的弦，减少极端负数品。
- **和弦**：和弦音分配到不同弦，按“品数尽量 0–15、避免大跨度、连接音保持同弦”打分。
- **节奏**：按 `duration / divisions` 精确拆分；一个时值无法用单个 GP 音符表示时（如 5 拍、7 拍、5/3 拍），拆成多个音符/休止符，必要时加连音线。三连音等 tuplet 会写入 `PrimaryTuplet`。
- **拍号/调号/谱号**：从每个小节的 `<attributes>` 读取并写入 GP 的 `MasterBar` / `Bar`，支持中途变化（例如 365 的 2/2 → 4/4、2 升 → 4 升）。
- **速度**：读取第一个 `<sound tempo>` 或 `<per-minute>`，没有则默认 120；写入 MasterTrack 的 Tempo automation。
- **标题/作者**：优先从 `<source>` 的 PDF 文件名提取（如“想你的365天”“利兹与青鸟”），其次 `movement-title`，最后输入文件名；作者取 `creator type="composer"`。

## 验证方式

脚本每次运行会自动校验：

- 源音符数 = GP 音符数（允许因超长音拆分成多个 GP 音符）；
- 每个 `Note` 的 `String + Fret` 与 `Midi` 一致；
- `ConcertPitch` 八度 = `Midi // 12`，`TransposedPitch` 为 0 或 +1 个八度；
- 小节/声部/拍/音符引用完整，无孤立音符；
- Bar 数 = 轨数 × 小节数。

已实际在 Guitar Pro 8 中打开：

- `利兹与青鸟-转换.gp`（旧版 2 轨结果）
- 365 的 13 轨输出

MuseScore 4 的 GP 导入器会拒绝“超拍/缺拍小节”（`Incomplete measure`），这是 MuseScore 的严格限制；Guitar Pro 8 可以正常打开。脚本按“保真”策略保留 mxl 的异常时值，不强行量化。

## 已知限制

- 未写入歌词、力度、装饰音、滑音、推弦、倚音等演奏法；只保留音高、时值、连音线、休止符、多声部和基础拍号/调号。
- 365 这类合唱谱的 part 名称是 OCR 结果（`A.solo`、`A. 5010`、`Altosolo` 等），轨名只能按原样保留，需要在 GP 里手动重命名/合并。
- 非吉他轨目前仍用 Nylon Guitar 音色和吉他谱表；打开 GP 后需要手动改乐器。改乐器后，弦/品数据通常会被 GP 忽略，音高以 `Midi` 为准。
- mxl 本身的 OCR 错误不会被纠正。

## 逆向工程
GP_FORMAT.md 里记录了当前探索到的 gp 格式的规则。

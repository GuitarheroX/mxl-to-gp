# `template.gp` 拆解：Guitar Pro 8 的 `.gp` 如何存乐谱

本文以仓库里的 `template.gp` 为样本，说明 Guitar Pro 7/8 的 `.gp` 文件怎样把一份乐谱拆成文件、对象和引用。

先给结论：

1. **`.gp` 是一个 ZIP 包**，不是单一的二进制文件；解压后乐谱本体是 `Content/score.gpif`（XML）。
2. `score.gpif` 用**五层对象**表示音乐：`MasterBar`（全局小节）→ `Bar`（某轨/某谱表的小节）→ `Voice`（声部）→ `Beat`（节奏事件）→ `Note`（音高/弦品）。
3. 这些对象**不按 XML 嵌套组织**，而是各自平铺在顶层 section 里，用 `id` 和空格分隔的 `id` 列表互相引用。
4. `Rhythm` 是独立的节奏表，`Beat` 只引用 `Rhythm` 的 id；`Beat` 本身不直接写时值。
5. 其余文件（`BinaryStylesheet`、`LayoutConfiguration`、`PartConfiguration`、`*.gpsv`、`*.gpss`、`Preferences.json`）主要管显示、布局、音色和偏好，不决定音符内容。

---

## 1. 容器：一个 ZIP 包

`template.gp` 解压后共 17 个 ZIP 条目（其中 3 个是目录），文件如下：

| 路径 | 大小（字节） | 内容 | 作用 |
|---|---:|---|---|
| `VERSION` | 3 | `7.0` | 归档/容器格式版本 |
| `meta.json` | 4 | `{}` | 附加元数据；GP8 可能用它记录音频轨等 |
| `Content/score.gpif` | 129,481 | XML | **乐谱本体** |
| `Content/BinaryStylesheet` | 21,980 | 二进制 | 全局显示样式（字体、页眉页脚、符头等） |
| `Content/LayoutConfiguration` | 14 | 二进制 | 页面/视图布局参数 |
| `Content/PartConfiguration` | 27 | 二进制 | 多轨/单轨视图的轨与谱表显示配置 |
| `Content/Preferences.json` | 303 | JSON | Guitar Pro 的播放/界面偏好 |
| `Content/ScoreViews/0.gpsv` | 9,770 | 二进制 | 视图 0：Full score（总谱） |
| `Content/ScoreViews/1.gpsv` | 4,094 | 二进制 | 视图 1：Nylon Guitar |
| `Content/ScoreViews/2.gpsv` | 236 | 二进制 | 视图 2：Nylon Guitar |
| `Content/Stylesheets/score.gpss` | 3,445 | 二进制 | 总谱样式 |
| `Content/Stylesheets/scoreview0.gpss` | 285 | 二进制 | 视图 0 样式 |
| `Content/Stylesheets/scoreview1.gpss` | 285 | 二进制 | 视图 1 样式 |
| `Content/Stylesheets/scoreview2.gpss` | 285 | 二进制 | 视图 2 样式 |

几个容易踩的坑：

- `VERSION` 是 `7.0`，但 `score.gpif` 里的 `<GPVersion>` 是 `8.1.5`。**不要用 `VERSION=7.0` 判断这是 GP7 文件**；前者是容器格式代际，后者是实际写入的 Guitar Pro 版本。
- ZIP 条目可以用 Deflate 压缩，也可以 Stored；Guitar Pro 自己能读就行。`template.gp` 的 XML 压缩后从 129 KB 降到约 6.8 KB。
- 这些文件都可以用普通 ZIP 工具查看。调试时直接：

```bash
unzip -o template.gp -d /tmp/template
xmllint --format /tmp/template/Content/score.gpif | less
```

---

## 2. `score.gpif` 的顶层骨架

去掉内容后，根节点下就是下面这些 section（括号内是 `template.gp` 的数量）：

```xml
<GPIF>
  <GPVersion>8.1.5</GPVersion>
  <GPRevision required="12024" recommended="13000">13008</GPRevision>
  <Encoding>
    <EncodingDescription>GP8</EncodingDescription>
  </Encoding>

  <Score>...</Score>                 <!-- 标题、作者、页眉页脚等 -->
  <MasterTrack>...</MasterTrack>     <!-- 全局速度、主效果器 -->
  <Tracks>...</Tracks>               <!-- 2 条轨 -->
  <MasterBars>...</MasterBars>       <!-- 25 个全局小节 -->
  <Bars>...</Bars>                   <!-- 50 个“轨/谱表小节” -->
  <Voices>...</Voices>               <!-- 51 个声部 -->
  <Beats>...</Beats>                 <!-- 181 个节奏事件 -->
  <Notes>...</Notes>                 <!-- 68 个音高对象 -->
  <Rhythms>...</Rhythms>             <!-- 9 种节奏型 -->
  <ScoreViews>...</ScoreViews>       <!-- 3 个视图 -->
</GPIF>
```

`GPVersion` 是文件声明的 GP 版本，`GPRevision` 的 `required`/`recommended` 是兼容性门槛，节点文本是文件修订号。真正决定“能不能打开”的通常还是对象结构本身。

---

## 3. 核心对象图：id + 引用

`score.gpif` 不是下面这种嵌套树：

```text
MasterBar > Bar > Voice > Beat > Note   （错误理解）
```

而是**平铺 + 引用**：

```text
MasterBar（按出现顺序编号，本身没有 id）
  └─ <Bars>0 1</Bars>                  → Bar id=0、Bar id=1
        └─ Bar id=0
             └─ <Voices>0 1 -1 -1</Voices>  → Voice id=0、Voice id=1；-1 表示空槽
                   └─ Voice id=0
                        └─ <Beats>0 1 2 3</Beats>  → Beat id=0..3
                              └─ Beat id=0
                                   ├─ <Rhythm ref="0"/>  → Rhythm id=0
                                   └─ <Notes>0</Notes>   → Note id=0
```

引用规则：

| 父对象 | 引用字段 | 指向 | 说明 |
|---|---|---|---|
| `MasterTrack` | `Tracks` | `Track` | 主轨包含哪些轨 |
| `MasterBar` | `Bars` | `Bar` | 本小节在每个 staff 上的 Bar |
| `Bar` | `Voices` | `Voice` | 模板中为 4 个槽位（第 1–4 声部） |
| `Voice` | `Beats` | `Beat` | 按时间顺序排列 |
| `Beat` | `Rhythm@ref` | `Rhythm` | 节奏型 |
| `Beat` | `Notes` | `Note` | 和弦时是多个 id |
| `Track` | `Staves` | `Staff` | 一个轨可以有多个谱表 |

要点：

- **id 只需在同一种元素内唯一**。`Bar id="0"` 和 `Note id="0"` 不冲突。
- 引用字段是**空格分隔的 id 列表**，例如 `0 1`、`7 8 9 0`。
- `-1` 是空引用，常见于 `Bar/Voices`。
- `MasterBar` 没有 id，**它在 `<MasterBars>` 中的顺序就是小节号**。这意味着插入/删除小节时，后续 `MasterBar` 不需要改 id，但它引用的 `Bar` id 需要整体重排。
- **同一个对象可以被多处引用**。`template.gp` 中 68 个 `Note` 对象总共被 Beat 引用 320 次；45 个 Note 被引用不止一次。`Beat` 也一样：181 个 Beat 对象总共被 Voice 引用 262 次，41 个 Beat 被复用。
  - 读取时不能假设“每个 Beat/Note 只出现一次”，要按 id 建表再解引用。
  - alphaTab 这类读取器在组装时会 `clone` 共享对象，展开成独立实例。
  - 写文件时**建议每个位置生成独立对象**，最省心；尤其带 `Tie`、演奏法的 Note 不要随便共享，否则状态会串到别的 Beat。

---

## 4. 从 `MasterBar` 到 `Note` 的完整实例

下面全部是 `template.gp` 的真实片段（只调整了缩进）。

### 4.1 `MasterBar`：全局小节

第 1 小节（数组下标 0）：

```xml
<MasterBar>
  <Key>
    <AccidentalCount>0</AccidentalCount>
    <Mode>Major</Mode>
    <TransposeAs>Sharps</TransposeAs>
  </Key>
  <Time>4/4</Time>
  <Bars>0 1</Bars>
</MasterBar>
```

- `Key/AccidentalCount` 是调号的五度圈数量：正数 = 升号数，负数 = 降号数，`0` = C 大调 / A 小调。
- `Mode` 是 `Major` 或 `Minor`；`TransposeAs` 是 `Sharps` / `Flats`，表示同音异名的拼写倾向。
- `Time` 是 `分子/分母` 字符串。
- `Bars` 指向本小节在每个 staff 上的 `Bar`。`template.gp` 每轨只有一个 staff，所以是 `0 1`（轨 0、轨 1 各一个 Bar）。

`template.gp` 的 25 个 `MasterBar` 全部是 `4/4`、0 个升降号、`Major`。

### 4.2 `Bar`：某轨/某谱表的小节

```xml
<Bar id="0">
  <Clef>G2</Clef>
  <Voices>0 1 -1 -1</Voices>
</Bar>
```

- `Clef` 是谱号：`G2`（高音谱号）、`F4`（低音谱号）、`C3`/`C4`（中音/次中音谱号）、`Neutral`（无谱号）等。
- `Voices` 在模板中固定写 4 个槽位，对应第 1–4 声部；`-1` 表示该声部为空。GP 最多支持 4 个声部。
- 对 `template.gp` 这种“一轨一谱表”的情况，`Bar` id 的惯例是：

```text
barId = masterBarIndex * staff总数 + staffIndex
```

但**读取时应以 `MasterBar/Bars` 的引用为准**，不要硬算；多谱表（如钢琴大谱表）时 `Bars` 是按 staff 展开的，数量不等于轨数。

### 4.3 `Voice`：声部

```xml
<Voice id="0">
  <Beats>0 1 2 3</Beats>
</Voice>
```

- `Beats` 按时间顺序排列。
- **没有“起始时间”字段**；一个 Beat 的起点 = 前面所有 Beat 时值之和。
- 一个 `Bar` 最多 4 个 Voice；`Voice` 本身是全局对象，id 不与小节绑定。

### 4.4 `Beat`：节奏事件

```xml
<Beat id="0">
  <Dynamic>MF</Dynamic>
  <Rhythm ref="0" />
  <TransposedPitchStemOrientation>Upward</TransposedPitchStemOrientation>
  <ConcertPitchStemOrientation>Undefined</ConcertPitchStemOrientation>
  <Notes>0</Notes>
  <Properties>
    <Property name="PrimaryPickupVolume">
      <Float>0.500000</Float>
    </Property>
    <Property name="PrimaryPickupTone">
      <Float>0.500000</Float>
    </Property>
  </Properties>
</Beat>
```

- **`Beat` 不是一个音符**，而是一个“同一时刻、同一时值”的节奏事件。
- `Rhythm ref` 决定时值。
- `Notes` 决定这个事件包含哪些音：
  - 没有 `Notes` 子节点 = **休止符**（`template.gp` 有 4 个）。
  - 一个 id = 单音。
  - 多个 id = **和弦**（例如 `Beat id="8"` 的 `<Notes>7 8 9 0</Notes>`）。
- `Dynamic` 是力度（`PPP`…`FFF`、`MF` 等），作用于整个 Beat。
- 两个 `...StemOrientation` 是符干方向，属于显示信息。
- `Properties` 是通用的“名字 + 类型化值”包；模板只放了拾音器音量/音色，其他演奏法也走这里。

`template.gp` 的 181 个 Beat 中：87 个单音、90 个和弦、4 个休止；共 320 个 Note 引用。

### 4.5 `Note`：音高与弦品

```xml
<Note id="0">
  <InstrumentArticulation>0</InstrumentArticulation>
  <Properties>
    <Property name="ConcertPitch">
      <Pitch>
        <Step>D</Step>
        <Accidental />
        <Octave>5</Octave>
      </Pitch>
    </Property>
    <Property name="Fret">
      <Fret>3</Fret>
    </Property>
    <Property name="Midi">
      <Number>62</Number>
    </Property>
    <Property name="String">
      <String>4</String>
    </Property>
    <Property name="TransposedPitch">
      <Pitch>
        <Step>D</Step>
        <Accidental />
        <Octave>6</Octave>
      </Pitch>
    </Property>
  </Properties>
</Note>
```

这个 Note 的含义：

| 字段 | 值 | 含义 |
|---|---|---|
| `ConcertPitch` | `D5` | 实际发声音高（GPIF 的八度写法） |
| `TransposedPitch` | `D6` | 记谱音高；吉他谱上写高一个八度 |
| `Midi` | `62` | 实际发声的 MIDI 音高 |
| `String` | `4` | 弦索引，**0 起、0 = 最低弦**；4 是 B 弦 |
| `Fret` | `3` | 第 3 品 |

验证公式（模板 68 个 Note 全部成立）：

```text
Midi = Tuning[String] + Fret
     = 59 + 3
     = 62
```

`InstrumentArticulation` 是乐器发音法索引。模板的 Nylon Guitar 只有一个 `Pitched` element、一个 articulation，所以是 `0`；打击乐会用它选择具体鼓件。

`Tie` 是可选的：

```xml
<Tie origin="true" destination="false" />
```

- `origin="true"`：连音线从这里开始。
- `destination="true"`：连音线在这里结束。
- 中间段可以两个都为 `true`。
- 连音不会合并 Note；它是**两个相邻 Beat 里的两个 Note**，只是显示/播放时连起来。

`template.gp` 有 4 个 `origin=true`、4 个 `destination=true`。

---

## 5. 各层字段详解

### 5.1 `Score`：元数据与页面

`template.gp` 的这些字段都是空字符串：

```text
Title / SubTitle / Artist / Album / Words / Music / WordsAndMusic
Copyright / Tabber / Instructions / Notices
```

非空的是页面相关字段：

| 字段 | 模板值 | 说明 |
|---|---|---|
| `FirstPageHeader` | HTML（CDATA） | 首页页眉，含 `%TITLE%`、`%ARTIST%` 等占位符 |
| `FirstPageFooter` | HTML | 首页页脚，含 `%page%/%pages%` |
| `PageFooter` | HTML | 后续页页脚 |
| `ScoreSystemsDefaultLayout` | `4` | 默认每页系统数 |
| `ScoreSystemsLayout` | `4 4 4 4 4 4 1` | 分页的系统数布局 |
| `ScoreZoomPolicy` | `Value` | 缩放策略 |
| `ScoreZoom` | `1.12305` | 缩放值 |
| `MultiVoice` | `0` | 多声部显示相关 |

这些字段影响打印/显示，不影响音高和时值。

### 5.2 `MasterTrack`：全局速度与主效果

```xml
<MasterTrack>
  <Tracks>0 1</Tracks>
  <Automations>
    <Automation>
      <Type>Tempo</Type>
      <Linear>false</Linear>
      <Bar>0</Bar>
      <Position>0</Position>
      <Visible>true</Visible>
      <Value>72 2</Value>
    </Automation>
  </Automations>
  ...
</MasterTrack>
```

- `Tracks` 列出主轨包含的轨 id；模板是 `0 1`。
- `Automation` 是自动化曲线的一个点：
  - `Bar` 是 `MasterBar` 的下标。
  - `Position` 是小节内的比例位置（0 = 小节开头）。
  - `Value` 的内容随 `Type` 变化；速度这里是 `"72 2"`，表示 **72 BPM**（第二个数是 GP 内部的参考时值码，常用 2）。
- `MasterTrack/RSE/Master` 下是主效果器（Dynamic、StudioReverb、GraphicEQ、VolumeAndPan 等），属于音色/播放，不是乐谱结构。
- 每条轨自己的音量、声像、音色自动化放在 `Track/Automations`，不要和全局速度混淆。

### 5.3 `Tracks`：轨、乐器、定弦、移调

`template.gp` 有两条 Nylon Guitar 轨：乐器、定弦、移调、音色相同，布局和 MIDI 通道略有差异。每条 `Track` 的关键内容：

| 子节点 | 模板值 | 说明 |
|---|---|---|
| `Name` | `Nylon Guitar` | 轨名 |
| `ShortName` | `n.guit.` | 短名 |
| `Color` | `237 116 116` | RGB 颜色 |
| `SystemsDefautLayout` | `3` | 每页系统数（注意格式里的拼写就是 `Defaut`） |
| `SystemsLayout` | `3 3 3 3` / `1` | 分页布局 |
| `PlayingStyle` | `StringedFinger` | 演奏风格 |
| `IconId` | `23` | 轨图标 |
| `InstrumentSet` | `nylonGuitar` | 乐器类型、谱表行数、发音法 |
| `Transpose` | `Chromatic=0`、`Octave=-1` | 记谱移调 |
| `Staves/Staff/Properties` | 标准定弦、24 品、Capo 0 | 谱表与定弦 |
| `MidiConnection` | `Port=0`、`PrimaryChannel=0/2` | MIDI 通道 |
| `Sounds/Sound/MIDI/Program` | `24` | GM 音色：Nylon Guitar |
| `AudioEngineState` | `RSE` | 使用 RSE 音源 |
| `Lyrics` | 5 行空歌词 | 轨级歌词行定义 |
| `Automations` | `Sound` @ Bar 0 | 音色自动化 |

**定弦**（`Staff/Properties/Property[@name='Tuning']/Pitches`）：

```xml
<Pitches>40 45 50 55 59 64</Pitches>
```

| `String` 索引 | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---:|---:|---:|---:|---:|---:|
| MIDI | 40 | 45 | 50 | 55 | 59 | 64 |
| 科学音名 | E2 | A2 | D3 | G3 | B3 | E4 |

注意：**索引 0 是最低弦，索引 5 是最高弦**。这和“1 弦 = 最高音弦”的日常说法相反；`mxl_to_gp.py` 里说的“1 弦放在 `String=5`”就是这个原因。

**移调**：

```xml
<Transpose>
  <Chromatic>0</Chromatic>
  <Octave>-1</Octave>
</Transpose>
```

`Octave=-1` 表示吉他实际发声比记谱低一个八度，所以：

```text
Midi（实际发声） = 62
ConcertPitch      = D5
TransposedPitch   = D6   （记谱高一个八度）
```

非吉他轨（人声、钢琴等）通常把 `Octave` 设为 `0`，让记谱和发声一致。

### 5.4 `MasterBars`：调号、拍号、小节映射

`MasterBar` 是“全局时间轴”上的小节，负责：

- 调号：`Key/AccidentalCount`、`Key/Mode`、`Key/TransposeAs`
- 拍号：`Time`
- 本小节对应的 `Bar` 列表：`Bars`

`template.gp` 的 25 个 `MasterBar` 全是 4/4、C 大调。中途变拍号/变调时，直接改对应 `MasterBar` 的 `Time`/`Key` 即可。

模板未用、但 GPIF 支持的同级扩展字段（按 alphaTab 的解析器整理）：

| 字段 | 作用 |
|---|---|
| `Repeat` | 反复记号：`start`、`end`、`count` 属性 |
| `AlternateEndings` | 1、2 房等反复结尾 |
| `Fermatas` | 延长记号（`Type`、`Length`、`Offset`） |
| `FreeTime` | 自由拍 |
| `DoubleBar` | 双小节线 |
| `Section` | 段落标记（`Letter`、`Text`） |
| `TripletFeel` | 三连音摇摆/附点摇摆 |
| `Directions` | Segno、Coda、Fine、Da Capo 等跳转 |
| `XProperties` | GP 内部扩展属性（如小节宽度缩放） |

### 5.5 `Bars`：谱号与声部槽

`Bar` 负责“某一条轨/某一个谱表在第 N 小节怎么显示”：

- `Clef`：谱号。
- `Voices`：4 个声部槽位。

`template.gp` 的 `Bar 0` 有 2 个声部；`Bar 2`、`Bar 4` 也有 2 个；其余 45 个非空 Bar 只有 1 个声部；最后 `Bar 48`、`Bar 49`（第 25 个 MasterBar）的 `Voices` 全是 `-1`，即两个轨都空小节。

`Bar` 还可能带：

| 字段 | 作用 |
|---|---|
| `Ottavia` | 谱号八度（8va/8vb 等） |
| `SimileMark` | simile 记号（重复前一节） |
| `XProperties` | 小节显示缩放等扩展属性 |

### 5.6 `Voices`：时间顺序

`Voice/Beats` 是一个有序的 Beat id 列表：

```xml
<Voice id="0">
  <Beats>0 1 2 3</Beats>
</Voice>
```

`Voice` 的总时值 = 列表中每个 Beat 的时值之和。以 `Voice 0` 为例：

| Beat | Rhythm | 时值（四分音符单位） |
|---|---|---:|
| 0 | 0：Quarter + 1 dot | 1.5 |
| 1 | 1：16th | 0.25 |
| 2 | 1：16th | 0.25 |
| 3 | 2：Half | 2 |
| **合计** | | **4** |

4/4 小节正好是 4 个四分音符。

这里要区分两个不同的“声部”计数：

- `Bar/Voices` 里的 **4 个槽位**是“每小节最多 4 条并发声部”的容量。
- `<Voices>` section 里的 `Voice id=...` 是**全曲累计的 Voice 对象数**。每个非空槽位都会指向一个 `Voice` 对象，所以这个数可以远大于 4。

`template.gp` 一共 50 个 `Bar`：2 个空小节、45 个单声部小节、3 个双声部小节，因此全曲 Voice 对象数 = 45 + 3×2 = **51**。但任意一个小节同时最多只有 2 条（模板里没有用到 3 条或 4 条）声部在响。

### 5.7 `Beats`：节奏、和弦、休止、演奏法

`Beat` 是“同一时刻的所有音 + 一个时值”。核心子节点：

| 子节点 | 必填 | 说明 |
|---|---|---|
| `Dynamic` | 否 | 力度，模板统一 `MF` |
| `Rhythm@ref` | **是** | 指向 `Rhythm` |
| `Notes` | 否 | 多个 Note id；省略 = 休止 |
| `Properties` | 否 | 演奏法/拾音器等扩展 |
| `TransposedPitchStemOrientation` | 否 | 符干方向 |
| `ConcertPitchStemOrientation` | 否 | 实际音高符干方向 |

模板未用、但同一位置可以出现的扩展子节点包括：`Fadding`（渐强/渐弱/音量 swell）、`Tremolo`、`Chord`（和弦名）、`Hairpin`、`Arpeggio`、`FreeText`、`GraceNotes`、`Legato`、`Whammy`、`Ottavia`、`Lyrics`、`Slashed`、`DeadSlapped`、`Golpe`、`Wah`、`Timer`、`XProperties`。

`Properties` 里的 `Property` 是“名字 + 值”的通用形式，值可以是：

```xml
<Property name="PrimaryPickupVolume"><Float>0.500000</Float></Property>
<Property name="String"><String>4</String></Property>
<Property name="Fret"><Fret>3</Fret></Property>
<Property name="Midi"><Number>62</Number></Property>
<Property name="ConcertPitch"><Pitch>...</Pitch></Property>
```

这种设计让 GP 可以在不改变 XML 骨架的情况下增加新演奏法。

### 5.8 `Notes`：音高、弦、品、连音

`Note` 的五个核心属性：

| 属性 | 值类型 | 含义 |
|---|---|---|
| `ConcertPitch` | `Pitch` | 实际发声音高（用于显示/拼写） |
| `TransposedPitch` | `Pitch` | 记谱音高（受 `Track/Transpose` 影响） |
| `Midi` | `Number` | 实际发声 MIDI |
| `String` | `String` | 弦索引，0 起、0 = 最低弦 |
| `Fret` | `Fret` | 品数，可为负数（模板范围 -14…13） |

`Pitch` 的结构：

```xml
<Pitch>
  <Step>D</Step>
  <Accidental />
  <Octave>5</Octave>
</Pitch>
```

- `Step`：`A`–`G`。
- `Accidental`：空 = 还原，`#` = 升，`b` = 降；GP 还支持 `x`（重升）和 `bb`（重降）。
- `Octave`：**GPIF 的八度 = `Midi // 12`**，不是科学音高记法。

这一点非常容易写错：

| MIDI | 科学音名 | GPIF 的 `Octave` |
|---:|---|---:|
| 40 | E2 | 3 |
| 60 | C4 | 5 |
| 62 | D4 | 5 |
| 64 | E4 | 5 |

所以 `template.gp` 里 MIDI 62 写成 `D5`，而不是 `D4`；低音 E（MIDI 40）写成 `E3`，而不是 `E2`。写文件时要按 `Midi // 12` 生成 `Octave`，否则 Guitar Pro 可能把音显示高/低一个八度。

即使轨不是吉他（钢琴、人声等），GP 的 `Note` 仍然要求 `String`/`Fret`；常见做法是给这类轨一根虚拟弦并用品数承载音高，改乐器后实际发声仍以 `Midi` 为准。

其余可选内容（模板未用）：

- `Tie`：连音线。
- `InstrumentArticulation`：发音法/鼓件索引。
- `Accent`、`Vibrato`、`LetRing`、`Trill`、`Ornament`、`LeftFingering`、`RightFingering` 等子节点。
- `Properties` 中的 `Bended`/`Bend*`、`HarmonicType`、`Muted`、`PalmMuted`、`HopoOrigin`、`Slide`、`Tapped` 等。

### 5.9 `Rhythms`：节奏型表

`template.gp` 的 9 种节奏型：

| id | `NoteValue` | `AugmentationDot` | `PrimaryTuplet` | 时值（四分音符单位） |
|---:|---|---:|---|---:|
| 0 | `Quarter` | 1 | — | 1.5 |
| 1 | `16th` | 0 | — | 0.25 |
| 2 | `Half` | 0 | — | 2 |
| 3 | `Eighth` | 0 | — | 0.5 |
| 4 | `Quarter` | 0 | — | 1 |
| 5 | `Whole` | 0 | — | 4 |
| 6 | `16th` | 0 | 3:2 | 1/6 |
| 7 | `32nd` | 0 | — | 0.125 |
| 8 | `Eighth` | 1 | — | 0.75 |

计算规则：

```text
基础时值（四分音符单位）：
  Whole=4, Half=2, Quarter=1, Eighth=1/2, 16th=1/4,
  32nd=1/8, 64th=1/16

附点因子：
  1 + 1/2 + 1/4 + ... = 2 - 1/2^count
  count=1 -> 1.5, count=2 -> 1.75

连音因子：
  den / num
  3:2 的三连音 -> 每个音 = 基础时值 × 2/3

最终时值 = 基础时值 × 附点因子 × 连音因子
```

`Rhythm` 是去重表：181 个 Beat 只引用 9 个 `Rhythm` 对象。`NoteValue` 还支持 `Long`、`DoubleWhole`、`64th`、`128th`、`256th`（模板未用）。

`Beat` 的 `Rhythm@ref` 必须存在；没有 `Rhythm` 的 Beat 是不完整的。

---

## 6. 三条必须成立的等式

写/读 GPIF 时，最值得先检查的是这三条。

### 6.1 小节时值

```text
sum(该 Voice 所有 Beat 的时值) = 拍号分子 × 4 / 拍号分母
```

- 4/4：`4 × 4 / 4 = 4` 个四分音符。
- 6/8：`6 × 4 / 8 = 3` 个四分音符。
- 3/4：`3` 个四分音符。

`template.gp` 的所有非空 Voice 都满足 4/4 的 4 个四分音符。

### 6.2 音高与弦品

```text
Midi = Tuning[String] + Fret
```

`Tuning` 是 `Staff/Properties/Property[@name='Tuning']/Pitches`，按低弦→高弦排列；`String` 是它的下标。模板的 `CapoFret=0`，所以这个等式直接成立；有变调夹时还要把 capo 的品数算进去。模板 68 个 Note 全部满足。

### 6.3 记谱音高与发声音高

```text
Midi          = 实际发声
ConcertPitch  = 实际发声的拼写
TransposedPitch = 记谱拼写
```

模板中 `Track/Transpose/Octave = -1`，所以 `TransposedPitch` 比 `ConcertPitch` 高一个八度。若轨的 `Octave = 0`，两者音高应一致（拼写可能因调号不同而不同）。

---

## 7. 其余文件：显示与布局，不是乐谱

### 7.1 `ScoreViews` + `.gpsv`

`score.gpif` 里：

```xml
<ScoreViews>
  <ScoreView id="0" />
  <ScoreView id="1" />
  <ScoreView id="2" />
</ScoreViews>
```

对应：

```text
Content/ScoreViews/0.gpsv
Content/ScoreViews/1.gpsv
Content/ScoreViews/2.gpsv
```

`0.gpsv` 里能看到字符串 `Full score`，`1.gpsv`/`2.gpsv` 是 `Nylon Guitar`。也就是：

- 视图 0：总谱
- 视图 1..N：各轨的单轨视图

`template.gp` 有 2 条轨，所以是 1 + 2 = 3 个视图。`365-转换.gp` 有 13 条轨，因此有 14 个 `.gpsv`。

`.gpsv` 是二进制/协议式的视图配置，不需要为了写音符去解析；从模板克隆即可。

### 7.2 `Stylesheets/*.gpss`

- `score.gpss`：总谱样式。
- `scoreviewN.gpss`：视图 N 的样式。
- 模板里三个 `scoreview*.gpss` 都是 285 字节，内容相同。

它们是页面/谱表样式，不是音乐数据。

### 7.3 `BinaryStylesheet`

二进制键值表，保存字体、页眉页脚、符头、符干宽度等显示设置。文件里能看到类似：

```text
Header/drawArtist
Even/EvenHeader
%TITLE% by %ARTIST%
Global/PalmMutePriority
```

它决定“怎么画”，不决定“画什么音”。

### 7.4 `LayoutConfiguration` / `PartConfiguration`

二进制布局文件：

- `LayoutConfiguration`：页面/视图布局。模板 14 字节，13 轨的 `365-转换.gp` 是 36 字节。
- `PartConfiguration`：多轨/单轨视图里显示哪些轨和谱表。模板 27 字节，13 轨时 104 字节。

大小会随轨数/视图数变化，但格式是私有的；生成时直接从模板复制即可。

### 7.5 `Preferences.json`

模板内容：

```json
{
  "tonality": { "isActive": false, "value": 0 },
  "tempo": {
    "isActive": false,
    "mode": "Constant",
    "constantTempo": 100,
    "constantBPM": 100,
    "fromTempo": 70,
    "toTempo": 100,
    "step": 10,
    "repeatCount": 1
  },
  "view": {
    "globalViewHeight": -1,
    "globalViewCurrentTrackFocusFactor": 0.5,
    "backingTrackVisible": false,
    "backingTrackHeight": 240
  }
}
```

这里的 `tempo` 是**速度训练器/节拍器偏好**，`isActive=false`，不是乐谱速度。乐谱速度在 `MasterTrack/Automations` 里（模板是 72 BPM）。

---

## 8. `template.gp` 的数据画像

把上面的结构统计一下：

| 项目 | 数量 |
|---|---:|
| 轨 `Track` | 2 |
| 全局小节 `MasterBar` | 25 |
| 轨/谱表小节 `Bar` | 50（25 × 2） |
| `Voice` 对象（全曲累计） | 51 |
| 节奏事件 `Beat` | 181 |
| 音高对象 `Note` | 68 |
| 节奏型 `Rhythm` | 9 |
| 视图 `ScoreView` | 3 |
| 速度 | 72 BPM |
| 拍号/调号 | 全 4/4、C 大调 |
| 谱号 | 全 `G2` |
| Note 引用总数 | 320 |
| Beat 引用总数 | 262 |

内容特征：

- 25 个 MasterBar 中，前 24 个有音乐，最后 1 个（`Bar 48/49`）是空小节。
- 50 个 Bar 中：45 个单声部、3 个双声部、2 个空。
- 177 个有音符的 Beat：87 个单音、90 个和弦；另有 4 个休止。
- 68 个 Note 被引用 320 次；45 个 Note 被多处复用。例如 Note 14 被引用 26 次。
- 181 个 Beat 被引用 262 次；41 个 Beat 被多处复用。例如 Beat 23 被引用 12 次。
- 两条轨都是 Nylon Guitar、标准定弦、`Transpose Octave=-1`；乐器与音色相同，布局和 MIDI 通道不同。
- 音域和品数很“压力测试”：品数从 -14 到 13，说明 GPIF 里可以写负数品（用于特殊记谱/虚拟位置）；实际兼容性取决于目标软件，普通转换器最好不要依赖这一点。

因此，`template.gp` 更像一个**结构模板/压力样本**，不是一首正常演奏的曲子。它把“多轨、多声部、和弦、休止、连音、附点、三连音、空小节、共享对象、极端品数”都塞进了一个小文件，很适合拿来观察格式。

---

## 9. 写一个 GP 文件的检查清单

如果要从零生成或改写 `score.gpif`，按这个顺序检查：

1. **容器**：ZIP 里有 `VERSION`、`meta.json`、`Content/score.gpif`；GP8 建议保留 `GPVersion=8.1.5`、`EncodingDescription=GP8`。
2. **轨**：`Tracks` 里 `Track id="0..N-1"`；每条轨有 `Name`、`InstrumentSet`、`Staves`、`Tuning`、`Transpose`、`Sounds`。
3. **全局小节**：`MasterBars` 有 M 个 `MasterBar`；每个有 `Key`、`Time`、`Bars`。
4. **小节**：`Bars` 有 M × S 个（S = 所有轨的 staff 总数；模板每轨 1 个 staff，所以 S = N）；每个 `Bar` 有 `Clef`、4 槽 `Voices`。
5. **声部**：只为非空槽创建 `Voice`；`Voice/Beats` 按时间顺序。
6. **节奏事件**：每个 `Beat` 必须有 `Rhythm@ref`；和弦写多个 `Notes`；休止不要写 `Notes`。
7. **音高**：每个 `Note` 至少有 `String`、`Fret`、`Midi`；非弦乐器也要给 `String`/`Fret`（GP 的硬性要求）；建议同时写 `ConcertPitch`、`TransposedPitch`。
8. **节奏表**：`Rhythms` 覆盖所有用到的 `NoteValue`/附点/连音组合。
9. **引用完整性**：`MasterBar/Bars`、`Bar/Voices`、`Voice/Beats`、`Beat/Notes`、`Beat/Rhythm` 全部指向存在的 id。
10. **时值**：每个非空 Voice 的总时值 = 拍号；否则 MuseScore 可能拒绝，GP 通常能打开但会显示异常。
11. **音高一致性**：`Midi == Tuning[String] + Fret`；`ConcertPitch` 的八度 = `Midi // 12`。
12. **连音**：用相邻 Beat 的两个 Note 表示，`origin`/`destination` 成对出现。
13. **视图/样式**：`ScoreViews` 的 id 与 `Content/ScoreViews/{id}.gpsv`、`Stylesheets/scoreview{id}.gpss` 对应；不确定就直接从模板复制整套。
14. **XML**：UTF-8，无命名空间，标签大小写敏感；空元素可以自闭合；文本可以用 CDATA。

一个最小可用的写法是：**克隆 `template.gp` 的容器和 `Track`，只替换 `MasterBars`/`Bars`/`Voices`/`Beats`/`Notes`/`Rhythms`/`ScoreViews`**。这正是 `mxl_to_gp.py` 的做法：保留模板的样式和布局，重新生成音乐对象。

---

## 10. 用 Python 读一遍

不依赖第三方库，直接解 ZIP + 解析 XML：

```python
import zipfile
import xml.etree.ElementTree as ET

with zipfile.ZipFile("template.gp") as z:
    root = ET.fromstring(z.read("Content/score.gpif"))

def text(e):
    return (e.text or "").strip() if e is not None else ""

def refs(e):
    return text(e).split() if e is not None else []

# 按 id 建表；同一 id 在各自 section 内唯一
bars    = {b.get("id"): b for b in root.find("Bars")}
voices  = {v.get("id"): v for v in root.find("Voices")}
beats   = {b.get("id"): b for b in root.find("Beats")}
notes   = {n.get("id"): n for n in root.find("Notes")}
rhythms = {r.get("id"): r for r in root.find("Rhythms")}

def prop(node, name):
    return node.find(f"Properties/Property[@name='{name}']")

for mi, mb in enumerate(root.find("MasterBars")):
    print(f"--- MasterBar {mi}: {text(mb.find('Time'))} ---")
    for bi in refs(mb.find("Bars")):
        bar = bars[bi]
        for vi in refs(bar.find("Voices")):
            if vi == "-1":
                continue
            voice = voices[vi]
            for bid in refs(voice.find("Beats")):
                beat = beats[bid]
                rhythm = rhythms[beat.find("Rhythm").get("ref")]
                nids = refs(beat.find("Notes"))
                if not nids:
                    print(f"  bar={bi} voice={vi} beat={bid} rest "
                          f"{text(rhythm.find('NoteValue'))}")
                    continue
                for nid in nids:
                    n = notes[nid]
                    midi = text(prop(n, "Midi").find("Number"))
                    string = text(prop(n, "String").find("String"))
                    fret = text(prop(n, "Fret").find("Fret"))
                    print(f"  bar={bi} voice={vi} beat={bid} "
                          f"{text(rhythm.find('NoteValue'))} "
                          f"midi={midi} string={string} fret={fret}")
```

这段代码会按“全局小节 → 每轨小节 → 声部 → Beat → Note”的顺序展开，并跳过 `-1` 空声部。注意 `Beat`/`Note` 可能被复用；如果要做统计，应该分别统计“对象数”和“引用数”。

---

## 11. 一句话总结

`.gp` 是“ZIP 容器 + XML 对象图”：`score.gpif` 用 `MasterBar` 管全局小节，用 `Bar` 管每轨/每谱表，用 `Voice` 管声部，用 `Beat` 管节奏事件，用 `Note` 管音高/弦品；`Rhythm` 单独存时值；对象之间靠 id 和空格分隔的引用列表连接。其余文件只负责“长什么样、用什么音色、怎么排版”。

---

## 参考

- [Guitar Pro 7 (.gp) — alphaTab](https://alphatab.net/docs/formats/guitar-pro-7/)
- [Guitar Pro 8 (.gp) — alphaTab](https://alphatab.net/docs/formats/guitar-pro-8/)
- [The Guitar Pro 7 file format（官方格式说明，.doc）](https://musescore.org/sites/musescore.org/files/2017-08/the_guitarpro_7_file%20format.doc)
- [alphaTab 源码（GPIF 读写实现）](https://github.com/CoderLine/alphaTab)

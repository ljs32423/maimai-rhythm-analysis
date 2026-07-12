# maimai Rhythm Analysis

> 解析 `maidata.txt` → 渲染节奏图 → 录制预览视频 → 生成可交互 HTML 分析页面，支持自动音频对齐。

## 快速开始

```powershell
pip install -r requirements.txt
python -m mra.run_all -d "QZKago Requiem"
# → songs/QZKago Requiem/outputs/MASTER/html/analysis.html
```

## 技术栈

| 层级 | 技术 |
|------|------|
| 谱面解析 | Python `re` — 自研 [simai](https://w.atwiki.jp/simai/) 格式解析器 |
| 信号处理 | `numpy`, `scipy.signal`（FFT 互相关、STFT） |
| 图形渲染 | `matplotlib` → SVG + PNG 节奏图表 |
| 视频录制 | `subprocess` → MajdataView HTTP API → FFmpeg 屏幕捕获 |
| 前端 | **零依赖原生 JS** — `requestAnimationFrame` + CSS `translate3d` GPU 滚动 |
| 编排调度 | `subprocess` 管线 — 每个阶段独立 `python -m` 进程 |
| 运行时 | MajdataView 4.3.1 (.NET 9)、MajdataBridge (.NET 8)、FFmpeg |

## 模块架构

```
mra/
├── simai_parser.py      # 词法/语法解析：maidata.txt → SongData（BPM 时间线、音符列表）
├── difficulty.py        # 难度 ID ↔ 名称映射、文件路径约定
├── song_library.py      # 歌曲发现、目录命名、DX 检测
├── visualize.py         # compute_rhythm_events() → matplotlib SVG/PNG 渲染
├── render_preview.py    # MajdataView 自动化 → FFmpeg → preview.mp4
├── align_audio.py       # 能量包络互相关 + 波形细化 → offset.txt
├── make_html.py         # 模板引擎：注入 JS 常量与 SVG 分段 → analysis.html
└── run_all.py           # 编排器：subprocess 管线（visualize → render → align → html）
```

### 数据流

```
maidata.txt ──[simai_parser]──▶ SongData
                                   │
     ┌─────────────────────────────┤
     ▼                             ▼
[visualize]                   [render_preview]
rhythm.{svg,png}               MajdataView → FFmpeg → preview.mp4
     │                             │
     ▼                             ▼
[align_audio] ◀── track.mp3 ──▶ 能量包络 FFT 互相关
     │                             │
     ▼                             ▼
offset.txt ──────────────────▶ [make_html]
                                   │
                                   ▼
                            analysis.html
                 （原生 JS，GPU 滚动，BPM 感知快进，±1ms 延迟微调）
```

### 音频对齐策略

```
Tier 1（主力）   能量包络（8ms 帧，±0.50 削波）→
                FFT 互相关 [-15s, +45s] →
                波形细化（150ms 搜索半径，全量数据）
                │
                ├─ 置信度 ≥ 0.15 → 采用
                └─ 置信度 < 0.15
                    │
                    ▼
Tier 2（回退）   RMS 能量 onset 检测（5ms 帧，P40 阈值）
                → 1s 后首个持续高能量段
```

## 目录结构

```
maimai-rhythm-analysis/
├── mra/                     # 核心模块
├── tests/                   # pytest（解析器、对齐、工作流、生成）
├── tools/
│   ├── build_release.ps1    # 发行包构建脚本
│   └── src/majdata_bridge/  # .NET 8 桥接程序（C#）
├── .tools/                  # MajdataView 4.3.1 + 桥接二进制（gitignore）
├── songs/                   # 歌曲输入目录（gitignore）
│   └── <曲名>/
│       ├── maidata.txt
│       ├── track.mp3
│       ├── bg.png
│       └── outputs/<难度>/
└── release/                 # 构建产物（gitignore）
```

## 命令

```powershell
# 全流程（默认 MASTER + Re:MASTER）
python -m mra.run_all                        # 批量处理全库
python -m mra.run_all -d "曲名"              # 单曲
python -m mra.run_all -d "曲名" -diff 5 -f   # 指定难度 + 强制覆盖

# 分步执行
python -m mra.visualize        -d "曲名" -diff 5
python -m mra.render_preview   -d "曲名" -diff 5 -f
python -m mra.align_audio      -d "曲名" -diff 5
python -m mra.make_html        -d "曲名" -diff 5 -f

# 测试
python -m pytest -q
```

`-diff`: `1`=EASY `2`=BASIC `3`=ADVANCED `4`=EXPERT `5`=MASTER `6`=Re:MASTER `7`=UTOPIA

## 输出产物

```
outputs/MASTER/
├── html/analysis.html      # 交互播放器（播放/暂停/拖拽/±1s/倍速）
├── video/preview.mp4       # 2560×1440 60fps，默认关闭 PV
├── sync/offset.txt         # 单行浮点数偏移（秒）
├── rhythm/rhythm.{png,svg} # 完整节奏图
└── strip/strip.svg         # 滚动条底图 + 分段 SVG（懒加载）
```

Re:MASTER → `outputs/ReMASTER/`。

## 环境要求

- Windows
- Python ≥ 3.10
- `pip install -r requirements.txt`（matplotlib、numpy、scipy）
- FFmpeg 在 PATH 中（或使用发行包自动复用 MajdataView 内置的 ffmpeg）

每首歌需要：`maidata.txt` + `track.mp3`。可选：`bg.png`、`pv.mp4`。
含 touch / touch hold / 烟花触摸的曲目自动标记为 `曲名 [DX]`。

## 发行打包

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_release.ps1
```

生成 `release/` 目录，免安装 Python。将 `app/` 与 `required-programs/` 同级放置，双击 `run_all.bat` 即可运行。

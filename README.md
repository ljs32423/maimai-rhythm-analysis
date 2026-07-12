# Maimai Rhythm Analysis

把 maimai 的 `maidata.txt` 谱面转成可视化的节奏分析结果：节奏图、预览视频、可交互的前端分析页面，支持自动音频对齐。

## 快速开始

```powershell
pip install -r requirements.txt
python -m mra.run_all -d "QZKago Requiem"
```

完成后在 `songs/QZKago Requiem/outputs/MASTER/html/analysis.html` 打开分析页面。

## 运行环境

- Windows + Python 3.10+
- FFmpeg（需在 PATH 中；或使用发行包，会复用 MajdataView 自带的 ffmpeg）

## 歌曲目录

```
songs/
└── 某首歌/
    ├── maidata.txt    # 谱面
    ├── track.mp3      # 音频
    ├── bg.png         # 背景图（可选）
    └── pv.mp4         # PV 视频（可选）
```

含 touch / 保护套的曲目自动标记为 `曲名 [DX]`。

## 命令参考

### 一键处理

```powershell
python -m mra.run_all                        # 批量处理全库
python -m mra.run_all -d "曲名"              # 单曲，默认 MASTER + Re:MASTER
python -m mra.run_all -d "曲名" -diff 5 -f   # 指定难度 + 强制覆盖
```

`-diff`: `1`=EASY `2`=BASIC `3`=ADVANCED `4`=EXPERT `5`=MASTER `6`=Re:MASTER `7`=UTOPIA

### 分步执行

```powershell
python -m mra.visualize   -d "曲名" -diff 5    # 只生成节奏图
python -m mra.render_preview -d "曲名" -diff 5 -f  # 只录预览视频
python -m mra.align_audio -d "曲名" -diff 5    # 只算音频偏移
python -m mra.make_html   -d "曲名" -diff 5 -f # 只生成分析页面
```

## 分析页面功能

`analysis.html` 支持播放、拖拽进度条、方向键 ±1s、0.01 精度倍速、节奏滚动条与视频同步。

## 输出结构

```
歌曲文件夹/outputs/MASTER/
├── html/analysis.html      # 分析页面
├── video/preview.mp4       # 预览视频
├── sync/offset.txt         # 音频对齐偏移
├── rhythm/rhythm.{png,svg} # 节奏图
└── strip/strip.svg         # 滚动条素材
```

Re:MASTER 对应 `outputs/ReMASTER/`。

## 下载使用

从 [Releases](../../releases) 下载 `maimai-analysis.zip`，解压后：

```
解压目录/
├── run_all.bat          ← 双击使用
├── songs/               ← 把歌曲放这里
│   └── 某首歌/
│       ├── maidata.txt
│       └── track.mp3
├── mra/
└── .tools/              # MajdataView + FFmpeg（已内置）
```

无需安装 Python 或 FFmpeg，解压即用。

## 测试

```powershell
python -m pytest -q
```

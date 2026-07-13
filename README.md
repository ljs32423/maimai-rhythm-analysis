# Maimai Rhythm Analysis

把 maimai 的 `maidata.txt` 谱面转成可视化的节奏分析结果：节奏图、预览视频、可交互的前端分析页面，支持人工变拍号时间轴与音频对齐。

## 快速开始

从项目的 [GitHub Releases](https://github.com/ljs32423/maimai-rhythm-analysis/releases)
下载名称以 `windows-x64-full.zip` 结尾的完整包并解压。完整包已经包含
MajdataViewX、ffprobe 和 MajdataBridge，运行过程中不会再下载第三方工具。

```powershell
cd app
pip install -r requirements.txt
python -m mra.run_all -d "QZKago Requiem"
```

也可以在 `app` 目录双击 `run_all.bat`。第一次使用前仍需安装 Python 3.11 或更高版本，
并执行一次 `pip install -r requirements.txt`。源码仓库不提交数百 MB 的第三方二进制，
因此普通用户应下载 Release 完整包，而不是 GitHub 自动生成的 Source code ZIP。

项目首次处理某个难度时会生成一份默认 `4/4` 的 `meter.json`，之后可以人工加入变拍节点。

按上述指令运行完成后，在 `songs/QZKago Requiem/outputs/MASTER/html/analysis.html` 打开分析页面，即可查看节奏解析。

## 歌曲目录

```
songs/
└── 某首歌/
    ├── maidata.txt    # 谱面
    ├── track.mp3      # 音频
    ├── bg.png         # 背景图
    └── pv.mp4         # PV 视频（可选）
```

含 touch / 保护套的曲目会自动标记为 `曲名 [DX]`。

## 命令参考

### 一键处理

```powershell
python -m mra.run_all                        # 批量处理全库
python -m mra.run_all -d "曲名"              # 单曲，默认 MASTER + Re:MASTER
python -m mra.run_all -d "曲名" -diff 5 -f   # 指定难度 + 强制覆盖
```

`-diff`: `1`=EASY `2`=BASIC `3`=ADVANCED `4`=EXPERT `5`=MASTER `6`=Re:MASTER `7`=UTOPIA

`-f` 只强制重建图片和网页，不会覆盖已有 `meter.json`、谱面预览视频或
`offset.txt` 音频对齐结果。

### 分步执行

```powershell
python -m mra.init_meter -d "曲名" -diff 5    # 生成默认 4/4 拍号文件
python -m mra.visualize   -d "曲名" -diff 5    # 只生成节奏图
python -m mra.render_preview -d "曲名" -diff 5 -f  # 只录预览视频
python -m mra.align_audio -d "曲名" -diff 5    # 只算音频偏移
python -m mra.make_html   -d "曲名" -diff 5 -f # 只生成分析页面
```

### 变拍号与人工编辑

Simai 格式没有拍号字段，首次运行时会在 `outputs/<难度>/meter/meter.json` 初始化一个 `4/4` 节点。

直接编辑这个文件，在实际发生拍号变化的位置添加节点：

```json
{
  "default": "4/4",
  "sections": [
    { "start_beat": 0, "signature": "4/4" },
    { "start_beat": 64, "signature": "7/8" },
    { "start_beat": 67.5, "signature": "3/4" },
    { "start_beat": 79.5, "signature": "4/4" }
  ]
}
```

修改拍号后，运行 `python -m mra.visualize -d "曲名" -diff 5 -f` 即可重新生成图片。

## 输出结构

```
歌曲文件夹/outputs/MASTER/
├── html/analysis.html      # 分析页面
├── video/preview.mp4       # 预览视频
├── sync/offset.txt         # 音频对齐偏移
├── meter/meter.json        # 仅含人工维护的拍号变化节点
├── rhythm/rhythm.{png,svg} # 节奏图
└── strip/strip.svg         # 滚动条素材
```

Re:MASTER 对应 `outputs/ReMASTER/`。

## 测试

```powershell
python -m pytest -q
```

## 构建发行包

维护者本机准备好 `.tools` 后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_release.ps1 -Version v0.2.0
```

脚本只打包 MajdataViewX 6.0.0、ffprobe 6.1.1 和 MajdataBridge，不包含歌曲、
旧版工具、模型文件或下载缓存，并在 `release/` 同时生成 ZIP 和 SHA-256 文件。

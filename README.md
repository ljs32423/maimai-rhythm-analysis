# maimai Rhythm Analysis

这个项目用于把 maimai 的 `maidata.txt` 谱面做成一套可直接观看和校对的分析结果，主要解决三件事：

- 把谱面解析成清晰的节奏图
- 自动录制 MajdataView 谱面预览视频
- 生成一个可播放、可拖动、可校对延迟的前端分析页面

最终每个难度会生成：

- 节奏长图 `outputs/<难度>/rhythm/rhythm.svg`
- 折行预览图 `outputs/<难度>/rhythm/rhythm.png`
- 前端滚动条素材 `outputs/<难度>/strip/strip.svg`
- 前端 SVG 分段素材 `outputs/<难度>/strip/segments/strip_seg_*.svg`
- 谱面预览视频 `outputs/<难度>/video/preview.mp4`
- 自动对齐结果 `outputs/<难度>/sync/offset.txt`
- 分析页面 `outputs/<难度>/html/analysis.html`

## 适用场景

- 做单曲节奏解析
- 校对谱面与音频的首音对齐
- 录制不带 PV、低亮度背景的谱面预览
- 对特定难度单独生成分析结果
- 批量处理整个歌曲库

## 运行环境

- Windows
- Python 3.10 或更高版本
- FFmpeg

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

如果使用仓库里的发行目录，FFmpeg 和 Majdata 相关运行时可以直接使用打包好的版本；否则需要自己准备 FFmpeg，并保证 `ffmpeg`、`ffprobe` 在 `PATH` 中可用。

如果拿到的是完整 `release` 压缩包，里面已经包含便携 Python、Python 依赖、MajdataView、谱面桥接程序、FFmpeg 和 FFprobe。对方解压后不需要安装 Python，也不需要配置 PATH。把歌曲放入 `app/songs/` 后，双击：

```text
app/开始使用.bat
```

## 歌曲目录

歌曲统一放在 `songs/` 下，每首歌一个文件夹。最基本需要：

```text
songs/
└── 某首歌/
    ├── maidata.txt
    └── track.mp3
```

如果要录制带背景的视频，可额外放：

```text
pv.mp4
```

项目会自动读取 `maidata.txt` 的标题来整理歌曲目录名。  
如果某首歌的任一难度包含 `touch`、`touch hold` 或烟花触摸，则会按 DX 曲处理，目录名自动补成：

```text
曲名 [DX]
```

## 最常用的命令

一键处理整首歌，默认处理 `MASTER`，如果存在则再处理 `Re:MASTER`：

```powershell
python -m mra.run_all -d "QZKago Requiem"
```

批量处理整个歌曲库：

```powershell
python -m mra.run_all
```

强制覆盖已有结果：

```powershell
python -m mra.run_all -d "QZKago Requiem" -f
```

只处理指定难度：

```powershell
python -m mra.run_all -d "QZKago Requiem" -diff 5
```

`-diff` 对应关系：

- `1` = EASY
- `2` = BASIC
- `3` = ADVANCED
- `4` = EXPERT
- `5` = MASTER
- `6` = Re:MASTER
- `7` = ORIGINAL / UTOPIA 这类额外难度位

## 分步骤使用

只生成节奏图：

```powershell
python -m mra.visualize -d "QZKago Requiem" -diff 5
```

只录制谱面预览视频：

```powershell
python -m mra.render_preview -d "QZKago Requiem" -diff 5 -f
```

只重算音频对齐延迟：

```powershell
python -m mra.align_audio -d "QZKago Requiem" -diff 5
```

只生成前端分析页面：

```powershell
python -m mra.make_html -d "QZKago Requiem" -diff 5 -f
```

## 前端页面能做什么

生成的 `outputs/<难度>/html/analysis.html` 支持：

- 播放 / 暂停
- 拖动进度条跳转
- 悬停查看时间
- 键盘左右方向键按秒快进 / 快退
- 倍速播放（0.01 精度）
- 节奏滚动条和视频同步查看

它适合拿来检查：

- 首音是否与首个音符同时到达
- 变 BPM 段是否滚动正确
- 某一段交互、转圈、尾杀是否按预期显示

## 录制默认参数

当前默认录制设置：

- 分辨率：`2560 x 1440`
- 帧率：`60 FPS`
- Tap 速度：`7.5`
- Touch 速度：`7.5`
- PV 播放：默认关闭
- 背景亮度覆盖：`50%`
- 键音：默认开启

如果只想看谱面，不想播放 PV，当前默认行为已经是关闭 PV，只保留压暗后的背景效果。

## 发行目录说明

项目整理后的发行目录分成两部分：

- `release/app/`：程序本体，不含歌曲库
- `release/required-programs/`：运行时必需工具

其中 `required-programs` 里会包含：

- `.tools/majdata/`：MajdataView 运行文件
- `.tools/majdata_bridge/`：谱面桥接程序

使用发行目录时，保持 `app` 和 `required-programs` 同级即可。

发行版推荐直接在 `release/app/` 中运行：

```powershell
run_all.bat -d "QZKago Requiem" -f
```

瘦身版发行目录不再额外附带独立 FFmpeg，程序会直接复用 `MajdataView` 自带的 `ffmpeg.exe` 和 `ffprobe.exe`。

## 输出文件说明

以 `MASTER` 为例，处理完成后歌曲目录通常是：

```text
歌曲文件夹/
├── maidata.txt
├── track.mp3
├── bg.png
└── outputs/
    └── MASTER/
        ├── html/
        │   └── analysis.html
        ├── video/
        │   └── preview.mp4
        ├── sync/
        │   └── offset.txt
        ├── rhythm/
        │   ├── rhythm.png
        │   └── rhythm.svg
        └── strip/
            ├── strip.svg
            └── segments/
                ├── strip_seg_000.svg
                ├── strip_seg_001.svg
                └── ...
```

如果存在 `Re:MASTER`，会对应生成 `outputs/ReMASTER/`。旧版扁平产物如 `MASTER_preview.mp4`、`ReMASTER_strip.svg` 仍可被程序识别；重新运行后会整理进新的 `outputs/<难度>/` 结构。

## 测试

```powershell
python -m pytest -q
```

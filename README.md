# Maimai Rhythm Analysis

把 maimai 的 `maidata.txt` 谱面转成可视化的节奏分析结果：节奏图、预览视频、可交互的前端分析页面，支持人工变拍号时间轴与音频对齐。

## 快速开始

从项目的 [GitHub Releases](https://github.com/ljs32423/maimai-rhythm-analysis/releases)
下载名称以 `windows-x64-full.zip` 结尾的完整包并解压。

完整包自带 Python、FFmpeg/ffprobe、MajdataViewX 和全部 Python 依赖。
解压后进入 `app`，双击 `启动节奏分析.cmd`，程序会启动本地 Web 服务并自动
打开浏览器，不需要预先安装 Python。

源码运行可以使用：

```powershell
cd maimai-rhythm-analysis
python -m pip install -r requirements.txt
python -m mra.web_app
```

Web 应用仅监听 `127.0.0.1`，可以扫描歌曲库、选择难度、编辑拍号和
`maidata_sweep.txt`、查看生成进度并直接打开分析播放器。录制任务串行执行，
避免多个 MajdataView 实例争用显卡与录制管线。

原命令行工作流仍然保留：

```powershell
python -m mra.run_all -d "QZKago Requiem"
```

项目首次处理某个难度时会生成一份默认 `4/4` 的 `meter.json`，之后可以人工加入变拍节点。

按上述指令运行完成后，在 `songs/QZKago Requiem/outputs/MASTER/html/analysis.html` 打开分析页面，即可查看节奏解析。

## 歌曲目录

```
songs/
└── 某首歌/
    ├── maidata.txt    # 谱面
    ├── maidata_sweep.txt # 首次可视化时自动生成；人工扫键头标记谱面
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

`-f` 强制重建可视化，但不会覆盖已有 `maidata_sweep.txt`、`meter.json`、
谱面预览视频或 `offset.txt` 音频对齐结果。

### 硬件编码

程序启动后会用短视频进行真实试编码，而不是只检查编码器名称，并按以下顺序
自动选择可用编码器：

1. NVIDIA NVENC
2. Intel Quick Sync
3. AMD AMF
4. CPU `libx264`

硬件转码失败时，PV 预处理和最终裁切会自动使用 `libx264` 重试。Web 设置页会
显示每个编码器的检测结果，也可以在 `config.json` 中人工指定：

```json
{
  "encoder": "auto",
  "recording": {
    "width": 2560,
    "height": 1440,
    "fps": 60,
    "quality": "high"
  }
}
```

支持的画质档位为 `balanced`、`high` 和 `maximum`。设置修改后重启 Web 应用
即可让 MajdataView 录制参数使用新配置。

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

只有分母为 `4` 的拍号会按每个四分拍绘制内部弱线。
`6/8`、`7/8`、`3/16` 等其他拍号只绘制强小节线。

修改拍号后，运行 `python -m mra.visualize -d "曲名" -diff 5 -f` 即可重新生成图片。

### 扫键头人工修正

首次生成可视化时，程序会复制 `maidata.txt` 为歌曲目录下的
`maidata_sweep.txt`，并在机器识别到的扫键头音符组末尾加入 `/S`。
从此以后完全以这份人工文件为准：有 `/S` 就标记，没有就不标记。
漏判时手动加入 `/S`，误判时直接删除已有的 `/S`。

例如：

```text
{32}5/7h[1:0]/S,8,1,2,3,4,
```

人工文件不会被 `-f` 覆盖。只应增删 `/S`，不要修改其中的谱面时间结构；
如果原始 `maidata.txt` 已变化，程序会保留人工文件并输出提示。需要重新初始化时，
删除 `maidata_sweep.txt` 后再次运行即可。

修改标记后，运行以下任一命令重建可视化：

```powershell
python -m mra.run_all -d "曲名" -diff 5 -f
python -m mra.visualize -d "曲名" -diff 5 -f
```

网页版直接滚动分段 SVG：每段最多 16 拍，生成页面时会把所有分段提前载入、
解码并上传给浏览器，完成预热前播放按钮保持禁用。播放期间所有分段始终挂载，
每个显示帧只对同一个父图层写入一次 `translate3d`，避免切段时增删 DOM 或重新
解析 SVG。这个策略会多占用内存和 GPU 纹理，但能换取更稳定的高刷新率滚动，
同时保留矢量线条、文字和彩色外环的清晰度。

PV 负责播放、暂停和拖动；节奏条位置由 `performance.now()` 单调时钟以绝对速度
连续推进，并在播放开始、暂停和人工拖动时重新建立锚点。正常播放途中不周期性
强制改位置，避免同步修正造成肉眼可见的顿挫。BPM、小节号和拍号状态只按
100ms 的低频率更新，不参与每帧滚动。

不要直接双击 `analysis.html`。生成器会在同目录创建 `打开分析页面.cmd`；
双击后启动支持 HTTP Range 的本地服务，使视频可以快速预加载和远距离拖动。
服务连续 30 分钟没有新请求后自动退出。

## 输出结构

```
歌曲文件夹/outputs/MASTER/
├── html/analysis.html      # 分析页面
├── html/打开分析页面.cmd   # 支持视频定位的本地服务器启动器
├── video/preview.mp4       # 预览视频
├── sync/offset.txt         # 音频对齐偏移
├── meter/meter.json        # 仅含人工维护的拍号变化节点
├── rhythm/rhythm.{png,svg} # 节奏图
├── strip/strip.svg         # 滚动条矢量素材
└── strip/segments/*.svg    # 网页预热并连续滚动的 16 拍分段
```

Re:MASTER 对应 `outputs/ReMASTER/`。

## 测试

```powershell
python -m pytest -q
```

## 构建完整发行包

以下命令会下载并校验 Python 3.12.10 Windows Embedded 运行时，把
`requirements-runtime.txt` 中固定版本的依赖安装到运行时，然后生成完整 ZIP：

```powershell
.\tools\build_runtime.ps1
.\tools\build_release.ps1 -Version v0.3.0
```

构建机需要安装同系列的 Python 3.12；最终发行包不需要系统 Python。

# Maimai Rhythm Analysis

将 maimai 的 `maidata.txt` 生成节奏图、预览视频和可交互分析页面。
支持变拍号、扫键头人工修正与音频对齐。

## 快速开始

从 [GitHub Releases](https://github.com/ljs32423/maimai-rhythm-analysis/releases)
下载 `windows-x64-full.zip`，解压后进入 `app`，双击
`启动节奏分析.cmd`。完整包已包含 Python、FFmpeg 和 MajdataViewX。

源码运行可以使用：

```powershell
cd maimai-rhythm-analysis
python -m pip install -r requirements.txt
python -m mra.web_app
```

Web 应用仅监听本机 `127.0.0.1`。命令行也可直接运行：

```powershell
python -m mra.run_all -d "QZKago Requiem"
```

## 歌曲目录

```
songs/
└── 某首歌/
    ├── maidata.txt       # 谱面
    ├── maidata_sweep.txt # 旧版聚合扫键文件（可选，仅作迁移来源）
    ├── track.mp3         # 音频
    ├── bg.png            # 背景图
    ├── pv.mp4            # PV 视频（可选）
    └── outputs/
        └── <难度>/
            ├── meter/meter.json
            └── sweep/maidata_sweep.txt # 该难度独立的扫键头标记谱面
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

`-f` 强制重建可视化，但不会覆盖已有的
`outputs/<难度>/sweep/maidata_sweep.txt`、`meter.json` 或谱面预览视频。
无论是否传入 `-f`，每次一键完整处理都会重新计算并覆盖该难度的
`offset.txt` 音频对齐结果。

### 硬件编码

程序会实际试编码，并依次选择：

1. NVIDIA NVENC
2. Intel Quick Sync
3. AMD AMF
4. CPU `libx264`

失败时自动回退到 CPU `libx264`。可在 Web 设置页或 `config.json` 中指定：

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

画质支持 `balanced`、`high`、`maximum`；修改后需重启 Web 应用。

### 分步执行

```powershell
python -m mra.init_meter -d "曲名" -diff 5    # 生成默认 4/4 拍号文件
python -m mra.visualize   -d "曲名" -diff 5    # 只生成节奏图
python -m mra.render_preview -d "曲名" -diff 5 -f  # 只录预览视频
python -m mra.align_audio -d "曲名" -diff 5    # 只算音频偏移
python -m mra.make_html   -d "曲名" -diff 5 -f # 只生成分析页面
```

### 变拍号与人工编辑

首次运行会创建 `outputs/<难度>/meter/meter.json`，默认拍号为 `4/4`。
按实际变拍位置添加节点：

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

仅分母为 `4` 的拍号绘制四分拍弱线；其他拍号只绘制强小节线。修改后运行
`python -m mra.visualize -d "曲名" -diff 5 -f`。

### 扫键头人工修正

每个难度都使用独立的
`outputs/<难度>/sweep/maidata_sweep.txt`；文件中只保留该难度的扫键标记
谱面。首次处理该难度时会创建它。音符组末尾有 `/S` 即标记为
扫键头；漏判时加入 `/S`，误判时删除 `/S`。

歌曲根目录中旧版的 `maidata_sweep.txt` 仅作为只读迁移来源。当对应难度的
新文件尚不存在时，程序可从中提取该难度内容；不会自动覆盖或删除旧文件。
Web 编辑器会按每个难度自己的 `meter.json` 重排扫键谱面，保证每个编号正文行
对应一个真实小节。前置空拍行不编号，包含第一个实际音符的行记为第 1 小节，
之后连续编号；末尾到 `E` 的不完整小节仍单独占一行。若小节线落在原逗号时值
内部，程序会插入等时值空分割，保持音符、BPM 与谱面结束时间不变。小节号只
用于定位，不会写入谱面。
机器识别会排除固定轴键交替出现的轴交互，例如 `6,5,6,7,6`。
单手连续扫中途夹入另一只手的单次按键时，整段只标记最开始的扫键头。
双手扫的起头同拍多出一个附加键时，仍标记包含真正起头的整个音符组。
自动识别的密度门槛为连续三个外键且达到 16 分音符或更密。

例如：

```text
{32}5/7h[1:0]/S,8,1,2,3,4,
```

`-f` 不会覆盖人工文件。不要修改谱面时间结构；删除某难度的新文件后重新运行，
若歌曲根目录仍有旧聚合文件，程序会再次从旧文件迁移该难度。若要按当前源谱和
机器识别结果重新初始化，请先把旧聚合文件移出歌曲目录，再删除该难度的新文件。
修改标记后执行：

```powershell
python -m mra.visualize -d "曲名" -diff 5 -f
```

## 播放性能

- 谱面预览视频使用 HTTP Range 按需流式读取，只预载元数据，不再整段复制到内存。
- 8 拍 SVG 分段全部解码后才允许播放；相邻分段绘制 2px 相同的重叠内容，
  消除拼接处的亮缝。
- 远离视口的分段按需渲染（`content-visibility: auto`），整条滚动条可达数万像素宽，
  光栅与 GPU 纹理压力仍限制在视口附近。
- 滚动条优先由浏览器合成线程的 Web Animation 连续驱动；不支持时回退到
  `requestAnimationFrame`。
- 播放途中不做位置校正；视频缓冲或解码停顿不会拉停滚动条。

若预览视频确实发生缓冲或解码停顿，节奏条仍会继续运行，因此可能产生少量不同步。

不要直接双击 `analysis.html`。生成器会在同目录创建 `打开分析页面.cmd`；
请用该入口启动支持 HTTP Range 的本地服务，否则视频拖动和流式读取可能失效。
服务闲置 30 分钟后自动退出。

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
└── strip/segments/*.svg    # 网页预热并连续滚动的 8 拍分段
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

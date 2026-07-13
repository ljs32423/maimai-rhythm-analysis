# Maimai Rhythm Analysis

把 maimai 的 `maidata.txt` 谱面转成可视化的节奏分析结果：节奏图、预览视频、可交互的前端分析页面，支持自动拍号分析与音频对齐。

## 快速开始

```powershell
pip install -r requirements.txt
python -m mra.run_all -d "QZKago Requiem"
```

拍号分析使用 BeatNet+ 的通用 CRNN 模型。首次使用先安装依赖并下载固定版本的
源码和预训练权重：

```powershell
powershell -ExecutionPolicy Bypass -File tools/setup_beatnet_plus.ps1
```

分析只读取 `track.mp3`，不使用谱面音符或谱面重音。音频结果缓存到歌曲目录的
`outputs/_shared/meter/beatnet-plus.json`，所有难度共用，不会重复分析同一音频。

完成后在 `songs/QZKago Requiem/outputs/MASTER/html/analysis.html` 打开分析页面，即可查看节奏解析。

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
python -m mra.analyze_meter -d "曲名" -diff 5    # 先分析音频中的拍号变化
python -m mra.visualize   -d "曲名" -diff 5    # 只生成节奏图
python -m mra.render_preview -d "曲名" -diff 5 -f  # 只录预览视频
python -m mra.align_audio -d "曲名" -diff 5    # 只算音频偏移
python -m mra.make_html   -d "曲名" -diff 5 -f # 只生成分析页面
```

### 变拍号与人工校正

Simai 格式没有拍号字段。程序用 BeatNet+ 从 `track.mp3` 提取 beat/downbeat
激活值，再用扩展到 3、4、5、7 拍的 DBN 追踪拍号变化。谱面的 BPM
时间轴只用于把检测秒数换算成图片坐标，音符与谱面重音不参与检测。

`outputs/<难度>/meter/meter.json` 只保存首拍号和真正变化的位置，不再保存每个
小节。渲染时才在内存中展开小节线，因此这份文件也可以直接人工修改：

需要校正时，在歌曲目录新建 `meter.json`。它的优先级高于自动结果：

```json
{
  "default": "4/4",
  "sections": [
    {"start_beat": 0, "signature": "4/4"},
    {"start_beat": 64, "signature": "7/8"},
    {"start_beat": 67.5, "signature": "3/4"},
    {"start_beat": 79.5, "signature": "4/4"}
  ]
}
```

`start_beat` 使用四分音符为 1 拍，所以一个 7/8 小节长度是 3.5。不同难度需要
不同配置时，可使用 `"difficulties": {"5": {...}, "6": {...}}`。修改后运行
`python -m mra.visualize -d "曲名" -diff 5 -f` 即可重新生成。

## 输出结构

```
歌曲文件夹/outputs/MASTER/
├── html/analysis.html      # 分析页面
├── video/preview.mp4       # 预览视频
├── sync/offset.txt         # 音频对齐偏移
├── meter/meter.json        # 仅含拍号变化节点、来源与置信度
├── rhythm/rhythm.{png,svg} # 节奏图
└── strip/strip.svg         # 滚动条素材
```

Re:MASTER 对应 `outputs/ReMASTER/`。

## 测试

```powershell
python -m pytest -q
```

# Maimai Rhythm Analysis

把 maimai 的 `maidata.txt` 谱面转成可视化的节奏分析结果：节奏图、预览视频、可交互的前端分析页面，支持人工变拍号时间轴与音频对齐。

## 快速开始

```powershell
pip install -r requirements.txt
python -m mra.run_all -d "QZKago Requiem"
```

项目不再使用模型自动识别拍号。首次处理某个难度时会生成一份默认 `4/4` 的
`meter.json`，之后可以人工加入变拍节点。

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

Simai 格式没有拍号字段，程序也不再通过模型猜测拍号。首次运行时会在
`outputs/<难度>/meter/meter.json` 初始化一个 `4/4` 节点；文件存在后，普通运行和
`-f` 都只读取它，绝不重新生成或修改。

`outputs/<难度>/meter/meter.json` 只保存首拍号和真正变化的位置，不再保存每个
小节。渲染时才在内存中展开小节线，因此这份文件也可以直接人工修改：

直接编辑这个文件，在实际发生拍号变化的位置添加节点：

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
不同难度分别编辑各自输出目录中的文件。修改拍号后运行
`python -m mra.visualize -d "曲名" -diff 5 -f` 即可重新生成图片。

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

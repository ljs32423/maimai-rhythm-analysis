# maimai Rhythm Analysis

把 maimai `maidata.txt` 谱面转换成可检查的节奏分析产物。项目会生成节奏图、谱面预览视频、音频对齐结果，以及带视频同步滚动条的 `analysis.html` 页面。

## 功能概览

- 解析 Simai / `maidata.txt` 谱面，支持 TAP、HOLD、SLIDE、TOUCH、TOUCH HOLD、烟花触摸与变 BPM。
- 为每个难度生成节奏图、滚动条素材、预览视频和网页版分析页面。
- 自动录制 MajdataView 谱面预览视频，并对齐原曲音频与谱面滚动条。
- 默认处理 `MASTER` / `Re:MASTER`；如果曲目没有这两个难度，则回退到实际存在的难度。

## 环境要求

- Windows
- Python 3.10+
- FFmpeg，需要在 `PATH` 中可用；发行包会复用 MajdataView 自带的 FFmpeg。

安装 Python 依赖：

```powershell
pip install -r requirements.txt
```

## 准备歌曲

每首歌放在 `songs/` 下的独立目录中：

```text
songs/
└── 曲名/
    ├── maidata.txt    # 必需：Simai 谱面
    ├── track.mp3      # 推荐：原曲音频，自动对齐需要
    ├── bg.png         # 可选：背景图
    └── pv.mp4         # 可选：PV 视频
```

项目会按 `maidata.txt` 中的 `&title` 识别曲名，并兼容旧目录名、当前曲名和绝对路径。含 touch / touch hold / 烟花触摸的曲目会自动标记为 `曲名 [DX]`。

## 快速开始

处理单曲默认难度：

```powershell
python -m mra.run_all -d "QZKago Requiem"
```

批量处理 `songs/` 下所有曲目：

```powershell
python -m mra.run_all
```

完成后打开：

```text
songs/曲名/outputs/MASTER/html/analysis.html
```

`Re:MASTER` 的结果位于 `outputs/ReMASTER/`。

## 常用命令

### 一键流程

`run_all` 会按顺序执行：节奏图生成、预览视频录制、音频自动对齐、HTML 页面生成。单个步骤失败不会阻断后续步骤。

```powershell
python -m mra.run_all                        # 批量处理全库
python -m mra.run_all -d "曲名"              # 处理单曲
python -m mra.run_all -d "曲名" -diff 5      # 只处理 MASTER
python -m mra.run_all -d "曲名" -diff 6 -f   # 只处理 Re:MASTER 并强制覆盖
```

难度编号：

| 编号 | 难度 |
| --- | --- |
| `1` | EASY |
| `2` | BASIC |
| `3` | ADVANCED |
| `4` | EXPERT |
| `5` | MASTER |
| `6` | Re:MASTER |
| `7` | UTOPIA |

### 分步执行

```powershell
python -m mra.visualize      -d "曲名" -diff 5    # 生成节奏图和滚动条素材
python -m mra.render_preview -d "曲名" -diff 5 -f # 录制谱面预览视频
python -m mra.align_audio    -d "曲名" -diff 5    # 计算音频对齐偏移
python -m mra.make_html      -d "曲名" -diff 5 -f # 生成分析页面
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-d`, `--dir` | 指定曲名或歌曲目录 |
| `-i`, `--input` | 指定歌曲根目录，默认使用项目根目录 |
| `-diff`, `--difficulty` | 指定难度编号 |
| `-f`, `--force` | 强制重新生成已有产物 |
| `-offset` | 生成 HTML 时附加初始延迟，单位秒 |

## 输出结构

每个难度的产物会写入 `歌曲目录/outputs/<难度>/`：

```text
outputs/MASTER/
├── html/analysis.html      # 可交互分析页面
├── video/preview.mp4       # 谱面预览视频
├── sync/offset.txt         # 音频对齐偏移
├── rhythm/rhythm.png       # 节奏图 PNG
├── rhythm/rhythm.svg       # 节奏图 SVG
└── strip/strip.svg         # HTML 滚动条素材
```

## 分析页面

`analysis.html` 用于检查谱面与音频/视频的同步关系，支持：

- 视频播放和暂停
- 拖拽进度条
- 方向键前后跳转 1 秒
- 0.01 精度倍速调整
- 变 BPM 下的滚动条同步
- 首音对齐、BPM 变化段、交互段、转圈段、尾杀段检查

## 发行包

生成独立发行目录：

```powershell
tools/build_release.ps1
```

构建完成后使用 `release/` 目录。发行包不要求用户安装 Python，双击 `run_all.bat` 即可运行。

## 测试

```powershell
python -m pytest -q
```

## 排障提示

- 找不到歌曲：确认目录位于 `songs/` 下，或使用 `-i` 指定歌曲根目录。
- 没有生成视频：确认 FFmpeg 可用，并检查 MajdataView / `.tools/` 相关输出。
- 自动对齐失败：确认歌曲目录中存在 `track.mp3`，且预览视频中包含有效音轨。
- Re:MASTER 文件名：磁盘目录使用 `ReMASTER`，页面显示仍为 `Re:MASTER`。

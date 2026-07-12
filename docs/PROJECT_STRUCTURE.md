# 项目结构

当前目录按下面几类理解：

## 根目录

- `README.md`
- `requirements.txt`
- `songs/`
- `tests/`
- `docs/`
- `tools/`
- `mra/`

## mra/

实际实现代码都在 `mra/`：

- `mra/run_all.py`
- `mra/visualize.py`
- `mra/render_preview.py`
- `mra/align_audio.py`
- `mra/make_html.py`
- `mra/simai_parser.py`
- `mra/difficulty.py`
- `mra/song_library.py`

现在统一使用模块方式运行：

```powershell
python -m mra.run_all
python -m mra.visualize -d "Song Name"
python -m mra.render_preview -d "Song Name"
python -m mra.align_audio -d "Song Name"
python -m mra.make_html -d "Song Name"
```

## docs/

- `PROJECT_STRUCTURE.md`：当前这份结构说明
- `项目记忆.md`：项目说明/历史记录类文档

## songs/

每首歌一个目录，目录内同时放：

- 原始输入：`maidata.txt`、`track.mp3`、`pv.mp4`、`bg.png`
- 生成结果：`*_rhythm.png`、`*_rhythm.svg`、`*_strip.svg`、`*_preview.mp4`、`*_offset.txt`、`*_analysis.html`

## tests/

Python 自动化测试。

## tools/

- `src/majdata_bridge/`：MajdataBridge 的 C# 源码

## .tools/

运行时自动下载或编译出来的外部工具，不作为源码结构的一部分。

## 清理原则

Python 实现统一放在 `mra/`，根目录不再保留同名入口脚本：

- 运行时统一使用 `python -m mra.xxx`
- 测试直接针对 `mra/` 里的实现模块
- 根目录更干净

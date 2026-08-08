# Maimai Rhythm Analysis

用于将 maimai 谱面生成节奏图、预览视频和可交互分析页面。

## 下载与启动

1. 在 [Releases](https://github.com/ljs32423/maimai-rhythm-analysis/releases) 下载最新版 `windows-x64-full.zip`。
2. 解压后打开 `app` 文件夹。
3. 将歌曲文件夹放入 `songs`。
4. 双击 `启动节奏分析.cmd`，程序会自动打开浏览器。

完整包已包含所需运行环境，无需另外安装 Python 或 FFmpeg。关闭命令行窗口即可停止程序。

## 歌曲文件

每首歌使用一个独立文件夹，至少需要 `maidata.txt` 和 `track.mp3`：

```text
songs/
└── 歌曲名/
    ├── maidata.txt
    ├── track.mp3
    ├── bg.png       # 可选
    └── pv.mp4       # 可选
```

## 使用方法

1. 在歌曲库中选择歌曲和难度。
2. 点击“开始完整处理”。
3. 处理完成后点击“打开分析页面”。

生成结果保存在对应歌曲的 `outputs` 文件夹中。

如需调整拍号或扫键标记，可在页面中的编辑框修改并保存，然后重新处理。勾选“强制重建可视化”可重新生成已有结果。

> 本程序仅在本机运行，不会上传歌曲或谱面文件。

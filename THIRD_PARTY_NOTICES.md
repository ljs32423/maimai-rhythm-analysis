# Third-party software

Windows x64 完整发行包携带以下第三方程序：

- [Python 3.12.10 Embedded Distribution](https://www.python.org/downloads/release/python-31210/)，
  Python Software Foundation License；许可证随运行时保存为 `LICENSE.txt`。
- [MajdataViewX v6.0.0](https://github.com/re-poem/MajdataViewX/releases/tag/v6.0.0)，GNU GPL v3.0。
- [ffprobe-static b6.1.1](https://github.com/eugeneware/ffmpeg-static/releases/tag/b6.1.1)，GNU GPL v3.0；完整许可证位于 `required-programs/.tools/ffprobe/6.1.1/LICENSE`。
- MajdataBridge 使用 [.NET 8](https://github.com/dotnet/runtime) 自包含运行时，并在运行时调用 MajdataViewX 随附的 `MajdataEdit.dll`。桥接程序源码位于本仓库的 `tools/src/majdata_bridge/`。

完整发行包还携带 `requirements-runtime.txt` 中列出的 Python 包。各包的许可证和
元数据保存在运行时的 `Lib/site-packages/*.dist-info/` 目录。

这些组件的著作权和商标归各自作者所有。对应源代码、许可证及构建信息请查看上面的项目链接。

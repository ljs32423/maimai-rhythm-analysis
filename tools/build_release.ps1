param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$project = (Resolve-Path $ProjectRoot).Path
$release = Join-Path $project "release"
$app = Join-Path $release "app"
$required = Join-Path $release "required-programs"

if (Test-Path $release) {
    Remove-Item $release -Recurse -Force
}

New-Item -ItemType Directory -Path `
    $app, `
    $required, `
    (Join-Path $app "songs"), `
    (Join-Path $app "mra"), `
    (Join-Path $app "docs"), `
    (Join-Path $app "tools\src\majdata_bridge"), `
    (Join-Path $required ".tools") | Out-Null

Copy-Item (Join-Path $project "README.md"), (Join-Path $project "requirements.txt") -Destination $app
Copy-Item (Join-Path $project "mra\*") -Destination (Join-Path $app "mra") -Recurse
Copy-Item (Join-Path $project "docs\*") -Destination (Join-Path $app "docs") -Recurse
Copy-Item `
    (Join-Path $project "tools\src\majdata_bridge\MajdataBridge.csproj"), `
    (Join-Path $project "tools\src\majdata_bridge\Program.cs") `
    -Destination (Join-Path $app "tools\src\majdata_bridge")

Copy-Item (Join-Path $project ".tools\majdata") -Destination (Join-Path $required ".tools") -Recurse
Copy-Item (Join-Path $project ".tools\majdata_bridge") -Destination (Join-Path $required ".tools") -Recurse

$runAllBat = @'
@echo off
setlocal

set "APP_DIR=%~dp0"
set "RUNTIME_DIR=%APP_DIR%..\required-programs"
set "RUNTIME_TOOLS=%RUNTIME_DIR%\.tools"
set "MAJDATA_DIR=%RUNTIME_TOOLS%\majdata\4.3.1\Majdata"
set "FFMPEG_BIN=%MAJDATA_DIR%"

if exist "%MAJDATA_DIR%\MajdataView.exe" (
    set "MAJDATA_HOME=%MAJDATA_DIR%"
)

if exist "%FFMPEG_BIN%\ffmpeg.exe" (
    set "PATH=%FFMPEG_BIN%;%PATH%"
)

python -m mra.run_all %*
exit /b %errorlevel%
'@

$songsNote = @'
把每首歌放在 `songs` 目录下的独立文件夹中。

每首歌至少需要：

- `maidata.txt`
- `track.mp3`

可选：

- `pv.mp4`

示例：

```text
songs/
└── QZKago Requiem/
    ├── maidata.txt
    ├── track.mp3
    └── pv.mp4
```
'@

$requiredReadme = @'
这个目录放的是运行项目时要用到的外部程序。

内容说明：

- `.tools\majdata\`：MajdataView 本体
- `.tools\majdata_bridge\`：谱面桥接程序

推荐用法：

1. 保持 `app` 和 `required-programs` 同级放置
2. 直接运行 `app\run_all.bat`

当前瘦身版不再额外附带独立 FFmpeg：

- `run_all.bat` 会直接把 Majdata 目录加入 `PATH`
- 项目会复用 `MajdataView` 自带的 `ffmpeg.exe` 和 `ffprobe.exe`
'@

Set-Content -Path (Join-Path $app "run_all.bat") -Value $runAllBat -Encoding ASCII
Set-Content -Path (Join-Path $app "songs\把歌曲放到这里.txt") -Value $songsNote -Encoding UTF8
Set-Content -Path (Join-Path $required "README.txt") -Value $requiredReadme -Encoding UTF8

Write-Host "Release rebuilt at: $release"

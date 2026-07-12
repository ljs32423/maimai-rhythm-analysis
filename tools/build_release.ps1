param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$project = (Resolve-Path $ProjectRoot).Path
$release = Join-Path $project "release"
$app = Join-Path $release "app"
$required = Join-Path $release "required-programs"

# 只清理 app 和 required-programs，保留 runtime/
if (Test-Path $app) { Remove-Item $app -Recurse -Force }
if (Test-Path $required) { Remove-Item $required -Recurse -Force }

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

# 复制 ffmpeg 到 required-programs (如果需要独立分发)
$ffmpegSrc = Join-Path $project ".tools\majdata\4.3.1\Majdata\MajdataView_Data\StreamingAssets"
if (Test-Path (Join-Path $ffmpegSrc "ffmpeg.exe")) {
    Copy-Item (Join-Path $ffmpegSrc "ffmpeg.exe") (Join-Path $required ".tools\ffmpeg") -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $ffmpegSrc "ffprobe.exe") (Join-Path $required ".tools\ffmpeg") -ErrorAction SilentlyContinue
}

# run_all.bat — 使用便携 Python (runtime/python/python.exe)
$runAllBat = @'
@echo off
setlocal

set "APP_DIR=%~dp0"
set "ROOT=%APP_DIR%.."
set "PYTHON=%ROOT%\runtime\python\python.exe"
set "MAJDATA=%ROOT%\required-programs\.tools\majdata\4.3.1\Majdata"

if exist "%MAJDATA%\ffmpeg.exe" set "PATH=%MAJDATA%;%PATH%"
if exist "%MAJDATA%\MajdataView.exe" set "MAJDATA_HOME=%MAJDATA%"

"%PYTHON%" -m mra.run_all %*
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

# 开始使用.bat
$startBat = @'
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo Maimai Rhythm Analysis
echo.
echo 1. 把歌曲文件夹放到当前目录的 songs 文件夹里。
echo    每首歌至少需要 maidata.txt、track.mp3。
echo.
echo 2. 如果要处理全部歌曲，直接按回车。
echo    如果要处理单曲，可以关闭本窗口后运行：
echo    run_all.bat -d "歌曲名"
echo.
pause
call "%~dp0run_all.bat" %*
pause
'@
Set-Content -Path (Join-Path $app "开始使用.bat") -Value $startBat -Encoding ASCII

Write-Host "Release rebuilt at: $release"

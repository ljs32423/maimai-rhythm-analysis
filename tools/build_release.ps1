[CmdletBinding()]
param(
    [string]$Version = "v0.2.0",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"

if ($Version -notmatch '^v\d+\.\d+\.\d+$') {
    throw "Version 必须使用 v主版本.次版本.修订号 格式，例如 v0.2.0"
}

$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $project "release"
}
$releaseRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$packageName = "maimai-rhythm-analysis-$Version-windows-x64-full"
$stageRoot = Join-Path $releaseRoot ".staging"
$packageRoot = Join-Path $stageRoot $packageName
$appRoot = Join-Path $packageRoot "app"
$runtimeRoot = Join-Path $packageRoot "required-programs\.tools"
$archive = Join-Path $releaseRoot "$packageName.zip"
$checksum = "$archive.sha256"

function Copy-DirectoryContent([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "缺少发行内容: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Destination -Recurse -Force
}

function Copy-Runtime([string]$RelativePath) {
    $source = Join-Path (Join-Path $project ".tools") $RelativePath
    $destination = Join-Path $runtimeRoot $RelativePath
    Copy-DirectoryContent $source $destination
}

New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
if (Test-Path -LiteralPath $stageRoot) {
    $resolvedStage = [System.IO.Path]::GetFullPath($stageRoot)
    if (-not $resolvedStage.StartsWith($releaseRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理发行目录以外的路径: $resolvedStage"
    }
    Remove-Item -LiteralPath $resolvedStage -Recurse -Force
}
Remove-Item -LiteralPath $archive, $checksum -Force -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Path $appRoot, $runtimeRoot, (Join-Path $appRoot "songs") -Force | Out-Null

Copy-DirectoryContent (Join-Path $project "mra") (Join-Path $appRoot "mra")
Copy-DirectoryContent (Join-Path $project "docs") (Join-Path $appRoot "docs")

$rootFiles = @(
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "requirements.txt",
    "align_audio.py",
    "init_meter.py",
    "make_html.py",
    "render_preview.py",
    "run_all.py",
    "visualize.py"
)
foreach ($file in $rootFiles) {
    Copy-Item -LiteralPath (Join-Path $project $file) -Destination $appRoot -Force
}

# 只携带当前运行链路需要的工具，不收录下载缓存、旧 Majdata 或 BeatNet。
Copy-Runtime "majdataviewx\6.0.0"
Copy-Runtime "ffprobe\6.1.1"
Copy-Runtime "majdata_bridge"

$requiredFiles = @(
    (Join-Path $runtimeRoot "majdataviewx\6.0.0\MajdataView.exe"),
    (Join-Path $runtimeRoot "ffprobe\6.1.1\ffprobe.exe"),
    (Join-Path $runtimeRoot "majdata_bridge\MajdataBridge.exe")
)
foreach ($file in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "发行包缺少必要可执行文件: $file"
    }
}

$runAll = @'
@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer first.
    pause
    exit /b 1
)
python -m mra.run_all %*
exit /b %errorlevel%
'@

$songsReadme = @'
把每首歌放在这个目录下的独立文件夹中。

每首歌至少需要 maidata.txt 和 track.mp3，bg.png、pv.mp4 为可选文件。
'@

Set-Content -LiteralPath (Join-Path $appRoot "run_all.bat") -Value $runAll -Encoding ASCII
Set-Content -LiteralPath (Join-Path $appRoot "songs\把歌曲放到这里.txt") -Value $songsReadme -Encoding UTF8
Set-Content -LiteralPath (Join-Path $packageRoot "VERSION") -Value $Version -Encoding ASCII

$tar = Get-Command tar.exe -ErrorAction Stop
& $tar.Source -a -c -f $archive -C $stageRoot $packageName
if ($LASTEXITCODE -ne 0) {
    throw "创建 ZIP 失败，退出码: $LASTEXITCODE"
}

$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $checksum -Value "$hash *$([System.IO.Path]::GetFileName($archive))" -Encoding ASCII
Remove-Item -LiteralPath $stageRoot -Recurse -Force

$size = [math]::Round((Get-Item -LiteralPath $archive).Length / 1MB, 2)
Write-Host "Release package: $archive"
Write-Host "Size: $size MB"
Write-Host "SHA-256: $hash"

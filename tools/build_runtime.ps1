[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12.10",
    [string]$PythonSha256 = "4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3",
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$toolsRoot = [System.IO.Path]::GetFullPath((Join-Path $project ".tools"))
$pythonRoot = [System.IO.Path]::GetFullPath((Join-Path $toolsRoot "python"))
if (-not $Destination) {
    $Destination = Join-Path $pythonRoot $PythonVersion
}
$runtimeRoot = [System.IO.Path]::GetFullPath($Destination)
if (-not $runtimeRoot.StartsWith(
        $pythonRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "运行时目录必须位于 $pythonRoot 内: $runtimeRoot"
}

$requirements = Join-Path $project "requirements-runtime.txt"
if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
    throw "缺少固定版本依赖文件: $requirements"
}

$buildPython = (Get-Command python -ErrorAction Stop).Source
$buildVersion = & $buildPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$expectedSeries = ($PythonVersion -split '\.')[0..1] -join '.'
if ($buildVersion.Trim() -ne $expectedSeries) {
    throw "构建机 Python 必须是 $expectedSeries，当前为 $buildVersion"
}

$cacheRoot = Join-Path $toolsRoot "download-cache"
New-Item -ItemType Directory -Path $cacheRoot, $pythonRoot -Force | Out-Null
$archiveName = "python-$PythonVersion-embed-amd64.zip"
$archive = Join-Path $cacheRoot $archiveName
$download = "$archive.download"
$downloadUrl = "https://www.python.org/ftp/python/$PythonVersion/$archiveName"

if (Test-Path -LiteralPath $archive -PathType Leaf) {
    $cachedHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($cachedHash -ne $PythonSha256) {
        Write-Host "Discarding incomplete or invalid cached archive."
        Remove-Item -LiteralPath $archive -Force
    }
}
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    Write-Host "Downloading $downloadUrl"
    Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
    Invoke-WebRequest -Uri $downloadUrl -OutFile $download
    $downloadHash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
    if ($downloadHash -ne $PythonSha256) {
        Remove-Item -LiteralPath $download -Force
        throw "下载的 Python 嵌入包 SHA-256 不匹配。预期 $PythonSha256，实际 $downloadHash"
    }
    Move-Item -LiteralPath $download -Destination $archive -Force
}
$actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
if ($actualHash -ne $PythonSha256) {
    throw "Python 嵌入包 SHA-256 不匹配。预期 $PythonSha256，实际 $actualHash"
}

if (Test-Path -LiteralPath $runtimeRoot) {
    $resolvedRuntime = [System.IO.Path]::GetFullPath($runtimeRoot)
    if (-not $resolvedRuntime.StartsWith(
            $pythonRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理 Python 运行时目录以外的路径: $resolvedRuntime"
    }
    Remove-Item -LiteralPath $resolvedRuntime -Recurse -Force
}
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot -Force

$pth = Get-ChildItem -LiteralPath $runtimeRoot -Filter "python*._pth" |
    Select-Object -First 1
if (-not $pth) {
    throw "嵌入式 Python 中没有找到 ._pth 文件"
}
$pthLines = Get-Content -LiteralPath $pth.FullName |
    Where-Object { $_ -notin @("#import site", "# import site", "import site", ".", "Lib", "Lib\site-packages") }
$pthLines += "."
$pthLines += "Lib"
$pthLines += "Lib\site-packages"
$pthLines += "import site"
Set-Content -LiteralPath $pth.FullName -Value $pthLines -Encoding ASCII

$sitePackages = Join-Path $runtimeRoot "Lib\site-packages"
New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null
& $buildPython -m pip install `
    --disable-pip-version-check `
    --only-binary=:all: `
    --upgrade `
    --target $sitePackages `
    -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "安装嵌入式运行时依赖失败，退出码 $LASTEXITCODE"
}

$siteCustomizeSource = Join-Path $project "tools\runtime_sitecustomize.py"
Copy-Item -LiteralPath $siteCustomizeSource `
    -Destination (Join-Path $sitePackages "sitecustomize.py") -Force

$runtimePython = Join-Path $runtimeRoot "python.exe"
& $runtimePython -c "import fastapi, matplotlib, numpy, scipy, uvicorn; print('runtime imports ok')"
if ($LASTEXITCODE -ne 0) {
    throw "嵌入式 Python 导入验证失败，退出码 $LASTEXITCODE"
}

Write-Host "Embedded Python runtime: $runtimeRoot"

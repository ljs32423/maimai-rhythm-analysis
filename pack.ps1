# Build a clean GitHub Release zip outside this repository.
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\pack.ps1
#   powershell -ExecutionPolicy Bypass -File .\pack.ps1 -OutDir "$env:USERPROFILE\Desktop"
param(
    [string]$OutDir = "$env:USERPROFILE\Desktop",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path $PSScriptRoot).Path
$release = Join-Path $root "release"
$out = [System.IO.Path]::GetFullPath($OutDir)
$name = "maimai-analysis"
$zip = Join-Path $out "$name.zip"
$stagingParent = Join-Path $env:TEMP "maimai-analysis-release"
$staging = Join-Path $stagingParent $name

if ($out.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write zip inside the project directory: $out"
}

if (-not $SkipBuild) {
    & (Join-Path $root "tools\build_release.ps1") -ProjectRoot $root
}

foreach ($requiredPath in @(
    (Join-Path $release "app"),
    (Join-Path $release "required-programs"),
    (Join-Path $release "runtime\python\python.exe")
)) {
    if (-not (Test-Path $requiredPath)) {
        throw "Release is incomplete. Missing: $requiredPath"
    }
}

if (Test-Path $stagingParent) {
    Remove-Item $stagingParent -Recurse -Force
}
New-Item -ItemType Directory -Path $staging | Out-Null
New-Item -ItemType Directory -Path $out -Force | Out-Null

Copy-Item (Join-Path $release "app") -Destination (Join-Path $staging "app") -Recurse
Copy-Item (Join-Path $release "required-programs") -Destination (Join-Path $staging "required-programs") -Recurse
Copy-Item (Join-Path $release "runtime") -Destination (Join-Path $staging "runtime") -Recurse

Get-ChildItem $staging -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem $staging -Recurse -File -Include "*.pyc", "*.pyo", "*.npy", "*.npz" | Remove-Item -Force
Get-ChildItem $staging -Recurse -Directory -Filter "Majdata.backup-*" | Remove-Item -Recurse -Force

if (Test-Path $zip) {
    Remove-Item $zip -Force
}

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if ($tar) {
    & $tar.Source -a -cf $zip -C $stagingParent $name
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed with exit code $LASTEXITCODE"
    }
} else {
    Compress-Archive -Path $staging -DestinationPath $zip -Force
}

Remove-Item $stagingParent -Recurse -Force

$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "Done: $zip ($size MB)"
Write-Host "Upload this zip manually to GitHub Releases."

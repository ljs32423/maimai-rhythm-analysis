$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Target = Join-Path $ProjectRoot ".tools\BeatNet-Plus"
$Revision = "bb90eb0a9065b101a4b4c4cb2b2061950266cb4b"

python -m pip install -r (Join-Path $ProjectRoot "requirements-beatnet-plus.txt")

if (-not (Test-Path $Target)) {
    git clone https://github.com/mjhydri/BeatNet-Plus.git $Target
}

git -C $Target fetch origin $Revision --depth 1
git -C $Target checkout --detach $Revision
Write-Host "BeatNet+ ready: $Target ($Revision)"

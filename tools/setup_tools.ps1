[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Push-Location $ProjectRoot
try {
    python -m mra.render_preview --install-only
    if ($LASTEXITCODE -ne 0) {
        throw "工具安装失败，退出码: $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

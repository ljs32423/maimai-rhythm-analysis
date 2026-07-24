param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot
)

$ErrorActionPreference = "Stop"
$package = (Resolve-Path -LiteralPath $PackageRoot).Path
$appRoot = Join-Path $package "app"
$python = Join-Path $package "required-programs\.tools\python\3.12.10\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Embedded Python not found: $python"
}
if (-not (Test-Path -LiteralPath (Join-Path $appRoot "mra\web_app.py") -PathType Leaf)) {
    throw "Packaged web application not found under: $appRoot"
}

$previousAppRoot = $env:MRA_APP_ROOT
try {
    $env:MRA_APP_ROOT = $appRoot
    & $python -c "import fastapi, uvicorn, numpy, scipy, matplotlib, PIL; from mra.web_app import create_app; app=create_app(songs_root=r'$appRoot\songs'); assert app.title == 'Maimai Rhythm Analysis'; print('release smoke test: ok')"
    if ($LASTEXITCODE -ne 0) {
        throw "Embedded Python import smoke test failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:MRA_APP_ROOT = $previousAppRoot
}

# 打包为 GitHub Release zip
# 运行: powershell -ExecutionPolicy Bypass -File pack.ps1
param($OutDir = "$env:USERPROFILE\Desktop")

$root = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$name = "maimai-analysis"
$zip = Join-Path $OutDir "$name.zip"
$tmp = Join-Path $env:TEMP $name

if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
if (Test-Path $zip) { Remove-Item $zip -Force }

# 复制需要的文件
New-Item -ItemType Directory -Path "$tmp\songs" | Out-Null
Copy-Item "$root\mra"             "$tmp\mra"            -Recurse
Copy-Item "$root\tests"           "$tmp\tests"          -Recurse
Copy-Item "$root\tools"           "$tmp\tools"          -Recurse
Copy-Item "$root\.tools"          "$tmp\.tools"         -Recurse
Copy-Item "$root\run_all.bat"     "$tmp\run_all.bat"
Copy-Item "$root\README.md"       "$tmp\README.md"
Copy-Item "$root\requirements.txt" "$tmp\requirements.txt"

# 打包
Compress-Archive -Path "$tmp\*" -DestinationPath $zip
Remove-Item $tmp -Recurse -Force

$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "Done: $zip ($size MB)"
Write-Host "Upload this to https://github.com/ljs32423/maimai-rhythm-analysis/releases/new"

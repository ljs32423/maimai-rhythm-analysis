@echo off
setlocal
set "ROOT=%~dp0"
set "MAJDATA=%ROOT%.tools\majdata\4.3.1\Majdata"
if exist "%MAJDATA%\ffmpeg.exe" set "PATH=%MAJDATA%;%PATH%"
python -m mra.run_all %*

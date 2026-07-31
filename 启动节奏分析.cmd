@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ===== 查找 Python =====
set "PYTHON="
py -3 -c "import sys" >nul 2>&1 && set "PYTHON=py -3"
if not defined PYTHON (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON (
    echo [错误] 未找到 Python。请安装 Python 3.12，安装时勾选 "Add python.exe to PATH"。
    echo 之后在命令行运行:  python -m pip install -r requirements.txt
    pause
    exit /b 1
)

REM ===== 检查并安装依赖 =====
%PYTHON% -c "import fastapi, uvicorn, pydantic" >nul 2>&1
if errorlevel 1 (
    echo [提示] 首次运行，正在安装依赖，请稍候...
    %PYTHON% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重新双击本脚本。
        pause
        exit /b 1
    )
)

echo 正在启动 Maimai Rhythm Analysis Web 应用...
echo 应用仅监听本机 127.0.0.1，关闭本窗口即停止服务。
%PYTHON% -m mra.web_app %*
if errorlevel 1 (
    echo.
    echo [错误] Web 应用异常退出，请查看上方错误信息。
    pause
)
exit /b %errorlevel%

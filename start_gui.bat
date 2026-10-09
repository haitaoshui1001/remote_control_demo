@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "PY=D:\anaconda3\envs\ewd611-hmi\python.exe"

if not exist "%PY%" (
    echo [错误] 找不到项目环境 Python： %PY%
    echo 请先创建 conda 环境 ewd611-hmi（见操作部署手册），或把上面的 PY 改成实际路径。
    pause
    exit /b 1
)

set "PYTHONPATH=%ROOT%.deps;%ROOT%src"
"%PY%" "%ROOT%run.py" gui %*
if errorlevel 1 (
    echo.
    echo [退出码 %ERRORLEVEL%] 若窗口没出现，请把上面的报错文字发给开发者。
    pause
)

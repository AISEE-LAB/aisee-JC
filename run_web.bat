@echo off
chcp 65001 >nul
title 中转站倍率监测 - Web 服务
cd /d "%~dp0"

echo ============================================================
echo  中转站倍率监测 Web 服务
echo ============================================================
echo.

REM 优先用虚拟环境，其次系统 python
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
if exist "venv\Scripts\python.exe" set "PYTHON=venv\Scripts\python.exe"

REM 检查依赖是否就绪
%PYTHON% -c "import flask, apscheduler, yaml, requests" 2>nul
if errorlevel 1 (
    echo [!] 检测到依赖未安装，正在安装...
    %PYTHON% -m pip install -r requirements.txt
    echo.
)

echo  访问地址: http://127.0.0.1:5000
echo  按 Ctrl+C 停止服务
echo.

%PYTHON% app.py

pause

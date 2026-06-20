@echo off
REM ========================================================================
REM RemoteDX 一键启动脚本 (Windows)
REM 双击即可启动 Flask 服务并自动打开浏览器。
REM 前提：已经运行过 install.bat 或手动在 .venv 里装过依赖。
REM ========================================================================

setlocal
cd /d "%~dp0"

REM 1. 检查虚拟环境是否存在
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 找不到 .venv 虚拟环境。请先双击运行 install.bat 完成初始化。
    echo 或者执行：python -m venv .venv --system-site-packages ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

REM 2. 启动 Flask 服务（0.0.0.0:5000 监听所有网卡）
start "RemoteDX" /B ".venv\Scripts\python.exe" "app.py" 0.0.0.0 5000

REM 3. 等 2 秒让 Flask 起来再开浏览器
timeout /t 2 /nobreak >nul
start "" "http://localhost:5000"

echo ==============================================
echo  RemoteDX 已启动，浏览器应已打开 http://localhost:5000
echo  关闭此窗口不会停止服务。要停止请在任务管理器里结束 python.exe 进程。
echo ==============================================
pause

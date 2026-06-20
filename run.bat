@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Please run install.bat first.
    pause
    exit /b 1
)
start "RemoteDX" /B ".venv\Scripts\python.exe" "app.py" 0.0.0.0 5000
timeout /t 2 /nobreak >nul
start "" "http://localhost:5000"
echo ==============================================
echo  RemoteDX running at http://localhost:5000
echo  Close this window does NOT stop the server.
echo  To stop: end python.exe in Task Manager.
echo ==============================================
pause
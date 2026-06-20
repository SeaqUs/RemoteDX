@echo off
setlocal
cd /d "%~dp0"
echo ==============================================
echo  RemoteDX install.bat
echo  1/3 check python...
echo ==============================================
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found. Please install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo.
echo ==============================================
echo  2/3 create .venv...
echo ==============================================
if exist ".venv" (
    echo  .venv already exists, skip.
) else (
    python -m venv .venv --system-site-packages
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        pause
        exit /b 1
    )
    echo  .venv ready.
)
echo.
echo ==============================================
echo  3/3 install packages into .venv ...
echo ==============================================
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install Flask uiautomation pyautogui pywin32 qrcode Pillow pyzbar
if errorlevel 1 (
    echo [ERROR] pip install failed, see output above.
    pause
    exit /b 1
)
echo.
echo ==============================================
echo  Done. Now double-click run.bat to start.
echo ==============================================
pause
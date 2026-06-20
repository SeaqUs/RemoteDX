@echo off
REM ========================================================================
REM RemoteDX 一键安装脚本 (Windows)
REM 第一次在你机器上跑这个项目时双击它：
REM   - 创建 .venv 虚拟环境（仅本项目目录内，不污染系统）
REM   - 在 venv 内安装 requirements.txt 里所有依赖
REM 之后只要重启用 run.bat 就行。
REM ========================================================================

setlocal
cd /d "%~dp0"

echo ==============================================
echo  1/3 检查系统 Python...
echo ==============================================
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 没找到 python，请先到 https://www.python.org/downloads/ 安装 Python 3.10+
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo %%v

echo ==============================================
echo  2/3 创建虚拟环境 .venv ...
echo ==============================================
if exist ".venv" (
    echo  .venv 已存在，跳过创建。
) else (
    python -m venv .venv --system-site-packages
    if errorlevel 1 (
        echo [ERROR] 创建虚拟环境失败。
        pause
        exit /b 1
    )
    echo  .venv 创建完成。
)

echo ==============================================
echo  3/3 安装依赖（Flask / uiautomation / pyautogui / qrcode / Pillow / pywin32 / pyzbar）...
echo ==============================================
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install Flask uiautomation pyautogui pywin32 qrcode Pillow pyzbar
if errorlevel 1 (
    echo [ERROR] 依赖安装失败，请检查上面输出。
    pause
    exit /b 1
)

echo.
echo ==============================================
echo  安装完成！下一步：双击 run.bat 启动服务。
echo ==============================================
pause

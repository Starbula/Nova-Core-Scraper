@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "NOVACORE_PYTHON="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "NOVACORE_PYTHON=py -3"
if not defined NOVACORE_PYTHON (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if not errorlevel 1 set "NOVACORE_PYTHON=python"
)
if not defined NOVACORE_PYTHON (
    echo Python 3.11 or newer was not found. Install Python with Tcl/Tk support.
    echo During installation, enable the Python launcher or Add Python to PATH.
    goto :failed
)

if not exist ".venv\Scripts\python.exe" (
    %NOVACORE_PYTHON% -m venv ".venv"
    if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo The existing .venv is unusable or uses an older Python.
    echo Rename the .venv folder and run this script again to create a fresh environment.
    goto :failed
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

".venv\Scripts\python.exe" main.py %*
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo NovaCore could not start. Review the error above.
pause
exit /b 1

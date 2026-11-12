@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto check_dependencies

py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv .venv
    if errorlevel 1 goto venv_error
    goto check_dependencies
)
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 (
    python -m venv .venv
    if errorlevel 1 goto venv_error
    goto check_dependencies
)
python3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 (
    python3 -m venv .venv
    if errorlevel 1 goto venv_error
    goto check_dependencies
)
echo Python 3.10 or later was not found.
echo Install Python 3.11 or 3.12 from https://www.python.org/downloads/windows/
echo Enable the Python launcher and Tcl/Tk options, then run this file again.
goto failure

:check_dependencies
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if errorlevel 1 (
    echo The existing .venv uses an unsupported or missing Python interpreter.
    echo Rename the .venv folder, install Python 3.10+, and run this file again.
    goto failure
)
".venv\Scripts\python.exe" -c "from importlib.metadata import version; assert version('rocketpy') == '1.13.0'; import rocketpy, numpy, matplotlib" >nul 2>&1
if not errorlevel 1 goto check_tk
echo Installing required packages. Internet access is needed for the first setup.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Package installation failed. Check the internet connection and Python version.
    goto failure
)

:check_tk
".venv\Scripts\python.exe" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo Tkinter is missing. Modify your Python installation to include Tcl/Tk.
    goto failure
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto failure
exit /b 0

:venv_error
echo Could not create the .venv environment. Check the Python installation.
:failure
echo.
echo See README.md for manual setup instructions.
pause
exit /b 1

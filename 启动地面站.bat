@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto workspace_failed

REM Resolve one Python 3 interpreter and use it for checks, installs and startup.
set "GS_PYTHON="
call :find_python python
if not defined GS_PYTHON call :find_python python3
if not defined GS_PYTHON call :find_python py -3
if not defined GS_PYTHON goto python_failed

echo [INFO] Python: "%GS_PYTHON%"
"%GS_PYTHON%" --version
"%GS_PYTHON%" -c "import tkinter" >nul 2>&1
if errorlevel 1 goto tkinter_failed

call :ensure_dependency "from serial import Serial; from serial.tools import list_ports" "pyserial"
if errorlevel 1 goto dependency_failed
call :ensure_dependency "from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg; from matplotlib.figure import Figure" "matplotlib"
if errorlevel 1 goto dependency_failed
set "GS_TERMINAL_READY=1"
call :ensure_dependency "from winpty import PtyProcess; from importlib.metadata import version; assert version('pywinpty').split('.')[0] == '2'" "pywinpty>=2,<3"
if errorlevel 1 set "GS_TERMINAL_READY=0"
call :ensure_dependency "from pyte import HistoryScreen, Stream; from importlib.metadata import version; assert version('pyte').split('.')[:2] == ['0', '8']" "pyte>=0.8,<0.9"
if errorlevel 1 set "GS_TERMINAL_READY=0"

if not exist "mira_ground_station.py" goto script_failed
echo.
if "%GS_TERMINAL_READY%"=="0" (
    echo [WARNING] Terminal unavailable: optional terminal dependencies are not ready.
    echo [INFO] Monitoring, serial reception, plots and CSV saving will still start.
)
echo [INFO] Starting MIRA Ground Station...
"%GS_PYTHON%" "mira_ground_station.py"
set "GS_EXIT_CODE=%errorlevel%"
if "%GS_EXIT_CODE%"=="0" exit /b 0
echo.
echo [ERROR] Ground station exited with code %GS_EXIT_CODE%.
echo Please review the error above. Check the COM port and close other serial programs.
pause
exit /b %GS_EXIT_CODE%

:find_python
%* -c "import sys; sys.exit(sys.version_info.major != 3)" >nul 2>&1
if errorlevel 1 exit /b 0
for /f "delims=" %%P in ('%* -c "import sys; print(sys.executable)" 2^>nul') do set "GS_PYTHON=%%P"
exit /b 0

:ensure_dependency
set "GS_REQUIREMENT=%~2"
"%GS_PYTHON%" -c "%~1" >nul 2>&1
if not errorlevel 1 (
    echo [OK] "%~2"
    exit /b 0
)
echo.
echo [INFO] Missing or incompatible dependency: "%~2"
echo [INFO] Installing into the same Python environment...
"%GS_PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    "%GS_PYTHON%" -m ensurepip --upgrade
    if errorlevel 1 exit /b 1
)
"%GS_PYTHON%" -m pip install --disable-pip-version-check --upgrade "%~2"
if errorlevel 1 exit /b 1
"%GS_PYTHON%" -c "%~1"
if errorlevel 1 exit /b 1
echo [OK] "%~2"
exit /b 0

:dependency_failed
echo.
echo [ERROR] Dependency setup failed: "%GS_REQUIREMENT%"
echo Ground station was not started. Review the installation error above.
echo Check Internet access and permissions for the selected Python environment.
pause
exit /b 1

:python_failed
echo [ERROR] Python 3 was not found.
echo Install Python 3 and enable Add Python to PATH, then run this file again.
pause
exit /b 1

:tkinter_failed
echo [ERROR] The selected Python installation cannot import tkinter.
echo Install Python with Tcl/Tk support. tkinter is not a pip package.
pause
exit /b 1

:workspace_failed
echo [ERROR] Cannot open the folder containing this launcher.
pause
exit /b 1

:script_failed
echo [ERROR] mira_ground_station.py was not found beside this launcher.
pause
exit /b 1

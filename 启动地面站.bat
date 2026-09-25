@echo off
REM ---- �е��� bat ����Ŀ¼��֧��·�����ո�----
cd /d "%~dp0"

REM ---- �Զ�̽�� Python ----
set "PY="
for %%P in (python python3 py) do (
    if not defined PY (
        %%P --version >nul 2>&1
        if not errorlevel 1 set "PY=%%P"
    )
)

if not defined PY (
    echo.
    echo [ERROR] Python not found.
    echo Please install Python and check "Add to PATH".
    echo.
    pause
    exit /b 1
)

echo Using: %PY%
%PY% --version

REM ---- ������� ----
%PY% -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [INFO] pyserial not installed, installing...
    %PY% -m pip install pyserial
)

%PY% -c "import matplotlib" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [INFO] matplotlib not installed, installing...
    %PY% -m pip install matplotlib
)

REM ---- ���� ----
echo.
echo Starting MIRA Ground Station...
echo.
%PY% "mira_ground_station.py"

if errorlevel 1 (
    echo.
    echo [ERROR] Program exited with an error.
    echo Common causes:
    echo   1. PuTTY is still using the COM port - close it first
    echo   2. Wrong COM port - check Device Manager
    echo.
    pause
)

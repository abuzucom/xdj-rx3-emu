@echo off
REM One-click MIDI controller client launcher for the XDJ-RX3 emulator.
REM Installs optional dependencies if they are missing, then runs the client.

setlocal enabledelayedexpansion

python -c "import mido" >nul 2>&1
if errorlevel 1 (
    echo Installing optional controller dependencies...
    python -m pip install -r "%~dp0requirements-controllers.txt"
    if errorlevel 1 (
        echo Failed to install controller dependencies. Run the command above manually.
        exit /b 1
    )
)

python -m controller_client.main %*

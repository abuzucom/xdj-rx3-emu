@echo off
REM One-click MIDI controller and RX3 screen launcher.
REM Installs optional dependencies if they are missing, then runs the client.

setlocal enabledelayedexpansion

python -c "import mido, rtmidi, PIL; raise SystemExit(PIL.__version__ != '12.3.0')" >nul 2>&1
if errorlevel 1 (
    echo Installing optional controller dependencies...
    python -m pip install --require-hashes -r "%~dp0requirements-controllers.txt"
    if errorlevel 1 (
        echo Failed to install controller dependencies. Run the command above manually.
        exit /b 1
    )
)

python -m controller_client.main --view %*

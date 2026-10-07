@echo off
rem One-click bootstrap and launch for the XDJ-RX3 emulator.
py -3 "%~dp0windows\rx3_windows.py" %*
if errorlevel 1 pause

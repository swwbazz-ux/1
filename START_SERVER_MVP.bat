@echo off
setlocal
set "MVP_DIR="
for /d %%D in ("%~dp0*_MVP") do set "MVP_DIR=%%~fD"
if not defined MVP_DIR (
    echo MVP directory was not found.
    pause
    exit /b 1
)
set "PY=%MVP_DIR%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Project Python was not found:
    echo "%PY%"
    pause
    exit /b 1
)
"%PY%" "%~dp0START_SERVER_MVP.py"
pause

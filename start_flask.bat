@echo off
REM Bourbon Hunter - one-click launch of the Phase 2 collection editor.
REM Activates the project venv, ensures Flask is installed, and serves the
REM editor on the Tailscale IP so you can tap it from your phone.

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo ERROR: venv not found at venv\Scripts\activate.bat
    echo Create it first:  python -m venv venv
    pause
    exit /b 1
)

call "venv\Scripts\activate.bat"

REM Make sure Flask is present (first run installs it).
python -c "import flask" 2>nul
if errorlevel 1 (
    echo Installing Flask...
    python -m pip install flask
)

echo.
echo ============================================================
echo   Bourbon Hunter editor is starting...
echo   Open on your phone:  http://100.111.112.8:5001
echo ============================================================
echo.

python flask_app.py

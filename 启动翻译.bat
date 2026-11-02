@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0float_trans.py"
    exit /b
)
where py >nul 2>nul
if %errorlevel%==0 (
    start "" py -w "%~dp0float_trans.py"
    exit /b
)
echo.
echo   Python not found. Please install Python 3.8+ from https://www.python.org
echo   (remember to tick "Add Python to PATH")
echo.
pause

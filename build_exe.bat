@echo off
cd /d "%~dp0"
echo [1/3] installing pyinstaller + winsdk ...
python -m pip install --upgrade pyinstaller
python -m pip install winsdk
echo [2/3] building icon ...
python make_icon.py
echo [3/3] packing exe ...
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name FloatTrans --icon app.ico --add-data "app.ico;." ^
    --hidden-import winsdk.windows.media.ocr ^
    --hidden-import winsdk.windows.globalization ^
    --hidden-import winsdk.windows.graphics.imaging ^
    --hidden-import winsdk.windows.storage ^
    float_trans.py
echo.
if exist "dist\FloatTrans.exe" (
    echo   Done.  dist\FloatTrans.exe
    echo   You can copy that single file anywhere.
) else (
    echo   Build failed, see messages above.
)
echo.
pause

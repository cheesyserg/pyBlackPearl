@echo off
if not exist venv (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b
)
call venv\Scripts\activate

echo Building EXE...
pyinstaller --noconfirm --onefile --windowed --add-data "icon.ico;." --icon "icon.ico" app.py

echo.
echo Build finished! Check the 'dist' folder for your executable.
pause

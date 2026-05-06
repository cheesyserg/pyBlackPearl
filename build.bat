@echo off
if not exist venv (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b
)
call venv\Scripts\activate

echo Building EXE...
pyinstaller --noconsole --onefile --icon=icon.ico --add-data "icon.jpg;." app.py

echo.
echo Build finished! Check the 'dist' folder for your executable.
pause
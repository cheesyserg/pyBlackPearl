@echo off
if not exist venv (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b
)

:: MANUALLY ADD THE STRIP PATH TO THIS SESSION
:: Replace the path below with your actual MSYS2/MinGW bin folder
call venv\Scripts\activate

echo Building EXE...
pyinstaller --clean build.spec

echo.
echo Build finished!
pause

@echo off
if not exist venv (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b
)
call venv\Scripts\activate
python app.py
pause
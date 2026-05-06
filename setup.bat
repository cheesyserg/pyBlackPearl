@echo off
echo Installing virtualenv globally...
pip install virtualenv

echo Creating environment using virtualenv...
virtualenv venv

echo Activating environment and installing modules...
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install pywinusb PySide6 PySide6-Fluent-Widgets pyinstaller

echo.
echo Setup complete!
pause
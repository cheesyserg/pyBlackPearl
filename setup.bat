@echo off
echo Installing virtualenv globally...
pip install virtualenv

echo Creating environment using virtualenv...
virtualenv venv

echo Activating environment and installing modules...
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Setup complete!
pause

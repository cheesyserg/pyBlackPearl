#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status

echo "Installing virtualenv globally..."
pip install virtualenv

echo "Creating environment using virtualenv..."
virtualenv venv

echo "Activating environment and installing modules..."
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo -e "\nSetup complete!"

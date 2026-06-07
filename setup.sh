#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status

# Check if virtualenv is installed
if ! command -v virtualenv >/dev/null 2>&1; then
  echo "virtualenv is not installed. Please install it (e.g., pip install virtualenv) and rerun this script."
  exit 1
fi

# Installing virtualenv globally is optional; ensure it's installed above.

echo "Creating environment using virtualenv..."
virtualenv venv

echo "Activating environment and installing modules..."
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo -e "\nSetup complete!"

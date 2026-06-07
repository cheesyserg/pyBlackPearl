#!/bin/bash
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Run setup.sh first."
    exit 1
fi

source venv/bin/activate
python app.py

#!/bin/bash
set -e

# GURU Fundamental Scanner - Start Script

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Python 3 is required but not installed."
    exit 1
fi

# Create venv if not exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Run setup if DB doesn't exist
if [ ! -f "guru_scanner.db" ]; then
    echo "Running first-time setup..."
    python3 setup.py
fi

# Start app
echo "Starting GURU Fundamental Scanner..."
streamlit run app.py --server.port=8501 --server.address=0.0.0.0

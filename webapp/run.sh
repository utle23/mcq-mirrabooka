#!/bin/bash
cd "$(dirname "$0")"
echo "Installing dependencies..."
pip3 install -r requirements.txt -q
echo ""
echo "============================================"
echo "  MCQ Mirrabooka Restaurant Management"
echo "  Open browser: http://localhost:5050"
echo "  Password: 7777  |  Location: mirrabooka"
echo "============================================"
echo ""
python3 app.py

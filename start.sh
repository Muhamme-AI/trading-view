#!/bin/bash
# GBP/USD Trading Intelligence — Startup Script

echo ""
echo "======================================"
echo "  GBP/USD Trading Intelligence"
echo "======================================"

# Navigate to script directory
cd "$(dirname "$0")"

# Check if dependencies are installed
if ! python -c "import fastapi, psycopg" 2>/dev/null; then
  echo ""
  echo "  Installing dependencies..."
  pip install -r requirements.txt -q
  echo "  Done."
fi

# Require Supabase database URL
if [ ! -f .env ] && [ -z "$DATABASE_URL" ]; then
  echo ""
  echo "  Missing .env — copy .env.example and set DATABASE_URL"
  echo "  Supabase → Project Settings → Database → Connection string"
  exit 1
fi

echo ""
echo "  Starting server..."
echo "  Opening http://localhost:8000 in your browser..."
echo ""
echo "  Press Ctrl+C to stop the app."
echo ""

# Open browser after short delay
sleep 1.5 && open http://localhost:8000 &

# Start the app
python app.py

#!/bin/bash
set -e

echo "Running database setup..."
python scripts/run_db_setup.py --seed

echo "Starting application..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

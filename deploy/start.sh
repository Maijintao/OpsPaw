set -eu

python scripts/build_kb.py
python scripts/vectorize_kb.py
exec uvicorn app.main:app --host 0.0.0.0 --port 8000

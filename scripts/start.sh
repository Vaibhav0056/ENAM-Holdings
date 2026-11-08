#!/bin/sh
# Boot FINN. If the configured database does not exist yet (fresh disk), start from the committed
# seed snapshot so the demo opens instantly; the app then refreshes from BSE/yfinance on demand.
set -e
DB="${FINN_DB:-data/finn.db}"
mkdir -p "$(dirname "$DB")"
if [ ! -f "$DB" ] && [ -f data/seed.db ]; then
  echo "Seeding $DB from data/seed.db"
  cp data/seed.db "$DB"
fi
exec uvicorn api:app --host 0.0.0.0 --port "${PORT:-8000}"

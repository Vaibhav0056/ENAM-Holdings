#!/bin/sh
# Boot FINN. Migrations use Neon's direct (unpooled) endpoint; the seed is idempotent (adds nothing on re-runs).
# If the database can't be reached, the app still starts and serves data/demo_snapshot.json for the main screens.
set -e
if [ -n "$DATABASE_URL" ]; then
  python -m alembic upgrade head || echo "migration skipped: database unreachable"
  python -m finn.seed || echo "seed skipped: database unreachable"
fi
exec python -m uvicorn api:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers

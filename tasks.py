"""Windows-friendly task runner (no Makefile, no bash). Usage: python tasks.py <task> [args]

  setup       migrate the database to the latest schema, then seed companies + alert rules
  migrate     alembic upgrade head
  seed        seed companies (data/companies.csv) and default alert rules
  validate    re-validate the 25 tickers on yfinance + BSE (rewrites data/companies.csv)
  probes      run every source probe and save the raw shapes to docs/probes/
  ingest      prices, filings, calendar, document links           (Phase 1)
  pipeline    classify -> score -> enrich -> thesis -> alerts -> notify (Phase 2+)
  serve       run the API + PWA on http://localhost:8000
  test        run the unit tests
  evals       python -m evals.run                                  (Phase 12)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(*args: str, check: bool = True) -> int:
    print("$", " ".join(args), flush=True)
    rc = subprocess.call(list(args), cwd=ROOT, env=ENV)
    if check and rc != 0:
        sys.exit(rc)
    return rc


def migrate():
    run(PY, "-m", "alembic", "upgrade", "head")


def seed():
    run(PY, "-m", "finn.seed")


def setup():
    migrate()
    seed()


def validate():
    run(PY, "scripts/validate_tickers.py")


def probes():
    out = ROOT / "docs" / "probes"
    out.mkdir(parents=True, exist_ok=True)
    for name, script, args in [("neon.txt", "scripts/probe_neon.py", []), ("bse.json", "scripts/probe_bse.py", []),
                               ("yfinance.json", "scripts/probe_yfinance.py", []),
                               ("screener.json", "scripts/probe_screener_docs.py", ["DIVISLAB", "CAMS", "KAYNES"]),
                               ("gemini.txt", "scripts/probe_gemini.py", []), ("tavily.json", "scripts/probe_tavily.py", [])]:
        print(f"-- {script} -> docs/probes/{name}", flush=True)
        with (out / name).open("w", encoding="utf-8") as f:
            subprocess.call([PY, script, *args], cwd=ROOT, env=ENV, stdout=f, stderr=subprocess.STDOUT)


def ingest():
    run(PY, "-m", "finn.ingest_v2")


def pipeline():
    run(PY, "-m", "finn.pipeline")


def serve():
    port = os.getenv("PORT", "8000")
    run(PY, "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", port)


def test():
    run(PY, "-m", "pytest", "-q", "tests")


def evals():
    run(PY, "-m", "evals.run")


TASKS = {f.__name__: f for f in (setup, migrate, seed, validate, probes, ingest, pipeline, serve, test, evals)}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in TASKS:
        print(__doc__)
        sys.exit(1)
    TASKS[sys.argv[1]]()

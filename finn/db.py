"""SQLite access: one file, plain sqlite3, schema created on connect."""
import csv, os, sqlite3, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("FINN_DB", DATA / "finn.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
  nse_symbol TEXT PRIMARY KEY, bse_code TEXT, yahoo_symbol TEXT, name TEXT, sector TEXT);
CREATE TABLE IF NOT EXISTS prices (
  symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS announcements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_hash TEXT UNIQUE, symbol TEXT, bse_code TEXT, news_id TEXT,
  category TEXT, subcategory TEXT, subject TEXT, headline TEXT, more TEXT,
  filed_at TEXT, pdf_url TEXT, raw_json TEXT,
  processed INTEGER DEFAULT 0, ann_type TEXT, type_source TEXT, magnitude REAL, novelty REAL,
  score REAL, bucket TEXT, one_liner TEXT, enrichment_json TEXT, status TEXT DEFAULT 'ok',
  error TEXT, reviewed INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_ann_filed ON announcements(filed_at);
CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY, task TEXT, model TEXT, response TEXT,
  in_tokens INTEGER, out_tokens INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS llm_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, model TEXT, cached INTEGER,
  in_tokens INTEGER, out_tokens INTEGER, ok INTEGER, error TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS alert_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT UNIQUE, label TEXT,
  threshold REAL, types TEXT, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule_kind TEXT, symbol TEXT, date TEXT,
  title TEXT, body TEXT, link TEXT, announcement_id INTEGER, read INTEGER DEFAULT 0,
  notified INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(rule_kind, symbol, date));
CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY, json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS documents (
  sha TEXT PRIMARY KEY, url TEXT, path TEXT, kind TEXT, symbol TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS research_jobs (
  id TEXT PRIMARY KEY, symbol TEXT, stage TEXT, progress REAL, status TEXT,
  result_json TEXT, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS research_reports (
  symbol TEXT PRIMARY KEY, result_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS llm_quota (model TEXT, day TEXT, PRIMARY KEY (model, day));
CREATE TABLE IF NOT EXISTS ingest_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, symbol TEXT, ok INTEGER, message TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

_initialized = set()

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    if str(DB_PATH) not in _initialized:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        _initialized.add(str(DB_PATH))
    return con

def q(sql, params=(), one=False):
    with _lock:
        con = connect()
        try:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
            con.commit()
            return (rows[0] if rows else None) if one else rows
        finally:
            con.close()

def x(sql, params=()):
    with _lock:
        con = connect()
        try:
            cur = con.execute(sql, params)
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

def log_ingest(source, symbol, ok, message=""):
    x("INSERT INTO ingest_log(source,symbol,ok,message) VALUES(?,?,?,?)",
      (source, symbol, int(ok), str(message)[:500]))

def load_portfolio():
    with open(DATA / "portfolio.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        x("INSERT OR REPLACE INTO portfolio VALUES(?,?,?,?,?)",
          (r["nse_symbol"], r["bse_code"], r["yahoo_symbol"], r["name"], r["sector"]))
    return rows

def portfolio():
    if not q("SELECT 1 FROM portfolio LIMIT 1"):
        load_portfolio()
    return q("SELECT * FROM portfolio ORDER BY nse_symbol")

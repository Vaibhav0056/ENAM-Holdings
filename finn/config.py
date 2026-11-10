"""Settings from .env. One place that knows env var names and defaults."""
import os
from dotenv import load_dotenv

load_dotenv()

APP_TZ = os.getenv("APP_TZ", "Asia/Kolkata")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "gemini-3.5-flash")
STRONG_MODEL = os.getenv("STRONG_MODEL", "gemini-3.8-flash")
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-2")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
MAX_COST_PER_REPORT_USD = float(os.getenv("MAX_COST_PER_REPORT_USD", "0.05"))
USER_AGENT = "Mozilla/5.0 (compatible; FINN-research-app/0.2)"


def database_url() -> str:
    """SQLAlchemy URL. Postgres (Neon) when DATABASE_URL is set; SQLite is a unit-test fallback only."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///" + os.getenv("FINN_TEST_DB", "data/finn_test.sqlite")
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def migration_url() -> str:
    """Direct (unpooled) Neon endpoint for DDL, as Neon recommends; falls back to the app URL."""
    url = os.getenv("DATABASE_URL_UNPOOLED", "").strip()
    if not url or not os.getenv("DATABASE_URL"):
        return ""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def is_postgres(url: str | None = None) -> bool:
    return (url or database_url()).startswith("postgresql")

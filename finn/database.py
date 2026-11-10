"""Engine and sessions. Postgres (Neon) when DATABASE_URL is set; SQLite only as a unit-test fallback."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import database_url, is_postgres

_engine = None
_Session = None


def engine():
    global _engine, _Session
    if _engine is None:
        url = database_url()
        if is_postgres(url):
            # Neon's pooled endpoint (PgBouncer) + a small client pool. pre_ping survives Neon's autosuspend.
            _engine = create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=240)
        else:
            _engine = create_engine(url, connect_args={"check_same_thread": False})

            @event.listens_for(_engine, "connect")
            def _fk_on(dbapi_conn, _):
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset_engine():
    """For tests that switch DATABASE_URL."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine, _Session = None, None


@contextmanager
def session() -> Session:
    engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

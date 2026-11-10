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


_read_engine = None
_ReadSession = None


def read_engine():
    """Hot read paths (the feed). AUTOCOMMIT and no pre-ping: one network round trip per query instead of four
    (BEGIN, ping, SELECT, COMMIT); from India to Neon Singapore that is ~85 ms vs ~300 ms. A connection Neon
    dropped while suspended is retried once in read_session()."""
    global _read_engine, _ReadSession
    if _read_engine is None:
        url = database_url()
        if is_postgres(url):
            _read_engine = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=5, max_overflow=5,
                                         pool_recycle=240)
        else:
            _read_engine = engine()
        _ReadSession = sessionmaker(_read_engine, expire_on_commit=False)
    return _read_engine


def read_session_run(fn):
    """Run fn(session) on the read engine; retry once if the pooled connection had gone stale."""
    from sqlalchemy.exc import DBAPIError, OperationalError
    read_engine()
    for attempt in (1, 2):
        s = _ReadSession()
        try:
            return fn(s)
        except (OperationalError, DBAPIError) as e:
            if attempt == 2 or not getattr(e, "connection_invalidated", True):
                raise
            _read_engine.dispose()
        finally:
            s.close()


def reset_engine():
    """For tests that switch DATABASE_URL."""
    global _engine, _Session, _read_engine, _ReadSession
    if _read_engine is not None and _read_engine is not _engine:
        _read_engine.dispose()
    if _engine is not None:
        _engine.dispose()
    _engine, _Session, _read_engine, _ReadSession = None, None, None, None


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

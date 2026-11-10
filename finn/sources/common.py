"""Shared helpers for source adapters: polite HTTP, idempotent inserts, per-company run logging."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from urllib.parse import urlparse

import requests
from sqlalchemy.dialects import postgresql, sqlite

from ..config import USER_AGENT
from ..database import engine, session
from ..models import IngestRun

log = logging.getLogger("finn.sources")


class PoliteHTTP:
    """Generic User-Agent, at least `delay` seconds between requests to the same host, retries on 429/5xx."""

    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})

    def get(self, url: str, referer: str | None = None, timeout: int = 60, attempts: int = 3) -> requests.Response:
        host = urlparse(url).netloc
        for i in range(attempts):
            with self._lock:
                wait = self._last.get(host, 0) + self.delay - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last[host] = time.monotonic()
            try:
                r = self.s.get(url, headers={"Referer": referer} if referer else None, timeout=timeout)
            except requests.RequestException:
                if i == attempts - 1:
                    raise
                time.sleep(2 * (i + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(3 * (i + 1))
                continue
            return r
        return r


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def insert_ignore(model, rows: list[dict], conflict_cols: list[str], update_cols: list[str] | None = None) -> int:
    """INSERT ... ON CONFLICT DO NOTHING (or DO UPDATE update_cols). Returns how many rows were newly inserted."""
    if not rows:
        return 0
    dialect = engine().dialect.name
    ins = (postgresql.insert if dialect == "postgresql" else sqlite.insert)(model)
    added = 0
    with session() as s:
        for chunk_start in range(0, len(rows), 500):
            chunk = rows[chunk_start:chunk_start + 500]
            if update_cols:
                stmt = ins.values(chunk)
                stmt = stmt.on_conflict_do_update(index_elements=conflict_cols,
                                                  set_={c: getattr(stmt.excluded, c) for c in update_cols})
                # xmax = 0 marks a fresh insert (Postgres); on SQLite count by existence beforehand
                if dialect == "postgresql":
                    res = s.execute(stmt.returning(postgresql_xmax_is_zero()))
                    added += sum(1 for (fresh,) in res if fresh)
                else:
                    before = _count_existing(s, model, conflict_cols, chunk)
                    s.execute(stmt)
                    added += len(chunk) - before
            else:
                stmt = ins.values(chunk).on_conflict_do_nothing(index_elements=conflict_cols)
                if dialect == "postgresql":
                    added += len(s.execute(stmt.returning(*[getattr(model, conflict_cols[0])])).all())
                else:
                    before = _count_existing(s, model, conflict_cols, chunk)
                    s.execute(stmt)
                    added += len(chunk) - before
    return added


def postgresql_xmax_is_zero():
    from sqlalchemy import literal_column
    return literal_column("(xmax = 0)")


def _count_existing(s, model, cols, chunk) -> int:
    from sqlalchemy import and_, select, func
    n = 0
    for r in chunk:
        n += s.scalar(select(func.count()).select_from(model).where(and_(*[getattr(model, c) == r[c] for c in cols]))) or 0
    return n


def log_run(source: str, company_id: int | None, added: int, seen: int, ok: bool = True, error: str | None = None):
    with session() as s:
        s.add(IngestRun(source=source, company_id=company_id, added=added, seen=seen, ok=ok,
                        error=(error or None) and error[:1000]))

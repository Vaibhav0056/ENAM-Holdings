"""Daily bars from yfinance for every company plus the NIFTY 50 index (market leg for relative moves).

Probed (docs/probes/yfinance.json): download() returns MultiIndex columns (field, ticker) and naive dates.
Both raw close and adjusted close are stored: alerts use adjusted prices so dividends and splits don't look like moves.
"""
from __future__ import annotations

import logging
import math

from ..models import Price
from .common import insert_ignore, log_run

log = logging.getLogger("finn.sources.prices")
MARKET_INDEX = "^NSEI"


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def frame_to_rows(df, symbol: str, company_id: int | None) -> list[dict]:
    if df is None or df.empty:
        return []
    if hasattr(df.columns, "levels"):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    rows = []
    for idx, r in df.iterrows():
        close = _num(r.get("Close"))
        if close is None:
            continue
        rows.append({"company_id": company_id, "symbol": symbol, "date": idx.date(), "open": _num(r.get("Open")),
                     "high": _num(r.get("High")), "low": _num(r.get("Low")), "close": close,
                     "adj_close": _num(r.get("Adj Close")) or close, "volume": _num(r.get("Volume"))})
    return rows


def fetch(symbol: str, days: int = 90):
    import yfinance as yf
    return yf.download(symbol, period=f"{days}d", interval="1d", auto_adjust=False, progress=False)


def ingest(companies, days: int = 90) -> dict[str, dict]:
    """companies: iterable of objects with id, nse_symbol, yahoo_symbol. Returns per-symbol counts."""
    out = {}
    targets = [(c.yahoo_symbol, c.id, c.nse_symbol) for c in companies] + [(MARKET_INDEX, None, MARKET_INDEX)]
    for ysym, cid, label in targets:
        try:
            rows = frame_to_rows(fetch(ysym, days), ysym, cid)
            if not rows:
                raise ValueError("no bars returned")
            added = insert_ignore(Price, rows, ["symbol", "date"],
                                  update_cols=["open", "high", "low", "close", "adj_close", "volume"])
            out[label] = {"seen": len(rows), "added": added}
            log_run("prices", cid, added, len(rows))
        except Exception as e:  # noqa: BLE001 - one bad ticker never stops the run
            log.warning("prices failed %s: %s", label, e)
            out[label] = {"seen": 0, "added": 0, "error": str(e)[:200]}
            log_run("prices", cid, 0, 0, ok=False, error=str(e))
    return out

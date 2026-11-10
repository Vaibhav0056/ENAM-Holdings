"""Pure price/volume maths shared by scoring (market reaction) and alerts. No I/O; unit-tested.

Moves are judged two ways: the fixed rule (|move| >= X %) and "unusual for this stock": the move in excess of the
NIFTY 50's move that day is >= 3 robust sigmas of the stock's own recent history. 3 % is noise for a small-cap and an
event for a large bank, and a market-wide sell-off is not twenty stock events.
"""
from __future__ import annotations

from statistics import median

VOL_WINDOW = 20      # sessions in the volume baseline
SIGMA_WINDOW = 60    # sessions of returns behind a stock's normal daily move
SIGMA_MIN_OBS = 20
SIGMA_MULT = 3.0
SIGMA_FLOOR = 0.5    # an "unusual" move must also be at least half the fixed % threshold


def daily_move_pct(prev_close: float | None, close: float | None) -> float | None:
    if not prev_close or close is None:
        return None
    return (close - prev_close) / prev_close * 100.0


def volume_baseline(volumes: list[float | None], window: int = VOL_WINDOW) -> float | None:
    """Median of the `window` traded sessions before the last one. Median, not mean: one earlier spike (a results
    day) would otherwise inflate the baseline for 20 sessions and hide the next one."""
    base = [v for v in volumes[:-1] if v and v > 0][-window:]
    return median(base) if len(base) == window else None


def volume_ratio(volumes: list[float | None], window: int = VOL_WINDOW) -> float | None:
    base = volume_baseline(volumes, window)
    return None if base is None or not volumes[-1] else volumes[-1] / base


def return_sigma(closes: list[float], window: int = SIGMA_WINDOW, min_obs: int = SIGMA_MIN_OBS) -> float | None:
    """The stock's normal daily move in %, from returns BEFORE the last close. Robust (1.4826 × MAD) so past jump
    days don't widen the band. None when history is short or flat."""
    prior = closes[:-1][-(window + 1):]
    rets = [m for m in (daily_move_pct(a, b) for a, b in zip(prior, prior[1:])) if m is not None]
    if len(rets) < min_obs:
        return None
    mid = median(rets)
    return 1.4826 * median(abs(r - mid) for r in rets) or None


def index_moves(index_rows: list[dict]) -> dict:
    """date -> NIFTY 50 daily move %, from ascending rows with date/close."""
    out = {}
    for prev, cur in zip(index_rows, index_rows[1:]):
        mv = daily_move_pct(prev["close"], cur["close"])
        if mv is not None:
            out[cur["date"]] = mv
    return out


def move_check(closes: list[float], move_pct: float, market_move: float | None) -> dict | None:
    """Evaluate the last close against the previous one. Returns a dict describing the move, with `hit` True when
    it crosses the fixed threshold or is unusual for the stock, or None when it can't be computed."""
    if len(closes) < 2:
        return None
    mv = daily_move_pct(closes[-2], closes[-1])
    if mv is None:
        return None
    sigma = return_sigma(closes)
    excess = mv - (market_move or 0.0)
    z = abs(excess) / sigma if sigma else None
    unusual = z is not None and z >= SIGMA_MULT and abs(mv) >= SIGMA_FLOOR * move_pct
    return {"move_pct": round(mv, 2), "market_pct": None if market_move is None else round(market_move, 2),
            "sigma_pct": None if sigma is None else round(sigma, 2), "z": None if z is None else round(z, 2),
            "fixed": abs(mv) >= move_pct, "unusual": unusual, "hit": abs(mv) >= move_pct or unusual}

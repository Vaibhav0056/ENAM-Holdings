"""Phase 10: feedback adjustments stay bounded; forward moves for replay."""
from datetime import date

from finn.experience import BOUND, adjustments, forward_move


def test_feedback_adjustments_are_bounded_and_signed():
    adj = adjustments([("other", "not_useful")] * 10 + [("results", "higher"), ("results", "useful")])
    assert adj["other"] == -BOUND and adj["results"] == 0.075
    assert adjustments([("x", "useful"), ("x", "not_useful"), ("x", "useful")]) == {}


def test_forward_move_uses_the_last_close_before_and_n_sessions_after():
    rows = [(date(2026, 10, d), c) for d, c in [(5, 100.0), (6, 102.0), (7, 95.0), (8, 94.0), (9, 90.0)]]
    m = forward_move(rows, date(2026, 10, 6), sessions=2)
    assert m == {"from": "2026-10-06", "to": "2026-10-08", "sessions": 2, "move_pct": -7.84}
    assert forward_move(rows, date(2026, 10, 9)) is None

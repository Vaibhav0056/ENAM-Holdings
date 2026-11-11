"""Write data/demo_snapshot.json from the live database (see finn/snapshot.py).

    python scripts/export_snapshot.py          # export
    python scripts/export_snapshot.py --check  # export, then verify every section is present (used in CI)
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging  # noqa: E402

logging.disable(logging.INFO)
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
from finn.snapshot import PATH  # noqa: E402


def main(check: bool) -> int:
    c = TestClient(api.app)
    get = lambda p, **q: c.get(p, params=q).json()  # noqa: E731
    holdings = get("/api/v2/holdings")
    snap = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "digest": {w: get("/digest", window=w) for w in ("24h", "3d", "7d", "30d")},
        "holdings": holdings,
        "holding_pages": {h["symbol"]: get(f"/api/v2/holdings/{h['symbol']}") for h in holdings["holdings"]},
        "alerts": get("/api/v2/alerts"),
        "catalysts": get("/api/v2/catalysts"),
        "research": {},
    }
    for sym in ["POLYCAB", "BAJFINANCE", "HAVELLS", "KEI", "ASTRAL"]:
        r = c.get(f"/api/v2/research/{sym}")
        if r.status_code == 200:
            snap["research"][sym] = r.json()
    PATH.write_text(json.dumps(snap, ensure_ascii=False, default=str), encoding="utf-8")
    size = PATH.stat().st_size
    print(f"wrote {PATH} ({size / 1e6:.1f} MB): {len(snap['holding_pages'])} holdings, "
          f"{len(snap['alerts']['alerts'])} alerts, research {sorted(snap['research'])}")
    if check:
        ok = all(snap[k] for k in ("digest", "holdings", "holding_pages", "alerts")) and size < 15e6
        print("snapshot check:", "ok" if ok else "FAILED")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))

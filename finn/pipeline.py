"""The scheduled run after ingest: push pipeline (classify, score, enrich, thesis) -> alerts -> notify.
Each stage is isolated: one failing never blocks the next.    python -m finn.pipeline [--all]"""
from __future__ import annotations

import json
import logging
import sys
import time


def run(rescore_all: bool = False) -> dict:
    out, t0 = {}, time.time()
    for name, fn in [("feed", lambda: __import__("finn.feed.pipeline", fromlist=["run"]).run(rescore_all=rescore_all)),
                     ("alerts", lambda: __import__("finn.alerting.engine", fromlist=["evaluate"]).evaluate()),
                     ("notify", lambda: __import__("finn.alerting.notify", fromlist=["notify_new_alerts"]).notify_new_alerts())]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            logging.getLogger("finn.pipeline").exception("%s failed", name)
            out[name] = f"failed: {e}"[:300]
    try:
        from .feed import digest
        digest.invalidate()
        from . import charts, web_ui
        charts.invalidate()
        web_ui.invalidate()
    except Exception:  # noqa: BLE001
        pass
    out["seconds"] = round(time.time() - t0, 1)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    print(json.dumps(run("--all" in sys.argv), indent=1, default=str))

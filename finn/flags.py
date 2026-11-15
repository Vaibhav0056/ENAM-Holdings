"""Feature flags. Every user-facing feature beyond the core inbox has one; a feature that is not verified end to end
ships off and is listed under "Next steps" in the README. Override with FINN_FLAGS, e.g. FINN_FLAGS="web_push=1,copilot=0".

GET /api/v2/flags returns {name: bool}; the PWA hides anything that is off.
"""
from __future__ import annotations

import os

FEATURES = {
    # name: (default, what it is, why it has that default)
    "charts": (True, "Charts on holdings, research, deep dives and alerts", "golden-path tested; render < 50 ms"),
    "research": (True, "Promise ledger research reports", "golden-path tested; 7-day cache; precomputed"),
    "deep_dives": (True, "Go deeper: results history, cash quality, balance sheet, language diff, hedge-meter, dodge "
                         "detector, worry map, fine print, peer compare", "golden-path tested on POLYCAB"),
    "copilot": (True, "Ask FINN about a holding (answers cite filings, calls and reports only)",
                "golden-path tested; answers are model calls and can take tens of seconds on the free tier"),
    "portfolio_edit": (True, "Add and remove holdings in Settings", "golden-path tested, incl. removal during onboarding"),
    "replay": (True, "Replay the feed as of a past date", "API checked; one explicit button with its own loading state"),
    "journal": (True, "Decision journal on a card", "unit-tested"),
    "card_feedback": (True, "Useful / Not useful / Should be higher", "unit-tested; bounded weight changes"),
    "audio_brief": (False, "60-second spoken brief of Today", "uses each device's own speech engine; pronunciation of "
                                                             "rupee figures not verified on real devices"),
    "web_push": (False, "Lock-screen alerts (Web Push)", "needs an installed PWA on a real phone; not verified end to "
                                                        "end here. Alerts still appear in the Alerts tab"),
}


def overrides() -> dict[str, bool]:
    out = {}
    for part in (os.getenv("FINN_FLAGS") or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() in FEATURES:
                out[k.strip()] = v.strip().lower() in ("1", "true", "on", "yes")
    return out


def enabled(name: str) -> bool:
    return overrides().get(name, FEATURES[name][0])


def all_flags() -> dict[str, bool]:
    o = overrides()
    return {k: o.get(k, v[0]) for k, v in FEATURES.items()}


def table() -> list[dict]:
    o = overrides()
    return [{"feature": k, "on": o.get(k, v[0]), "what": v[1], "why": v[2]} for k, v in FEATURES.items()]

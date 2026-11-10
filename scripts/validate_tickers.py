"""Validate the 20 holdings + 5 research peers (spec §2.2).

A ticker passes when yfinance <SYM>.NS returns recent bars AND the BSE scrip code resolves. BSE metadata adds the
official name, ISIN, sector and industry, which are checked against ENAM's exclusions (tobacco, alcohol,
meat/poultry, hotels). Writes data/companies.csv and prints a report for the build log.
"""
import csv
import json
import re
import sys
import tempfile
import time
from pathlib import Path

import yfinance as yf
from bse import BSE

PORTFOLIO = ["HDFCBANK", "BAJFINANCE", "CAMS", "TCS", "PERSISTENT", "NAUKRI", "ASIANPAINT", "TITAN", "DMART",
             "PIDILITIND", "LT", "POLYCAB", "HAL", "DIXON", "KAYNES", "SUNPHARMA", "APOLLOHOSP", "DEEPAKNTR",
             "MARUTI", "TATAPOWER"]
PEERS = {"HAVELLS": "POLYCAB", "TRENT": "DMART", "CHOLAFIN": "BAJFINANCE", "KPITTECH": "PERSISTENT",
         "DIVISLAB": "SUNPHARMA"}
EXCLUDED = re.compile(r"tobacco|cigarette|alcohol|liquor|brewer|distill|spirits|wine|beer|meat|poultry|hotel|resort",
                      re.I)
OUT = Path("data/companies.csv")
# Swaps (logged in docs/build-log.md, Phase 0): CDSL -> CAMS. CDSL is listed on NSE only (BSE is one of its
# promoters), so it has no BSE scrip code and no BSE filings. CAMS is the closest BSE-listed peer: asset-light
# capital-market infrastructure (mutual-fund registrar) with annuity-like, fee-based revenue.
SWAPS = {"CDSL": ("CAMS", "NSE-only listing: no BSE scrip code or BSE filings")}


def _retry(fn, attempts=3):
    """BSE occasionally times out; a timeout is not evidence that a scrip doesn't exist."""
    for i in range(attempts):
        try:
            return fn()
        except TimeoutError:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))


def check(b: BSE, sym: str) -> dict:
    row = {"nse_symbol": sym, "yahoo_symbol": f"{sym}.NS", "is_portfolio": sym in PORTFOLIO,
           "peer_of": PEERS.get(sym, ""), "status": "ok",
           "note": next((f"replaces {old}: {why}" for old, (new, why) in SWAPS.items() if new == sym), "")}
    try:
        df = yf.download(row["yahoo_symbol"], period="10d", interval="1d", auto_adjust=False, progress=False)
        row["yf_rows"] = len(df)
        if df.empty:
            row["status"], row["note"] = "fail", "yfinance returned no bars"
    except Exception as e:  # noqa: BLE001
        row["status"], row["note"], row["yf_rows"] = "fail", f"yfinance: {e}", 0
    try:
        code = str(_retry(lambda: b.getScripCode(sym)))
        row["bse_code"] = code
        time.sleep(0.5)
        meta = _retry(lambda: b.equityMetaInfo(code))
        row.update(isin=meta.get("ISIN", ""), sector=meta.get("Sector") or meta.get("IndustryNew", ""),
                   industry=meta.get("ISubGroup") or meta.get("Industry", ""))
        name = b.getScripName(code) if hasattr(b, "getScripName") else ""
        row["name"] = name or meta.get("SecurityId", sym)
    except Exception as e:  # noqa: BLE001
        row["status"], row["note"] = "fail", (row["note"] + f"; BSE: {e}").strip("; ")
    if row["note"] and row["status"] == "ok" and not row["note"].startswith("replaces"):
        row["note"] = ""
    text = f"{row.get('sector', '')} {row.get('industry', '')}"
    if EXCLUDED.search(text):
        row["status"], row["note"] = "excluded", f"industry '{text.strip()}' is on ENAM's exclusion list"
    return row


def main():
    rows = []
    with BSE(download_folder=tempfile.gettempdir()) as b:
        for sym in PORTFOLIO + list(PEERS):
            r = check(b, sym)
            rows.append(r)
            print(f"{sym:12} {r['status']:9} bse={r.get('bse_code', '-'):8} yf_rows={r.get('yf_rows')} "
                  f"| {r.get('name', '')[:40]} | {r.get('industry', '')} {('| ' + r['note']) if r['note'] else ''}")
            time.sleep(0.5)
    bad = [r for r in rows if r["status"] != "ok"]
    cols = ["nse_symbol", "bse_code", "yahoo_symbol", "isin", "name", "sector", "industry", "is_portfolio", "peer_of",
            "status", "note"]
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"checked": len(rows), "ok": len(rows) - len(bad), "problems": [r["nse_symbol"] for r in bad]}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

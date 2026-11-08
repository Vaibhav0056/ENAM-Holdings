"""Validate candidate NSE stocks against yfinance + BSE scrip lookup; write data/portfolio.csv.

Each stock must (a) return price data from yfinance as <SYM>.NS and (b) resolve a BSE scrip code.
Failures are replaced from the BACKUPS list.
"""
import csv, time
import yfinance as yf
from bse import BSE

CANDIDATES = ["RELIANCE", "HDFCBANK", "TCS", "INFY", "ITC", "LT", "SUNPHARMA", "MARUTI",
              "TITAN", "ASIANPAINT", "BAJFINANCE", "TATAPOWER", "HAL", "POLYCAB", "DIXON", "PERSISTENT"]
BACKUPS = ["ICICIBANK", "BHARTIARTL", "NESTLEIND", "DMART", "TRENT", "CUMMINSIND", "PIDILITIND", "SIEMENS"]
TARGET = 16

def validate(b, sym):
    df = yf.download(f"{sym}.NS", period="5d", auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise ValueError("no yfinance data")
    code = b.getScripCode(sym)
    info = yf.Ticker(f"{sym}.NS").info
    return {"nse_symbol": sym, "bse_code": code, "yahoo_symbol": f"{sym}.NS",
            "name": info.get("longName") or info.get("shortName") or sym,
            "sector": info.get("sector") or "Unknown"}

def main():
    b = BSE(download_folder="data")
    out, queue = [], CANDIDATES + BACKUPS
    for sym in queue:
        if len(out) >= TARGET:
            break
        try:
            row = validate(b, sym); out.append(row); print("OK  ", row)
        except Exception as e:
            print("FAIL", sym, e)
        time.sleep(0.5)
    b.exit()
    with open("data/portfolio.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["nse_symbol", "bse_code", "yahoo_symbol", "name", "sector"])
        w.writeheader(); w.writerows(out)
    print(f"wrote {len(out)} stocks")

if __name__ == "__main__":
    main()

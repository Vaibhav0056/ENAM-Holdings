"""Probe yfinance: 90 days of daily bars for one NSE stock and the NIFTY 50 index, plus dividends/splits."""
import json, sys, time
import yfinance as yf

sym = sys.argv[1] if len(sys.argv) > 1 else "TCS.NS"
out = {"yfinance": yf.__version__}
for s in (sym, "^NSEI"):
    t = time.time()
    df = yf.download(s, period="90d", interval="1d", auto_adjust=False, progress=False)
    out[s] = {"secs": round(time.time() - t, 2), "rows": len(df), "columns": [str(c) for c in df.columns],
              "multiindex": hasattr(df.columns, "levels"), "first": str(df.index.min()), "last": str(df.index.max()),
              "tz": str(df.index.tz)}
tk = yf.Ticker(sym)
out["dividends_last"] = {str(k.date()): float(v) for k, v in tk.dividends.tail(3).items()}
out["splits_last"] = {str(k.date()): float(v) for k, v in tk.splits.tail(3).items()}
print(json.dumps(out, indent=1))

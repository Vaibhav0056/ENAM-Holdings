"""Probe yfinance and the bse library: print real response shapes before building on them."""
import json
from datetime import datetime, timedelta
import yfinance as yf
from bse import BSE

df = yf.download("RELIANCE.NS", period="5d", auto_adjust=True, progress=False)
print("yfinance columns:", list(df.columns)); print(df.tail(2))
t = yf.Ticker("RELIANCE.NS")
info = t.info
print("info keys sample:", {k: info.get(k) for k in ["longName", "shortName", "sector", "industry", "marketCap"]})

b = BSE(download_folder="data")
code = b.getScripCode("RELIANCE"); print("scrip code:", code)
ann = b.announcements(scripcode=code, from_date=datetime.now() - timedelta(days=30), to_date=datetime.now())
print("announcements keys:", list(ann.keys()))
rows = ann.get("Table", [])
print("n rows:", len(rows))
if rows:
    print(json.dumps(rows[0], indent=1, default=str))
print("Table1:", ann.get("Table1"))
b.exit()

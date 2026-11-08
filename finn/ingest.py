"""Ingest prices (yfinance) and BSE announcements for the portfolio. Idempotent.

Run: python -m finn.ingest
"""
import hashlib, json, logging, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import db

log = logging.getLogger("finn.ingest")
IST = ZoneInfo("Asia/Kolkata")
PDF_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"


def ingest_prices(stocks, days=90):
    import yfinance as yf
    n = 0
    for s in stocks:
        sym = s["nse_symbol"]
        try:
            df = yf.download(s["yahoo_symbol"], period=f"{days}d", auto_adjust=True, progress=False)
            if df is None or df.empty:
                raise ValueError("empty price frame")
            if hasattr(df.columns, "levels"):  # probed: MultiIndex (field, ticker)
                df.columns = df.columns.get_level_values(0)
            for idx, r in df.iterrows():
                db.x("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?)",
                     (sym, idx.strftime("%Y-%m-%d"), float(r["Open"]), float(r["High"]), float(r["Low"]),
                      float(r["Close"]), float(r["Volume"])))
                n += 1
            db.log_ingest("prices", sym, True, f"{len(df)} rows")
        except Exception as e:
            log.warning("prices failed %s: %s", sym, e)
            db.log_ingest("prices", sym, False, e)
    return n


def content_hash(row: dict) -> str:
    key = f"{row.get('SCRIP_CD')}|{row.get('NEWSID')}|{row.get('NEWSSUB')}|{row.get('ATTACHMENTNAME')}"
    return hashlib.sha256(key.encode()).hexdigest()


def _filed_at(row):
    raw = row.get("DissemDT") or row.get("NEWS_DT") or row.get("DT_TM")
    try:
        dt = datetime.fromisoformat(raw.split(".")[0])  # BSE times are IST, naive
        return dt.replace(tzinfo=IST).isoformat()
    except Exception:
        return raw


def fetch_announcements(b, code, days=30):
    """Pages through bse.announcements until ROWCNT rows are collected."""
    end = datetime.now()
    start = end - timedelta(days=days)
    out, page = [], 1
    while True:
        res = b.announcements(page_no=page, scripcode=code, from_date=start, to_date=end)
        rows = res.get("Table", []) or []
        out.extend(rows)
        total = (res.get("Table1") or [{}])[0].get("ROWCNT", len(out))
        if not rows or len(out) >= total or page > 20:
            break
        page += 1
        time.sleep(0.5)
    return out


def store_announcement(sym, row) -> bool:
    h = content_hash(row)
    att = (row.get("ATTACHMENTNAME") or "").strip()
    cur = db.q("SELECT 1 FROM announcements WHERE content_hash=?", (h,), one=True)
    if cur:
        return False
    db.x("""INSERT OR IGNORE INTO announcements(content_hash,symbol,bse_code,news_id,category,subcategory,subject,
            headline,more,filed_at,pdf_url,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
         (h, sym, str(row.get("SCRIP_CD")), row.get("NEWSID"), (row.get("CATEGORYNAME") or "").strip(),
          (row.get("SUBCATNAME") or "").strip(), (row.get("NEWSSUB") or "").strip(),
          (row.get("HEADLINE") or "").strip(), (row.get("MORE") or "").strip(), _filed_at(row),
          PDF_BASE + att if att else None, json.dumps(row, default=str)))
    return True


def ingest_announcements(stocks, days=30):
    from bse import BSE
    new = 0
    b = BSE(download_folder=str(db.DATA))
    try:
        for s in stocks:
            sym = s["nse_symbol"]
            try:
                rows = fetch_announcements(b, s["bse_code"], days)
                added = sum(store_announcement(sym, r) for r in rows)
                new += added
                db.log_ingest("bse", sym, True, f"{len(rows)} fetched, {added} new")
            except Exception as e:
                log.warning("bse failed %s: %s", sym, e)
                db.log_ingest("bse", sym, False, e)
            time.sleep(0.5)
    finally:
        try:
            b.exit()
        except Exception:
            pass
    return new


def run(days_prices=90, days_ann=30):
    stocks = db.portfolio()
    p = ingest_prices(stocks, days_prices)
    a = ingest_announcements(stocks, days_ann)
    summary = {"price_rows": p, "new_announcements": a, "stocks": len(stocks)}
    log.info("ingest done %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    print(run())

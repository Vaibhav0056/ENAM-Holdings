"""Probe the `bse` library: announcements (90d), result calendar, corporate actions, scrip meta.
Prints the real response shape so the adapter is built on fields that exist."""
import json, sys, tempfile, time
from datetime import datetime, timedelta
from bse import BSE

SCRIP = sys.argv[1] if len(sys.argv) > 1 else "532540"  # TCS


def shape(rows, n=1):
    if isinstance(rows, dict):
        return {k: (f"list[{len(v)}]" if isinstance(v, list) else v) for k, v in rows.items()}
    return {"count": len(rows), "keys": sorted(rows[0].keys()) if rows else [], "sample": rows[:n]}


out = {}
with BSE(download_folder=tempfile.gettempdir()) as b:
    now = datetime.now()
    t = time.time()
    ann = b.announcements(page_no=1, from_date=now - timedelta(days=90), to_date=now, scripcode=SCRIP)
    out["announcements"] = {"top_keys": list(ann.keys()), "Table1": ann.get("Table1"), "secs": round(time.time() - t, 2),
                            **shape(ann.get("Table", []))}
    rows = ann.get("Table", [])
    out["announcements"]["xbrl_field_nonempty"] = sum(1 for r in rows if r.get("XML_NAME"))
    out["announcements"]["categories"] = sorted({r.get("CATEGORYNAME") or "" for r in rows})
    time.sleep(1)
    cal = b.resultCalendar(from_date=now, to_date=now + timedelta(days=14))
    out["resultCalendar_14d"] = shape(cal, 2)
    time.sleep(1)
    acts = b.actions(from_date=now, to_date=now + timedelta(days=30))
    out["actions_30d"] = shape(acts, 2)
    out["actions_30d"]["purposes"] = sorted({a.get("Purpose", "") for a in acts})[:25]
    time.sleep(1)
    out["equityMetaInfo"] = b.equityMetaInfo(SCRIP)
print(json.dumps(out, indent=1, default=str)[:9000])

"""Snapshot the working DB into data/seed.db (committed) so a fresh deploy opens with real data.
Push subscriptions are device-specific and are stripped from the snapshot."""
import sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = ROOT / "data" / "finn.db"
dst = ROOT / "data" / "seed.db"
if dst.exists():
    dst.unlink()
con = sqlite3.connect(src)
con.execute(f"VACUUM INTO '{dst.as_posix()}'")
con.close()
seed = sqlite3.connect(dst)
seed.execute("DELETE FROM push_subscriptions")
seed.execute("DELETE FROM research_jobs")
seed.execute("UPDATE alerts SET notified=1")
seed.execute("PRAGMA journal_mode=DELETE")
seed.commit()
seed.execute("VACUUM")
seed.close()
print(f"wrote {dst} ({dst.stat().st_size / 1e6:.1f} MB)")

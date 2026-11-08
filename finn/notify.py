"""Web Push for new alerts. Never raises: a push failure must not block the pipeline.
The Alerts tab is the source of truth; a push only points at it."""
import json, logging, os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from . import db

load_dotenv()
log = logging.getLogger("finn.notify")


def configured() -> bool:
    return bool(os.getenv("VAPID_PRIVATE_KEY") and os.getenv("VAPID_PUBLIC_KEY"))


def send_to_all(title: str, body: str, url: str) -> dict:
    sent = removed = failed = 0
    if not configured():
        return {"sent": 0, "removed": 0, "failed": 0, "skipped": "VAPID keys not set"}
    from pywebpush import webpush, WebPushException
    payload = json.dumps({"title": title, "body": body, "data": {"url": url}})
    for sub in db.q("SELECT * FROM push_subscriptions"):
        try:
            webpush(subscription_info=json.loads(sub["json"]), data=payload,
                    vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                    vapid_claims={"sub": os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")}, timeout=10)
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                db.x("DELETE FROM push_subscriptions WHERE endpoint=?", (sub["endpoint"],))
                removed += 1
            else:
                log.warning("push failed (%s): %s", code, e)
                failed += 1
        except Exception as e:
            log.warning("push failed: %s", e)
            failed += 1
    return {"sent": sent, "removed": removed, "failed": failed}


def notify_new_alerts() -> dict:
    """Push each un-notified alert from the last 2 days, then mark everything notified."""
    total = {"alerts": 0, "sent": 0}
    try:
        cutoff = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = db.q("SELECT * FROM alerts WHERE notified=0 AND created_at>=? ORDER BY id", (cutoff,))
        # one push per alert, but cap a burst so a first run doesn't spam the phone
        for a in fresh[-5:]:
            r = send_to_all(a["title"], a["body"] or "", a["link"] or "/#alerts")
            total["alerts"] += 1
            total["sent"] += r.get("sent", 0)
        if len(fresh) > 5:
            send_to_all("FINN", f"{len(fresh) - 5} more alerts in your inbox", "/#alerts")
        db.x("UPDATE alerts SET notified=1 WHERE notified=0")
    except Exception as e:
        log.warning("notify failed: %s", e)
    return total

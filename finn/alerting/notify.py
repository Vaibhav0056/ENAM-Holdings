"""Web Push (VAPID) for new alerts. Never raises: a push failure must not block the pipeline. Subscriptions the push
service reports gone (404/410) are deleted. A burst is capped (5 pushes + one summary) so a first run doesn't spam."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from ..database import session
from ..models import Alert, PushSubscription

log = logging.getLogger("finn.notify")
BURST = 5


def configured() -> bool:
    return bool(os.getenv("VAPID_PRIVATE_KEY") and os.getenv("VAPID_PUBLIC_KEY"))


def public_key() -> str:
    return os.getenv("VAPID_PUBLIC_KEY", "")


def subscribe(sub: dict) -> int:
    if not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
        raise ValueError("not a push subscription")
    with session() as s:
        row = s.scalar(select(PushSubscription).where(PushSubscription.endpoint == sub["endpoint"]))
        if row:
            row.keys = sub["keys"]
        else:
            row = PushSubscription(endpoint=sub["endpoint"], keys=sub["keys"])
            s.add(row)
        s.flush()
        return row.id


def send_to_all(title: str, body: str, url: str) -> dict:
    out = {"sent": 0, "removed": 0, "failed": 0}
    if not configured():
        return out | {"skipped": "VAPID keys not set"}
    from pywebpush import WebPushException, webpush
    payload = json.dumps({"title": title, "body": body, "data": {"url": url}})
    with session() as s:
        subs = [(x.id, x.endpoint, x.keys) for x in s.scalars(select(PushSubscription))]
    for sid, endpoint, keys in subs:
        try:
            webpush(subscription_info={"endpoint": endpoint, "keys": keys}, data=payload,
                    vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                    vapid_claims={"sub": os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")}, timeout=10, ttl=86400)
            out["sent"] += 1
            with session() as s:
                s.get(PushSubscription, sid).last_ok_at = datetime.now(timezone.utc)
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                with session() as s:
                    s.execute(delete(PushSubscription).where(PushSubscription.id == sid))
                out["removed"] += 1
            else:
                log.warning("push failed (%s): %s", code, str(e)[:200])
                out["failed"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("push failed: %s", str(e)[:200])
            out["failed"] += 1
    return out


def notify_new_alerts(max_age_days: int = 2) -> dict:
    total = {"alerts": 0, "sent": 0}
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        with session() as s:
            fresh = [(a.id, a.title, a.line, a.deep_link) for a in
                     s.scalars(select(Alert).where(Alert.notified.is_(False), Alert.created_at >= cutoff).order_by(Alert.id))]
        for _, title, line, link in fresh[-BURST:]:
            r = send_to_all(title, line or "", link or "/#alerts")
            total["alerts"] += 1
            total["sent"] += r.get("sent", 0)
        if len(fresh) > BURST:
            send_to_all("FINN", f"{len(fresh) - BURST} more alerts in your inbox", "/#alerts")
        with session() as s:
            for a in s.scalars(select(Alert).where(Alert.notified.is_(False))):
                a.notified = True
    except Exception as e:  # noqa: BLE001
        log.warning("notify failed: %s", e)
    return total

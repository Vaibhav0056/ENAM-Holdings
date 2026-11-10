"""/health must answer without touching any database."""
from fastapi.testclient import TestClient


def test_health_needs_no_database(monkeypatch):
    import api
    from finn import database, db

    def boom(*a, **k):
        raise AssertionError("health touched the database")

    monkeypatch.setattr(db, "q", boom)
    monkeypatch.setattr(database, "session", boom)
    r = TestClient(api.app).get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True

"""Conversions survive across serverless instances when stored in Redis (simulated with fakeredis)."""
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.engine import store as store_mod
from app.engine.store import RedisStore

FIX = Path(__file__).parent / "fixtures" / "shakun"


@pytest.fixture
def client(monkeypatch):
    rs = RedisStore.__new__(RedisStore)
    rs.r = fakeredis.FakeRedis()
    monkeypatch.setattr(store_mod, "store", lambda: rs)
    import app.main as main
    monkeypatch.setattr(main, "store", lambda: rs)
    import app.engine.config  # settings read through store_mod.store
    return TestClient(main.app), rs


def test_ledger_flow_through_redis(client):
    c, rs = client
    with open(FIX / "ledgers.xls", "rb") as lf, open(FIX / "trial.xls", "rb") as tf:
        r = c.post("/api/ledger/jobs", data={"software": "shakun"},
                   files={"ledger_file": ("l.xls", lf), "tb_file": ("t.xls", tf)})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    raw = rs.r.get(f"accobiz:job:{jid}")
    assert raw and len(raw) < 1_000_000                       # fits Redis value limits
    rows = c.get(f"/api/ledger/jobs/{jid}/records?filter=errors").json()["rows"]
    assert len(rows) == 2
    # change on one "instance" must be visible on the next request (fresh unpickle)
    assert c.patch(f"/api/ledger/jobs/{jid}/records/{rows[1]['idx']}", json={"include": False}).status_code == 200
    assert c.get(f"/api/ledger/jobs/{jid}").json()["can_download"] is True
    d = c.get(f"/api/ledger/jobs/{jid}/download")
    assert d.status_code == 200 and d.content[:2] == b"PK"


def test_unknown_job_is_404(client):
    c, _ = client
    assert c.get("/api/ledger/jobs/nope").status_code == 404


def test_settings_saved_in_redis(client, tmp_path):
    c, rs = client
    s = c.get("/api/settings/stock").json()["settings"]
    s["tcs"]["rate"] = "1"
    assert c.put("/api/settings/stock", json=s).status_code == 200
    assert b'"rate": "1"' in rs.r.get("accobiz:settings:stock")
    assert c.get("/api/settings/stock").json()["settings"]["tcs"]["rate"] == "1"
    with open(FIX / "stock.xls", "rb") as f:
        j = c.post("/api/stock/jobs", data={"software": "shakun"}, files={"stock_file": ("s.xls", f)}).json()
    assert j["settings"]["tcs"]["rate"] == "1"

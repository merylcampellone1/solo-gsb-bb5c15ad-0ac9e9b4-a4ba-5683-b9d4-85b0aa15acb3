"""HTTP API tests with an in-memory stand-in for PostgreSQL."""
import json

import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as main


@pytest.fixture
def client(monkeypatch):
    store = {}

    class FakeConn:
        async def fetchval(self, q):
            return 1

        async def close(self):
            pass

    async def fake_connect(dsn=None):
        return FakeConn()

    async def fake_get_frozen(conn, request_id):
        return store.get(request_id)

    async def fake_freeze(conn, request_id, rhash, status, payload):
        data = json.loads(payload) if isinstance(payload, str) else payload
        store.setdefault(request_id, (rhash, status, data))

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(db, "get_frozen", fake_get_frozen)
    monkeypatch.setattr(db, "freeze", fake_freeze)
    monkeypatch.setattr(main, "connect", fake_connect)
    monkeypatch.setattr(main, "get_frozen", fake_get_frozen)
    monkeypatch.setattr(main, "freeze", fake_freeze)

    with TestClient(main.app) as c:
        yield c


GOOD = {
    "stocks": [{"id": "S1", "concentration": "100", "available_nl": 1_000_000}],
    "diluent": {"available_nl": 10_000_000},
    "wells": [{"id": "A1", "final_volume_nl": 1000,
               "final_concentration": "40"}],
    "containers": [],
    "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
    "default_well_capacity_nl": 5000,
}


def test_feasible_and_frozen_retry(client):
    r = client.post("/plans", json=GOOD, headers={"X-Request-ID": "r1"})
    assert r.status_code == 201
    assert sum(t["volume_nl"]
               for t in r.json()["plan"]["transfers"]) == 1000
    r2 = client.post("/plans", json=GOOD, headers={"X-Request-ID": "r1"})
    assert r2.status_code == 200
    assert r2.json()["plan"] == r.json()["plan"]


def test_conflict_on_different_content(client):
    client.post("/plans", json=GOOD, headers={"X-Request-ID": "r2"})
    changed = json.loads(json.dumps(GOOD))
    changed["wells"][0]["final_concentration"] = "30"
    r = client.post("/plans", json=changed, headers={"X-Request-ID": "r2"})
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_conflict"


def test_get_plan(client):
    client.post("/plans", json=GOOD, headers={"X-Request-ID": "r3"})
    assert client.get("/plans/r3").status_code == 200
    assert client.get("/plans/missing").status_code == 404


def test_infeasible_frozen_and_retried(client):
    bad = json.loads(json.dumps(GOOD))
    bad["stocks"][0]["available_nl"] = 50
    bad["wells"][0]["final_concentration"] = "90"
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "r4"})
    assert r.status_code == 422
    assert r.json()["status"] == "infeasible"
    assert r.json()["blocked"]
    assert client.post("/plans", json=bad,
                       headers={"X-Request-ID": "r4"}).status_code == 422
    assert client.get("/plans/r4").status_code == 422


def test_float_concentration_rejected(client):
    bad = json.loads(json.dumps(GOOD))
    bad["wells"][0]["final_concentration"] = 0.5
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "r5"})
    assert r.status_code == 400


def test_negative_volume_rejected(client):
    bad = json.loads(json.dumps(GOOD))
    bad["wells"][0]["final_volume_nl"] = -5
    assert client.post("/plans", json=bad,
                       headers={"X-Request-ID": "r6"}).status_code == 400


def test_bad_pipette_range_rejected(client):
    bad = json.loads(json.dumps(GOOD))
    bad["pipette"]["min_nl"] = 2000
    assert client.post("/plans", json=bad,
                       headers={"X-Request-ID": "r7"}).status_code == 400


def test_missing_request_id_rejected(client):
    assert client.post("/plans", json=GOOD).status_code == 400


def test_duplicate_well_id_rejected(client):
    bad = json.loads(json.dumps(GOOD))
    bad["wells"].append(dict(bad["wells"][0]))
    assert client.post("/plans", json=bad,
                       headers={"X-Request-ID": "r8"}).status_code == 400


def test_rational_string_concentration(client):
    body = json.loads(json.dumps(GOOD))
    body["wells"][0]["final_concentration"] = "50/2"
    r = client.post("/plans", json=body, headers={"X-Request-ID": "r9"})
    assert r.status_code == 201
    shares = sorted(t["volume_nl"]
                    for t in r.json()["plan"]["transfers"])
    assert shares == [250, 750]


def test_serial_dilution_with_named_container(client):
    body = {
        "stocks": [{"id": "S1", "concentration": "1000",
                    "available_nl": 1_000_000}],
        "diluent": {"available_nl": 10_000_000},
        "wells": [{"id": "A1", "final_volume_nl": 1000,
                   "final_concentration": "100"}],
        "containers": [{"id": "tube-1", "capacity_nl": 10_000}],
        "pipette": {"min_nl": 500, "max_nl": 1000, "step_nl": 100},
        "default_well_capacity_nl": 5000,
    }
    r = client.post("/plans", json=body, headers={"X-Request-ID": "r10"})
    assert r.status_code == 201
    plan = r.json()["plan"]
    assert plan["intermediates"][0]["container"] == "tube-1"
    assert plan["intermediates"][0]["concentration"] == "100"
    # all transfer volumes obey the pipette window
    assert all(500 <= t["volume_nl"] <= 1000 for t in plan["transfers"])


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}

"""API integration tests without a real PostgreSQL server.

A tiny in-memory connection stand-in implements the few methods the app uses
(fetchval/fetchrow/execute), so the full FastAPI stack incl. validation,
idempotency and conflict handling is exercised.
"""
import json
import sys

sys.path.insert(0, ".")

from fastapi.testclient import TestClient

import app.db as db
import app.main as main


class FakeConn:
    def __init__(self):
        self.rows = {}

    async def fetchval(self, q):
        return 1

    async def close(self):
        pass

    async def fetchrow(self, q, request_id):
        row = self.rows.get(request_id)
        if row is None:
            return None
        import asyncpg
        return asyncpg.Record  # placeholder (never used as such)

    async def execute(self, q, *args):
        if q.startswith("CREATE TABLE"):
            return
        if q.startswith("INSERT"):
            request_id, rhash, status, payload = args
            if request_id not in self.rows:
                self.rows[request_id] = (rhash, status, json.loads(payload))


# monkey-patch get_frozen/freeze to use the in-memory store
_store = {}


async def fake_get_frozen(conn, request_id):
    return _store.get(request_id)


async def fake_freeze(conn, request_id, rhash, status, payload):
    if request_id not in _store:
        _store[request_id] = (rhash, status, payload)


async def fake_connect(dsn=None):
    return FakeConn()


db.connect = fake_connect
db.get_frozen = fake_get_frozen
db.freeze = fake_freeze
main.connect = fake_connect
main.get_frozen = fake_get_frozen
main.freeze = fake_freeze

client = TestClient(main.app)


GOOD = {
    "stocks": [{"id": "S1", "concentration": "100", "available_nl": 1_000_000}],
    "diluent": {"available_nl": 10_000_000},
    "wells": [{
        "id": "A1", "final_volume_nl": 1000,
        "final_concentration": "40",
    }],
    "containers": [],
    "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
    "default_well_capacity_nl": 5000,
}


def test_feasible():
    r = client.post("/plans", json=GOOD, headers={"X-Request-ID": "req-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "feasible"
    total = sum(t["volume_nl"] for t in body["plan"]["transfers"])
    assert total == 1000
    wells = body["plan"]["wells"]
    assert wells[0]["final_concentration"] == "40"
    print("feasible -> 201,", len(body["plan"]["transfers"]), "transfers")


def test_idempotent_retry():
    r = client.post("/plans", json=GOOD, headers={"X-Request-ID": "req-1"})
    assert r.status_code == 200, r.text
    print("same retry -> 200 frozen plan")


def test_conflict():
    changed = json.loads(json.dumps(GOOD))
    changed["wells"][0]["final_concentration"] = "30"
    r = client.post("/plans", json=changed, headers={"X-Request-ID": "req-1"})
    assert r.status_code == 409, r.text
    print("different content same id -> 409")


def test_get_plan():
    r = client.get("/plans/req-1")
    assert r.status_code == 200
    r = client.get("/plans/nope")
    assert r.status_code == 404
    print("GET frozen 200 / missing 404")


def test_infeasible_frozen():
    bad = json.loads(json.dumps(GOOD))
    bad["stocks"][0]["available_nl"] = 50
    bad["wells"][0]["final_concentration"] = "90"
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "req-2"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["status"] == "infeasible"
    assert body["blocked"]
    print("infeasible -> 422, blocked:", body["blocked"])
    r2 = client.post("/plans", json=bad, headers={"X-Request-ID": "req-2"})
    assert r2.status_code == 422
    print("infeasible retry -> 422 frozen")


def test_bad_float_concentration():
    bad = json.loads(json.dumps(GOOD))
    bad["wells"][0]["final_concentration"] = 0.5
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "req-3"})
    assert r.status_code == 400
    print("float concentration -> 400")


def test_bad_volume():
    bad = json.loads(json.dumps(GOOD))
    bad["wells"][0]["final_volume_nl"] = -5
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "req-4"})
    assert r.status_code == 400
    print("negative volume -> 400")


def test_bad_pipette_range():
    bad = json.loads(json.dumps(GOOD))
    bad["pipette"]["min_nl"] = 2000
    r = client.post("/plans", json=bad, headers={"X-Request-ID": "req-5"})
    assert r.status_code == 400
    print("pipette min > max -> 400")


def test_missing_request_id():
    r = client.post("/plans", json=GOOD)
    assert r.status_code == 400, r.text
    print("missing X-Request-ID -> 400")


def test_rational_concentration():
    body = json.loads(json.dumps(GOOD))
    # rational 25 = "50/2" string form: high share 250 nl, diluent 750 nl
    body["wells"][0]["final_concentration"] = "50/2"
    r = client.post("/plans", json=body, headers={"X-Request-ID": "req-6"})
    assert r.status_code == 201, r.text
    shares = sorted(t["volume_nl"] for t in r.json()["plan"]["transfers"])
    assert shares == [250, 750]
    print("50/2 rational-string target -> 201 with shares 250/750 nl")


def test_serial_via_api():
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
    r = client.post("/plans", json=body, headers={"X-Request-ID": "req-7"})
    assert r.status_code == 201, r.text
    plan = r.json()["plan"]
    assert plan["intermediates"][0]["container"] == "tube-1"
    assert plan["intermediates"][0]["concentration"] == "100"
    print("serial dilution via API -> 201, container label tube-1")


if __name__ == "__main__":
    with client:
        test_feasible()
        test_idempotent_retry()
        test_conflict()
        test_get_plan()
        test_infeasible_frozen()
        test_bad_float_concentration()
        test_bad_volume()
        test_bad_pipette_range()
        test_missing_request_id()
        test_rational_concentration()
        test_serial_via_api()
    print("\nALL API TESTS PASSED")

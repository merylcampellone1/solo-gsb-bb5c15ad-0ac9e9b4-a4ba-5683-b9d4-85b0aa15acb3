"""Inventory mode tests: batch registry, atomic reservations, confirm/cancel.

The in-memory stand-in mirrors the SQL semantics of app.db: each fake does
its check-and-set without intermediate awaits, which is atomic under asyncio
(the real implementation uses transactions + SELECT ... FOR UPDATE).
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as main
from app.planning import wells_for_batch_in_plan


# --------------------------------------------------------------------------- #
# In-memory persistence stand-in
# --------------------------------------------------------------------------- #


class FakePool:
    async def fetchval(self, q):
        return 1

    async def close(self):
        pass


class FakeStore:
    def __init__(self):
        self.plans = {}         # request_id -> (hash, status, payload)
        self.batches = {}       # batch_id -> mutable row dict
        self.reservations = {}  # request_id -> {"status", "items"}
        self.recalls = {}       # batch_id -> {"reason", "impact", ...}

    # -- batches ----------------------------------------------------------- #

    @staticmethod
    def _pub(b):
        return {
            "id": b["id"],
            "concentration": b["concentration"],
            "initial_volume_nl": b["initial_volume_nl"],
            "reserved_nl": b["reserved_nl"],
            "used_nl": b["used_nl"],
            "available_nl": (b["initial_volume_nl"]
                             - b["reserved_nl"] - b["used_nl"]),
            "recalled": b.get("recalled", False),
            **({"recall_reason": b["recall_reason"]}
               if b.get("recalled") else {}),
        }

    async def create_batch(self, conn, batch_id, concentration, initial_nl):
        existing = self.batches.get(batch_id)
        if existing is not None:
            return False, self._pub(existing)
        row = {"id": batch_id, "concentration": concentration,
               "initial_volume_nl": initial_nl, "reserved_nl": 0,
               "used_nl": 0}
        self.batches[batch_id] = row
        return True, self._pub(row)

    async def get_batch(self, conn, batch_id):
        b = self.batches.get(batch_id)
        return self._pub(b) if b else None

    async def get_batches(self, conn, ids):
        await asyncio.sleep(0)  # widen the race window for concurrency tests
        return {i: self._pub(self.batches[i])
                for i in ids if i in self.batches}

    async def list_batches(self, conn):
        return [self._pub(self.batches[k]) for k in sorted(self.batches)]

    # -- frozen plans -------------------------------------------------------- #

    async def get_frozen(self, conn, request_id):
        return self.plans.get(request_id)

    async def freeze(self, conn, request_id, rhash, status, payload):
        data = json.loads(payload) if isinstance(payload, str) else payload
        self.plans.setdefault(request_id, (rhash, status, data))

    # -- reservations -------------------------------------------------------- #

    async def reserve_and_freeze(self, conn, request_id, rhash, payload,
                                 usage):
        # atomic: no awaits between the checks and the updates
        if request_id in self.plans:
            return "exists", None
        recalled = []
        for bid in sorted(usage):
            b = self.batches.get(bid)
            if b is None:
                return "unknown", [bid]
            if b.get("recalled"):
                recalled.append({"batch_id": bid,
                                 "reason": b.get("recall_reason")})
        if recalled:
            return "recalled", recalled
        shortages = []
        for bid in sorted(usage):
            b = self.batches.get(bid)
            if b is None:
                return "unknown", [bid]
            available = (b["initial_volume_nl"] - b["reserved_nl"]
                         - b["used_nl"])
            if available < usage[bid]:
                shortages.append({"batch_id": bid,
                                  "required_nl": usage[bid],
                                  "available_nl": available})
        if shortages:
            return "insufficient", shortages
        self.plans[request_id] = (rhash, "feasible", payload)
        for bid, amount in usage.items():
            self.batches[bid]["reserved_nl"] += amount
        self.reservations[request_id] = {
            "status": "reserved",
            "items": [{"batch_id": bid, "amount_nl": amount}
                      for bid, amount in sorted(usage.items())],
        }
        return "ok", None

    async def get_reservation(self, conn, request_id):
        r = self.reservations.get(request_id)
        if r is None:
            return None
        return {"request_id": request_id, "status": r["status"],
                "items": [dict(i) for i in r["items"]]}

    async def transition_reservation(self, conn, request_id, action):
        # atomic: no awaits between the check and the updates
        r = self.reservations.get(request_id)
        if r is None:
            return None
        result = {"status": r["status"], "changed": False,
                  "items": [dict(i) for i in r["items"]]}
        if r["status"] != "reserved":
            return result
        if action == "confirm":
            blocked = [{"batch_id": i["batch_id"],
                        "reason": self.batches[i["batch_id"]]
                        .get("recall_reason")}
                       for i in r["items"]
                       if self.batches[i["batch_id"]].get("recalled")]
            if blocked:
                result["recalled_batches"] = blocked
                return result
        for item in r["items"]:
            b = self.batches[item["batch_id"]]
            b["reserved_nl"] -= item["amount_nl"]
            if action == "confirm":
                b["used_nl"] += item["amount_nl"]
        r["status"] = "confirmed" if action == "confirm" else "cancelled"
        result["status"] = r["status"]
        result["changed"] = True
        return result

    # -- recalls ------------------------------------------------------------ #

    async def create_recall(self, conn, batch_id, reason, build_impact):
        # atomic: no awaits between the check and the state changes
        b = self.batches.get(batch_id)
        if b is None:
            return "unknown", None
        existing = self.recalls.get(batch_id)
        if existing is not None:
            if existing["reason"] == reason:
                return "replayed", existing
            return "conflict", existing
        rows = [
            {"request_id": rid,
             "payload": self.plans[rid][2],
             "reservation_status": self.reservations[rid]["status"]}
            for rid in sorted(self.plans)
            if self.reservations.get(rid, {}).get("status")
               in ("reserved", "confirmed")
            and any(i["batch_id"] == batch_id
                    for i in self.reservations[rid]["items"])
        ]
        impact = await build_impact(None, rows)
        recall = {"batch_id": batch_id, "reason": reason,
                  "recalled_at": "2026-10-05T00:00:00+00:00",
                  "impact": impact}
        self.recalls[batch_id] = recall
        b["recalled"] = True
        b["recall_reason"] = reason
        return "created", recall

    async def get_recall(self, conn, batch_id):
        return self.recalls.get(batch_id)


@pytest.fixture
def store(monkeypatch):
    st = FakeStore()

    async def fake_connect(dsn=None):
        return FakePool()

    pairs = [
        ("connect", fake_connect),
        ("get_frozen", st.get_frozen),
        ("freeze", st.freeze),
        ("create_batch", st.create_batch),
        ("get_batch", st.get_batch),
        ("get_batches", st.get_batches),
        ("list_batches", st.list_batches),
        ("get_reservation", st.get_reservation),
        ("reserve_and_freeze", st.reserve_and_freeze),
        ("transition_reservation", st.transition_reservation),
        ("create_recall", st.create_recall),
        ("get_recall", st.get_recall),
    ]
    for name, fn in pairs:
        monkeypatch.setattr(db, name, fn)
        monkeypatch.setattr(main, name, fn)
    main.app.state.db = FakePool()
    return st


@pytest.fixture
def client(store):
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def anyio_backend():
    return "asyncio"


BATCH = {"id": "S1", "concentration": "100", "initial_volume_nl": 1000}

PLAN = {
    "batch_ids": ["S1"],
    "diluent": {"available_nl": 10_000_000},
    "wells": [{"id": "A1", "final_volume_nl": 600,
               "final_concentration": "100"}],
    "containers": [],
    "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
    "default_well_capacity_nl": 5000,
}


def _plan(rid_conc="100", volume=600):
    body = json.loads(json.dumps(PLAN))
    body["wells"][0]["final_concentration"] = rid_conc
    body["wells"][0]["final_volume_nl"] = volume
    return body


# --------------------------------------------------------------------------- #
# Batch registry
# --------------------------------------------------------------------------- #


def test_batch_register_replay_conflict(client):
    r = client.post("/batches", json=BATCH)
    assert r.status_code == 201
    assert r.json()["available_nl"] == 1000

    r = client.post("/batches", json=BATCH)
    assert r.status_code == 200  # identical re-registration is idempotent

    changed = dict(BATCH, initial_volume_nl=2000)
    r = client.post("/batches", json=changed)
    assert r.status_code == 409
    assert r.json()["error"] == "batch_conflict"

    changed = dict(BATCH, concentration="50")
    assert client.post("/batches", json=changed).status_code == 409


def test_batch_validation(client):
    assert client.post(
        "/batches", json={"id": "X", "concentration": 0.5,
                          "initial_volume_nl": 10}).status_code == 400
    assert client.post(
        "/batches", json={"id": "X", "concentration": "10",
                          "initial_volume_nl": 0}).status_code == 400
    assert client.post(
        "/batches", json={"id": "@bad", "concentration": "10",
                          "initial_volume_nl": 10}).status_code == 400


def test_batch_get_and_list(client):
    assert client.get("/batches/S1").status_code == 404
    client.post("/batches", json=BATCH)
    r = client.get("/batches/S1")
    assert r.status_code == 200
    assert r.json()["reserved_nl"] == 0
    listing = client.get("/batches").json()["batches"]
    assert [b["id"] for b in listing] == ["S1"]


# --------------------------------------------------------------------------- #
# Inventory planning
# --------------------------------------------------------------------------- #


def test_inventory_plan_reserves(client, store):
    client.post("/batches", json=BATCH)
    r = client.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "inv-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "feasible"
    assert body["reservations"] == [{"batch_id": "S1", "reserved_nl": 600}]
    assert body["reservation_status"] == "reserved"
    drawn = sum(t["volume_nl"] for t in body["plan"]["transfers"]
                if t["source"]["kind"] == "stock")
    assert drawn == 600
    batch = client.get("/batches/S1").json()
    assert batch["reserved_nl"] == 600
    assert batch["available_nl"] == 400


def test_inventory_retry_returns_original_without_double_reserve(client):
    client.post("/batches", json=BATCH)
    r1 = client.post("/inventory/plans", json=PLAN,
                     headers={"X-Request-ID": "inv-2"})
    r2 = client.post("/inventory/plans", json=PLAN,
                     headers={"X-Request-ID": "inv-2"})
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert client.get("/batches/S1").json()["reserved_nl"] == 600


def test_inventory_conflict_same_id_different_body(client):
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "inv-3"})
    r = client.post("/inventory/plans", json=_plan("50"),
                    headers={"X-Request-ID": "inv-3"})
    assert r.status_code == 409
    assert r.json()["error"] == "idempotency_conflict"


def test_inventory_unknown_batch(client):
    r = client.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "inv-4"})
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_batch"
    # nothing frozen, nothing reserved: fixing the request reuses the id
    client.post("/batches", json=BATCH)
    r = client.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "inv-4"})
    assert r.status_code == 201


def test_inventory_infeasible_occupies_nothing(client):
    client.post("/batches", json=BATCH)
    r = client.post("/inventory/plans", json=_plan("150"),
                    headers={"X-Request-ID": "inv-5"})
    assert r.status_code == 422
    assert r.json()["status"] == "infeasible"
    assert r.json()["inventory"][0]["available_nl"] == 1000
    assert client.get("/batches/S1").json()["reserved_nl"] == 0
    # frozen: the same retry replays the 422
    assert client.post("/inventory/plans", json=_plan("150"),
                       headers={"X-Request-ID": "inv-5"}).status_code == 422


def test_inventory_insufficient_occupies_nothing(client):
    client.post("/batches", json=BATCH)
    assert client.post("/inventory/plans", json=PLAN,
                       headers={"X-Request-ID": "inv-6a"}).status_code == 201
    r = client.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "inv-6b"})
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "insufficient_inventory"
    assert body["inventory"][0]["available_nl"] == 400
    assert client.get("/batches/S1").json()["reserved_nl"] == 600


def test_inventory_serial_dilution(client):
    client.post("/batches", json={"id": "S2", "concentration": "1000",
                                  "initial_volume_nl": 1_000_000})
    body = {
        "batch_ids": ["S2"],
        "diluent": {"available_nl": 10_000_000},
        "wells": [{"id": "A1", "final_volume_nl": 1000,
                   "final_concentration": "100"}],
        "containers": [{"id": "tube-1", "capacity_nl": 10_000}],
        "pipette": {"min_nl": 500, "max_nl": 1000, "step_nl": 100},
        "default_well_capacity_nl": 5000,
    }
    r = client.post("/inventory/plans", json=body,
                    headers={"X-Request-ID": "inv-7"})
    assert r.status_code == 201, r.text
    plan = r.json()["plan"]
    assert plan["intermediates"][0]["container"] == "tube-1"
    # pipette min 500 forces a 5000 nl intermediate batch; the plan actually
    # draws 500 nl of stock, so 500 is what gets reserved
    assert r.json()["reservations"] == [{"batch_id": "S2",
                                         "reserved_nl": 500}]


def test_inventory_frozen_visible_via_plans_get(client):
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "inv-8"})
    r = client.get("/plans/inv-8")
    assert r.status_code == 200
    assert r.json()["reservation_status"] == "reserved"


# --------------------------------------------------------------------------- #
# Confirm / cancel lifecycle
# --------------------------------------------------------------------------- #


def _reserved_plan(client, rid):
    client.post("/batches", json=BATCH)
    assert client.post("/inventory/plans", json=PLAN,
                       headers={"X-Request-ID": rid}).status_code == 201


def test_confirm_moves_reserved_to_used_and_is_final(client):
    _reserved_plan(client, "inv-c1")
    r = client.post("/inventory/plans/inv-c1/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert r.json()["changed"] is True
    batch = client.get("/batches/S1").json()
    assert (batch["reserved_nl"], batch["used_nl"]) == (0, 600)
    assert batch["available_nl"] == 400

    # repeated confirm: idempotent, balances untouched
    r = client.post("/inventory/plans/inv-c1/confirm")
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert client.get("/batches/S1").json()["used_nl"] == 600

    # a confirmed reservation can no longer be cancelled
    r = client.post("/inventory/plans/inv-c1/cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "reservation_state_conflict"
    assert client.get("/batches/S1").json()["used_nl"] == 600


def test_cancel_releases_exactly_once(client):
    _reserved_plan(client, "inv-x1")
    r = client.post("/inventory/plans/inv-x1/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert r.json()["changed"] is True
    batch = client.get("/batches/S1").json()
    assert (batch["reserved_nl"], batch["used_nl"],
            batch["available_nl"]) == (0, 0, 1000)

    # repeated cancel must not release a second time
    r = client.post("/inventory/plans/inv-x1/cancel")
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert client.get("/batches/S1").json()["available_nl"] == 1000

    # a cancelled reservation can no longer be confirmed
    assert client.post(
        "/inventory/plans/inv-x1/confirm").status_code == 409


def test_transition_unknown_reservation(client):
    assert client.post(
        "/inventory/plans/nope/confirm").status_code == 404
    assert client.post(
        "/inventory/plans/nope/cancel").status_code == 404


def test_cancel_frees_inventory_for_later_plans(client):
    _reserved_plan(client, "inv-x2")
    assert client.post("/inventory/plans", json=PLAN,
                       headers={"X-Request-ID": "inv-x3"}).status_code == 422
    client.post("/inventory/plans/inv-x2/cancel")
    assert client.post("/inventory/plans", json=PLAN,
                       headers={"X-Request-ID": "inv-x4"}).status_code == 201


def test_inventory_get_shows_live_reservation_status(client):
    _reserved_plan(client, "inv-g1")
    assert client.get("/inventory/plans/inv-g1").json()[
        "reservation_status"] == "reserved"
    client.post("/inventory/plans/inv-g1/confirm")
    r = client.get("/inventory/plans/inv-g1")
    assert r.json()["reservation_status"] == "confirmed"
    assert r.json()["reservation_items"] == [{"batch_id": "S1",
                                              "amount_nl": 600}]
    assert client.get("/inventory/plans/nope").status_code == 404


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_concurrent_plans_never_oversell(store, anyio_backend):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as ac:
        await ac.post("/batches", json=BATCH)
        results = await asyncio.gather(*[
            ac.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": f"cc-{i}"})
            for i in range(5)
        ])
        codes = sorted(r.status_code for r in results)
        assert codes == [201, 422, 422, 422, 422]
        batch = (await ac.get("/batches/S1")).json()
        assert batch["reserved_nl"] == 600  # exactly one plan reserved
        assert batch["available_nl"] == 400


@pytest.mark.anyio
async def test_concurrent_same_id_retries_reserve_once(store, anyio_backend):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as ac:
        await ac.post("/batches", json=BATCH)
        results = await asyncio.gather(*[
            ac.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "same-id"})
            for _ in range(5)
        ])
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 200, 200, 200, 201]
        payloads = {json.dumps(r.json(), sort_keys=True) for r in results}
        assert len(payloads) == 1
        assert (await ac.get("/batches/S1")).json()["reserved_nl"] == 600


@pytest.mark.anyio
async def test_concurrent_confirm_cancel_converge(store, anyio_backend):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as ac:
        await ac.post("/batches", json=BATCH)
        await ac.post("/inventory/plans", json=PLAN,
                      headers={"X-Request-ID": "race-1"})
        results = await asyncio.gather(
            ac.post("/inventory/plans/race-1/confirm"),
            ac.post("/inventory/plans/race-1/cancel"),
            ac.post("/inventory/plans/race-1/confirm"),
            ac.post("/inventory/plans/race-1/cancel"),
        )
        # exactly one call performed the transition
        assert sum(r.json().get("changed") is True for r in results) == 1
        final = (await ac.get("/inventory/plans/race-1")).json()
        status = final["reservation_status"]
        assert status in ("confirmed", "cancelled")
        batch = (await ac.get("/batches/S1")).json()
        if status == "confirmed":
            assert (batch["used_nl"], batch["reserved_nl"]) == (600, 0)
            # further confirms are idempotent, cancels conflict
            assert (await ac.post(
                "/inventory/plans/race-1/confirm")).status_code == 200
            assert (await ac.post(
                "/inventory/plans/race-1/cancel")).status_code == 409
            assert (await ac.get("/batches/S1")).json()["used_nl"] == 600
        else:
            assert (batch["used_nl"], batch["reserved_nl"],
                    batch["available_nl"]) == (0, 0, 1000)
            assert (await ac.post(
                "/inventory/plans/race-1/cancel")).status_code == 200
            assert (await ac.post(
                "/inventory/plans/race-1/confirm")).status_code == 409
            assert (await ac.get("/batches/S1")).json()[
                "available_nl"] == 1000


# --------------------------------------------------------------------------- #
# Batch recalls
# --------------------------------------------------------------------------- #


def _recall(client, batch_id, reason, code=None):
    r = client.post(f"/batches/{batch_id}/recall", json={"reason": reason})
    if code is not None:
        assert r.status_code == code, r.text
    return r


def test_recall_unknown_batch_is_error(client):
    r = _recall(client, "GHOST", "contamination")
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_batch"
    # nothing recalled: registering later and recalling works
    client.post("/batches", json=dict(BATCH, id="GHOST"))
    assert _recall(client, "GHOST", "contamination").status_code == 201


def test_recall_empty_reason_rejected(client):
    client.post("/batches", json=BATCH)
    r = client.post("/batches/S1/recall", json={"reason": "   "})
    assert r.status_code == 400


def test_recall_lists_reserved_and_confirmed_plans_with_traced_wells(client):
    client.post("/batches", json=dict(BATCH, initial_volume_nl=10_000))
    # three plans on S1: one reserved, one confirmed, one cancelled
    for rid in ("rc-r1", "rc-r2", "rc-r3"):
        client.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": rid})
    client.post("/inventory/plans/rc-r2/confirm")
    client.post("/inventory/plans/rc-r3/cancel")

    r = _recall(client, "S1", "contamination found in QC run 42")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "recalled"
    assert body["replayed"] is False
    assert body["reason"] == "contamination found in QC run 42"
    impact = {e["request_id"]: e for e in body["affected_plans"]}
    # cancelled plan is not listed
    assert sorted(impact) == ["rc-r1", "rc-r2"]
    assert impact["rc-r1"]["reservation_status"] == "reserved"
    assert impact["rc-r2"]["reservation_status"] == "confirmed"
    for entry in impact.values():
        # direct stock -> well plan; each plan/well appears once
        assert entry["wells"] == ["A1"]
        assert entry["drawn_nl"] == 600


def test_recall_traces_wells_through_intermediate_containers(client):
    client.post("/batches", json={"id": "S9", "concentration": "1000",
                                  "initial_volume_nl": 1_000_000})
    body = json.loads(open("examples/inventory.json").read())
    body["batch_ids"] = ["S9"]
    r = client.post("/inventory/plans", json=body,
                    headers={"X-Request-ID": "rc-mid1"})
    assert r.status_code == 201, r.text
    plan = r.json()["plan"]
    # the stock never fills a well directly; it is diluted first
    direct = [t for t in plan["transfers"]
              if t["source"]["kind"] == "stock"
              and t["destination"]["kind"] == "well"]
    assert direct == []

    body = _recall(client, "S9", "particulate contamination").json()
    entry, = body["affected_plans"]
    assert entry["request_id"] == "rc-mid1"
    assert sorted(entry["wells"]) == ["A1", "A2"]


def test_recall_replay_same_reason_returns_original_result(client):
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "rc-rp1"})
    r1 = _recall(client, "S1", "same reason")
    # cancel the pending reservation before the replay: the replay must still
    # return the original frozen impact snapshot
    client.post("/inventory/plans/rc-rp1/cancel")
    r2 = _recall(client, "S1", "same reason")
    assert r1.status_code == 201
    assert r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b2["replayed"] is True
    assert b2["affected_plans"] == b1["affected_plans"]
    assert b2["recalled_at"] == b1["recalled_at"]
    assert [e["request_id"] for e in b2["affected_plans"]] == ["rc-rp1"]


def test_recall_different_reason_conflicts(client):
    client.post("/batches", json=BATCH)
    _recall(client, "S1", "reason A", code=201)
    r = _recall(client, "S1", "reason B")
    assert r.status_code == 409
    assert r.json()["error"] == "recall_conflict"
    assert r.json()["recall"]["reason"] == "reason A"
    # same reason still replays
    assert _recall(client, "S1", "reason A").status_code == 200


def test_new_plan_referencing_recalled_batch_is_rejected_without_freezing(
        client):
    client.post("/batches", json=BATCH)
    _recall(client, "S1", "contamination", code=201)
    r = client.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "rc-new1"})
    assert r.status_code == 409
    assert r.json()["error"] == "batch_recalled"
    assert r.json()["recalled"] == [
        {"batch_id": "S1", "reason": "contamination"}]
    # request id was not frozen: referencing a clean batch with the same id
    # must succeed
    client.post("/batches", json={"id": "S2", "concentration": "100",
                                  "initial_volume_nl": 1000})
    body = json.loads(json.dumps(PLAN))
    body["batch_ids"] = ["S2"]
    r = client.post("/inventory/plans", json=body,
                    headers={"X-Request-ID": "rc-new1"})
    assert r.status_code == 201, r.text


def test_pending_reservation_not_confirmed_but_still_cancellable(client):
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "rc-pend1"})
    _recall(client, "S1", "contamination", code=201)

    r = client.post("/inventory/plans/rc-pend1/confirm")
    assert r.status_code == 409
    assert r.json()["error"] == "reservation_state_conflict"
    assert r.json()["recalled_batches"][0]["batch_id"] == "S1"
    # still reserved, balances untouched
    batch = client.get("/batches/S1").json()
    assert (batch["reserved_nl"], batch["used_nl"]) == (600, 0)

    # cancellation remains allowed and releases exactly once
    r = client.post("/inventory/plans/rc-pend1/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    batch = client.get("/batches/S1").json()
    assert (batch["reserved_nl"], batch["available_nl"]) == (0, 1000)
    # confirm after cancel still conflicts with the ordinary state error
    assert client.post(
        "/inventory/plans/rc-pend1/confirm").status_code == 409


def test_confirmed_reservation_keeps_original_read_and_retry_semantics(client):
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "rc-done1"})
    original = client.post("/inventory/plans", json=PLAN,
                           headers={"X-Request-ID": "rc-done1"}).json()
    client.post("/inventory/plans/rc-done1/confirm")
    _recall(client, "S1", "contamination", code=201)

    # retry of the frozen request keeps the original semantics
    r = client.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "rc-done1"})
    assert r.status_code == 200
    assert r.json() == original
    # reads keep working and report the confirmed state
    assert client.get("/inventory/plans/rc-done1").json()[
        "reservation_status"] == "confirmed"
    # repeat confirm stays idempotent, used volume unchanged
    r = client.post("/inventory/plans/rc-done1/confirm")
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert client.get("/batches/S1").json()["used_nl"] == 600


def test_batch_detail_and_list_mark_recalled(client):
    client.post("/batches", json=BATCH)
    client.post("/batches", json={"id": "S2", "concentration": "100",
                                  "initial_volume_nl": 10})
    _recall(client, "S1", "contamination")
    detail = client.get("/batches/S1").json()
    assert detail["recalled"] is True
    assert detail["recall_reason"] == "contamination"
    listing = {b["id"]: b for b in client.get("/batches").json()["batches"]}
    assert listing["S1"]["recalled"] is True
    assert listing["S2"]["recalled"] is False
    assert "recall_reason" not in listing["S2"]


def test_get_recall(client):
    client.post("/batches", json=BATCH)
    assert client.get("/batches/S1/recall").status_code == 404
    assert client.get("/batches/GHOST/recall").status_code == 404
    _recall(client, "S1", "contamination", code=201)
    r = client.get("/batches/S1/recall")
    assert r.status_code == 200
    assert r.json()["reason"] == "contamination"
    assert r.json()["affected_plans"] == []


# --------------------------------------------------------------------------- #
# Recall concurrency
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_concurrent_recall_and_plan_never_reserve_after_recall(
        store, anyio_backend):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as ac:
        await ac.post("/batches", json=BATCH)
        recall_res, plan_res = await asyncio.gather(
            ac.post("/batches/S1/recall",
                    json={"reason": "contamination"}),
            ac.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "rc-cc1"}),
        )
        assert recall_res.status_code == 201
        # the plan either won the batch lock and reserved (then it appears in
        # the recall impact), or lost and was rejected; it can never have
        # reserved against a recalled batch without being listed.
        impact = recall_res.json()["affected_plans"]
        if plan_res.status_code == 201:
            assert [e["request_id"] for e in impact] == ["rc-cc1"]
            assert impact[0]["reservation_status"] == "reserved"
            batch = (await ac.get("/batches/S1")).json()
            assert batch["reserved_nl"] == 600
            # confirming it after recall must fail
            assert (await ac.post(
                "/inventory/plans/rc-cc1/confirm")).status_code == 409
        else:
            assert plan_res.status_code == 409
            assert plan_res.json()["error"] == "batch_recalled"
            assert impact == []
            assert (await ac.get("/batches/S1")).json()["reserved_nl"] == 0


@pytest.mark.anyio
async def test_concurrent_recall_and_confirm_adjudicated_by_batch_state(
        store, anyio_backend):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as ac:
        await ac.post("/batches", json=BATCH)
        await ac.post("/inventory/plans", json=PLAN,
                      headers={"X-Request-ID": "rc-cc2"})
        recall_res, confirm_res = await asyncio.gather(
            ac.post("/batches/S1/recall",
                    json={"reason": "contamination"}),
            ac.post("/inventory/plans/rc-cc2/confirm"),
        )
        batch = (await ac.get("/batches/S1")).json()
        if confirm_res.status_code == 200 and confirm_res.json()["changed"]:
            # confirm won the lock first; the recall still lists it confirmed
            assert batch["used_nl"] == 600
            entry, = recall_res.json()["affected_plans"]
            assert entry["reservation_status"] == "confirmed"
            # further confirm remains idempotent
            assert (await ac.post(
                "/inventory/plans/rc-cc2/confirm")).json()["changed"] is False
        else:
            # recall won: confirmation blocked, cancellation still possible
            assert confirm_res.status_code == 409
            assert (batch["reserved_nl"], batch["used_nl"]) == (600, 0)
            assert (await ac.post(
                "/inventory/plans/rc-cc2/cancel")).status_code == 200
            assert (await ac.get("/batches/S1")).json()[
                "available_nl"] == 1000


# --------------------------------------------------------------------------- #
# Well tracing (unit)
# --------------------------------------------------------------------------- #


def test_well_tracing_dedupes_and_follows_chains():
    plan = {
        "transfers": [
            {"step": 1, "source": {"kind": "stock", "id": "S1"},
             "destination": {"kind": "intermediate", "id": "I0",
                             "container": "t1"},
             "volume_nl": 500},
            {"step": 2, "source": {"kind": "diluent"},
             "destination": {"kind": "intermediate", "id": "I0",
                             "container": "t1"},
             "volume_nl": 500},
            {"step": 3, "source": {"kind": "intermediate", "id": "I0",
                                   "container": "t1"},
             "destination": {"kind": "intermediate", "id": "I1",
                             "container": "t2"},
             "volume_nl": 500},
            {"step": 4, "source": {"kind": "diluent"},
             "destination": {"kind": "intermediate", "id": "I1",
                             "container": "t2"},
             "volume_nl": 500},
            {"step": 5, "source": {"kind": "intermediate", "id": "I1",
                                   "container": "t2"},
             "destination": {"kind": "well", "id": "A1"},
             "volume_nl": 300},
            {"step": 6, "source": {"kind": "intermediate", "id": "I0",
                                   "container": "t1"},
             "destination": {"kind": "well", "id": "A2"},
             "volume_nl": 300},
            # well reached twice (split pipette steps) must list once
            {"step": 7, "source": {"kind": "intermediate", "id": "I1",
                                   "container": "t2"},
             "destination": {"kind": "well", "id": "A1"},
             "volume_nl": 100},
            # a well fed only by diluent must not be attributed to the stock
            {"step": 8, "source": {"kind": "diluent"},
             "destination": {"kind": "well", "id": "A3"},
             "volume_nl": 300},
        ],
        "intermediates": [],
        "wells": [],
    }
    assert wells_for_batch_in_plan(plan, "S1") == ["A1", "A2"]
    assert wells_for_batch_in_plan(plan, "OTHER") == []

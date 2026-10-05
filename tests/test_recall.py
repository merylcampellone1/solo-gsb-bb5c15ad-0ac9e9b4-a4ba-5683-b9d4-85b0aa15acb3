"""Batch recall tests: irrevocable recalls, affected-plan/well tracing,
rejection of new reservations, blocked confirms, and concurrency.

Uses the same in-memory stand-in as test_inventory.py (its check-and-set fakes
are atomic under asyncio; the real SQL implementation uses one transaction
with batch rows locked FOR UPDATE before reservation rows).
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.planning import traced_wells_for_batch
from tests.test_inventory import (
    BATCH,
    PLAN,
    FakePool,
    FakeStore,
)
import app.db as db
import app.main as main


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
        ("recall_batch", st.recall_batch),
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


# A serial-dilution scenario: stock 1000 -> intermediate 400 -> A1 and
# intermediate 100 -> A2, so the stock reaches the wells only THROUGH the
# intermediate liquids.
SERIAL = {
    "batch_ids": ["S1"],
    "diluent": {"available_nl": 10_000_000},
    "wells": [
        {"id": "A1", "final_volume_nl": 1000, "final_concentration": "400"},
        {"id": "A2", "final_volume_nl": 1000, "final_concentration": "100"},
    ],
    "containers": [
        {"id": "tube-1", "capacity_nl": 10_000},
        {"id": "tube-2", "capacity_nl": 20_000},
    ],
    "pipette": {"min_nl": 500, "max_nl": 1000, "step_nl": 100},
    "default_well_capacity_nl": 2000,
}

RECALL_BODY = {"reason": "fungal contamination detected in QC aliquot"}


def _recall(client, bid="S1", body=None):
    return client.post(f"/batches/{bid}/recall", json=body or RECALL_BODY)


# --------------------------------------------------------------------------- #
# Pure tracing helper
# --------------------------------------------------------------------------- #


def test_traced_wells_helper_follows_chains_and_dedupes():
    # Transfers are topologically ordered (an intermediate is produced before
    # it is consumed), exactly as the solver emits them.
    plan = {
        "transfers": [
            {"step": 1, "source": {"kind": "stock", "id": "S1"},
             "destination": {"kind": "intermediate", "id": "I1",
                             "container": "C0"}, "volume_nl": 500},
            {"step": 2, "source": {"kind": "diluent"},
             "destination": {"kind": "intermediate", "id": "I1",
                             "container": "C0"}, "volume_nl": 500},
            {"step": 3, "source": {"kind": "stock", "id": "S1"},
             "destination": {"kind": "intermediate", "id": "I2",
                             "container": "C1"}, "volume_nl": 200},
            # two S1-fed intermediates converge on the same well
            {"step": 4, "source": {"kind": "intermediate", "id": "I1",
                                   "container": "C0"},
             "destination": {"kind": "well", "id": "W1"}, "volume_nl": 300},
            {"step": 5, "source": {"kind": "intermediate", "id": "I2",
                                   "container": "C1"},
             "destination": {"kind": "well", "id": "W1"}, "volume_nl": 300},
            {"step": 6, "source": {"kind": "stock", "id": "OTHER"},
             "destination": {"kind": "well", "id": "W2"}, "volume_nl": 100},
        ],
    }
    # W1 reached (twice, via two intermediates) but listed once; W2 is fed by
    # a different batch and must be absent.
    assert traced_wells_for_batch(plan, "S1") == ["W1"]
    assert traced_wells_for_batch(plan, "OTHER") == ["W2"]


# --------------------------------------------------------------------------- #
# Recall basics
# --------------------------------------------------------------------------- #


def test_recall_unknown_batch_is_404(client):
    r = _recall(client, "NOPE")
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_batch"


def test_recall_reason_validation(client):
    client.post("/batches", json=BATCH)
    assert client.post("/batches/S1/recall", json={"reason": "   "}) \
        .status_code == 400
    assert client.post("/batches/S1/recall", json={}).status_code == 400
    assert client.post(
        "/batches/S1/recall", json={"reason": "x" * 1001}).status_code == 400
    # batch was not recalled by the rejected calls
    assert client.get("/batches/S1").json()["recalled"] is False


def test_recall_clean_batch_empty_report(client):
    client.post("/batches", json=BATCH)
    r = _recall(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch_id"] == "S1"
    assert body["reason"] == RECALL_BODY["reason"]
    assert body["recalled_at"]
    assert body["report"]["affected_plan_count"] == 0
    assert body["report"]["plans"] == []
    assert client.get("/batches/S1").json()["recalled"] is True
    assert client.get("/batches/S1").json()["recalled_reason"] == \
        RECALL_BODY["reason"]
    assert client.get("/batches").json()["batches"][0]["recalled"] is True


def test_repeated_recall_same_reason_replays_original(client):
    client.post("/batches", json=BATCH)
    r1 = _recall(client)
    r2 = client.post("/batches/S1/recall",
                     json={"reason": RECALL_BODY["reason"]})
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_recall_different_reason_conflicts_irreversibly(client):
    client.post("/batches", json=BATCH)
    assert _recall(client).status_code == 201
    r = client.post("/batches/S1/recall",
                    json={"reason": "actually mislabeled"})
    assert r.status_code == 409
    assert r.json()["error"] == "recall_reason_conflict"
    assert r.json()["existing_reason"] == RECALL_BODY["reason"]
    # original recall is unchanged; same-reason still replays it
    assert _recall(client).status_code == 200


# --------------------------------------------------------------------------- #
# Affected plans / target-well tracing
# --------------------------------------------------------------------------- #


def test_recall_report_traces_through_intermediates_to_wells(client):
    client.post("/batches", json={"id": "S1", "concentration": "1000",
                                  "initial_volume_nl": 1_000_000})
    r = client.post("/inventory/plans", json=SERIAL,
                    headers={"X-Request-ID": "rc-serial"})
    assert r.status_code == 201, r.text
    plan = r.json()["plan"]
    # sanity: the stock never fills a well directly
    direct = [t for t in plan["transfers"]
              if t["source"]["kind"] == "stock"
              and t["destination"]["kind"] == "well"]
    assert not direct
    assert traced_wells_for_batch(plan, "S1") == ["A1", "A2"]
    assert traced_wells_for_batch(plan, "OTHER") == []

    body = _recall(client).json()["report"]
    assert body["affected_plan_count"] == 1
    entry = body["plans"][0]
    assert entry["request_id"] == "rc-serial"
    assert entry["reservation_status"] == "reserved"
    assert entry["wells"] == ["A1", "A2"]  # each well listed once
    assert entry["drawn_nl"] > 0


def test_recall_report_lists_only_reserved_and_confirmed(client):
    client.post("/batches", json={"id": "S1", "concentration": "1000",
                                  "initial_volume_nl": 1_000_000})
    # reserved
    client.post("/inventory/plans", json=SERIAL,
                headers={"X-Request-ID": "p-reserved"})
    # confirmed
    client.post("/inventory/plans", json=SERIAL,
                headers={"X-Request-ID": "p-confirmed"})
    client.post("/inventory/plans/p-confirmed/confirm")
    # cancelled -> must be excluded
    client.post("/inventory/plans", json=SERIAL,
                headers={"X-Request-ID": "p-cancelled"})
    client.post("/inventory/plans/p-cancelled/cancel")

    report = _recall(client).json()["report"]
    by_id = {p["request_id"]: p for p in report["plans"]}
    assert report["affected_plan_count"] == 2
    assert set(by_id) == {"p-reserved", "p-confirmed"}
    assert by_id["p-reserved"]["reservation_status"] == "reserved"
    assert by_id["p-confirmed"]["reservation_status"] == "confirmed"
    assert "p-cancelled" not in by_id
    for p in by_id.values():
        assert p["wells"] == ["A1", "A2"]


def test_recall_report_same_plan_same_well_once(client):
    # multiple transfers may converge on a well via different intermediates;
    # the well still appears exactly once
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "p1"})
    entry = _recall(client).json()["report"]["plans"][0]
    assert entry["wells"] == ["A1"]
    assert len(entry["wells"]) == len(set(entry["wells"]))


def test_get_recall_endpoint(client):
    client.post("/batches", json=BATCH)
    assert client.get("/batches/S1/recall").status_code == 404  # not recalled
    assert client.get("/batches/NOPE/recall").status_code == 404  # unknown
    first = _recall(client).json()
    got = client.get("/batches/S1/recall")
    assert got.status_code == 200
    assert got.json() == first


# --------------------------------------------------------------------------- #
# Post-recall semantics
# --------------------------------------------------------------------------- #


def test_new_request_referencing_recalled_batch_rejected_not_frozen(client):
    client.post("/batches", json=BATCH)
    assert _recall(client).status_code == 201
    r = client.post("/inventory/plans", json=PLAN,
                    headers={"X-Request-ID": "after-rc-1"})
    assert r.status_code == 409
    assert r.json()["error"] == "batch_recalled"
    assert r.json()["recalled_batches"] == ["S1"]
    # request id was not frozen: no 404/409 collision when reading plans
    assert client.get("/plans/after-rc-1").status_code == 404
    # and no reservation row exists either
    assert client.get("/inventory/plans/after-rc-1").status_code == 404


def test_recalled_batch_in_multi_batch_request_rejected(client):
    client.post("/batches", json={"id": "S1", "concentration": "100",
                                  "initial_volume_nl": 1_000_000})
    client.post("/batches", json={"id": "S2", "concentration": "50",
                                  "initial_volume_nl": 1_000_000})
    client.post("/batches/S2/recall", json=RECALL_BODY)
    body = {
        "batch_ids": ["S1", "S2"],
        "diluent": {"available_nl": 10_000_000},
        "wells": [{"id": "A1", "final_volume_nl": 600,
                   "final_concentration": "60"}],
        "containers": [],
        "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
        "default_well_capacity_nl": 5000,
    }
    r = client.post("/inventory/plans", json=body,
                    headers={"X-Request-ID": "mb-1"})
    assert r.status_code == 409
    assert r.json()["error"] == "batch_recalled"
    assert "S2" in r.json()["recalled_batches"]


def test_pending_reservation_cannot_confirm_but_can_cancel(client):
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "pend-1"})
    _recall(client)

    r = client.post("/inventory/plans/pend-1/confirm")
    assert r.status_code == 409
    assert r.json()["error"] == "batch_recalled"
    assert r.json()["status"] == "reserved"
    # still pending: nothing moved to used
    batch = client.get("/batches/S1").json()
    assert (batch["reserved_nl"], batch["used_nl"]) == (600, 0)

    # cancel still works and releases exactly once
    r = client.post("/inventory/plans/pend-1/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert client.get("/batches/S1").json()["available_nl"] == 1000
    r = client.post("/inventory/plans/pend-1/cancel")
    assert r.status_code == 200 and r.json()["changed"] is False
    # confirm after cancel keeps the original state-conflict semantics
    assert client.post(
        "/inventory/plans/pend-1/confirm").status_code == 409


def test_confirmed_result_keeps_original_semantics(client):
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "done-1"})
    client.post("/inventory/plans/done-1/confirm")
    _recall(client)
    # already confirmed: confirm replay stays idempotent 200, cancel stays 409
    r = client.post("/inventory/plans/done-1/confirm")
    assert r.status_code == 200
    assert r.json()["changed"] is False
    r = client.post("/inventory/plans/done-1/cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "reservation_state_conflict"
    assert client.get("/batches/S1").json()["used_nl"] == 600
    # frozen reads are untouched
    assert client.get("/plans/done-1").status_code == 200
    live = client.get("/inventory/plans/done-1").json()
    assert live["reservation_status"] == "confirmed"


def test_pre_recall_frozen_request_replays_original(client):
    # a request frozen (201) before the recall keeps its replay semantics
    client.post("/batches", json=BATCH)
    first = client.post("/inventory/plans", json=PLAN,
                        headers={"X-Request-ID": "pre-1"})
    assert first.status_code == 201
    _recall(client)
    # reusing the SAME request id with the SAME body replays the frozen plan
    again = client.post("/inventory/plans", json=PLAN,
                        headers={"X-Request-ID": "pre-1"})
    assert again.status_code == 200
    assert again.json() == first.json()
    # same id, different body still conflicts
    changed = json.loads(json.dumps(PLAN))
    changed["wells"][0]["final_volume_nl"] = 500
    conflict = client.post("/inventory/plans", json=changed,
                           headers={"X-Request-ID": "pre-1"})
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "idempotency_conflict"


def test_cancelled_before_recall_stays_releasable_semantics(client):
    client.post("/batches", json=BATCH)
    client.post("/inventory/plans", json=PLAN,
                headers={"X-Request-ID": "cx-1"})
    client.post("/inventory/plans/cx-1/cancel")
    _recall(client)
    # idempotent cancel replay: 200 changed=false; confirm: 409 state
    r = client.post("/inventory/plans/cx-1/cancel")
    assert r.status_code == 200 and r.json()["changed"] is False
    r = client.post("/inventory/plans/cx-1/confirm")
    assert r.status_code == 409
    assert r.json()["error"] == "reservation_state_conflict"


def test_plain_plans_mode_unaffected_by_recall(client):
    client.post("/batches", json=BATCH)
    _recall(client)
    body = {
        "stocks": [{"id": "S1", "concentration": "100",
                    "available_nl": 1_000_000}],
        "diluent": {"available_nl": 10_000_000},
        "wells": [{"id": "A1", "final_volume_nl": 600,
                   "final_concentration": "100"}],
        "containers": [],
        "pipette": {"min_nl": 100, "max_nl": 1000, "step_nl": 50},
        "default_well_capacity_nl": 5000,
    }
    r = client.post("/plans", json=body,
                    headers={"X-Request-ID": "plain-1"})
    assert r.status_code == 201


# --------------------------------------------------------------------------- #
# Concurrency: recall vs plan creation / confirmation
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_concurrent_recall_and_plan_creation(store, anyio_backend):
    # a batch large enough that every plan would succeed absent the recall
    await store.create_batch(None, "S1", "100", 10_000_000)
    transport = httpx.ASGITransport(app=main.app)

    async def plan(i):
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as ac:
            return await ac.post("/inventory/plans", json=PLAN,
                                 headers={"X-Request-ID": f"rp-{i}"})

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as ac:
        results = await asyncio.gather(
            *[plan(i) for i in range(10)],
            ac.post("/batches/S1/recall", json=RECALL_BODY),
            *[plan(i) for i in range(10, 20)],
        )
    plan_results = results[:10] + results[11:]
    codes = sorted(r.status_code for r in plan_results)
    assert set(codes) <= {201, 409}
    assert 409 in codes  # post-recall requests were rejected
    assert all(r.json().get("error") == "batch_recalled"
               for r in plan_results if r.status_code == 409)
    b = store._pub(store.batches["S1"])
    assert b["recalled"] is True
    # each accepted plan reserved exactly once; no post-recall reservation
    accepted = [r for r in plan_results if r.status_code == 201]
    assert b["reserved_nl"] == 600 * len(accepted)
    # every accepted (still reserved/confirmed) plan is in the recall report
    accepted_rids = {r.request.headers["x-request-id"] for r in accepted}
    recalled = await store.get_recall(None, "S1")
    report_ids = {p["request_id"] for p in recalled["report"]["plans"]}
    assert report_ids == accepted_rids
    assert all(p["reservation_status"] in ("reserved", "confirmed")
               for p in recalled["report"]["plans"])


@pytest.mark.anyio
async def test_concurrent_recall_and_confirm(store, anyio_backend):
    await store.create_batch(None, "S1", "100", 1000)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as ac:
        await ac.post("/inventory/plans", json=PLAN,
                      headers={"X-Request-ID": "rcf-1"})
        results = await asyncio.gather(
            ac.post("/inventory/plans/rcf-1/confirm"),
            ac.post("/batches/S1/recall", json=RECALL_BODY),
        )
        confirm_res, recall_res = results
        b = store._pub(store.batches["S1"])
        if confirm_res.status_code == 200 and confirm_res.json()["changed"]:
            # confirm linearized first: used volume recorded; report shows the
            # confirmed reservation
            assert (b["used_nl"], b["reserved_nl"]) == (600, 0)
            report = recall_res.json()["report"]
            assert report["plans"][0]["request_id"] == "rcf-1"
            assert report["plans"][0]["reservation_status"] == "confirmed"
            # later confirm replay stays idempotent; the past result is final
            again = await ac.post("/inventory/plans/rcf-1/confirm")
            assert again.status_code == 200 and again.json()["changed"] is False
        else:
            # recall linearized first: confirm is blocked, reservation
            # pending; cancel still possible
            assert confirm_res.status_code == 409
            assert confirm_res.json()["error"] == "batch_recalled"
            assert (b["used_nl"], b["reserved_nl"]) == (0, 600)
            report = recall_res.json()["report"]
            assert report["plans"][0]["reservation_status"] == "reserved"
            cancel = await ac.post("/inventory/plans/rcf-1/cancel")
            assert cancel.status_code == 200
            assert store._pub(store.batches["S1"])["available_nl"] == 1000

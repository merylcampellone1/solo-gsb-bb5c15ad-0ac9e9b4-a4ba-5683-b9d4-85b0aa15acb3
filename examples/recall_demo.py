#!/usr/bin/env python3
"""Batch recall demo using only the standard library.

Registers a stock batch, creates a reserved / confirmed / cancelled plan,
then irrevocably recalls the batch and demonstrates every recall semantic:

* first recall -> 201 with affected plans and traced wells;
* same reason repeats -> 200 with the original result;
* different reason -> 409; unknown batch -> 404;
* new plans referencing the batch are rejected without freezing the request id;
* pending reservations cannot be confirmed but can still be cancelled;
* confirmed plans keep their original read/retry semantics.

Usage:
    python examples/recall_demo.py [base_url]
Default base URL: http://localhost:8000
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

PLAN = {
    "batch_ids": ["RC1"],
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


def call(path, body=None, request_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if request_id:
        headers["X-Request-ID"] = request_id
    data = json.dumps(body).encode() if body is not None else None
    if method is None:
        method = "POST" if data is not None or path.endswith(
            ("/confirm", "/cancel")) else "GET"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(label, status, body, keys=()):
    print(f"{label}: HTTP {status}")
    for k in keys:
        print(f"   {k}: {body.get(k)!r}")


print("1) register batch RC1:")
show("   ", *call("/batches", {"id": "RC1", "concentration": "1000",
                               "initial_volume_nl": 10_000_000}),
     keys=("id", "available_nl"))

print("2) three plans: reserved, confirmed, cancelled:")
for rid in ("rc-keep", "rc-done", "rc-drop"):
    status, _ = call("/inventory/plans", PLAN, request_id=rid)
    print(f"   plan {rid}: HTTP {status}")
call("/inventory/plans/rc-done/confirm")
call("/inventory/plans/rc-drop/cancel")

print("3) irrevocable recall:")
status, body = call("/batches/RC1/recall",
                    {"reason": "contamination found in QC run 42"})
print(f"   HTTP {status}; {len(body['affected_plans'])} affected plan(s):")
for entry in body["affected_plans"]:
    print(f"     - request {entry['request_id']}: "
          f"status={entry['reservation_status']}, wells={entry['wells']}, "
          f"drawn_nl={entry['drawn_nl']}")
assert [e["request_id"] for e in body["affected_plans"]] == ["rc-done", "rc-keep"]
assert all(e["wells"] == ["A1", "A2"] for e in body["affected_plans"])

print("4) repeat with the SAME reason returns the original result:")
status, again = call("/batches/RC1/recall",
                     {"reason": "contamination found in QC run 42"})
print(f"   HTTP {status}, replayed={again['replayed']}, "
      f"identical={again['affected_plans'] == body['affected_plans']}")
assert status == 200 and again["replayed"] is True

print("5) repeat with a DIFFERENT reason conflicts:")
status, body = call("/batches/RC1/recall", {"reason": "other reason"})
print(f"   HTTP {status}, error={body['error']}, "
      f"existing_reason={body['recall']['reason']!r}")
assert status == 409 and body["error"] == "recall_conflict"

print("6) unknown batch:")
status, body = call("/batches/NOPE/recall", {"reason": "x"})
print(f"   HTTP {status}, error={body['error']}")
assert status == 404 and body["error"] == "unknown_batch"

print("7) new plan referencing the recalled batch is rejected:")
status, body = call("/inventory/plans", PLAN, request_id="rc-after")
print(f"   HTTP {status}, error={body['error']}")
assert status == 409 and body["error"] == "batch_recalled"

print("   the request id was NOT frozen: same id against clean batch RC2:")
call("/batches", {"id": "RC2", "concentration": "1000",
                  "initial_volume_nl": 10_000_000})
clean = json.loads(json.dumps(PLAN))
clean["batch_ids"] = ["RC2"]
status, _ = call("/inventory/plans", clean, request_id="rc-after")
print(f"   HTTP {status}")
assert status == 201

print("8) pending reservation cannot be confirmed after recall:")
status, body = call("/inventory/plans/rc-keep/confirm")
print(f"   HTTP {status}, error={body['error']}, "
      f"recalled={[b['batch_id'] for b in body['recalled_batches']]}")
assert status == 409

print("   ...but it can still be cancelled:")
status, body = call("/inventory/plans/rc-keep/cancel")
print(f"   HTTP {status}, status={body['status']}")
assert status == 200 and body["status"] == "cancelled"

print("9) confirmed plan keeps original retry/read semantics:")
status, body = call("/inventory/plans", PLAN, request_id="rc-done")
print(f"   retry -> HTTP {status} (200 = original frozen result)")
assert status == 200
status, body = call("/inventory/plans/rc-done/confirm")
print(f"   repeat confirm -> HTTP {status}, changed={body['changed']}")
assert status == 200 and body["changed"] is False

print("10) GET the recall record:")
status, body = call("/batches/RC1/recall")
print(f"   HTTP {status}, reason={body['reason']!r}, "
      f"{len(body['affected_plans'])} affected plan(s)")
assert status == 200
print("\nALL RECALL DEMO STEPS PASSED")

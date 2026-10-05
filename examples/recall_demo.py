#!/usr/bin/env python3
"""Batch-recall demo using only the standard library.

Sets up one pending, one confirmed and one cancelled reservation, recalls the
batch, then exercises every recall rule (replay/conflict/unknown, rejection
of new requests without freezing the request id, blocked confirm but allowed
cancel, unchanged confirmed semantics).

Usage:
    python examples/recall_demo.py [base_url]
Default base URL: http://localhost:8000
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
REASON = "QC aliquot tested positive for contamination"

with open("examples/inventory.json", encoding="utf-8") as fh:
    PLAN_BODY = json.load(fh)


def call(path, body=None, request_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if request_id:
        headers["X-Request-ID"] = request_id
    data = json.dumps(body).encode() if body is not None else None
    if method is None:
        method = "POST" if (body is not None
                            or path.endswith("/confirm")
                            or path.endswith("/cancel")) else "GET"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


print("1) register batch and prepare three reservations:")
status, _ = call("/batches", {"id": "S1", "concentration": "1000",
                              "initial_volume_nl": 1_000_000})
print("   register ->", status)
call("/inventory/plans", PLAN_BODY, request_id="recall-demo-A")  # reserved
call("/inventory/plans", PLAN_BODY, request_id="recall-demo-B")
call("/inventory/plans/recall-demo-B/confirm")                   # confirmed
call("/inventory/plans", PLAN_BODY, request_id="recall-demo-C")
call("/inventory/plans/recall-demo-C/cancel")                    # cancelled
print("   A=reserved, B=confirmed, C=cancelled")

print("2) irrevocable recall (201) with the frozen impact report:")
status, rec = call("/batches/S1/recall", {"reason": REASON})
print("   HTTP", status)
for p in rec["report"]["plans"]:
    print(f"   plan {p['request_id']}: {p['reservation_status']:<9} "
          f"drawn={p['drawn_nl']} wells={p['wells']}")
print("   (cancelled plan C is absent; wells traced via intermediates)")

print("3) same reason again replays the original result -> 200:")
status, again = call("/batches/S1/recall", {"reason": REASON})
print("   HTTP", status, "identical:", again == rec)

print("4) different reason conflicts -> 409; unknown batch -> 404:")
status, body = call("/batches/S1/recall", {"reason": "labelling error"})
print("   HTTP", status, body["error"])
status, body = call("/batches/NOPE/recall", {"reason": "x"})
print("   HTTP", status, body["error"])

print("5) GET the frozen recall record:")
status, _ = call("/batches/S1/recall")
print("   HTTP", status)

print("6) a NEW plan referencing the batch is rejected, id not frozen:")
status, body = call("/inventory/plans", PLAN_BODY,
                    request_id="recall-demo-new")
print("   HTTP", status, body["error"])
status, _ = call("/plans/recall-demo-new")
print("   GET /plans/recall-demo-new ->", status, "(404 = not frozen)")

print("7) pending A: confirm blocked (409 batch_recalled), cancel allowed:")
status, body = call("/inventory/plans/recall-demo-A/confirm")
print("   confirm ->", status, body["error"])
status, body = call("/inventory/plans/recall-demo-A/cancel")
print("   cancel  ->", status, body["status"], "changed:", body["changed"])

print("8) already-confirmed B keeps its original semantics:")
status, body = call("/inventory/plans/recall-demo-B/confirm")
print("   confirm replay ->", status, "changed:", body["changed"])
status, body = call("/inventory/plans/recall-demo-B/cancel")
print("   cancel ->", status, body["error"])

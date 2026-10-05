#!/usr/bin/env python3
"""Inventory-mode demo using only the standard library.

Registers a stock batch, plans against it (reserving the drawn volume),
shows idempotent retries, then confirms and cancels reservations.

Usage:
    python examples/inventory_demo.py [base_url]
Default base URL: http://localhost:8000
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

with open("examples/inventory.json", encoding="utf-8") as fh:
    PLAN_BODY = json.load(fh)

BATCH = {"id": "S1", "concentration": "1000", "initial_volume_nl": 1_000_000}
RID = "inv-demo-001"


def call(path, body=None, request_id=None, method=None):
    headers = {"Content-Type": "application/json"}
    if request_id:
        headers["X-Request-ID"] = request_id
    data = json.dumps(body).encode() if body is not None else None
    if method is None:
        method = "POST" if body is not None or "/confirm" in path \
            or "/cancel" in path else "GET"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


print("1) register batch:")
status, batch = call("/batches", BATCH)
print("   HTTP", status, batch)

print("2) identical re-registration (idempotent):")
status, _ = call("/batches", BATCH)
print("   HTTP", status, "(200 = already registered with same content)")

print("3) inventory plan (reserves stock atomically):")
status, plan = call("/inventory/plans", PLAN_BODY, request_id=RID)
print("   HTTP", status)
print("   reservations:", plan["reservations"])
print("   reservation_status:", plan["reservation_status"])
for t in plan["plan"]["transfers"]:
    print(f"   {t['step']:2d}. {t['source']['kind']:>12} -> "
          f"{t['destination']['kind']:<13} {t['volume_nl']:>5} nl")

print("4) identical retry returns the original result:")
status, again = call("/inventory/plans", PLAN_BODY, request_id=RID)
print("   HTTP", status, "same payload:", again == plan)

print("5) batch balances after reservation:")
status, batch = call("/batches/S1")
print("   HTTP", status, {k: batch[k] for k in
                           ("reserved_nl", "used_nl", "available_nl")})

print("6) confirm the reservation (reserved -> used):")
status, res = call(f"/inventory/plans/{RID}/confirm")
print("   HTTP", status, res["status"], "changed:", res["changed"])
status, res = call(f"/inventory/plans/{RID}/confirm")
print("   repeated confirm -> HTTP", status, "changed:", res["changed"])

print("7) cancel after confirm is rejected:")
status, res = call(f"/inventory/plans/{RID}/cancel")
print("   HTTP", status, res["error"])

print("8) second plan, then cancel releases the reservation exactly once:")
status, _ = call("/inventory/plans", PLAN_BODY, request_id="inv-demo-002")
status, res = call("/inventory/plans/inv-demo-002/cancel")
print("   cancel -> HTTP", status, res["status"], "changed:", res["changed"])
status, res = call("/inventory/plans/inv-demo-002/cancel")
print("   repeated cancel -> HTTP", status, "changed:", res["changed"])

print("9) final batch balances:")
status, batch = call("/batches/S1")
print("   HTTP", status, {k: batch[k] for k in
                           ("reserved_nl", "used_nl", "available_nl")})

#!/usr/bin/env python3
"""Call the planner HTTP API using only the standard library.

Usage:
    python examples/call_api.py [base_url]
Default base URL: http://localhost:8000
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

with open("examples/request.json", encoding="utf-8") as fh:
    body = json.load(fh)

REQUEST_ID = "demo-run-001"


def call(body, request_id, method="POST", path="/plans"):
    url = BASE + path
    headers = {"Content-Type": "application/json", "X-Request-ID": request_id}
    data = json.dumps(body).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


print("1) create:", )
status, plan = call(body, REQUEST_ID)
print("   HTTP", status)
print("   wells:", [(w["id"], w["final_concentration"])
                    for w in plan["plan"]["wells"]])
for t in plan["plan"]["transfers"]:
    src = t["source"]
    dst = t["destination"]
    print(f"   {t['step']:2d}. {src['kind']:>12} -> {dst['kind']:<13} "
          f"{t['volume_nl']:>5} nl")

print("2) identical retry:")
status, _ = call(body, REQUEST_ID)
print("   HTTP", status, "(200 = frozen plan returned)")

print("3) conflicting body with the same X-Request-ID:")
changed = json.loads(json.dumps(body))
changed["wells"][0]["final_concentration"] = "250"
status, payload = call(changed, REQUEST_ID)
print("   HTTP", status, payload)

print("4) infeasible request (stock overdraft):")
bad = json.loads(json.dumps(body))
bad["stocks"][0]["available_nl"] = 100
status, payload = call(bad, "demo-run-bad")
print("   HTTP", status)
print("   blocked:", payload["blocked"])
print("   constraints:", payload["constraints"])

print("5) invalid request (float concentration):")
invalid = json.loads(json.dumps(body))
invalid["wells"][0]["final_concentration"] = 0.5
status, payload = call(invalid, "demo-run-invalid")
print("   HTTP", status, payload["error"])

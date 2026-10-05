#!/usr/bin/env bash
# Inventory-mode demo against a locally running service (docker compose up).
#
# Walks the full lifecycle: register a stock batch, plan against it (which
# atomically reserves the drawn volume), exercise idempotent retries and
# conflicts, then confirm / cancel the reservation.
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
RID="inv-demo-001"

echo "== register batch S1 (1000 uM, 1 mL) =="
curl -sS -X POST "$BASE/batches" \
  -H "Content-Type: application/json" \
  -d '{"id": "S1", "concentration": "1000", "initial_volume_nl": 1000000}' \
  | python3 -m json.tool

echo "== identical re-registration -> 200 (idempotent) =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/batches" \
  -H "Content-Type: application/json" \
  -d '{"id": "S1", "concentration": "1000", "initial_volume_nl": 1000000}'

echo "== same id, different content -> 409 =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/batches" \
  -H "Content-Type: application/json" \
  -d '{"id": "S1", "concentration": "500", "initial_volume_nl": 1000000}'

echo "== inventory plan: draws from batch S1 and reserves it atomically =="
curl -sS -X POST "$BASE/inventory/plans" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID" \
  --data @examples/inventory.json | python3 -m json.tool

echo "== identical retry -> 200, original result, no double reservation =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/inventory/plans" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID" \
  --data @examples/inventory.json

echo "== same X-Request-ID, different body -> 409 =="
python3 - <<'PY'
import json, urllib.request, urllib.error
body = json.load(open("examples/inventory.json"))
body["wells"][0]["final_concentration"] = "250"
req = urllib.request.Request(
    "http://localhost:8000/inventory/plans",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json",
             "X-Request-ID": "inv-demo-001"},
    method="POST",
)
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print("status:", e.code)
    print(e.read().decode())
PY

echo "== batch now shows the reserved volume =="
curl -sS "$BASE/batches/S1" | python3 -m json.tool

echo "== confirm the reservation: reserved -> used (final) =="
curl -sS -X POST "$BASE/inventory/plans/$RID/confirm" | python3 -m json.tool

echo "== confirm again -> 200 idempotent, changed=false =="
curl -sS -X POST "$BASE/inventory/plans/$RID/confirm" | python3 -m json.tool

echo "== cancel after confirm -> 409 =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST \
  "$BASE/inventory/plans/$RID/cancel"

echo "== a second plan, then cancel: the reservation is released exactly once =="
curl -sS -X POST "$BASE/inventory/plans" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: inv-demo-002" \
  --data @examples/inventory.json > /dev/null
curl -sS -X POST "$BASE/inventory/plans/inv-demo-002/cancel" | python3 -m json.tool
echo "-- repeated cancel: changed=false, balances unchanged --"
curl -sS -X POST "$BASE/inventory/plans/inv-demo-002/cancel" | python3 -m json.tool

echo "== live view of the first plan (confirmed) =="
curl -sS "$BASE/inventory/plans/$RID" | python3 -m json.tool

echo "== unknown batch reference -> 400, nothing reserved =="
python3 - <<'PY'
import json, urllib.request, urllib.error
body = json.load(open("examples/inventory.json"))
body["batch_ids"] = ["NOPE"]
req = urllib.request.Request(
    "http://localhost:8000/inventory/plans",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json",
             "X-Request-ID": "inv-demo-003"},
    method="POST",
)
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print("status:", e.code)
    print(e.read().decode())
PY

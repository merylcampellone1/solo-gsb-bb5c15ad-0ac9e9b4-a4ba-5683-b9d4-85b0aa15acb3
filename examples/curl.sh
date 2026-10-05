#!/usr/bin/env bash
# Example calls against a locally running service (docker compose up).
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
RID="demo-run-001"

echo "== health =="
curl -sS "$BASE/health"
echo

echo "== create plan (feasible) =="
curl -sS -X POST "$BASE/plans" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID" \
  --data @examples/request.json | python3 -m json.tool

echo "== retry with the same X-Request-ID and body -> 200 frozen plan =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/plans" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID" \
  --data @examples/request.json

echo "== fetch the frozen plan =="
curl -sS "$BASE/plans/$RID" | python3 -m json.tool

echo "== same X-Request-ID, different body -> 409 conflict =="
python3 - <<'PY'
import json, urllib.request, urllib.error
body = json.load(open("examples/request.json"))
body["wells"][0]["final_concentration"] = "250"
req = urllib.request.Request(
    "http://localhost:8000/plans",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "X-Request-ID": "demo-run-001"},
    method="POST",
)
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print("status:", e.code)
    print(e.read().decode())
PY

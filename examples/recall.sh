#!/usr/bin/env bash
# Batch recall demo against a locally running service (docker compose up).
#
# Scenario:
#   1. register stock batch RC1 and create three inventory plans against it:
#      one kept reserved, one confirmed, one cancelled;
#   2. irrevocably recall RC1 with a reason -> 201, including the list of
#      affected plans and the target wells traced through intermediate
#      containers, each plan/well listed once with its reservation status;
#   3. repeat the recall with the SAME reason -> 200 (original result);
#      a DIFFERENT reason -> 409; an unknown batch -> 404;
#   4. a new plan referencing the recalled batch is rejected with 409 and its
#      X-Request-ID is not frozen (the same id works against a clean batch);
#   5. the still-reserved plan cannot be confirmed (409) but can be cancelled;
#      the already-confirmed plan keeps its original read/retry semantics;
#   6. GET the recall record at any time.
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

req() { # req METHOD PATH [JSON_BODY] [X-Request-ID]
  local method="$1" path="$2" body="${3:-}" rid="${4:-}"
  local args=(-sS -X "$method" "$BASE$path" -H "Content-Type: application/json"
              -o /tmp/recall_resp -w "%{http_code}")
  [ -n "$body" ] && args+=(--data "$body")
  [ -n "$rid" ] && args+=(-H "X-Request-ID: $rid")
  local code
  code="$(curl "${args[@]}")"
  echo "---- $method $path -> HTTP $code"
  python3 -m json.tool < /tmp/recall_resp 2>/dev/null || cat /tmp/recall_resp
  echo
}

PLAN='{
  "batch_ids": ["RC1"],
  "diluent": {"available_nl": 10000000},
  "wells": [
    {"id": "A1", "final_volume_nl": 1000, "final_concentration": "400"},
    {"id": "A2", "final_volume_nl": 1000, "final_concentration": "100"}
  ],
  "containers": [
    {"id": "tube-1", "capacity_nl": 10000},
    {"id": "tube-2", "capacity_nl": 20000}
  ],
  "pipette": {"min_nl": 500, "max_nl": 1000, "step_nl": 100},
  "default_well_capacity_nl": 2000
}'

echo "== register a large batch (1000 uM, 10 mL) =="
req POST /batches '{"id": "RC1", "concentration": "1000", "initial_volume_nl": 10000000}'

echo "== three plans: rc-keep (reserved), rc-done (confirmed), rc-drop (cancelled) =="
req POST /inventory/plans "$PLAN" rc-keep
req POST /inventory/plans "$PLAN" rc-done > /dev/null
req POST /inventory/plans "$PLAN" rc-drop > /dev/null
req POST /inventory/plans/rc-done/confirm
req POST /inventory/plans/rc-drop/cancel

echo "== RECALL: irrevocable, with reason =="
req POST /batches/RC1/recall '{"reason": "contamination found in QC run 42"}'

echo "== repeat with the SAME reason -> 200, identical original result =="
req POST /batches/RC1/recall '{"reason": "contamination found in QC run 42"}'

echo "== repeat with a DIFFERENT reason -> 409 recall_conflict =="
req POST /batches/RC1/recall '{"reason": "some other reason"}'

echo "== unknown batch -> 404 unknown_batch =="
req POST /batches/NOPE/recall '{"reason": "contamination"}'

echo "== new plan referencing the recalled batch -> 409 batch_recalled =="
echo "   (no plan is frozen; the same X-Request-ID stays usable)"
req POST /inventory/plans "$PLAN" rc-after-recall

echo "== same request id, but a clean batch RC2 -> 201 (id was not frozen) =="
req POST /batches '{"id": "RC2", "concentration": "1000", "initial_volume_nl": 10000000}'
req POST /inventory/plans "${PLAN/RC1/RC2}" rc-after-recall

echo "== pending reservation: confirm blocked by recall (409) =="
req POST /inventory/plans/rc-keep/confirm

echo "== pending reservation: cancel still allowed (200) =="
req POST /inventory/plans/rc-keep/cancel

echo "== confirmed plan: retry keeps the original frozen result (200) =="
req POST /inventory/plans "$PLAN" rc-done

echo "== confirmed plan: repeat confirm stays idempotent (200, changed=false) =="
req POST /inventory/plans/rc-done/confirm

echo "== GET the recall record =="
req GET /batches/RC1/recall

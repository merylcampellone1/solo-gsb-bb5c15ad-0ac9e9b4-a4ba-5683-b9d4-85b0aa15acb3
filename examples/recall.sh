#!/usr/bin/env bash
# Batch-recall demo against a locally running service (docker compose up).
#
# Registers a stock batch, creates three inventory plans (one pending, one
# confirmed, one cancelled), then irrevocably recalls the batch and shows the
# full recall semantics:
#   * affected report traces stock -> intermediates -> target wells,
#     listing only reserved/confirmed plans, each well once per plan;
#   * repeat recall with the same reason replays the original result;
#   * a different reason conflicts (recall is irrevocable);
#   * new requests referencing the batch are rejected without freezing the
#     request id;
#   * a pending reservation cannot be confirmed but can still be cancelled;
#   * already-confirmed results keep their idempotent read/retry semantics.
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

post() { curl -sS -X POST "$BASE$1" "${@:2}"; }

echo "== register batch S1 (1000 uM) =="
post /batches -H "Content-Type: application/json" \
  -d '{"id":"S1","concentration":"1000","initial_volume_nl":1000000}' \
  | python3 -m json.tool

echo "== plan A: stays RESERVED (pending) =="
post /inventory/plans -H "Content-Type: application/json" \
  -H "X-Request-ID: recall-demo-A" --data @examples/inventory.json > /dev/null
echo "  (frozen under recall-demo-A)"

echo "== plan B: CONFIRMED =="
post /inventory/plans -H "Content-Type: application/json" \
  -H "X-Request-ID: recall-demo-B" --data @examples/inventory.json > /dev/null
post /inventory/plans/recall-demo-B/confirm > /dev/null
echo "  (recall-demo-B confirmed)"

echo "== plan C: CANCELLED (must NOT appear in the recall report) =="
post /inventory/plans -H "Content-Type: application/json" \
  -H "X-Request-ID: recall-demo-C" --data @examples/inventory.json > /dev/null
post /inventory/plans/recall-demo-C/cancel > /dev/null
echo "  (recall-demo-C cancelled)"

echo
echo "== IRREVOCABLE RECALL (batch id + reason) -> 201, frozen impact report =="
post /batches/S1/recall -H "Content-Type: application/json" \
  -d '{"reason":"QC aliquot tested positive for contamination"}' \
  | python3 -m json.tool

echo "== repeat recall with the SAME reason -> 200 (original result replayed) =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/batches/S1/recall" \
  -H "Content-Type: application/json" \
  -d '{"reason":"QC aliquot tested positive for contamination"}'

echo "== recall with a DIFFERENT reason -> 409 recall_reason_conflict =="
curl -sS -X POST "$BASE/batches/S1/recall" \
  -H "Content-Type: application/json" \
  -d '{"reason":"actually a labelling error"}' | python3 -m json.tool

echo "== recall an unknown batch -> 404 unknown_batch =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/batches/NOPE/recall" \
  -H "Content-Type: application/json" -d '{"reason":"x"}'

echo "== GET the frozen recall record =="
curl -sS "$BASE/batches/S1/recall" | python3 -m json.tool

echo "== NEW plan referencing the recalled batch -> 409 batch_recalled =="
curl -sS -X POST "$BASE/inventory/plans" \
  -H "Content-Type: application/json" -H "X-Request-ID: recall-demo-new" \
  --data @examples/inventory.json | python3 -m json.tool
echo "   ... and the request id was NOT frozen (GET -> 404):"
curl -sS -o /dev/null -w "   GET /plans/recall-demo-new -> %{http_code}\n" \
  "$BASE/plans/recall-demo-new"

echo "== pending plan A cannot be CONFIRMED -> 409 batch_recalled ... =="
post /inventory/plans/recall-demo-A/confirm | python3 -m json.tool

echo "== ... but it can still be CANCELLED -> 200 =="
post /inventory/plans/recall-demo-A/cancel | python3 -m json.tool

echo "== confirmed plan B keeps original semantics: confirm 200 / cancel 409 =="
curl -sS -o /dev/null -w "confirm again -> %{http_code}\n" \
  -X POST "$BASE/inventory/plans/recall-demo-B/confirm"
curl -sS -o /dev/null -w "cancel after confirm -> %{http_code}\n" \
  -X POST "$BASE/inventory/plans/recall-demo-B/cancel"

echo "== batch now carries the recall state =="
curl -sS "$BASE/batches/S1" | python3 -m json.tool

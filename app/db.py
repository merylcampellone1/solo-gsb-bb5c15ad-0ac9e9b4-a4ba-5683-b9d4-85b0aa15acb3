"""PostgreSQL persistence.

Two concerns share one database:

* frozen plans (idempotency support for both planning modes), and
* the stock-batch inventory: registered batches, per-plan reservations and
  their confirm/cancel lifecycle.

The application connects through a small asyncpg pool so that concurrent
requests serialize on row locks (``SELECT ... FOR UPDATE``) instead of on a
single shared connection.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

import asyncpg

# Errors raised while the server is starting / accepting connections but the
# database / role is not ready yet.
_CONNECT_ERRORS = (OSError, asyncpg.PostgresError, ConnectionError)


SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    request_id      TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('feasible', 'infeasible')),
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    concentration   TEXT NOT NULL,
    initial_nl      BIGINT NOT NULL CHECK (initial_nl > 0),
    reserved_nl     BIGINT NOT NULL DEFAULT 0 CHECK (reserved_nl >= 0),
    used_nl         BIGINT NOT NULL DEFAULT 0 CHECK (used_nl >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (reserved_nl + used_nl <= initial_nl)
);

CREATE TABLE IF NOT EXISTS reservations (
    request_id      TEXT PRIMARY KEY,
    status          TEXT NOT NULL
                    CHECK (status IN ('reserved', 'confirmed', 'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reservation_items (
    request_id      TEXT NOT NULL REFERENCES reservations(request_id),
    batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
    amount_nl       BIGINT NOT NULL CHECK (amount_nl > 0),
    PRIMARY KEY (request_id, batch_id)
);

-- Irrevocable batch recalls. A row's mere existence is the authoritative
-- "recalled" state; the API offers no update or delete. The reason is part of
-- the recall's identity: repeating a recall with the same reason replays the
-- original result, a different reason conflicts.
CREATE TABLE IF NOT EXISTS recalls (
    batch_id        TEXT PRIMARY KEY REFERENCES batches(batch_id),
    reason          TEXT NOT NULL CHECK (length(btrim(reason)) > 0),
    impact          JSONB NOT NULL DEFAULT '[]'::jsonb,
    recalled_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Upgrade path for databases created before recalls carried an impact
-- snapshot; harmless on fresh databases.
ALTER TABLE recalls ADD COLUMN IF NOT EXISTS impact
    JSONB NOT NULL DEFAULT '[]'::jsonb;
"""


async def connect(dsn: Optional[str] = None) -> asyncpg.Pool:
    dsn = dsn or os.environ.get(
        "DATABASE_URL",
        "postgresql://planner:planner@db:5432/planner",
    )
    last_err = None
    for attempt in range(60):
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
            break
        except _CONNECT_ERRORS as exc:  # database not ready yet
            last_err = exc
            await asyncio.sleep(1)
    else:
        raise RuntimeError(f"cannot reach database: {last_err}")
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    return pool


# --------------------------------------------------------------------------- #
# Frozen plans (idempotency)
# --------------------------------------------------------------------------- #


async def get_frozen(pool, request_id: str):
    """Return (hash, status, payload dict) or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT request_hash, status, payload FROM plans "
            "WHERE request_id = $1",
            request_id,
        )
    if row is None:
        return None
    return row["request_hash"], row["status"], json.loads(row["payload"])


async def freeze(pool, request_id: str, request_hash: str,
                 status: str, payload: dict) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO plans (request_id, request_hash, status, payload)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (request_id) DO NOTHING
            """,
            request_id, request_hash, status, json.dumps(payload),
        )


# --------------------------------------------------------------------------- #
# Batch registry
# --------------------------------------------------------------------------- #


def _batch_dict(row) -> dict:
    available = row["initial_nl"] - row["reserved_nl"] - row["used_nl"]
    out = {
        "id": row["batch_id"],
        "concentration": row["concentration"],
        "initial_volume_nl": row["initial_nl"],
        "reserved_nl": row["reserved_nl"],
        "used_nl": row["used_nl"],
        "available_nl": available,
        "recalled": row["recall_reason"] is not None,
    }
    if row["recall_reason"] is not None:
        out["recall_reason"] = row["recall_reason"]
    return out


_BATCH_COLS = (
    "b.batch_id, b.concentration, b.initial_nl, b.reserved_nl, b.used_nl, "
    "r.reason AS recall_reason"
)
_BATCH_FROM = "batches b LEFT JOIN recalls r ON r.batch_id = b.batch_id"


async def create_batch(pool, batch_id: str, concentration: str,
                       initial_nl: int):
    """Insert a batch; return (created, batch dict).

    When the id already exists the existing row is returned unchanged so the
    caller can decide between an idempotent replay and a conflict.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO batches (batch_id, concentration, initial_nl)
            VALUES ($1, $2, $3)
            ON CONFLICT (batch_id) DO NOTHING
            RETURNING batch_id, concentration, initial_nl,
                      reserved_nl, used_nl, NULL::text AS recall_reason
            """,
            batch_id, concentration, initial_nl,
        )
        if row is not None:
            return True, _batch_dict(row)
        row = await conn.fetchrow(
            f"SELECT {_BATCH_COLS} FROM {_BATCH_FROM} "
            "WHERE b.batch_id = $1",
            batch_id,
        )
        return False, _batch_dict(row)


async def get_batch(pool, batch_id: str) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_BATCH_COLS} FROM {_BATCH_FROM} "
            "WHERE b.batch_id = $1",
            batch_id,
        )
    return _batch_dict(row) if row is not None else None


async def get_batches(pool, batch_ids) -> dict:
    """Return {batch_id: batch dict} for the ids that exist."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_BATCH_COLS} FROM {_BATCH_FROM} "
            "WHERE b.batch_id = ANY($1::text[])",
            list(batch_ids),
        )
    return {r["batch_id"]: _batch_dict(r) for r in rows}


async def list_batches(pool) -> list:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_BATCH_COLS} FROM {_BATCH_FROM} ORDER BY b.batch_id"
        )
    return [_batch_dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Reservations
# --------------------------------------------------------------------------- #


class _Abort(Exception):
    """Rollback the open transaction and report this outcome instead."""

    def __init__(self, outcome: str, detail):
        super().__init__(outcome)
        self.outcome = outcome
        self.detail = detail


async def reserve_and_freeze(pool, request_id: str, request_hash: str,
                             payload: dict, usage: dict):
    """Atomically freeze a feasible inventory plan and reserve its stock.

    ``usage`` maps batch_id -> nanoliters drawn from that batch by the plan.
    Everything happens in one transaction:

    1. insert the frozen plan row (first writer wins);
    2. lock the touched batch rows ``FOR UPDATE`` (sorted, deadlock-free);
    3. verify every batch still has enough available volume;
    4. bump ``reserved_nl`` and record the reservation with its items.

    Returns ``(outcome, detail)`` where outcome is one of:

    * ``"ok"``           - plan frozen and stock reserved;
    * ``"exists"``       - request_id already frozen by a concurrent request;
                           nothing was reserved by this call;
    * ``"unknown"``      - a referenced batch no longer exists (defensive);
    * ``"recalled"``     - detail is a list of ``{batch_id, reason}`` for
                           referenced batches that have been recalled; nothing
                           was reserved and nothing was frozen;
    * ``"insufficient"`` - detail is a list of per-batch shortages; nothing
                           was reserved and nothing was frozen.
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO plans (request_id, request_hash, status,
                                       payload)
                    VALUES ($1, $2, 'feasible', $3::jsonb)
                    ON CONFLICT (request_id) DO NOTHING
                    RETURNING request_id
                    """,
                    request_id, request_hash, json.dumps(payload),
                )
                if row is None:
                    # Lost the insert race; the conflicting INSERT waits for
                    # the winner to commit, so its row is visible afterwards.
                    return "exists", None
                ids = sorted(usage)
                rows = await conn.fetch(
                    """
                    SELECT b.batch_id AS batch_id, b.initial_nl AS initial_nl,
                           b.reserved_nl AS reserved_nl, b.used_nl AS used_nl,
                           r.reason AS recall_reason
                    FROM batches b
                    LEFT JOIN recalls r ON r.batch_id = b.batch_id
                    WHERE b.batch_id = ANY($1::text[])
                    ORDER BY b.batch_id
                    FOR UPDATE OF b
                    """,
                    ids,
                )
                found = {r["batch_id"]: r for r in rows}
                missing = [b for b in ids if b not in found]
                if missing:
                    raise _Abort("unknown", missing)
                recalled = [{"batch_id": b, "reason": found[b]["recall_reason"]}
                            for b in ids if found[b]["recall_reason"] is not None]
                if recalled:
                    # Authoritative batch state wins: a recall that committed
                    # before this transaction blocks any new reservation.
                    raise _Abort("recalled", recalled)
                shortages = []
                for bid in ids:
                    r = found[bid]
                    available = (r["initial_nl"] - r["reserved_nl"]
                                 - r["used_nl"])
                    if available < usage[bid]:
                        shortages.append({
                            "batch_id": bid,
                            "required_nl": usage[bid],
                            "available_nl": available,
                        })
                if shortages:
                    raise _Abort("insufficient", shortages)
                for bid in ids:
                    await conn.execute(
                        "UPDATE batches SET reserved_nl = reserved_nl + $2 "
                        "WHERE batch_id = $1",
                        bid, usage[bid],
                    )
                await conn.execute(
                    "INSERT INTO reservations (request_id, status) "
                    "VALUES ($1, 'reserved')",
                    request_id,
                )
                if ids:
                    await conn.executemany(
                        "INSERT INTO reservation_items "
                        "(request_id, batch_id, amount_nl) "
                        "VALUES ($1, $2, $3)",
                        [(request_id, bid, usage[bid]) for bid in ids],
                    )
    except _Abort as abort:
        return abort.outcome, abort.detail
    return "ok", None


async def get_reservation(pool, request_id: str) -> Optional[dict]:
    """Return {request_id, status, items} or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM reservations WHERE request_id = $1",
            request_id,
        )
        if row is None:
            return None
        items = await conn.fetch(
            "SELECT batch_id, amount_nl FROM reservation_items "
            "WHERE request_id = $1 ORDER BY batch_id",
            request_id,
        )
    return {
        "request_id": request_id,
        "status": row["status"],
        "items": [{"batch_id": r["batch_id"], "amount_nl": r["amount_nl"]}
                  for r in items],
    }


async def transition_reservation(pool, request_id: str, action: str):
    """Confirm or cancel a reservation exactly once.

    * confirm: reserved -> confirmed; each item's volume moves from
      ``reserved_nl`` to ``used_nl`` (it stays consumed forever);
    * cancel:  reserved -> cancelled; each item's ``reserved_nl`` is
      released back to the available pool exactly once.

    The reservation row is locked ``FOR UPDATE`` so repeated or concurrent
    calls converge on the same final state.  Confirm additionally locks the
    referenced batch rows and is refused when any of them has been recalled
    (the reservation stays ``reserved``; it can still be cancelled).  Returns
    ``None`` when no reservation exists for ``request_id``; otherwise a dict
    with the (possibly unchanged) ``status``, whether this call performed the
    transition (``changed``), the reservation ``items``, and for a blocked
    confirm a ``recalled_batches`` list.
    """
    assert action in ("confirm", "cancel")
    target = "confirmed" if action == "confirm" else "cancelled"
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM reservations WHERE request_id = $1 "
                "FOR UPDATE",
                request_id,
            )
            if row is None:
                return None
            status = row["status"]
            rows = await conn.fetch(
                "SELECT batch_id, amount_nl FROM reservation_items "
                "WHERE request_id = $1 ORDER BY batch_id",
                request_id,
            )
            result = {
                "status": status,
                "changed": False,
                "items": [{"batch_id": r["batch_id"],
                           "amount_nl": r["amount_nl"]} for r in rows],
            }
            if status != "reserved":
                return result  # idempotent replay: balances stay untouched
            item_rows = sorted(rows, key=lambda r: r["batch_id"])
            if action == "confirm":
                # Lock the referenced batches (same order as reservation) and
                # consult their authoritative recall state: a pending
                # reservation must never be confirmed once a recall is in
                # force. Cancellation stays available.
                brows = await conn.fetch(
                    """
                    SELECT b.batch_id AS batch_id, r.reason AS recall_reason
                    FROM batches b
                    LEFT JOIN recalls r ON r.batch_id = b.batch_id
                    WHERE b.batch_id = ANY($1::text[])
                    ORDER BY b.batch_id
                    FOR UPDATE OF b
                    """,
                    [r["batch_id"] for r in item_rows],
                )
                blocked = [
                    {"batch_id": br["batch_id"], "reason": br["recall_reason"]}
                    for br in brows if br["recall_reason"] is not None
                ]
                if blocked:
                    result["recalled_batches"] = blocked
                    return result
            for r in item_rows:
                if action == "confirm":
                    await conn.execute(
                        "UPDATE batches SET reserved_nl = reserved_nl - $2, "
                        "used_nl = used_nl + $2 WHERE batch_id = $1",
                        r["batch_id"], r["amount_nl"],
                    )
                else:
                    await conn.execute(
                        "UPDATE batches SET reserved_nl = reserved_nl - $2 "
                        "WHERE batch_id = $1",
                        r["batch_id"], r["amount_nl"],
                    )
            await conn.execute(
                "UPDATE reservations SET status = $2, updated_at = now() "
                "WHERE request_id = $1",
                request_id, target,
            )
            result["status"] = target
            result["changed"] = True
            return result


# --------------------------------------------------------------------------- #
# Batch recalls
# --------------------------------------------------------------------------- #


def _recall_dict(row) -> dict:
    return {
        "batch_id": row["batch_id"],
        "reason": row["reason"],
        "recalled_at": row["recalled_at"].isoformat()
        if row["recalled_at"] else None,
        "impact": json.loads(row["impact"]),
    }


_AFFECTED_SELECT = """
    SELECT p.request_id AS request_id, p.payload AS payload,
           rs.status AS reservation_status
    FROM plans p
    JOIN reservations rs ON rs.request_id = p.request_id
    JOIN reservation_items ri ON ri.request_id = p.request_id
    WHERE ri.batch_id = $1
      AND p.status = 'feasible'
      AND rs.status IN ('reserved', 'confirmed')
    ORDER BY p.request_id
"""


async def _affected_rows(conn, batch_id: str):
    """Plans that actually drew from ``batch_id`` and whose reservation is
    still reserved or confirmed (cancelled plans excluded), as visible to the
    open transaction."""
    return await conn.fetch(_AFFECTED_SELECT, batch_id)


async def get_recall(pool, batch_id: str):
    """Return the recall dict (with its frozen impact snapshot) or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT batch_id, reason, impact, recalled_at FROM recalls "
            "WHERE batch_id = $1",
            batch_id,
        )
    return _recall_dict(row) if row else None


async def create_recall(pool, batch_id: str, reason: str, build_impact):
    """Irrevocably recall a batch, snapshotting its impact in one transaction.

    Locks the batch row ``FOR UPDATE`` first; this is the same lock (and same
    lock order) that plan reservation and reservation confirmation take, so
    the three operations serialize on the authoritative batch state:

    * a reservation whose batch lock is granted before the recall commits may
      complete and is then captured by this recall's impact snapshot;
    * a reservation / confirm blocked behind the recall lock observes the
      recall and is rejected.

    ``build_impact(conn, rows)`` is an async callback supplied by the API
    layer: given the affected reservation rows (frozen payload + live status)
    it computes the traced target wells, keeping SQL out of that logic.

    Returns ``(outcome, detail)``:

    * ``("created", recall_dict)``  - first recall, now in force;
    * ``("replayed", recall_dict)`` - batch already recalled with the same
                                      reason; the original snapshot is
                                      returned byte-for-byte;
    * ``("conflict", recall_dict)`` - already recalled with a different reason;
    * ``("unknown", None)``         - no such batch.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM batches WHERE batch_id = $1 FOR UPDATE",
                batch_id,
            )
            if exists is None:
                return "unknown", None
            rows = await _affected_rows(conn, batch_id)
            impact = await build_impact(conn, rows)
            row = await conn.fetchrow(
                """
                INSERT INTO recalls (batch_id, reason, impact)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (batch_id) DO NOTHING
                RETURNING batch_id, reason, impact, recalled_at
                """,
                batch_id, reason, json.dumps(impact),
            )
            if row is not None:
                return "created", _recall_dict(row)
            # The batch was recalled concurrently/earlier; the existing row
            # (inserted while we held the batch lock, impossible for two
            # concurrent recalls, so this is a repeat call) is authoritative.
            row = await conn.fetchrow(
                "SELECT batch_id, reason, impact, recalled_at FROM recalls "
                "WHERE batch_id = $1",
                batch_id,
            )
            existing = _recall_dict(row)
            if row["reason"] == reason:
                return "replayed", existing
            return "conflict", existing

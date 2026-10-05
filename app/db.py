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
    recalled        BOOLEAN NOT NULL DEFAULT FALSE,
    recalled_reason TEXT,
    recalled_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (reserved_nl + used_nl <= initial_nl)
);

ALTER TABLE batches ADD COLUMN IF NOT EXISTS recalled
    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS recalled_reason TEXT;
ALTER TABLE batches ADD COLUMN IF NOT EXISTS recalled_at TIMESTAMPTZ;

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

CREATE TABLE IF NOT EXISTS recalls (
    batch_id        TEXT PRIMARY KEY REFERENCES batches(batch_id),
    reason          TEXT NOT NULL,
    report          JSONB NOT NULL,
    recalled_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
    recalled_at = row["recalled_at"]
    return {
        "id": row["batch_id"],
        "concentration": row["concentration"],
        "initial_volume_nl": row["initial_nl"],
        "reserved_nl": row["reserved_nl"],
        "used_nl": row["used_nl"],
        "available_nl": available,
        "recalled": row["recalled"],
        "recalled_reason": row["recalled_reason"],
        "recalled_at": recalled_at.isoformat() if recalled_at else None,
    }


_BATCH_COLS = ("batch_id, concentration, initial_nl, reserved_nl, used_nl, "
               "recalled, recalled_reason, recalled_at")


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
            RETURNING {_BATCH_COLS}
            """,
            batch_id, concentration, initial_nl,
        )
        if row is not None:
            return True, _batch_dict(row)
        row = await conn.fetchrow(
            f"SELECT {_BATCH_COLS} FROM batches WHERE batch_id = $1",
            batch_id,
        )
        return False, _batch_dict(row)


async def get_batch(pool, batch_id: str) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_BATCH_COLS} FROM batches WHERE batch_id = $1",
            batch_id,
        )
    return _batch_dict(row) if row is not None else None


async def get_batches(pool, batch_ids) -> dict:
    """Return {batch_id: batch dict} for the ids that exist."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_BATCH_COLS} FROM batches "
            "WHERE batch_id = ANY($1::text[])",
            list(batch_ids),
        )
    return {r["batch_id"]: _batch_dict(r) for r in rows}


async def list_batches(pool) -> list:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_BATCH_COLS} FROM batches ORDER BY batch_id"
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
    * ``"recalled"``     - a referenced batch has been recalled (authoritative
                           batch state checked under the row lock);
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
                    SELECT batch_id, initial_nl, reserved_nl, used_nl, recalled
                    FROM batches
                    WHERE batch_id = ANY($1::text[])
                    ORDER BY batch_id
                    FOR UPDATE
                    """,
                    ids,
                )
                found = {r["batch_id"]: r for r in rows}
                missing = [b for b in ids if b not in found]
                if missing:
                    raise _Abort("unknown", missing)
                recalled = [b for b in ids if found[b]["recalled"]]
                if recalled:
                    # Authoritative batch state wins: a recall committed (or
                    # committing) alongside this request forbids new reserves.
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

    A pending (``reserved``) reservation that touches a recalled batch may
    still be cancelled but can no longer be confirmed; such a confirm returns
    a dict carrying ``"recalled": [batch_id, ...]`` (the caller answers 409).

    Lock order is always batch rows (sorted) first, then the reservation row,
    which matches :func:`recall_batch`; recalls and transitions therefore
    serialize on the authoritative batch state and cannot deadlock.  Repeated
    or concurrent calls converge on the same final state.  Returns ``None``
    when no reservation exists for ``request_id``; otherwise a dict with the
    (possibly unchanged) ``status``, whether this call performed the
    transition (``changed``), and the reservation ``items``.
    """
    assert action in ("confirm", "cancel")
    target = "confirmed" if action == "confirm" else "cancelled"
    async with pool.acquire() as conn:
        async with conn.transaction():
            pre = await conn.fetchrow(
                "SELECT status FROM reservations WHERE request_id = $1",
                request_id,
            )
            if pre is None:
                return None
            items = await conn.fetch(
                "SELECT batch_id, amount_nl FROM reservation_items "
                "WHERE request_id = $1 ORDER BY batch_id",
                request_id,
            )
            item_list = [{"batch_id": r["batch_id"],
                          "amount_nl": r["amount_nl"]} for r in items]
            # Lock the touched batches first (sorted) -- the same global order
            # the recall transaction uses -- then re-lock the reservation.
            batch_ids = sorted(r["batch_id"] for r in items)
            if batch_ids:
                await conn.fetch(
                    "SELECT batch_id, recalled FROM batches "
                    "WHERE batch_id = ANY($1::text[]) "
                    "ORDER BY batch_id FOR UPDATE",
                    batch_ids,
                )
            row = await conn.fetchrow(
                "SELECT status FROM reservations WHERE request_id = $1 "
                "FOR UPDATE",
                request_id,
            )
            status = row["status"]
            result = {
                "status": status,
                "changed": False,
                "items": item_list,
            }
            if status != "reserved":
                return result  # idempotent replay: balances stay untouched
            if action == "confirm":
                recalled = await conn.fetch(
                    "SELECT batch_id FROM batches "
                    "WHERE batch_id = ANY($1::text[]) AND recalled",
                    batch_ids,
                )
                if recalled:
                    # Authoritative batch state forbids confirming a pending
                    # reservation that consumes a recalled batch.
                    result["recalled"] = [r["batch_id"] for r in recalled]
                    return result
            for r in items:
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


async def recall_batch(pool, batch_id: str, reason: str, make_report):
    """Irrevocably recall a batch and freeze its impact report atomically.

    Everything is one transaction, locking in the global order (batches
    before reservations) so the report is adjudicated against authoritative
    state even while plan creation / confirmation run concurrently:

    1. lock the batch row ``FOR UPDATE``; a missing batch ->
       ``("unknown", None)``;
    2. if it is already recalled, return ``("replay", existing)`` when the
       reason matches or ``("conflict", existing)`` otherwise -- the original
       result is never altered;
    3. lock every reservation touching this batch (sorted by request_id) so a
       concurrent confirm cannot flip a pending reservation under us, then
       read each live reservation's items together with its frozen plan;
    4. ``make_report(candidates)`` (pure, no I/O) builds the affected list;
    5. mark the batch recalled and insert the frozen ``recalls`` row.

    ``candidates`` is a list of
    ``{"request_id", "status", "amount_nl", "payload"}`` for reservations in
    ``reserved`` / ``confirmed`` state that actually drew from the batch.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_BATCH_COLS} FROM batches WHERE batch_id = $1 "
                "FOR UPDATE",
                batch_id,
            )
            if row is None:
                return "unknown", None
            if row["recalled"]:
                existing = await conn.fetchrow(
                    "SELECT reason, report, recalled_at FROM recalls "
                    "WHERE batch_id = $1",
                    batch_id,
                )
                data = {
                    "batch_id": batch_id,
                    "reason": existing["reason"],
                    "recalled_at": existing["recalled_at"].isoformat(),
                    "report": json.loads(existing["report"]),
                }
                outcome = ("replay" if existing["reason"] == reason
                           else "conflict")
                return outcome, data

            # Serialize against concurrent confirm/cancel (which lock batches
            # first, then reservations).
            res_rows = await conn.fetch(
                """
                SELECT r.request_id AS request_id, r.status AS status,
                       i.amount_nl AS amount_nl, p.payload AS payload
                FROM reservation_items i
                JOIN reservations r ON r.request_id = i.request_id
                JOIN plans p ON p.request_id = i.request_id
                WHERE i.batch_id = $1
                  AND r.status IN ('reserved', 'confirmed')
                ORDER BY r.request_id
                FOR UPDATE OF r
                """,
                batch_id,
            )
            candidates = [
                {
                    "request_id": r["request_id"],
                    "status": r["status"],
                    "amount_nl": r["amount_nl"],
                    "payload": json.loads(r["payload"]),
                }
                for r in res_rows
            ]
            report = make_report(candidates)
            ts = await conn.fetchval(
                "UPDATE batches SET recalled = TRUE, recalled_reason = $2, "
                "recalled_at = now() WHERE batch_id = $1 RETURNING recalled_at",
                batch_id, reason,
            )
            await conn.execute(
                "INSERT INTO recalls (batch_id, reason, report, recalled_at) "
                "VALUES ($1, $2, $3::jsonb, $4)",
                batch_id, reason, json.dumps(report), ts,
            )
            data = {
                "batch_id": batch_id,
                "reason": reason,
                "recalled_at": ts.isoformat(),
                "report": report,
            }
            return "ok", data


async def get_recall(pool, batch_id: str):
    """Return the frozen recall record for a batch, or a sentinel.

    * ``"missing"``  - no such batch (404 unknown_batch);
    * ``None``       - batch exists but was never recalled (404);
    * dict           - the frozen recall (reason, recalled_at, report).
    """
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT TRUE FROM batches WHERE batch_id = $1", batch_id)
        if not exists:
            return "missing"
        row = await conn.fetchrow(
            "SELECT reason, report, recalled_at FROM recalls "
            "WHERE batch_id = $1",
            batch_id,
        )
        if row is None:
            return None
        return {
            "batch_id": batch_id,
            "reason": row["reason"],
            "recalled_at": row["recalled_at"].isoformat(),
            "report": json.loads(row["report"]),
        }

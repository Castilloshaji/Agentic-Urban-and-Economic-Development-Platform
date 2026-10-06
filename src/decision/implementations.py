"""The implementation ledger: what someone decided to actually do, and how it went.

Everything else in this project is derived — rerun the pipeline and you get it
back. This is not. Each row is a commitment a person made, recorded nowhere
else, which is why the table sits outside the schema's DROP list and why no
loader ever truncates it.

The design decision worth defending is what completing an implementation does
**not** do. It does not touch a measured parameter. When drainage work finishes,
`flood_risk` still reads exactly what KSDMA measured, because editing a
measurement to reflect an intervention would be fabricating data — and this
engine's whole claim is that its numbers are traceable to a publication. A
measurement may only change when someone re-measures.

What the ledger does instead is sit *alongside* the measurements. The shortfall
stays real and keeps its severity; it additionally carries "work is under way on
this since March". That is enough to stop the system proposing the same thing
every time a budget arrives, which is the actual problem, without pretending the
ground has changed before it has.
"""

from __future__ import annotations

from datetime import datetime, timezone

# planned -> in_progress -> completed is the happy path. The others are real
# outcomes that a tracker which only models success cannot represent.
STATUSES = ("planned", "in_progress", "completed", "on_hold", "cancelled")

# Which statuses mean "this shortfall is being dealt with, do not re-propose it".
# `completed` counts: the work is done, and proposing it again would be the same
# mistake as proposing it mid-build. `cancelled` does not — a cancelled project
# leaves the need wide open, and the system should raise it again.
ACTIVE_STATUSES = ("planned", "in_progress", "completed")

# What a status change is allowed to be. Rejecting the rest is not pedantry:
# a tracker that lets `completed` silently become `planned` cannot be trusted to
# answer "what is in progress", which is the question it exists to answer.
TRANSITIONS = {
    "planned": ("in_progress", "on_hold", "cancelled"),
    "in_progress": ("completed", "on_hold", "cancelled"),
    "on_hold": ("planned", "in_progress", "cancelled"),
    "completed": (),          # terminal; re-doing work is a new implementation
    "cancelled": ("planned",),  # revived, deliberately
}

FIELDS = ("id", "title", "domain", "addresses", "admin_ids", "est_cost_inr_crore",
          "status", "feasibility", "conditions", "detail", "evidence", "origin",
          "origin_id", "note", "created_at", "updated_at", "completed_at")


def _connect():
    import psycopg2
    import psycopg2.extras

    from ..storage.postgres.load import connection_string
    connection = psycopg2.connect(connection_string())
    return connection, connection.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor)


def _row(record: dict) -> dict:
    out = dict(record)
    for key in ("created_at", "updated_at", "completed_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    if out.get("est_cost_inr_crore") is not None:
        out["est_cost_inr_crore"] = float(out["est_cost_inr_crore"])
    return out


def _text(value) -> str | None:
    """Coerce an agent-supplied field to text for a TEXT column.

    The ideation agents are asked for a string and mostly comply, but a live
    run returned `addresses` as a list. Since the website hands a proposal
    straight to this function, an unguarded value reaches psycopg2 and fails
    the insert — so the Implement button would break on exactly the ideas most
    worth logging. A list becomes a comma-joined string; anything else becomes
    its repr rather than an exception.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple, set)):
        joined = ", ".join(str(v).strip() for v in value
                           if v is not None and str(v).strip())
        return joined or None
    return str(value).strip() or None


def create(idea: dict, admin_ids: list[str], origin: str = "manual",
           origin_id: str | None = None, status: str = "planned") -> dict:
    """Commit one idea to the ledger.

    Takes an idea in the shape the ideation agents produce, so the website can
    hand a proposal straight through without reshaping it. Every field is
    coerced, because "the shape the agents produce" is a hope, not a contract.
    """
    title = _text(idea.get("title")) or ""
    if not title:
        raise ValueError("an implementation needs a title")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    admin_ids = [a for a in (str(x).strip() for x in (admin_ids or [])) if a]
    admin_ids = list(dict.fromkeys(admin_ids))
    if not admin_ids:
        raise ValueError("an implementation needs at least one local body")

    import json as _json

    cost = idea.get("est_cost_inr_crore")
    try:
        cost = float(cost) if cost not in (None, "") else None
    except (TypeError, ValueError):
        cost = None

    connection, cursor = _connect()
    try:
        cursor.execute(
            """
            INSERT INTO implementation
                (title, domain, addresses, admin_ids, est_cost_inr_crore, status,
                 feasibility, conditions, detail, evidence, origin, origin_id, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (title, _text(idea.get("domain")) or "unknown",
             _text(idea.get("addresses")),
             admin_ids, cost, status, _text(idea.get("feasibility")),
             _json.dumps(idea.get("conditions") or []),
             _text(idea.get("what")) or _text(idea.get("detail")),
             _text(idea.get("evidence")),
             origin, origin_id, _text(idea.get("note"))),
        )
        created = cursor.fetchone()
        cursor.execute(
            "INSERT INTO implementation_event (implementation_id, from_status, "
            "to_status, note) VALUES (%s, NULL, %s, %s)",
            (created["id"], status, "created"),
        )
        connection.commit()
        return _row(created)
    finally:
        connection.close()


def set_status(implementation_id: int, status: str, note: str | None = None) -> dict:
    """Move one implementation along, refusing a transition that makes no sense."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")

    connection, cursor = _connect()
    try:
        cursor.execute("SELECT * FROM implementation WHERE id = %s", (implementation_id,))
        current = cursor.fetchone()
        if not current:
            raise KeyError(f"no implementation with id {implementation_id}")
        if current["status"] == status:
            return _row(current)
        allowed = TRANSITIONS.get(current["status"], ())
        if status not in allowed:
            raise ValueError(
                f"cannot move from {current['status']!r} to {status!r}; "
                f"allowed from here: {allowed or '(terminal)'}")

        cursor.execute(
            """
            UPDATE implementation
               SET status = %s,
                   updated_at = now(),
                   completed_at = CASE WHEN %s = 'completed' THEN now() ELSE NULL END,
                   note = COALESCE(%s, note)
             WHERE id = %s
            RETURNING *
            """,
            (status, status, note, implementation_id),
        )
        updated = cursor.fetchone()
        cursor.execute(
            "INSERT INTO implementation_event (implementation_id, from_status, "
            "to_status, note) VALUES (%s, %s, %s, %s)",
            (implementation_id, current["status"], status, note),
        )
        connection.commit()
        return _row(updated)
    finally:
        connection.close()


def listing(admin_ids: list[str] | None = None, status: str | None = None,
            limit: int = 200) -> dict:
    """The ledger, newest first, optionally narrowed to areas or a status."""
    clauses, params = [], []
    if admin_ids:
        clauses.append("admin_ids && %s")
        params.append(list(admin_ids))
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    connection, cursor = _connect()
    try:
        cursor.execute(
            f"SELECT * FROM implementation {where} ORDER BY created_at DESC LIMIT %s",
            (*params, limit))
        rows = [_row(r) for r in cursor.fetchall()]
        cursor.execute(f"SELECT status, count(*) AS n, "
                       f"coalesce(sum(est_cost_inr_crore), 0) AS cost "
                       f"FROM implementation {where} GROUP BY status", tuple(params))
        summary = {r["status"]: {"count": r["n"], "cost_inr_crore": float(r["cost"])}
                   for r in cursor.fetchall()}
    finally:
        connection.close()

    committed = sum(v["cost_inr_crore"] for s, v in summary.items()
                    if s in ACTIVE_STATUSES)
    return {
        "implementations": rows,
        "by_status": summary,
        "committed_inr_crore": round(committed, 2),
        "note": ("committed spend counts planned, in_progress and completed; "
                 "cancelled work is excluded because the need it addressed is "
                 "open again"),
    }


def history(implementation_id: int) -> dict:
    """One implementation and every status change it has been through."""
    connection, cursor = _connect()
    try:
        cursor.execute("SELECT * FROM implementation WHERE id = %s", (implementation_id,))
        record = cursor.fetchone()
        if not record:
            raise KeyError(f"no implementation with id {implementation_id}")
        cursor.execute(
            "SELECT from_status, to_status, note, created_at FROM implementation_event "
            "WHERE implementation_id = %s ORDER BY created_at", (implementation_id,))
        events = [{**e, "created_at": e["created_at"].isoformat()}
                  for e in cursor.fetchall()]
    finally:
        connection.close()
    return {"implementation": _row(record), "events": events,
            "allowed_next": list(TRANSITIONS.get(record["status"], ()))}


def coverage(admin_ids: list[str]) -> dict:
    """Which shortfalls in these areas already have work against them.

    Keyed by (parameter, admin_id) because that is the join the ideation path
    needs: it asks "is anyone already dealing with this specific gap in this
    specific place" before letting an agent propose it again.
    """
    connection, cursor = _connect()
    try:
        cursor.execute(
            """
            SELECT id, title, domain, addresses, admin_ids, status,
                   est_cost_inr_crore, updated_at
              FROM implementation
             WHERE admin_ids && %s AND status = ANY(%s) AND addresses IS NOT NULL
             ORDER BY updated_at DESC
            """,
            (list(admin_ids), list(ACTIVE_STATUSES)))
        rows = [_row(r) for r in cursor.fetchall()]
    finally:
        connection.close()

    by_key: dict[str, list[dict]] = {}
    for row in rows:
        for admin_id in row["admin_ids"]:
            if admin_id not in admin_ids:
                continue
            by_key.setdefault(f"{row['addresses']}|{admin_id}", []).append({
                "id": row["id"], "title": row["title"], "status": row["status"],
                "est_cost_inr_crore": row["est_cost_inr_crore"],
                "since": row["updated_at"],
            })
    return {
        "by_parameter_and_area": by_key,
        "active_count": len(rows),
        "statuses_counted": list(ACTIVE_STATUSES),
    }


def covered_titles(admin_ids: list[str]) -> list[dict]:
    """Active work in these areas, for telling an agent what not to re-propose."""
    connection, cursor = _connect()
    try:
        cursor.execute(
            """
            SELECT title, domain, addresses, status, est_cost_inr_crore, admin_ids
              FROM implementation
             WHERE admin_ids && %s AND status = ANY(%s)
             ORDER BY domain, title
            """,
            (list(admin_ids), list(ACTIVE_STATUSES)))
        return [_row(r) for r in cursor.fetchall()]
    finally:
        connection.close()


def stats() -> dict:
    """Ledger-wide counts, for the dashboard header."""
    connection, cursor = _connect()
    try:
        cursor.execute(
            "SELECT status, count(*) AS n, coalesce(sum(est_cost_inr_crore), 0) AS cost "
            "FROM implementation GROUP BY status")
        by_status = {r["status"]: {"count": r["n"],
                                   "cost_inr_crore": float(r["cost"])}
                     for r in cursor.fetchall()}
    finally:
        connection.close()
    return {
        "by_status": by_status,
        "total": sum(v["count"] for v in by_status.values()),
        "committed_inr_crore": round(
            sum(v["cost_inr_crore"] for s, v in by_status.items()
                if s in ACTIVE_STATUSES), 2),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

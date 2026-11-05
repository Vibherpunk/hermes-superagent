"""Superagent orchestrator read operations and pure derived state projections.

Provides pure functions for calculating derived ticket stages across the
attention panel, list endpoints, and orchestrator inspection views without
database access or kanban module dependencies.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any


def _row_to_dict(cur: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    """Convert a SQLite row into a plain dictionary."""
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, dict):
        return dict(row)
    if cur.description:
        cols = [col[0] for col in cur.description]
        return dict(zip(cols, row))
    return {}


def _one_line(text: Any, max_len: int = 120) -> str:
    """Format text into a single-line snippet bounded to max_len chars."""
    if not text:
        return ""
    clean = " ".join(str(text).split())
    if len(clean) > max_len:
        return clean[: max_len - 3] + "..."
    return clean


def derived_stage(ticket: dict[str, Any] | dict, ctx: dict[str, Any] | dict) -> str:
    """Derive the human-attention / orchestrator lifecycle stage for a ticket.

    PURE function over plain dicts — it reads NO database and imports NO kanban
    modules.

    Input contract (caller assembles these dicts from the tasks row, the
    sa_mandates row, sa_decisions, and sa_outbox):
    - ticket: the kanban tasks row as a dict — fields read:
        * status (str | None): ticket status column (may be absent/None).
        * block_kind (str | None): reason for blocked status (may be absent/None).
    - ctx: caller-assembled context dict with EXACTLY these optional keys:
        * last_delivery_error (str | None): latest sa_outbox failure for this ticket.
        * proposal_state (None | 'awaiting_answer' | 'awaiting_review'): approval status.
        * sa_state (None | 'working' | 'dispatched'): orchestrator's per-ticket working state.

    Evaluation precedence (Stage by FIRST MATCH in this exact order, SPEC-V2 §9):
    1. ctx.last_delivery_error is truthy -> "person/delivery"
       Fields read: ctx['last_delivery_error']
    2. ctx.proposal_state == 'awaiting_answer' OR
       (ticket.status == 'blocked' AND ticket.block_kind == 'needs_input') -> "person/answer"
       Fields read: ctx['proposal_state'], ticket['status'], ticket['block_kind']
    3. ctx.proposal_state == 'awaiting_review' OR
       (ticket.status == 'blocked' AND ticket.block_kind == 'review') -> "person/review"
       Fields read: ctx['proposal_state'], ticket['status'], ticket['block_kind']
    4. ticket.status IN ('blocked', 'failed') OR (ticket.status == 'triage' AND scoped)
       -> "person/blocked", where scoped is true iff the ticket is SA-linked
       (ticket.mandate_id is not None) OR ticket.block_kind is one of the three
       Super Agent gate kinds ('gate_worker', 'gate_env', 'spend_ceiling').
       A legacy triage card that is neither keeps the fall-through to "idle"
       (DESIGN rev 4, §2.7 — scoped branch; precedence order unchanged).
       Fields read: ticket['status'], ticket['mandate_id'], ticket['block_kind']
    5. ticket.status IN ('running', 'claimed') OR
       ctx.sa_state == 'working' -> "working"
       Fields read: ticket['status'], ctx['sa_state']
    6. ticket.status IN ('ready', 'todo') OR
       ctx.sa_state == 'dispatched' -> "queued"
       Fields read: ticket['status'], ctx['sa_state']
    7. ticket.status == 'done' -> "closed"
       Fields read: ticket['status']
    8. else -> "idle"
       Fields read: none (fallback)

    Returns:
        One of the stage identifier strings:
        "person/delivery", "person/answer", "person/review", "person/blocked",
        "working", "queued", "closed", or "idle".
    """
    ticket_dict = ticket or {}
    ctx_dict = ctx or {}

    # 1. ctx.last_delivery_error is truthy -> "person/delivery"
    if ctx_dict.get("last_delivery_error"):
        return "person/delivery"

    # 2. ctx.proposal_state == 'awaiting_answer' OR (ticket.status == 'blocked' AND ticket.block_kind == 'needs_input') -> "person/answer"
    status = ticket_dict.get("status")
    block_kind = ticket_dict.get("block_kind")
    proposal_state = ctx_dict.get("proposal_state")

    if proposal_state == "awaiting_answer" or (status == "blocked" and block_kind == "needs_input"):
        return "person/answer"

    # 3. ctx.proposal_state == 'awaiting_review' OR (ticket.status == 'blocked' AND ticket.block_kind == 'review') -> "person/review"
    if proposal_state == "awaiting_review" or (status == "blocked" and block_kind == "review"):
        return "person/review"

    # 4. ticket.status IN ('blocked', 'failed') OR (triage AND scoped) -> "person/blocked"
    if status in ("blocked", "failed"):
        return "person/blocked"
    if status == "triage" and (
        ticket_dict.get("mandate_id") is not None
        or block_kind in ("gate_worker", "gate_env", "spend_ceiling")
    ):
        return "person/blocked"

    # 5. ticket.status IN ('running', 'claimed') OR ctx.sa_state == 'working' -> "working"
    sa_state = ctx_dict.get("sa_state")
    if status in ("running", "claimed") or sa_state == "working":
        return "working"

    # 6. ticket.status IN ('ready', 'todo') OR ctx.sa_state == 'dispatched' -> "queued"
    if status in ("ready", "todo") or sa_state == "dispatched":
        return "queued"

    # 7. ticket.status == 'done' -> "closed"
    if status == "done":
        return "closed"

    # 8. else -> "idle"
    return "idle"


def tickets(conn: sqlite3.Connection, stage: str | None = None) -> list[dict[str, Any]]:
    """Return tickets with their derived human-attention / orchestrator stages.

    Pinned entry shape in returned list:
    {
        "ticket_id": str,
        "id": str,
        "stage": str,
        "title": str,
        "status": str,
        "block_kind": str | None,
        "assignee": str | None,
        "priority": int,
        "created_at": int,
        "ticket_kind": str | None,
        "mandate_id": int | None,
    }

    Args:
        conn: SQLite connection to the kanban board.
        stage: Optional stage filter string (e.g. 'person/delivery', 'queued',
               or 'person' to match any 'person/*' stage).

    Returns:
        list[dict[str, Any]]: Filtered tickets, newest first.
    """
    # 1. Gather latest outbox state per ticket
    latest_outbox_status: dict[str, str] = {}
    latest_outbox_failure: dict[str, str] = {}
    cur = conn.execute(
        "SELECT ticket_id, status, last_error FROM sa_outbox "
        "WHERE ticket_id IS NOT NULL ORDER BY id ASC"
    )
    for r in cur.fetchall():
        tid = str(r[0])
        status = str(r[1] or "")
        err = str(r[2] or "")
        latest_outbox_status[tid] = status
        if status == "failed" or err:
            latest_outbox_failure[tid] = err or "delivery_failed"

    # 2. Gather latest decisions outcome per ticket
    latest_decisions: dict[str, str] = {}
    cur = conn.execute(
        "SELECT ticket_id, outcome FROM sa_decisions "
        "WHERE ticket_id IS NOT NULL ORDER BY id ASC"
    )
    for r in cur.fetchall():
        latest_decisions[str(r[0])] = str(r[1] or "")

    # 3. Gather latest mandates status per ticket
    latest_mandates: dict[str, str] = {}
    cur = conn.execute(
        "SELECT ticket_id, status FROM sa_mandates "
        "WHERE ticket_id IS NOT NULL ORDER BY id ASC"
    )
    for r in cur.fetchall():
        latest_mandates[str(r[0])] = str(r[1] or "")

    # 4. Query tasks and derive stages
    cur = conn.execute(
        "SELECT id, title, status, block_kind, assignee, priority, created_at, ticket_kind, mandate_id "
        "FROM tasks ORDER BY created_at DESC, id ASC"
    )
    rows = cur.fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        t_dict = _row_to_dict(cur, row)
        tid = str(t_dict.get("id") or "")

        ctx: dict[str, Any] = {}
        # Set last_delivery_error if outbox has a failure and wasn't subsequently sent
        if tid in latest_outbox_failure and latest_outbox_status.get(tid) != "sent":
            ctx["last_delivery_error"] = latest_outbox_failure[tid]

        # Set proposal_state if awaiting answer or review
        dec_outcome = latest_decisions.get(tid)
        if dec_outcome in ("awaiting_answer", "awaiting_review"):
            ctx["proposal_state"] = dec_outcome
        else:
            mand_status = latest_mandates.get(tid)
            if mand_status in ("awaiting_answer", "awaiting_review"):
                ctx["proposal_state"] = mand_status

        derived = derived_stage(t_dict, ctx)

        if stage is not None:
            if stage in ("person", "person/"):
                if not derived.startswith("person/"):
                    continue
            elif derived != stage:
                continue

        entry = {
            "ticket_id": tid,
            "id": tid,
            "stage": derived,
            "title": str(t_dict.get("title") or ""),
            "status": str(t_dict.get("status") or ""),
            "block_kind": t_dict.get("block_kind"),
            "assignee": t_dict.get("assignee"),
            "priority": int(t_dict.get("priority") or 0),
            "created_at": int(t_dict.get("created_at") or 0),
            "ticket_kind": t_dict.get("ticket_kind"),
            "mandate_id": t_dict.get("mandate_id"),
        }
        results.append(entry)

    return results


def needs_person(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every ticket whose derived stage starts with 'person/'.

    Evaluates tickets against :func:`derived_stage` using task state and associated
    outbox / decision / mandate context.

    Pinned entry shape in returned list:
    {
        "ticket_id": str,
        "id": str,
        "stage": str,
        "title": str,
        "status": str,
        "block_kind": str | None,
        "assignee": str | None,
        "priority": int,
        "created_at": int,
        "ticket_kind": str | None,
        "mandate_id": int | None,
    }

    Returns:
        list[dict[str, Any]]: Tickets requiring human attention, newest first.
    """
    return tickets(conn, stage="person")


def health(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    """Retrieve system health indicators for the superagent orchestrator.

    Pinned keys in returned dict:
    {
        "paused": bool,           # sa_meta key 'paused'
        "outbox_due": int,        # count of queued outbox rows due for sending
        "outbox_failed": int,     # count of failed outbox rows
        "eval_queued": int,       # count of sa_triggers awaiting evaluation
        "unavailable_24h": int,   # count of decisions with outcome='unavailable' within last 24h
    }

    Returns:
        dict[str, Any]: Orchestrator health summary dict.
    """
    ts = int(now) if now is not None else int(time.time())

    # 1. paused from sa_meta
    cur = conn.execute("SELECT value FROM sa_meta WHERE key = 'paused'")
    row = cur.fetchone()
    paused = False
    if row and row[0] is not None:
        val = str(row[0]).strip().lower()
        paused = val in ("1", "true", "yes", "t", "paused")

    # 2. outbox_due from sa_outbox
    cur = conn.execute(
        "SELECT COUNT(*) FROM sa_outbox WHERE status = 'queued' AND (next_attempt_at <= ? OR next_attempt_at = 0)",
        (ts,),
    )
    outbox_due = int(cur.fetchone()[0])

    # 3. outbox_failed from sa_outbox
    cur = conn.execute("SELECT COUNT(*) FROM sa_outbox WHERE status = 'failed'")
    outbox_failed = int(cur.fetchone()[0])

    # 4. eval_queued from sa_triggers
    cur = conn.execute("SELECT COUNT(*) FROM sa_triggers WHERE eval_state = 'queued'")
    eval_queued = int(cur.fetchone()[0])

    # 5. unavailable_24h from sa_decisions
    cutoff_24h = ts - 86400
    cur = conn.execute(
        "SELECT COUNT(*) FROM sa_decisions WHERE outcome = 'unavailable' AND created_at >= ?",
        (cutoff_24h,),
    )
    unavailable_24h = int(cur.fetchone()[0])

    return {
        "paused": paused,
        "outbox_due": outbox_due,
        "outbox_failed": outbox_failed,
        "eval_queued": eval_queued,
        "unavailable_24h": unavailable_24h,
    }


def flow_counts_24h(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    """Retrieve operational flow counts over the last 86,400 seconds (24 hours).

    Counts over the last 86400s: triggers received, decisions by outcome,
    tickets created, and replies sent/failed/dropped from sa_outbox.

    Pinned keys in returned dict:
    {
        "triggers_received": int,               # count of sa_triggers received
        "decisions_by_outcome": dict[str, int], # decision outcome -> count mapping
        "tickets_created": int,                 # count of tasks created
        "replies_sent": int,                    # outbox rows with status='sent'
        "replies_failed": int,                  # outbox rows with status='failed'
        "replies_dropped": int,                 # outbox rows with status='dropped'
        "replies": {                            # nested counts for replies
            "sent": int,
            "failed": int,
            "dropped": int,
        },
    }

    Returns:
        dict[str, Any]: 24-hour activity flow summary.
    """
    ts = int(now) if now is not None else int(time.time())
    cutoff = ts - 86400

    # 1. triggers received
    cur = conn.execute(
        "SELECT COUNT(*) FROM sa_triggers WHERE created_at >= ? AND created_at <= ?",
        (cutoff, ts),
    )
    triggers_received = int(cur.fetchone()[0])

    # 2. decisions by outcome
    cur = conn.execute(
        "SELECT outcome, COUNT(*) FROM sa_decisions WHERE created_at >= ? AND created_at <= ? GROUP BY outcome",
        (cutoff, ts),
    )
    decisions_by_outcome = {str(row[0]): int(row[1]) for row in cur.fetchall()}

    # 3. tickets created
    cur = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND created_at <= ?",
        (cutoff, ts),
    )
    tickets_created = int(cur.fetchone()[0])

    # 4. replies sent / failed / dropped (from sa_outbox)
    cur = conn.execute(
        "SELECT status, COUNT(*) FROM sa_outbox "
        "WHERE created_at >= ? AND created_at <= ? AND status IN ('sent', 'failed', 'dropped') "
        "GROUP BY status",
        (cutoff, ts),
    )
    replies_map = {str(row[0]): int(row[1]) for row in cur.fetchall()}
    replies_sent = replies_map.get("sent", 0)
    replies_failed = replies_map.get("failed", 0)
    replies_dropped = replies_map.get("dropped", 0)

    return {
        "triggers_received": triggers_received,
        "decisions_by_outcome": decisions_by_outcome,
        "tickets_created": tickets_created,
        "replies_sent": replies_sent,
        "replies_failed": replies_failed,
        "replies_dropped": replies_dropped,
        "replies": {
            "sent": replies_sent,
            "failed": replies_failed,
            "dropped": replies_dropped,
        },
    }


def thread_timeline(conn: sqlite3.Connection, thread_key: str) -> list[dict[str, Any]]:
    """Return chronological merge of triggers, decisions, and inputs for a thread.

    Merges:
    - sa_triggers by thread_key
    - sa_decisions by thread_key
    - sa_inputs for tickets subscribed to the thread (via sa_subscriptions)

    Pinned entry shape:
    {
        "ts": int,       # event timestamp (created_at)
        "kind": str,     # 'trigger' | 'decision' | 'input'
        "summary": str,  # human-readable one-line summary
    }

    Summary formatting per kind:
    - trigger: "Trigger [{source}] {kind}: {body_snippet}"
    - decision: "Decision outcome={outcome} (strength={strength}, ticket={ticket_id})"
    - input: "Input [{kind}] ticket={ticket_id}: {body_snippet}"

    Returns:
        list[dict[str, Any]]: Chronologically merged events (oldest first).
    """
    entries: list[dict[str, Any]] = []

    # 1. Triggers by thread_key
    cur = conn.execute(
        "SELECT source, kind, body, created_at FROM sa_triggers WHERE thread_key = ?",
        (thread_key,),
    )
    for r in cur.fetchall():
        source = str(r[0] or "")
        t_kind = str(r[1] or "")
        body_snip = _one_line(r[2])
        summary = (
            f"Trigger [{source}] {t_kind}: {body_snip}"
            if body_snip
            else f"Trigger [{source}] {t_kind}"
        )
        entries.append({
            "ts": int(r[3]),
            "kind": "trigger",
            "summary": summary,
        })

    # 2. Decisions by thread_key
    cur = conn.execute(
        "SELECT outcome, strength, ticket_id, created_at FROM sa_decisions WHERE thread_key = ?",
        (thread_key,),
    )
    for r in cur.fetchall():
        outcome = str(r[0] or "")
        strength = str(r[1] or "none")
        ticket_id = str(r[2] or "none")
        summary = f"Decision outcome={outcome} (strength={strength}, ticket={ticket_id})"
        entries.append({
            "ts": int(r[3]),
            "kind": "decision",
            "summary": summary,
        })

    # 3. Inputs for tickets subscribed to this thread
    cur = conn.execute(
        """
        SELECT DISTINCT i.ticket_id, i.kind, i.body, i.created_at
        FROM sa_inputs i
        JOIN sa_subscriptions s ON i.ticket_id = s.ticket_id
        WHERE s.thread_key = ?
        """,
        (thread_key,),
    )
    for r in cur.fetchall():
        ticket_id = str(r[0] or "")
        i_kind = str(r[1] or "")
        body_snip = _one_line(r[2])
        summary = (
            f"Input [{i_kind}] ticket={ticket_id}: {body_snip}"
            if body_snip
            else f"Input [{i_kind}] ticket={ticket_id}"
        )
        entries.append({
            "ts": int(r[3]),
            "kind": "input",
            "summary": summary,
        })

    # Chronological sort (oldest first)
    entries.sort(key=lambda x: (x["ts"], x["kind"], x["summary"]))
    return entries


def ticket_detail(conn: sqlite3.Connection, ticket_id: str) -> dict[str, Any] | None:
    """Return comprehensive detail for a single ticket.

    Retrieves the task row plus associated mandate (if any), inputs, and outbox rows.

    Pinned return shape:
    {
        ... task columns ...,
        "mandate": dict | None,
        "inputs": list[dict],
        "outbox": list[dict],
        "task": dict, # copy of base task columns
    }

    Returns:
        dict[str, Any] | None: Ticket details dict, or None if ticket_id is missing from tasks.
    """
    cur = conn.execute("SELECT * FROM tasks WHERE id = ?", (ticket_id,))
    task_row = cur.fetchone()
    if task_row is None:
        return None

    task_dict = _row_to_dict(cur, task_row)

    # Mandate (if any)
    cur = conn.execute(
        "SELECT * FROM sa_mandates WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    )
    mandate_row = cur.fetchone()
    mandate_dict = _row_to_dict(cur, mandate_row) if mandate_row else None

    # Inputs
    cur = conn.execute(
        "SELECT * FROM sa_inputs WHERE ticket_id = ? ORDER BY created_at ASC, id ASC",
        (ticket_id,),
    )
    inputs_list = [_row_to_dict(cur, r) for r in cur.fetchall()]

    # Outbox rows
    cur = conn.execute(
        "SELECT * FROM sa_outbox WHERE ticket_id = ? ORDER BY created_at ASC, id ASC",
        (ticket_id,),
    )
    outbox_list = [_row_to_dict(cur, r) for r in cur.fetchall()]

    result = dict(task_dict)
    result["mandate"] = mandate_dict
    result["inputs"] = inputs_list
    result["outbox"] = outbox_list
    result["task"] = dict(task_dict)
    return result


def decisions(
    conn: sqlite3.Connection,
    outcome: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return decisions from sa_decisions ordered newest first, with optional outcome filter.

    Returns:
        list[dict[str, Any]]: List of decision row dicts, newest first.
    """
    max_rows = max(1, int(limit))
    if outcome is not None:
        cur = conn.execute(
            "SELECT * FROM sa_decisions WHERE outcome = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (outcome, max_rows),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM sa_decisions ORDER BY created_at DESC, id DESC LIMIT ?",
            (max_rows,),
        )
    return [_row_to_dict(cur, r) for r in cur.fetchall()]


def dropped_and_failed_replies(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return sa_outbox rows with status='dropped' OR (status='failed' AND attempts>=3), newest first.

    Returns:
        list[dict[str, Any]]: List of dropped and repeatedly failed outbox row dicts.
    """
    query = (
        "SELECT * FROM sa_outbox "
        "WHERE status = 'dropped' OR (status = 'failed' AND attempts >= 3) "
        "ORDER BY created_at DESC, id DESC"
    )
    if limit is not None:
        query += f" LIMIT {max(1, int(limit))}"
    cur = conn.execute(query)
    return [_row_to_dict(cur, r) for r in cur.fetchall()]


def overview(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    """Retrieve comprehensive attention overview.

    Combines health indicators, 24-hour flow counts, human attention tickets,
    and tickets grouped by derived stage.

    Pinned return shape:
    {
        "health": {
            "paused": bool,
            "outbox_due": int,
            "outbox_failed": int,
            "eval_queued": int,
            "unavailable_24h": int,
        },
        "flow_counts_24h": {
            "triggers_received": int,
            "decisions_by_outcome": dict[str, int],
            "tickets_created": int,
            "replies_sent": int,
            "replies_failed": int,
            "replies_dropped": int,
            "replies": dict[str, int],
        },
        "needs_person": list[dict[str, Any]],
        "needs_person_count": int,
        "stages": dict[str, list[dict[str, Any]]],
        "tickets_by_stage": dict[str, list[dict[str, Any]]],
        "tickets": list[dict[str, Any]],
        # Conditionally included when dropped or failed replies exist:
        "dropped_and_failed_replies": list[dict[str, Any]],
    }

    Returns:
        dict[str, Any]: Attention panel overview summary dict.
    """
    ts = int(now) if now is not None else int(time.time())
    h = health(conn, ts)
    f = flow_counts_24h(conn, ts)
    all_tkts = tickets(conn)
    np = [t for t in all_tkts if t["stage"].startswith("person/")]

    stage_keys = [
        "person/delivery",
        "person/answer",
        "person/review",
        "person/blocked",
        "working",
        "queued",
        "closed",
        "idle",
    ]
    by_stage: dict[str, list[dict[str, Any]]] = {s: [] for s in stage_keys}
    for t in all_tkts:
        st = t["stage"]
        if st in by_stage:
            by_stage[st].append(t)
        else:
            by_stage.setdefault(st, []).append(t)

    result: dict[str, Any] = {
        "health": h,
        "flow_counts_24h": f,
        "needs_person": np,
        "needs_person_count": len(np),
        "stages": by_stage,
        "tickets_by_stage": by_stage,
        "tickets": all_tkts,
    }

    bad_replies = dropped_and_failed_replies(conn)
    if bad_replies:
        result["dropped_and_failed_replies"] = bad_replies

    return result


def threads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List distinct thread keys with their latest trigger and decision summaries.

    Pinned entry shape in returned list:
    {
        "thread_key": str,
        "latest_trigger_summary": str | None,
        "latest_decision_summary": str | None,
        "latest_summary": str,
        "latest_trigger": dict[str, Any] | None,
        "latest_decision": dict[str, Any] | None,
        "latest_activity_at": int,
        "updated_at": int,
    }

    Returns:
        list[dict[str, Any]]: Distinct threads ordered by most recent activity.
    """
    cur = conn.execute(
        """
        SELECT DISTINCT thread_key FROM (
            SELECT thread_key FROM sa_triggers WHERE thread_key IS NOT NULL AND thread_key != ''
            UNION
            SELECT thread_key FROM sa_decisions WHERE thread_key IS NOT NULL AND thread_key != ''
            UNION
            SELECT thread_key FROM sa_subscriptions WHERE thread_key IS NOT NULL AND thread_key != ''
        )
        """
    )
    thread_keys = [str(r[0]) for r in cur.fetchall()]

    results: list[dict[str, Any]] = []
    for tk in thread_keys:
        t_cur = conn.execute(
            "SELECT source, kind, body, created_at FROM sa_triggers "
            "WHERE thread_key = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (tk,),
        )
        t_row = t_cur.fetchone()
        t_summary: str | None = None
        t_dict: dict[str, Any] | None = None
        t_ts = 0
        if t_row:
            t_source = str(t_row[0] or "")
            t_kind = str(t_row[1] or "")
            t_body = _one_line(t_row[2])
            t_ts = int(t_row[3] or 0)
            t_summary = f"Trigger [{t_source}] {t_kind}: {t_body}" if t_body else f"Trigger [{t_source}] {t_kind}"
            t_dict = {
                "source": t_source,
                "kind": t_kind,
                "body": str(t_row[2] or ""),
                "summary": t_summary,
                "created_at": t_ts,
            }

        d_cur = conn.execute(
            "SELECT outcome, strength, ticket_id, created_at FROM sa_decisions "
            "WHERE thread_key = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (tk,),
        )
        d_row = d_cur.fetchone()
        d_summary: str | None = None
        d_dict: dict[str, Any] | None = None
        d_ts = 0
        if d_row:
            d_outcome = str(d_row[0] or "")
            d_strength = str(d_row[1] or "none")
            d_ticket = str(d_row[2] or "none")
            d_ts = int(d_row[3] or 0)
            d_summary = f"Decision outcome={d_outcome} (strength={d_strength}, ticket={d_ticket})"
            d_dict = {
                "outcome": d_outcome,
                "strength": d_strength,
                "ticket_id": d_ticket,
                "summary": d_summary,
                "created_at": d_ts,
            }

        activity_ts = max(t_ts, d_ts)
        if d_ts >= t_ts and d_summary:
            latest_summary = d_summary
        elif t_summary:
            latest_summary = t_summary
        else:
            latest_summary = "No activity"

        results.append({
            "thread_key": tk,
            "latest_trigger_summary": t_summary,
            "latest_decision_summary": d_summary,
            "latest_summary": latest_summary,
            "latest_trigger": t_dict,
            "latest_decision": d_dict,
            "latest_activity_at": activity_ts,
            "updated_at": activity_ts,
        })

    results.sort(key=lambda x: (x["latest_activity_at"], x["thread_key"]), reverse=True)
    return results


def settings_view(
    conn: sqlite3.Connection,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retrieve superagent configuration and runtime operational state.

    Reads mode, enabled, paused, and active surfaces from config.yaml and
    the sa_meta table.

    Pinned return shape:
    {
        "mode": str,             # 'off' | 'observe' | 'auto'
        "enabled": bool,         # whether superagent is enabled
        "paused": bool,          # whether superagent is paused via sa_meta
        "surfaces": list[str] | dict[str, Any], # configured inbound surfaces
        "accept_mode": str,      # alias for mode
        "sources": dict[str, Any],
        "eval_timeout_seconds": int,
        "worker_slots": int,
        "human_reserve_slots": int,
        "router_profile": str | None,
        "router_model": dict[str, Any] | str | None,
        "worker_model": dict[str, Any] | str | None,
        "rules": list[dict[str, Any]],
        "github": dict[str, Any],
    }

    Returns:
        dict[str, Any]: Superagent settings snapshot.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
        except Exception:  # health: allow BLE001 -- fallback to empty dict if load_config fails
            cfg = {}
    else:
        cfg = config

    sa_raw = cfg.get("superagent", {}) if isinstance(cfg, dict) else {}
    if not isinstance(sa_raw, dict):
        sa_raw = {}

    enabled = bool(sa_raw.get("enabled", False))
    mode = str(sa_raw.get("accept_mode", "observe"))
    if mode not in {"off", "observe", "auto"}:
        mode = "observe"

    # Read paused from sa_meta
    cur = conn.execute("SELECT value FROM sa_meta WHERE key = 'paused'")
    row = cur.fetchone()
    paused = False
    if row and row[0] is not None:
        val = str(row[0]).strip().lower()
        paused = val in ("1", "true", "yes", "t", "paused")

    # Read surfaces / sources
    raw_surfaces = sa_raw.get("surfaces")
    raw_sources = sa_raw.get("sources", {})
    if raw_surfaces is not None:
        surfaces = raw_surfaces
    elif isinstance(raw_sources, dict):
        surfaces = list(raw_sources.keys())
    elif isinstance(raw_sources, (list, tuple)):
        surfaces = list(raw_sources)
    else:
        surfaces = []

    eval_timeout = int(sa_raw.get("eval_timeout_seconds", 60) or 60)
    worker_slots = int(sa_raw.get("worker_slots", 4) or 4)
    human_reserve = int(sa_raw.get("human_reserve_slots", 1) or 1)

    return {
        "mode": mode,
        "enabled": enabled,
        "paused": paused,
        "surfaces": surfaces,
        "accept_mode": mode,
        "sources": raw_sources if isinstance(raw_sources, dict) else {},
        "eval_timeout_seconds": eval_timeout,
        "worker_slots": worker_slots,
        "human_reserve_slots": human_reserve,
        "router_profile": sa_raw.get("router_profile"),
        "router_model": sa_raw.get("router_model"),
        "worker_model": sa_raw.get("worker_model"),
        "rules": list(sa_raw.get("rules", [])),
        "github": sa_raw.get("github") or {
            "repository": None,
            "receive_all": {"issues": False, "pull_requests": False, "comments": False, "ci": False},
        },
    }

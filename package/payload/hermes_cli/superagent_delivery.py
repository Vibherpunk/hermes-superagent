"""Super-agent inbound Input delivery and wake coordination (Phase 3, Card 3.3).

Wires unread sa_inputs delivery to the worker run wake path (the existing
kanban launch/resume path). Renders unread inputs as one message (Trigger cards:
source, actor, body; pointers for long bodies per spec).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional


BODY_TRUNCATE_LIMIT = 2000


def has_input_read_tool() -> bool:
    """Return True if a kanban_show-style tool exists to read sa_inputs by id.

    Observation: No kanban_show-style read exists for sa_inputs.id in the
    Hermes codebase (E-019/E-021/E-027). Per card specification:
    'pointers not full dumps for bodies >2,000 chars -- truncate with a sa_inputs.id
    reference the Worker can read via kanban_show-style read; if no such read exists,
    include full body and record the deviation as an observation, not a blocker'.
    Therefore, full body is included by default unless force_pointer is requested.
    """
    return False


def render_trigger_card(
    inp: dict[str, Any] | sqlite3.Row,
    trigger: dict[str, Any] | sqlite3.Row | None = None,
    *,
    force_pointer: bool = False,
) -> str:
    """Render a single unread input as a Trigger card (source, actor, body)."""
    inp_id = inp["id"] if isinstance(inp, sqlite3.Row) else inp.get("id")
    inp_kind = inp["kind"] if isinstance(inp, sqlite3.Row) else inp.get("kind", "trigger")
    is_person = inp["is_person"] if isinstance(inp, sqlite3.Row) else inp.get("is_person", 0)
    raw_body = inp["body"] if isinstance(inp, sqlite3.Row) else inp.get("body", "")
    body_str = str(raw_body or "")

    source = None
    actor = None
    if trigger is not None:
        source = trigger["source"] if isinstance(trigger, sqlite3.Row) else trigger.get("source")
        actor = trigger["actor_source_id"] if isinstance(trigger, sqlite3.Row) else trigger.get("actor_source_id")

    # If trigger wasn't passed directly, check if inp dictionary has joined trigger columns
    if not source:
        try:
            source = inp["source"] if isinstance(inp, sqlite3.Row) else inp.get("source")
        except (KeyError, IndexError):
            source = None

    if not actor:
        try:
            actor = inp["actor_source_id"] if isinstance(inp, sqlite3.Row) else inp.get("actor_source_id")
        except (KeyError, IndexError):
            actor = None

    if not source:
        source = inp_kind if inp_kind != "trigger" else "unknown"

    if not actor:
        actor = "person" if is_person else "system"

    # Truncate with pointer if >2,000 chars and (force_pointer or a read tool exists)
    if len(body_str) > BODY_TRUNCATE_LIMIT and (force_pointer or has_input_read_tool()):
        truncated = body_str[:BODY_TRUNCATE_LIMIT]
        body_text = f"{truncated}... [pointer: sa_inputs.id={inp_id}]"
    else:
        body_text = body_str

    return (
        f"[Trigger Card: id={inp_id}]\n"
        f"Source: {source}\n"
        f"Actor: {actor}\n"
        f"Body:\n{body_text}"
    )


def render_inputs_message(
    inputs: list[dict[str, Any] | sqlite3.Row],
    *,
    force_pointer: bool = False,
) -> str:
    """Render unread inputs as one combined message for the run's input."""
    cards = [render_trigger_card(inp, force_pointer=force_pointer) for inp in inputs]
    return "\n\n---\n\n".join(cards)


def fetch_unread_inputs(
    conn: sqlite3.Connection,
    ticket_id: str,
) -> list[dict[str, Any]]:
    """Query unread inputs for ticket_id in chronological order."""
    rows = conn.execute(
        """
        SELECT i.id, i.ticket_id, i.trigger_id, i.kind, i.body, i.is_person,
               i.context_only, i.created_at, t.source, t.actor_source_id
        FROM sa_inputs i
        LEFT JOIN sa_triggers t ON t.id = i.trigger_id
        WHERE i.ticket_id = ? AND i.read_at IS NULL
        ORDER BY i.created_at ASC, i.id ASC
        """,
        (ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_inputs_read(
    conn: sqlite3.Connection,
    input_ids: list[int],
    read_at: Optional[int] = None,
) -> int:
    """Mark inputs as read by setting read_at epoch timestamp."""
    if not input_ids:
        return 0
    now = int(read_at if read_at is not None else time.time())
    placeholders = ",".join("?" for _ in input_ids)
    cur = conn.execute(
        f"UPDATE sa_inputs SET read_at = ? WHERE id IN ({placeholders}) AND read_at IS NULL",
        [now, *input_ids],
    )
    return cur.rowcount


def deliver_inputs(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    now: Optional[int] = None,
    force_pointer: bool = False,
) -> tuple[Optional[str], list[int]]:
    """Render unread inputs for ticket_id as one message and mark them read_at.

    Returns (rendered_message, delivered_input_ids).
    If no unread inputs, returns (None, []).
    """
    unread = fetch_unread_inputs(conn, ticket_id)
    if not unread:
        return None, []

    msg = render_inputs_message(unread, force_pointer=force_pointer)
    ids = [u["id"] for u in unread]
    mark_inputs_read(conn, ids, read_at=now)
    return msg, ids


def get_ticket_wake_kind(
    conn: sqlite3.Connection,
    ticket_id: str,
) -> Optional[str]:
    """Check if ticket has unread inputs.

    Per E-023:
    - If newest unread input has is_person=1: returns 'person_wake'
    - If newest unread input has is_person=0: returns 'wake'
    - If no unread inputs: returns None
    """
    row = conn.execute(
        """
        SELECT is_person FROM sa_inputs
        WHERE ticket_id = ? AND read_at IS NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (ticket_id,),
    ).fetchone()
    if row is None:
        return None
    is_person = row["is_person"] if isinstance(row, sqlite3.Row) else row[0]
    return "person_wake" if is_person == 1 else "wake"


def request_launch_for_unread_inputs(
    conn: sqlite3.Connection,
    board: Optional[str] = None,
) -> list[str]:
    """When no run is active or queued, delivery requests the normal launch.

    Promotes tickets with unread inputs from non-active/non-queued status to 'ready'.
    """
    from hermes_cli import kanban_db as _kb

    rows = conn.execute(
        "SELECT DISTINCT ticket_id FROM sa_inputs WHERE read_at IS NULL"
    ).fetchall()
    promoted: list[str] = []
    with _kb.write_txn(conn):
        for r in rows:
            tid = r[0] if isinstance(r, (tuple, list)) else r["ticket_id"]
            task = _kb.get_task(conn, tid)
            if task is None:
                continue
            # If task is not running (active) and not ready (queued)
            if task.status not in ("running", "ready"):
                conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ?",
                    (tid,),
                )
                _kb._append_event(
                    conn, tid, "promoted",
                    {"reason": "inbound_input", "status": "ready"}
                )
                promoted.append(tid)
    return promoted


def dispatch_task_acquire_slot(
    conn: sqlite3.Connection,
    task_id: str,
    lane: str,
    result: Any,
    dry_run: bool,
    slot_pool: Any,
) -> tuple[bool, Optional[str]]:
    """Acquire a slot for task_id, accounting for person_wake vs normal launch."""
    from gateway.kanban_watchers_dispatcher import acquire_lane_slot

    wake_kind = get_ticket_wake_kind(conn, task_id)
    slot_kind = "person_wake" if wake_kind == "person_wake" else "launch"
    return acquire_lane_slot(
        slot_pool, task_id, lane, result, dry_run, conn, kind=slot_kind,
    )


def dispatch_task_on_claimed(
    conn: sqlite3.Connection,
    claimed: Any,
    slot_pool: Any,
    slot_token: Optional[str],
    board: Optional[str] = None,
) -> None:
    """Bind slot and deliver any unread inputs to the newly claimed task."""
    from gateway.kanban_watchers_dispatcher import bind_lane_slot
    from hermes_cli import kanban_db as _kb

    bind_lane_slot(slot_pool, claimed.id, slot_token, board=board, run_id=claimed.current_run_id)
    try:
        rendered, delivered_ids = deliver_inputs(conn, claimed.id)
        if rendered:
            claimed.run_input = rendered
            claimed.delivered_inputs = rendered
            _kb.add_comment(conn, claimed.id, author="dispatcher:inbound", body=rendered)
            _kb._append_event(
                conn, claimed.id, "input_delivered",
                {"input_ids": delivered_ids, "count": len(delivered_ids)},
                run_id=claimed.current_run_id,
            )
    except Exception as exc:  # health: allow BLE001 -- delivery failure must not crash spawn
        _kb._log.debug("superagent: deliver_inputs failed for %s (%s)", claimed.id, exc)

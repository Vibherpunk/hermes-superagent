"""Superagent Health Triggers API: emit internal health triggers for dispatcher anomalies.

This module provides health trigger emission for the Superagent subsystem (Phase 4).
Health triggers surface anomalies in the worker and dispatch subsystems (reclaims,
orphaned tasks, circuit-breaker trips, and exhausted failure budgets) as internal triggers
for orchestrator evaluation.

Integration & Dispatcher Tick Call Site:
    The Superagent pack calls for emission of health triggers directly inside the
    dispatcher tick loop. However, the dispatcher tick (located in
    ``gateway/kanban_watchers_dispatcher.py`` inside ``_KanbanDispatcher.tick_once_for_board``
    and ``hermes_cli/kanban_db_dispatch.py``) is gateway/runtime code owned by other
    lanes. Therefore, this module provides the standalone ``emit_health_triggers``
    function and unit tests, designed to be called at the conclusion of each dispatcher
    tick with the collected anomaly events from ``DispatchResult``:
        events = []
        if res.reclaimed > 0: ...
        if res.reconciled_orphans: ...
        if res.auto_blocked: ...
        emit_health_triggers(conn, events, now=time.time())

Direct Database Insertion vs. Orchestrator Receive Path:
    The Orchestrator's ``receive_trigger`` path is NOT used here; these rows are
    written directly into ``sa_triggers`` with ``eval_state='queued'`` so that the
    normal Superagent Eval pipeline processes them.
    Reason: ``receive_trigger`` is implemented in Lane B's ``hermes_cli/orchestrator.py``.
    To maintain single-writer lane independence across worktrees without cyclic
    dependencies or cross-lane merge conflicts during buildout, this module writes
    directly to ``sa_triggers``. At final integration, this function can optionally
    delegate to ``receive_trigger``. The inserted row shape matches ``receive_trigger``
    identically:
      - id: "trg_<uuid4hex12>" per DECISIONS E-005
      - source: "internal"
      - scope: "health"
      - thread_key: f"health:{kind}:{subject}"
      - kind: "health"
      - actor_source_id: NULL
      - actor_is_bot: 0
      - addressed: 0
      - body: one-line summary
      - meta_json: JSON encoded detail
      - correlation_json: "[]"
      - idempotency_key: f"health:{kind}:{subject}:{now // 3600}"
      - eval_state: "queued"
      - eval_requeues: 0
      - disposition: NULL
      - created_at: int(now)

Health Event Vocabulary Mapping:
    SPEC-V2 specifies four internal health events to be emitted as triggers:
    1. Stale run reclaimed by the dispatcher:
       - Repo actual vocabulary: 'reclaimed' (task_events.kind from release_stale_claims
         in hermes_cli/kanban_db.py) or 'stale' (from detect_stale_running in
         hermes_cli/kanban_db_dispatch.py).
       - Mapped kind: 'reclaimed'
    2. Orphaned task/run detected:
       - Repo actual vocabulary: 'orphaned_running' (payload reason in reconcile_orphaned_running
         in hermes_cli/kanban_db_dispatch.py), 'reconciled_orphans' (DispatchResult field),
         or 'reconciled' (task_events.kind).
       - Mapped kind: 'orphaned_running'
    3. Circuit-breaker trip in dispatch:
       - Repo actual vocabulary: 'circuit_breaker' (spawn-failure circuit breaker in
         _record_task_failure), or 'auto_blocked' (DispatchResult.auto_blocked /
         detect_crashed_workers in hermes_cli/kanban_db_dispatch.py).
       - Mapped kind: 'circuit_breaker'
    4. Worker run that exhausted its failure budget (gave_up):
       - Repo actual vocabulary: 'gave_up' (task_events.kind emitted by _record_task_failure
         in hermes_cli/kanban_db_dispatch.py).
       - Mapped kind: 'gave_up'

Hourly Deduplication:
    Triggers use hourly idempotency keys:
        f"health:{kind}:{subject}:{int(now) // 3600}"
    Using INSERT OR IGNORE, duplicate emissions for the same kind and subject within
    the same clock hour produce NO new row. Only triggers actually created are returned.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Generator
import uuid

from hermes_cli import kanban_db_connect as kbc

logger = logging.getLogger(__name__)

# Canonical four health event kinds per SPEC-V2
FOUR_MAPPED_KINDS: tuple[str, ...] = (
    "reclaimed",
    "orphaned_running",
    "circuit_breaker",
    "gave_up",
)
MAPPED_HEALTH_KINDS = FOUR_MAPPED_KINDS

# Mapping dictionary supporting repo vocabulary and common aliases
HEALTH_EVENT_MAPPINGS: dict[str, str] = {
    # (1) Stale run reclaimed by the dispatcher
    "reclaimed": "reclaimed",
    "stale": "reclaimed",
    "stale_reclaimed": "reclaimed",

    # (2) Orphaned task/run detected
    "orphaned_running": "orphaned_running",
    "reconciled_orphans": "orphaned_running",
    "reconciled": "orphaned_running",
    "orphaned_detected": "orphaned_running",
    "orphan": "orphaned_running",

    # (3) Circuit-breaker trip in dispatch
    "circuit_breaker": "circuit_breaker",
    "auto_blocked": "circuit_breaker",
    "circuit_breaker_tripped": "circuit_breaker",

    # (4) Worker run that exhausted its failure budget (gave_up)
    "gave_up": "gave_up",
    "failure_budget_exhausted": "gave_up",
}

__all__ = [
    "CANONICAL_HEALTH_KINDS",
    "FOUR_MAPPED_KINDS",
    "HEALTH_EVENT_MAPPINGS",
    "MAPPED_HEALTH_KINDS",
    "emit_health_triggers",
]

CANONICAL_HEALTH_KINDS = FOUR_MAPPED_KINDS


@contextlib.contextmanager
def _resolve_conn(
    conn_or_path: sqlite3.Connection | Path | str,
) -> Generator[sqlite3.Connection, None, None]:
    """Yield a sqlite3.Connection from either an existing connection or a path."""
    if isinstance(conn_or_path, sqlite3.Connection):
        yield conn_or_path
    else:
        with kbc.connect_closing(Path(conn_or_path)) as conn:
            yield conn


def _format_summary(kind: str, subject: str, detail: Any) -> str:
    """Format a one-line summary for the trigger body."""
    if isinstance(detail, dict):
        msg = detail.get("error") or detail.get("reason") or detail.get("message")
        if msg:
            summary = f"Health alert [{kind}] on {subject}: {msg}"
        else:
            compact = json.dumps(detail, sort_keys=True)
            summary = f"Health alert [{kind}] on {subject}: {compact}"
    elif detail:
        summary = f"Health alert [{kind}] on {subject}: {detail}"
    else:
        summary = f"Health alert [{kind}] on {subject}"

    return " ".join(summary.split())


def _format_meta_json(detail: Any) -> str:
    """Serialize event detail into JSON for meta_json storage."""
    if detail is None:
        return "{}"
    if isinstance(detail, dict):
        return json.dumps(detail, sort_keys=True)
    return json.dumps({"detail": detail}, sort_keys=True)


def emit_health_triggers(
    conn: sqlite3.Connection | Path | str,
    events: list[dict[str, Any]],
    now: float,
) -> list[str]:
    """Emit internal health triggers into ``sa_triggers`` with hourly deduplication.

    Args:
        conn: An open ``sqlite3.Connection`` or a filesystem path to the Kanban DB.
        events: List of event dicts ``{"kind": str, "subject": str, "detail": Any}``.
        now: Epoch timestamp (float or int) used for creation time and hourly dedup bucket.

    Returns:
        List of newly created trigger IDs (e.g. ``["trg_1a2b3c4d5e6f"]``).
        Deduplicated / ignored triggers are excluded.

    Raises:
        ValueError: If an event has an unknown or missing kind, or missing subject.
    """
    if not events:
        return []

    validated_events: list[tuple[str, str, Any]] = []
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"Event at index {idx} must be a dict, got {type(event).__name__}")
        raw_kind = event.get("kind")
        if not raw_kind or not isinstance(raw_kind, str) or raw_kind not in HEALTH_EVENT_MAPPINGS:
            raise ValueError(
                f"Unknown health event kind: {raw_kind!r}. "
                f"Expected one of: {sorted(HEALTH_EVENT_MAPPINGS.keys())}"
            )
        subject = event.get("subject")
        if subject is None or not str(subject).strip():
            raise ValueError(f"Event at index {idx} must have a non-empty 'subject'")

        mapped_kind = HEALTH_EVENT_MAPPINGS[raw_kind]
        detail = event.get("detail")
        validated_events.append((mapped_kind, str(subject).strip(), detail))

    created_ids: list[str] = []
    hour_bucket = int(now) // 3600

    with _resolve_conn(conn) as active_conn:
        with kbc.write_txn(active_conn, allow_nested=True):
            for mapped_kind, subject_str, detail in validated_events:
                trigger_id = f"trg_{uuid.uuid4().hex[:12]}"
                thread_key = f"health:{mapped_kind}:{subject_str}"
                idempotency_key = f"health:{mapped_kind}:{subject_str}:{hour_bucket}"
                body = _format_summary(mapped_kind, subject_str, detail)
                meta_json = _format_meta_json(detail)

                cur = active_conn.execute(
                    """
                    INSERT OR IGNORE INTO sa_triggers (
                        id, source, scope, thread_key,
                        actor_source_id, actor_is_bot, addressed,
                        kind, body, meta_json, correlation_json,
                        idempotency_key, eval_state, eval_requeues,
                        disposition, created_at
                    ) VALUES (?, 'internal', 'health', ?, NULL, 0, 0, 'health', ?, ?, '[]', ?, 'queued', 0, NULL, ?)
                    """,
                    (
                        trigger_id,
                        thread_key,
                        body,
                        meta_json,
                        idempotency_key,
                        int(now),
                    ),
                )
                if cur.rowcount > 0:
                    created_ids.append(trigger_id)

    return created_ids

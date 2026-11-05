"""VibeHard Source poller for Super-agent (Phase 6 / Card 6.2).

Polls the VibeHard ledger (read-only SQLite database) and emits triggers to
the Super-agent orchestrator for newly submitted jobs and job status transitions.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.orchestrator import receive_trigger

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH = "~/.vibehard-agent/ledger.sqlite"
DEFAULT_STUCK_AFTER_SECONDS = 1800
WORKING_STATUSES = frozenset({"pending", "claimed", "running", "gating", "verifying"})


def _parse_epoch(val: Any) -> int | None:
    """Convert timestamp (int, float, numeric string, or ISO-8601) to epoch seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def _extract_spec_title(spec: str | None) -> str:
    """Extract the first non-empty line of the spec as the title."""
    if not spec:
        return ""
    for line in spec.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned
    return ""


def _format_body(
    job_kind: str,
    from_status: str | None,
    to_status: str,
    spec_title: str,
    target_dir: str | None,
    error: str | None,
    kind: str = "state_change",
) -> str:
    """Format body containing job kind, from->to status, title, target_dir, and error when failed."""
    if kind == "stuck":
        transition = f"{to_status} (stuck)"
    else:
        from_str = from_status if from_status else "None"
        transition = f"{from_str} -> {to_status}"
    lines = [
        f"Kind: {job_kind}",
        f"Status: {transition}",
        f"Title: {spec_title}",
        f"Target Dir: {target_dir or ''}",
    ]
    if to_status == "failed" and error:
        lines.append(f"Error: {error}")
    return "\n".join(lines)


def _get_cursor(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """Retrieve the stored cursor for a job from sa_source_cursors."""
    row = conn.execute(
        "SELECT cursor FROM sa_source_cursors WHERE source = 'vibehard' AND scope = ?",
        (str(job_id),),
    ).fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _set_cursor(
    conn: sqlite3.Connection,
    job_id: str,
    cursor_data: dict[str, Any],
    now: float,
) -> None:
    """Update stored cursor for a job in sa_source_cursors."""
    cursor_json = json.dumps(cursor_data, sort_keys=True)
    with write_txn(conn, allow_nested=True):
        conn.execute(
            """
            INSERT INTO sa_source_cursors (source, scope, cursor, updated_at)
            VALUES ('vibehard', ?, ?, ?)
            ON CONFLICT (source, scope) DO UPDATE SET
                cursor = excluded.cursor,
                updated_at = excluded.updated_at
            """,
            (str(job_id), cursor_json, int(now)),
        )


def _resolve_vibehard_config(cfg: Any) -> tuple[bool, str, int]:
    """Extract (enabled, ledger_path, stuck_after_seconds) from cfg."""
    if not isinstance(cfg, dict):
        return False, DEFAULT_LEDGER_PATH, DEFAULT_STUCK_AFTER_SECONDS

    # If full config or superagent block
    sa_block = cfg.get("superagent")
    if isinstance(sa_block, dict):
        if sa_block.get("enabled") is False:
            return False, DEFAULT_LEDGER_PATH, DEFAULT_STUCK_AFTER_SECONDS
        vh = sa_block.get("vibehard")
    else:
        vh = cfg.get("vibehard", cfg)

    if not isinstance(vh, dict):
        return False, DEFAULT_LEDGER_PATH, DEFAULT_STUCK_AFTER_SECONDS

    enabled = bool(vh.get("enabled", False))
    ledger_path = str(vh.get("ledger_path") or DEFAULT_LEDGER_PATH)
    stuck_after_seconds = int(vh.get("stuck_after_seconds", DEFAULT_STUCK_AFTER_SECONDS) or DEFAULT_STUCK_AFTER_SECONDS)
    return enabled, ledger_path, stuck_after_seconds


def poll_vibehard(
    conn: sqlite3.Connection,
    cfg: dict[str, Any] | None,
    now: float,
) -> list[str]:
    """Poll VibeHard ledger and emit triggers to orchestrator per Card 6.2.

    Opens the VibeHard ledger read-only (sqlite3 URI mode=ro) at configured path.
    Reads jobs joined to sessions for the client name.
    Maintains per-job cursor in sa_source_cursors with source='vibehard', scope=job_id.
    Emits triggers via receive_trigger for newly submitted jobs and status flips.

    Returns a list of created trigger IDs.
    """
    enabled, ledger_path, stuck_after_seconds = _resolve_vibehard_config(cfg)
    if not enabled:
        logger.debug("VibeHard source is disabled in configuration")
        return []

    try:
        raw_path = os.path.expanduser(ledger_path)
        path = Path(raw_path).resolve()
        if not path.is_file():
            logger.info("VibeHard ledger file does not exist or is not a file: %s", path)
            return []
    except Exception as exc:  # health: allow BLE001 -- path resolution failure boundary
        logger.warning("VibeHard ledger path invalid: %s (%s)", ledger_path, exc)
        return []

    uri = f"{path.as_uri()}?mode=ro"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as vh_conn:
            vh_conn.row_factory = sqlite3.Row
            cursor = vh_conn.execute(
                """
                SELECT
                    j.id,
                    j.session_id,
                    j.spec,
                    j.rigor,
                    j.status,
                    j.created_at,
                    j.updated_at,
                    j.result_path,
                    j.error,
                    j.retries,
                    j.idempotency_key,
                    j.runner_id,
                    j.claimed_at,
                    j.heartbeat_at,
                    j.pipeline_version,
                    j.kind,
                    j.target_dir,
                    j.progress_revision,
                    j.output_dir,
                    s.client
                FROM jobs j
                LEFT JOIN sessions s ON j.session_id = s.id
                ORDER BY j.created_at ASC, j.id ASC
                """
            )
            jobs = cursor.fetchall()
    except Exception as exc:  # health: allow BLE001 -- db read failure boundary
        logger.warning("Failed to open/read VibeHard ledger at %s: %s", uri, exc)
        return []

    emitted_trigger_ids: list[str] = []

    for row in jobs:
        job_id = str(row["id"])
        curr_status = str(row["status"] or "pending")
        curr_progress_rev = int(row["progress_revision"] or 0)
        curr_updated_at = row["updated_at"]
        curr_heartbeat_at = row["heartbeat_at"]
        curr_created_at = row["created_at"]
        client = str(row["client"] or "").strip()
        spec = row["spec"]
        job_kind = str(row["kind"] or "build")
        target_dir = row["target_dir"]
        error = row["error"]

        hb_epoch = _parse_epoch(curr_heartbeat_at)
        up_epoch = _parse_epoch(curr_updated_at)
        act_epoch = hb_epoch if hb_epoch is not None else up_epoch
        if act_epoch is None:
            act_epoch = _parse_epoch(curr_created_at)

        prev_cursor = _get_cursor(conn, job_id)

        prev_status: str | None = None
        prev_progress_rev = 0
        prev_stuck_flag = False
        prev_act_epoch: int | None = None
        status_advanced = False
        heartbeat_advanced = False

        if prev_cursor is not None:
            prev_status = str(prev_cursor.get("status") or "")
            prev_progress_rev = int(prev_cursor.get("progress_revision", 0) or 0)
            prev_stuck_flag = bool(prev_cursor.get("stuck_flag", False))
            prev_act_epoch = prev_cursor.get("activity_epoch")
            if prev_act_epoch is None:
                prev_hb = _parse_epoch(prev_cursor.get("heartbeat_at"))
                prev_up = _parse_epoch(prev_cursor.get("updated_at"))
                prev_act_epoch = prev_hb if prev_hb is not None else prev_up

            status_advanced = (curr_status != prev_status)
            if act_epoch is not None and prev_act_epoch is not None:
                if act_epoch > prev_act_epoch:
                    heartbeat_advanced = True
            elif prev_act_epoch is None and act_epoch is not None:
                heartbeat_advanced = True
            if curr_progress_rev > prev_progress_rev:
                heartbeat_advanced = True

        stuck_flag = prev_stuck_flag
        if status_advanced or heartbeat_advanced:
            stuck_flag = False

        new_cursor_data = {
            "status": curr_status,
            "progress_revision": curr_progress_rev,
            "updated_at": curr_updated_at,
            "heartbeat_at": curr_heartbeat_at,
            "activity_epoch": act_epoch,
            "stuck_flag": stuck_flag,
        }

        should_emit = False
        from_status: str | None = None
        to_status: str = curr_status
        kind: str = "submitted"

        if prev_cursor is None:
            # First sight: emit 'submitted'
            kind = "submitted"
            from_status = None
            to_status = curr_status
            should_emit = True
            new_cursor_data["stuck_flag"] = False
        else:
            if status_advanced:
                # Status change (awaiting_human stays plain state_change)
                if curr_status == "failed":
                    kind = "failed"
                else:
                    kind = "state_change"
                from_status = prev_status
                to_status = curr_status
                should_emit = True
                new_cursor_data["stuck_flag"] = False
            else:
                # No status change: check stuck if in non-terminal working status
                if curr_status in WORKING_STATUSES:
                    is_stale = (act_epoch is not None and (int(now) - act_epoch) >= stuck_after_seconds)
                    if is_stale and not stuck_flag:
                        kind = "stuck"
                        from_status = curr_status
                        to_status = curr_status
                        should_emit = True
                        new_cursor_data["stuck_flag"] = True
                    else:
                        new_cursor_data["stuck_flag"] = stuck_flag
                        if new_cursor_data != prev_cursor:
                            _set_cursor(conn, job_id, new_cursor_data, now)
                else:
                    # Not in a working status (awaiting_human, completed, etc.): never stuck
                    new_cursor_data["stuck_flag"] = False
                    if new_cursor_data != prev_cursor:
                        _set_cursor(conn, job_id, new_cursor_data, now)

        if should_emit:
            title = _extract_spec_title(spec)
            body = _format_body(
                job_kind=job_kind,
                from_status=from_status,
                to_status=to_status,
                spec_title=title,
                target_dir=target_dir,
                error=error,
                kind=kind,
            )

            actor = f"vibehard:{client}" if client else "vibehard"
            thread_key = f"vibehard:job-{job_id}"

            if kind == "stuck":
                act_key = act_epoch if act_epoch is not None else int(now)
                idempotency_key = f"vibehard:{job_id}:stuck:{act_key}"
            elif from_status is None:
                idempotency_key = f"vibehard:{job_id}:submitted"
            else:
                idempotency_key = f"vibehard:{job_id}:{from_status}->{to_status}:{curr_progress_rev}"

            meta = {
                "job_id": job_id,
                "session_id": row["session_id"],
                "client": client,
                "kind": job_kind,
                "status": curr_status,
                "from_status": from_status,
                "to_status": to_status,
                "spec": spec,
                "target_dir": target_dir,
                "error": error,
                "progress_revision": curr_progress_rev,
                "updated_at": curr_updated_at,
                "heartbeat_at": curr_heartbeat_at,
            }
            if kind == "stuck":
                meta["stuck"] = True
                meta["stuck_after_seconds"] = stuck_after_seconds

            trg_id, disposition = receive_trigger(
                conn,
                source="vibehard",
                scope=job_id,
                thread_key=thread_key,
                actor_source_id=actor,
                actor_is_bot=False,
                addressed=False,
                kind=kind,
                body=body,
                meta=meta,
                correlation=None,
                idempotency_key=idempotency_key,
                now=now,
            )

            _set_cursor(conn, job_id, new_cursor_data, now)

            if disposition != "duplicate":
                emitted_trigger_ids.append(trg_id)

    return emitted_trigger_ids

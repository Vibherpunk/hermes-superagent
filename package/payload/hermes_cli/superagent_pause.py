"""Superagent Pause API: pause state persistence and trigger deadline shifting.

This module provides pause state management for the Superagent subsystem (Phase 4).
The pause state persists in the ``sa_meta`` table with keys:
  - ``'paused'``: ``'0'`` (running) | ``'1'`` (paused)
  - ``'paused_at'``: epoch timestamp string when pause began, or ``''`` when running.

Double-Pause Invariant:
  If already paused, subsequent calls to ``set_paused(conn, True)`` keep the
  original ``paused_at`` timestamp rather than resetting it. This guarantees
  that the calculated pause delta reflects the entire duration from the initial
  pause event.

Resume & Deadline Shifting:
  When resuming via ``set_paused(conn, False, now)``, pending evaluation deadlines
  in ``sa_triggers`` (for triggers in 'queued' or 'evaluating' state) are shifted
  forward by the pause delta (now - paused_at). Shifting while still paused is a
  no-op; deadlines are only shifted on resume. Triggers without an ``eval_deadline``
  are untouched, and all other metadata keys in ``meta_json`` are preserved.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Generator

from hermes_cli import kanban_db_connect as kbc

logger = logging.getLogger(__name__)

__all__ = [
    "apply_pause_shift",
    "is_paused",
    "pause_delta",
    "set_paused",
    "shifted_deadline",
]


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


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Read a string value from sa_meta by key, or None if missing."""
    cur = conn.execute("SELECT value FROM sa_meta WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Write or replace a key-value pair in sa_meta."""
    conn.execute(
        "INSERT OR REPLACE INTO sa_meta (key, value) VALUES (?, ?)",
        (key, value),
    )


def is_paused(conn: sqlite3.Connection | Path | str) -> bool:
    """Return True if superagent is currently paused ('paused' == '1' in sa_meta)."""
    with _resolve_conn(conn) as c:
        val = _get_meta(c, "paused")
        return val == "1"


def pause_delta(
    conn: sqlite3.Connection | Path | str,
    now: float | None = None,
) -> int:
    """Return seconds since paused_at if currently paused, else 0."""
    now_int = int(now) if now is not None else int(time.time())
    with _resolve_conn(conn) as c:
        if not is_paused(c):
            return 0
        paused_at_str = _get_meta(c, "paused_at")
        if not paused_at_str:
            return 0
        try:
            paused_at = int(paused_at_str)
        except (ValueError, TypeError):
            return 0
        return max(0, now_int - paused_at)


def shifted_deadline(deadline_epoch: int, delta: int) -> int:
    """Pure arithmetic helper: calculate new deadline epoch shifted by pause delta."""
    return int(deadline_epoch) + int(delta)


def apply_pause_shift(
    conn: sqlite3.Connection | Path | str,
    now: float | None = None,
    *,
    _on_resume: bool = False,
) -> int:
    """Shift pending Eval deadlines in sa_triggers.meta_json by the pause delta on resume.

    Shifting while still paused is a no-op (apply only via resume). Triggers without
    an eval_deadline are untouched, and all other meta_json keys are preserved.
    Rewrites queued and evaluating triggers' meta_json eval_deadline by delta.
    Returns the number of triggers shifted.
    """
    now_int = int(now) if now is not None else int(time.time())

    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            # Shifting while still paused is a no-op (apply only via resume)
            if is_paused(c) and not _on_resume:
                return 0

            paused_at_str = _get_meta(c, "paused_at")
            if not paused_at_str:
                return 0

            try:
                paused_at = int(paused_at_str)
            except (ValueError, TypeError):
                _set_meta(c, "paused_at", "")
                return 0

            delta = max(0, now_int - paused_at)
            shifted_count = 0

            if delta > 0:
                cur = c.execute(
                    "SELECT id, meta_json FROM sa_triggers WHERE eval_state IN ('queued', 'evaluating')"
                )
                rows = cur.fetchall()
                for row in rows:
                    row_id = row[0] if isinstance(row, (tuple, list)) else row["id"]
                    raw_meta = row[1] if isinstance(row, (tuple, list)) else row["meta_json"]
                    try:
                        meta = json.loads(raw_meta) if raw_meta else {}
                        if not isinstance(meta, dict):
                            continue
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue

                    if "eval_deadline" in meta and meta["eval_deadline"] is not None:
                        try:
                            old_deadline = int(meta["eval_deadline"])
                        except (ValueError, TypeError):
                            continue
                        new_deadline = shifted_deadline(old_deadline, delta)
                        meta["eval_deadline"] = new_deadline
                        c.execute(
                            "UPDATE sa_triggers SET meta_json = ? WHERE id = ?",
                            (json.dumps(meta, sort_keys=True), row_id),
                        )
                        shifted_count += 1

            _set_meta(c, "paused_at", "")
            return shifted_count


def set_paused(
    conn: sqlite3.Connection | Path | str,
    paused: bool,
    now: float | None = None,
) -> int:
    """Set the pause state in sa_meta.

    Writes both keys: 'paused' ('0'|'1') and 'paused_at' (epoch timestamp str).

    Double-pause invariant:
      If already paused, a second call to set_paused(True) keeps the original
      paused_at timestamp rather than resetting it.

    When pausing (paused=True):
      Writes paused='1' and paused_at=now (if not already paused). Returns 0.

    When resuming (paused=False):
      If currently paused, shifts queued/evaluating triggers' meta_json eval_deadline
      by the pause delta via apply_pause_shift, clears paused_at to '', sets paused='0',
      and returns the number of triggers shifted.
      If not currently paused, ensures paused='0' and paused_at='' and returns 0.
    """
    now_int = int(now) if now is not None else int(time.time())

    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            currently_paused = is_paused(c)
            if paused:
                if not currently_paused:
                    _set_meta(c, "paused", "1")
                    _set_meta(c, "paused_at", str(now_int))
                # Double-pause rule: second set_paused(True) keeps the original paused_at
                return 0
            else:
                if not currently_paused:
                    _set_meta(c, "paused", "0")
                    _set_meta(c, "paused_at", "")
                    return 0

                # Resuming from paused state: compute delta, shift triggers, clear paused_at
                shifted_count = apply_pause_shift(c, now_int, _on_resume=True)
                _set_meta(c, "paused", "0")
                _set_meta(c, "paused_at", "")
                return shifted_count

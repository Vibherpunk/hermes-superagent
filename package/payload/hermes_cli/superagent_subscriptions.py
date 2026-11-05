"""Superagent thread-ticket subscription storage operations.

Implements the connection-passing pattern mirroring :mod:`hermes_cli.kanban_db`,
where callers supply an active SQLite connection rather than this module
managing connection lifecycles.

Subscriptions map between communication thread keys and Kanban tickets,
supporting owning (dispatch/routing) and context-only subscriptions.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from hermes_cli.kanban_db_connect import write_txn


def subscribe(
    conn: sqlite3.Connection,
    ticket_id: str,
    thread_key: str,
    context_only: bool = False,
    now: int | None = None,
) -> None:
    """Subscribe a ticket to a thread. Idempotent.

    Subscribing the same (thread_key, ticket_id) twice yields one row.
    If the pair exists with a different context_only flag, the new call's
    context_only wins (UPDATE), still keeping one row.
    """
    ts = int(now) if now is not None else int(time.time())
    flag = 1 if context_only else 0
    with write_txn(conn, allow_nested=True):
        conn.execute(
            """
            INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (thread_key, ticket_id) DO UPDATE SET
                context_only = excluded.context_only
            """,
            (thread_key, ticket_id, flag, ts),
        )


def unsubscribe(
    conn: sqlite3.Connection,
    ticket_id: str,
    thread_key: str,
) -> bool:
    """Unsubscribe a ticket from a thread.

    Returns True if a row was removed, False otherwise.
    """
    with write_txn(conn, allow_nested=True):
        cur = conn.execute(
            "DELETE FROM sa_subscriptions WHERE ticket_id = ? AND thread_key = ?",
            (ticket_id, thread_key),
        )
        return cur.rowcount > 0


def drop_thread_subscriptions(
    conn: sqlite3.Connection,
    thread_key: str,
) -> int:
    """Remove ALL subscriptions for the thread (owning + context-only).

    Returns the number of rows removed.
    """
    with write_txn(conn, allow_nested=True):
        cur = conn.execute(
            "DELETE FROM sa_subscriptions WHERE thread_key = ?",
            (thread_key,),
        )
        return int(cur.rowcount)


def subscriptions_for(
    conn: sqlite3.Connection,
    thread_key: str,
) -> list[dict[str, Any]]:
    """Return every subscription on the thread with its context_only flag."""
    cur = conn.execute(
        """
        SELECT thread_key, ticket_id, context_only, created_at
        FROM sa_subscriptions
        WHERE thread_key = ?
        ORDER BY created_at ASC, ticket_id ASC
        """,
        (thread_key,),
    )
    rows = cur.fetchall()
    cols = (
        [col[0] for col in cur.description]
        if cur.description
        else ["thread_key", "ticket_id", "context_only", "created_at"]
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r) if hasattr(r, "keys") else dict(zip(cols, r))
        item["context_only"] = bool(item["context_only"])
        out.append(item)
    return out


def threads_for(
    conn: sqlite3.Connection,
    ticket_id: str,
) -> list[str]:
    """Return every thread_key the ticket subscribes to."""
    cur = conn.execute(
        """
        SELECT thread_key
        FROM sa_subscriptions
        WHERE ticket_id = ?
        ORDER BY created_at ASC, thread_key ASC
        """,
        (ticket_id,),
    )
    return [row[0] for row in cur.fetchall()]


__all__ = [
    "drop_thread_subscriptions",
    "subscribe",
    "subscriptions_for",
    "threads_for",
    "unsubscribe",
]

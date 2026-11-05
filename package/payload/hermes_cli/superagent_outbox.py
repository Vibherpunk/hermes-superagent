# health: allow FILE_LINES -- superagent phase 4 outbox API
"""Superagent Outbox API: durable outbound reply queue and delivery state machine.

This module provides the Outbox persistence and scheduling functions for the
Superagent subsystem (Phase 4). These functions manage the lifecycle of outbound
messages stored in the ``sa_outbox`` table:
  - ``queue_reply``: Enqueue an outbound reply obligation.
  - ``due_replies``: Select pending replies eligible for transmission under FIFO ordering rules.
  - ``mark_sent``: Transition an outbox entry to 'sent' with delivery receipt.
  - ``mark_failed``: Increment attempts and compute exponential backoff or apply flood delay.
  - ``mark_dropped``: Terminally abort delivery for dead destinations or permanent errors.
  - ``mark_sending``: Transition an outbox entry to 'sending' with active attempt timestamp.
  - ``reset_stale_sending``: Recover crashed/abandoned 'sending' rows older than 10 minutes.
  - ``drain_outbox``: Drain eligible replies, enforce 100-word IM cap, execute sender, and handle errors.
  - ``increment_sa_meta_counter``: Atomically increment key counters in ``sa_meta``.
  - ``get_sa_meta``: Read metadata key values from ``sa_meta``.

Crash Safety Architecture (Choice: Drain-Level Stale Recovery):
  A process kill or crash mid-drain can leave an outbox row stuck in 'sending' state.
  This module implements the drain-level recovery approach: at the start of each
  ``drain_outbox`` execution, ``reset_stale_sending`` resets any row stuck in 'sending'
  older than 10 minutes (600s) back to 'failed' with its ``attempts`` count unchanged
  and ``next_attempt_at`` set to the current timestamp. This keeps ``due_replies``
  strictly focused on ('queued', 'failed') statuses, prevents stale entries from
  blocking newer messages for the same identity, and guarantees interrupted messages
  are safely recovered and retried on subsequent drain passes without duplicating
  already-sent messages.

Tick Wiring Integration Call Site:
  ``drain_outbox`` is exported for invocation by the Kanban dispatcher or gateway
  tick loops (e.g. in ``gateway/kanban_watchers_dispatcher.py`` inside
  ``_KanbanDispatcher.tick_once_for_board(slug)`` or ``gateway/kanban_watchers_notifier.py``).
  The gateway dispatcher injects a ``sender(address, body, purpose)`` callable that
  delegates directly to the authorized platform adapter's send method
  (``adapter.send(chat_id, text, metadata=...)``, E-028).

Connection-Passing Pattern:
  Mirroring the convention in ``hermes_cli/kanban_db.py``, every API function accepts
  either an existing ``sqlite3.Connection`` or a filesystem database path (``Path | str``).
  When a path is provided, the function manages the connection lifecycle automatically
  via ``hermes_cli.kanban_db_connect.connect_closing``. All write operations use
  ``hermes_cli.kanban_db_connect.write_txn(..., allow_nested=True)`` to guarantee
  atomic execution and immediate durability across concurrent SQLite transactions.

Gateway Flood Helper Signatures (referenced from notes/p4-1-anchors.md & gateway/delivery_ledger.py):
  The flood hints passed to ``mark_failed(..., flood_hint_seconds=...)`` originate
  from the drain caller's interaction with the gateway platform adapters and the
  delivery ledger. The authoritative signatures from ``gateway/delivery_ledger.py`` are:
    - ``flood_wait_seconds(error: Any, default: float = FLOOD_RETRY_DEFAULT_SECONDS) -> float``
      Extracts platform-requested retry wait seconds from 'flood_control:<seconds>'
      or raw error strings (e.g. Telegram flood control regex); falls back to default (60.0s).
    - ``retry_not_before(updated_at: Any, last_error: Any, attempts: Any) -> Optional[float]``
      Calculates earliest retry timestamp based on error classification, flood wait,
      or tiered retry backoff; returns None for dead errors or exhausted attempts.
    - ``is_flood_error(error: Any) -> bool``
      Returns True if error text matches normalized or raw platform flood signatures.
    - ``flood_retry_delay(error: Any) -> float``
      Computes retry delay for flood conditions using ``flood_wait_seconds``.
    - ``flood_not_before(updated_at: Any, error: Any) -> float``
      Returns ``_failed_stamp(updated_at) + flood_wait_seconds(error)``.
    - ``is_reconnect_only(text: str) -> bool``
      Returns True if the failure is an ephemeral connection drop eligible for immediate retry.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Generator

from hermes_cli import kanban_db_connect as kbc

logger = logging.getLogger(__name__)


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


def _address_identity(raw: Any) -> tuple[str | None, str | None, str | None]:
    """Extract normalized (platform, chat_id, thread_id) identity from address_json."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            data = {}
    elif isinstance(raw, dict):
        data = raw
    else:
        data = {}

    platform = str(data["platform"]).strip().lower() if data.get("platform") is not None and str(data["platform"]).strip() else None
    chat_id = str(data["chat_id"]).strip() if data.get("chat_id") is not None and str(data["chat_id"]).strip() else None
    thread_id = str(data["thread_id"]).strip() if data.get("thread_id") is not None and str(data["thread_id"]).strip() else None
    return (platform, chat_id, thread_id)


def queue_reply(
    conn: sqlite3.Connection | Path | str,
    ticket_id: str | None,
    source: str,
    address: dict[str, Any],
    purpose: str,
    body: str,
    now: float,
) -> int:
    """Insert one row into sa_outbox with status='queued'.

    Insert only — never sends. Returns the created row id.
    """
    address_payload = address if isinstance(address, dict) else {}
    address_raw = json.dumps(address_payload, sort_keys=True)
    now_int = int(now)

    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            cur = c.execute(
                """INSERT INTO sa_outbox (
                    ticket_id, source, address_json, purpose, body,
                    status, attempts, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)""",
                (ticket_id, source, address_raw, purpose, body, now_int, now_int),
            )
            row_id = cur.lastrowid
            if row_id is None:
                raise RuntimeError("Failed to insert outbox row")
            return int(row_id)


def due_replies(
    conn: sqlite3.Connection | Path | str,
    now: float,
) -> list[sqlite3.Row]:
    """Return rows with status IN ('queued', 'failed') AND next_attempt_at <= now AND attempts < 3.

    Enforces the ORDERING RULE: a row is due only if no earlier (smaller id) row for the
    same (ticket_id, address chat+thread identity) exists in status 'queued'|'sending' or
    'failed' with attempts < 3. Address identity is (platform, chat_id, thread_id) from address_json.
    """
    now_int = int(now)
    with _resolve_conn(conn) as c:
        cur = c.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute(
            """SELECT * FROM sa_outbox
            WHERE status IN ('queued', 'sending')
               OR (status = 'failed' AND attempts < 3)
            ORDER BY id ASC"""
        )
        candidates = cur.fetchall()

        blocked_identities: set[tuple[str | None, str | None, str | None, str | None]] = set()
        due: list[sqlite3.Row] = []

        for row in candidates:
            addr = _address_identity(row["address_json"])
            ticket_id = (
                str(row["ticket_id"]).strip()
                if row["ticket_id"] is not None and str(row["ticket_id"]).strip()
                else None
            )
            identity = (ticket_id, addr[0], addr[1], addr[2])

            if identity in blocked_identities:
                continue

            # Candidate eligibility check
            if (
                row["status"] in ("queued", "failed")
                and row["next_attempt_at"] <= now_int
                and row["attempts"] < 3
            ):
                due.append(row)

            # Any row in queued, sending, or failed (attempts < 3) blocks later rows for this destination
            blocked_identities.add(identity)

        return due


def mark_sent(
    conn: sqlite3.Connection | Path | str,
    id: int,
    receipt: dict[str, Any],
    now: float,
) -> None:
    """Mark outbox row as status='sent', set sent_at=now, and store receipt_json."""
    receipt_payload = receipt if isinstance(receipt, dict) else {}
    receipt_raw = json.dumps(receipt_payload, sort_keys=True)
    now_int = int(now)

    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            cur = c.execute(
                """UPDATE sa_outbox
                SET status = 'sent',
                    sent_at = ?,
                    receipt_json = ?
                WHERE id = ?""",
                (now_int, receipt_raw, id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Outbox entry {id} not found")


def mark_failed(
    conn: sqlite3.Connection | Path | str,
    id: int,
    error: str,
    now: float,
    flood_hint_seconds: float | None = None,
) -> None:
    """Record a failure: increment attempts, calculate backoff / flood delay, store last_error."""
    now_int = int(now)
    err_str = str(error or "")

    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            cur = c.execute("SELECT attempts FROM sa_outbox WHERE id = ?", (id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Outbox entry {id} not found")

            attempts_before = int(row[0] or 0)
            if flood_hint_seconds is not None:
                delay = int(flood_hint_seconds)
            else:
                delay = int(min(5 * (2 ** attempts_before), 300))

            next_attempt_at = now_int + delay
            new_attempts = attempts_before + 1

            c.execute(
                """UPDATE sa_outbox
                SET status = 'failed',
                    attempts = ?,
                    next_attempt_at = ?,
                    last_error = ?
                WHERE id = ?""",
                (new_attempts, next_attempt_at, err_str, id),
            )


def mark_dropped(
    conn: sqlite3.Connection | Path | str,
    id: int,
    reason: str,
) -> None:
    """Mark outbox row as status='dropped' with dropped_reason. Terminal state."""
    reason_str = str(reason or "")

    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            cur = c.execute(
                """UPDATE sa_outbox
                SET status = 'dropped',
                    dropped_reason = ?
                WHERE id = ?""",
                (reason_str, id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Outbox entry {id} not found")


# Unreachable / dead recipient error substrings copied directly from platform adapter
# implementations in this repo:
# - "chat not found": gateway/platforms/base.py:1632 (_CHAT_LEVEL_NOT_FOUND_SUBSTRINGS)
# - "bot was blocked": gateway/platforms/base.py:1659 (_SEND_ERROR_CLASSIFIERS forbidden)
# - "blocked by the user": gateway/platforms/base.py:1659 (_SEND_ERROR_CLASSIFIERS forbidden)
# - "user is deactivated": gateway/platforms/base.py:1659 (_SEND_ERROR_CLASSIFIERS forbidden)
# - "not enough rights": gateway/platforms/base.py:1660 (_SEND_ERROR_CLASSIFIERS forbidden)
# - "have no rights": gateway/platforms/base.py:1660 (_SEND_ERROR_CLASSIFIERS forbidden)
# - "not a member": gateway/platforms/base.py:1660 (_SEND_ERROR_CLASSIFIERS forbidden)
# - "forbidden": gateway/platforms/base.py:1658 (_SEND_ERROR_CLASSIFIERS forbidden)
# - "channel not found": plugins/platforms/discord/adapter.py:3281 (SendResult channel resolution)
# - "channel_not_found": plugins/platforms/slack/adapter.py:6518 (_WRONG_WORKSPACE_TOKEN_ERRORS)
# - "account_inactive": plugins/platforms/slack/adapter.py:6517 (_WRONG_WORKSPACE_TOKEN_ERRORS)
# - "not_in_channel": plugins/platforms/slack/adapter.py:6517 (_WRONG_WORKSPACE_TOKEN_ERRORS)
UNREACHABLE_ERROR_STRINGS: tuple[str, ...] = (
    "chat not found",
    "bot was blocked",
    "blocked by the user",
    "user is deactivated",
    "not enough rights",
    "have no rights",
    "not a member",
    "forbidden",
    "channel not found",
    "channel_not_found",
    "account_inactive",
    "not_in_channel",
)

# Sub-chat / thread-level error substrings that do NOT mean the parent chat is dead
# (gateway/platforms/base.py:1633-1635):
_SUBCHAT_ERROR_STRINGS: tuple[str, ...] = (
    "message to edit not found",
    "message to reply not found",
    "thread not found",
    "topic_deleted",
    "message_id_invalid",
)

# Regex matching dynamic channel/chat not found patterns such as:
# - plugins/platforms/discord/adapter.py:3281 (f"Channel {chat_id} not found")
_CHANNEL_NOT_FOUND_RE = re.compile(r"(?:chat|channel)(?:\s+\S+)?[\s_]+not[\s_]+found")


# Registered IM/chat platforms subject to the 100-word cap before sending.
# Sourced from:
# 1. Platform enum in gateway/config.py:35-62:
#    telegram, discord, slack, dingtalk, whatsapp, whatsapp_cloud, signal,
#    mattermost, matrix, feishu, wecom, wecom_callback, weixin, bluebubbles,
#    qqbot, yuanbao.
# 2. Bundled platform plugins under plugins/platforms/:
#    google_chat, teams, irc, line, simplex, ntfy, photon, raft, buzz, a2a.
# Excludes non-IM platforms such as 'github', 'email', 'webhook', 'api_server'.
IM_CAP_PLATFORMS: frozenset[str] = frozenset({
    "telegram",
    "discord",
    "slack",
    "dingtalk",
    "whatsapp",
    "whatsapp_cloud",
    "signal",
    "mattermost",
    "matrix",
    "feishu",
    "wecom",
    "wecom_callback",
    "weixin",
    "bluebubbles",
    "qqbot",
    "yuanbao",
    "google_chat",
    "teams",
    "irc",
    "line",
    "simplex",
    "ntfy",
    "photon",
    "raft",
    "buzz",
    "a2a",
})


def is_unreachable_error(error: Any) -> bool:
    """Return True if error indicates a permanently gone or unreachable recipient.

    Matches against documented platform adapter error strings, excluding sub-chat/thread errors.
    """
    text = str(error or "").strip().lower()
    if not text:
        return False
    if any(subchat in text for subchat in _SUBCHAT_ERROR_STRINGS):
        return False
    if any(needle in text for needle in UNREACHABLE_ERROR_STRINGS):
        return True
    return bool(_CHANNEL_NOT_FOUND_RE.search(text))


def _is_flood(error: Any) -> bool:
    """True if error matches platform flood or rate-limiting patterns."""
    try:
        from gateway.delivery_ledger import is_flood_error
        if is_flood_error(error):
            return True
    except (ImportError, AttributeError):
        pass
    text = str(error or "").strip().lower()
    return (
        text.startswith("flood_control:")
        or "flood control exceeded" in text
        or "rate limit" in text
        or "rate_limited" in text
        or "too many requests" in text
        or "retry after" in text
    )


def _flood_wait(error: Any) -> float:
    """Compute the flood retry delay hint in seconds from platform error."""
    try:
        from gateway.delivery_ledger import flood_wait_seconds
        return flood_wait_seconds(error)
    except (ImportError, AttributeError):
        pass
    text = str(error or "").strip().lower()
    if text.startswith("flood_control:"):
        try:
            return float(text.split(":", 1)[1].strip())
        except (ValueError, TypeError):
            pass
    return 60.0


def increment_sa_meta_counter(
    conn: sqlite3.Connection | Path | str,
    key: str,
) -> int:
    """Atomically increment an integer counter in sa_meta and return the new value."""
    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            cur = c.execute("SELECT value FROM sa_meta WHERE key = ?", (key,))
            row = cur.fetchone()
            if row is None:
                new_val = 1
                c.execute("INSERT INTO sa_meta (key, value) VALUES (?, ?)", (key, str(new_val)))
            else:
                try:
                    new_val = int(row[0] or 0) + 1
                except (ValueError, TypeError):
                    new_val = 1
                c.execute("UPDATE sa_meta SET value = ? WHERE key = ?", (str(new_val), key))
            return new_val


def get_sa_meta(
    conn: sqlite3.Connection | Path | str,
    key: str,
) -> str | None:
    """Read a string value from sa_meta by key, or None if missing."""
    with _resolve_conn(conn) as c:
        cur = c.execute("SELECT value FROM sa_meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return str(row[0]) if row is not None and row[0] is not None else None


def mark_sending(
    conn: sqlite3.Connection | Path | str,
    id: int,
    now: float,
) -> None:
    """Mark an outbox row as status='sending' and record the attempt start timestamp in next_attempt_at."""
    now_int = int(now)
    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            cur = c.execute(
                """UPDATE sa_outbox
                SET status = 'sending',
                    next_attempt_at = ?
                WHERE id = ?""",
                (now_int, id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Outbox entry {id} not found")


def reset_stale_sending(
    conn: sqlite3.Connection | Path | str,
    now: float,
    timeout_seconds: float = 600.0,
) -> int:
    """Reset rows stuck in 'sending' older than timeout_seconds back to 'failed' with attempts unchanged.

    Returns the number of rows reset.
    """
    now_int = int(now)
    cutoff = now_int - int(timeout_seconds)
    with _resolve_conn(conn) as c:
        with kbc.write_txn(c, allow_nested=True):
            cur = c.execute(
                """UPDATE sa_outbox
                SET status = 'failed',
                    next_attempt_at = ?,
                    last_error = COALESCE(last_error, 'stale sending timeout')
                WHERE status = 'sending'
                  AND (next_attempt_at <= ? OR created_at <= ?)""",
                (now_int, cutoff, cutoff),
            )
            return cur.rowcount


def drain_outbox(
    conn: sqlite3.Connection | Path | str,
    now: float,
    sender: Callable[[dict[str, Any], str, str], dict[str, Any]],
) -> dict[str, int]:
    """Drain due replies from the outbox, transmitting via the injected sender.

    For each due reply from ``due_replies(conn, now)``:
      1. Enforces 100-word cap on IM platforms: if exceeded, marks 'failed' with
         last_error='over_limit' and increments sa_meta 'over_limit_notices'.
      2. Marks status as 'sending' with current timestamp.
      3. Invokes ``sender(address, body, purpose)``.
      4. On success: marks 'sent' with receipt.
      5. On exception:
         - Unreachable recipient: marks 'dropped' with reason.
         - Flood / rate limit: marks 'failed' with flood_hint_seconds delay.
         - Any other error: marks 'failed' with standard doubling backoff.

    Args:
        conn: SQLite connection or database path.
        now: Current epoch timestamp.
        sender: Injected callable returning receipt dict or raising on failure.

    Returns:
        Summary dict: {'sent': n, 'failed': n, 'dropped': n}.
    """
    summary = {"sent": 0, "failed": 0, "dropped": 0}

    with _resolve_conn(conn) as c:
        # Crash safety: recover stale 'sending' rows older than 10 minutes (600s)
        reset_stale_sending(c, now=now, timeout_seconds=600.0)

        processed_ids: set[int] = set()
        for _ in range(1000):
            due_rows = due_replies(c, now=now)
            unprocessed = [r for r in due_rows if int(r["id"]) not in processed_ids]
            if not unprocessed:
                break

            for row in unprocessed:
                row_id = int(row["id"])
                processed_ids.add(row_id)
                body = str(row["body"] or "")
                purpose = str(row["purpose"] or "")
                source = str(row["source"] or "").strip().lower()

                try:
                    raw_addr = row["address_json"]
                    address = json.loads(raw_addr) if raw_addr else {}
                    if not isinstance(address, dict):
                        address = {}
                except (json.JSONDecodeError, ValueError, TypeError):
                    address = {}

                # 100-word cap: check if source is an IM platform
                addr_plat = _address_identity(address)[0]
                is_im = (source in IM_CAP_PLATFORMS) or (not source and addr_plat in IM_CAP_PLATFORMS)
                word_count = len(body.split())

                if is_im and word_count > 100:
                    mark_failed(c, row_id, error="over_limit", now=now)
                    increment_sa_meta_counter(c, "over_limit_notices")
                    summary["failed"] += 1
                    continue

                mark_sending(c, row_id, now=now)

                try:
                    receipt = sender(address, body, purpose)
                    receipt_dict = receipt if isinstance(receipt, dict) else {"result": str(receipt)}
                    mark_sent(c, row_id, receipt=receipt_dict, now=now)
                    summary["sent"] += 1
                except Exception as exc:  # health: allow BLE001 -- deliberate catch-all boundary for injected platform sender exceptions
                    logger.debug("Outbox sender error for entry %s: %s", row_id, exc, exc_info=True)
                    err_str = str(exc)
                    if is_unreachable_error(err_str):
                        mark_dropped(c, row_id, reason=err_str)
                        summary["dropped"] += 1
                    elif _is_flood(err_str):
                        hint = _flood_wait(err_str)
                        mark_failed(c, row_id, error=err_str, now=now, flood_hint_seconds=hint)
                        summary["failed"] += 1
                    else:
                        mark_failed(c, row_id, error=err_str, now=now)
                        summary["failed"] += 1

    return summary

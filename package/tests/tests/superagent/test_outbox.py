"""Unit tests for Superagent Phase 4 Outbox API (hermes_cli/superagent_outbox.py).

Verifies:
1. queue -> due immediately upon insertion.
2. ordering gate:
   - Second row for same ticket + address is NOT due while the first is queued.
   - Second row becomes due after the first is sent.
   - A different address is due independently.
   - A row in status 'sending' blocks subsequent rows.
   - A failed row with attempts < 3 blocks subsequent rows.
3. mark_failed backoff sequence:
   - Doubling sequence 5, 10, 20, 40, ... capped at 300 (assert next_attempt_at deltas with attempts pre-set).
4. flood hint overrides exponential backoff.
5. attempts >= 3 rows are never due.
6. mark_dropped is terminal (never due, does not block subsequent rows).
7. connection or path support: APIs accept sqlite3.Connection or Path/str seamlessly.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import superagent_outbox as outbox


def _init_test_db(tmp_path: Path) -> Path:
    """Initialize a fresh temporary Kanban DB with all superagent schemas."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    return db_path


def test_queue_due_immediately(tmp_path: Path) -> None:
    """queue_reply inserts row with status='queued' and is immediately returned by due_replies."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000
    address = {"platform": "telegram", "chat_id": "12345", "thread_id": "topic_1"}

    with kbc.connect_closing(db_path) as conn:
        row_id = outbox.queue_reply(
            conn,
            ticket_id="ticket-100",
            source="user",
            address=address,
            purpose="acknowledgement",
            body="Task received.",
            now=now,
        )
        assert row_id > 0

        # Verify DB row values
        cur = conn.execute("SELECT * FROM sa_outbox WHERE id = ?", (row_id,))
        row = cur.fetchone()
        assert row is not None
        assert row["ticket_id"] == "ticket-100"
        assert row["source"] == "user"
        assert json.loads(row["address_json"]) == address
        assert row["purpose"] == "acknowledgement"
        assert row["body"] == "Task received."
        assert row["status"] == "queued"
        assert row["attempts"] == 0
        assert row["next_attempt_at"] == now
        assert row["created_at"] == now

        # Verify due_replies returns the row immediately
        due = outbox.due_replies(conn, now=now)
        assert len(due) == 1
        assert due[0]["id"] == row_id
        assert due[0]["body"] == "Task received."


def test_ordering_gate(tmp_path: Path) -> None:
    """Ordering gate ensures FIFO per (ticket_id, platform, chat_id, thread_id).

    - Second row for same ticket + address is NOT due while first is queued.
    - Second row becomes due after first is sent.
    - Different address is due independently.
    """
    db_path = _init_test_db(tmp_path)
    now = 1700000000
    addr_a = {"platform": "telegram", "chat_id": "100", "thread_id": "1"}
    addr_b = {"platform": "telegram", "chat_id": "200", "thread_id": "1"}

    with kbc.connect_closing(db_path) as conn:
        # 1. Enqueue two messages to addr_a (same ticket)
        id_a1 = outbox.queue_reply(conn, "t-1", "user", addr_a, "update", "Msg A1", now)
        id_a2 = outbox.queue_reply(conn, "t-1", "user", addr_a, "update", "Msg A2", now)

        # 2. Enqueue message to addr_b (different address, same ticket)
        id_b1 = outbox.queue_reply(conn, "t-1", "user", addr_b, "update", "Msg B1", now)

        # Check due: id_a1 and id_b1 must be due, id_a2 must NOT be due
        due = outbox.due_replies(conn, now=now)
        due_ids = [r["id"] for r in due]
        assert id_a1 in due_ids
        assert id_b1 in due_ids
        assert id_a2 not in due_ids

        # 3. Mark id_a1 as sent
        receipt_a1 = {"message_id": "msg-999", "platform": "telegram"}
        outbox.mark_sent(conn, id_a1, receipt=receipt_a1, now=now + 2)

        # Verify id_a1 state in DB
        cur = conn.execute("SELECT status, sent_at, receipt_json FROM sa_outbox WHERE id = ?", (id_a1,))
        r_a1 = cur.fetchone()
        assert r_a1["status"] == "sent"
        assert r_a1["sent_at"] == now + 2
        assert json.loads(r_a1["receipt_json"]) == receipt_a1

        # 4. Now id_a2 should be due!
        due_after = outbox.due_replies(conn, now=now + 2)
        due_after_ids = [r["id"] for r in due_after]
        assert id_a2 in due_after_ids
        assert id_b1 in due_after_ids
        assert id_a1 not in due_after_ids


def test_ordering_gate_blocks_on_sending_and_failed(tmp_path: Path) -> None:
    """Rows in 'sending' or 'failed' (attempts < 3) block later rows for the same identity."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000
    addr = {"platform": "discord", "chat_id": "ch-1", "thread_id": "th-1"}

    with kbc.connect_closing(db_path) as conn:
        id_1 = outbox.queue_reply(conn, "t-1", "user", addr, "msg", "First", now)
        id_2 = outbox.queue_reply(conn, "t-1", "user", addr, "msg", "Second", now)

        # Set id_1 to 'sending' manually
        conn.execute("UPDATE sa_outbox SET status = 'sending' WHERE id = ?", (id_1,))

        # Neither id_1 nor id_2 should be due (id_1 is sending, id_2 is blocked by id_1)
        due = outbox.due_replies(conn, now=now)
        assert due == []

        # Now id_1 fails (attempts = 1, backoff scheduled at now + 5)
        outbox.mark_failed(conn, id_1, error="network timeout", now=now)

        # At now + 1, id_1 is not due yet (next_attempt_at is now + 5), and id_2 is STILL blocked!
        due_early = outbox.due_replies(conn, now=now + 1)
        assert due_early == []

        # At now + 5, id_1 is due again, but id_2 is still blocked behind id_1
        due_retry = outbox.due_replies(conn, now=now + 5)
        assert [r["id"] for r in due_retry] == [id_1]
        assert id_2 not in [r["id"] for r in due_retry]


def test_mark_failed_backoff_sequence(tmp_path: Path) -> None:
    """mark_failed executes doubling backoff: 5, 10, 20, 40, ... capped at 300s."""
    db_path = _init_test_db(tmp_path)
    now = 1000000
    addr = {"platform": "slack", "chat_id": "C123"}

    with kbc.connect_closing(db_path) as conn:
        row_id = outbox.queue_reply(conn, "t-1", "user", addr, "ping", "Ping", now)

        # Test sequence with attempts pre-set
        expected_deltas = [
            (0, 5),    # 5 * 2^0 = 5
            (1, 10),   # 5 * 2^1 = 10
            (2, 20),   # 5 * 2^2 = 20
            (3, 40),   # 5 * 2^3 = 40
            (4, 80),   # 5 * 2^4 = 80
            (5, 160),  # 5 * 2^5 = 160
            (6, 300),  # 5 * 2^6 = 320 -> capped at 300
            (7, 300),  # 5 * 2^7 = 640 -> capped at 300
            (10, 300), # capped at 300
        ]

        for pre_attempts, expected_delta in expected_deltas:
            conn.execute("UPDATE sa_outbox SET attempts = ? WHERE id = ?", (pre_attempts, row_id))
            outbox.mark_failed(conn, row_id, error=f"error-at-{pre_attempts}", now=now)

            cur = conn.execute("SELECT attempts, next_attempt_at, last_error, status FROM sa_outbox WHERE id = ?", (row_id,))
            row = cur.fetchone()
            assert row["attempts"] == pre_attempts + 1
            assert row["next_attempt_at"] == now + expected_delta, (
                f"Failed for attempts={pre_attempts}: expected delta {expected_delta}, got {row['next_attempt_at'] - now}"
            )
            assert row["last_error"] == f"error-at-{pre_attempts}"
            assert row["status"] == "failed"


def test_flood_hint_overrides_backoff(tmp_path: Path) -> None:
    """flood_hint_seconds overrides the computed exponential backoff delay."""
    db_path = _init_test_db(tmp_path)
    now = 2000000
    addr = {"platform": "telegram", "chat_id": "flood_chat"}

    with kbc.connect_closing(db_path) as conn:
        row_id = outbox.queue_reply(conn, "t-1", "user", addr, "ping", "Body", now)

        # Attempts = 0 would normally delay 5s; hint of 45s must override
        outbox.mark_failed(conn, row_id, error="flood_control:45", now=now, flood_hint_seconds=45)
        cur = conn.execute("SELECT attempts, next_attempt_at FROM sa_outbox WHERE id = ?", (row_id,))
        row = cur.fetchone()
        assert row["attempts"] == 1
        assert row["next_attempt_at"] == now + 45

        # Attempts = 5 would normally delay 160s; hint of 12s must override
        conn.execute("UPDATE sa_outbox SET attempts = 5 WHERE id = ?", (row_id,))
        outbox.mark_failed(conn, row_id, error="flood_control:12", now=now, flood_hint_seconds=12)
        cur = conn.execute("SELECT attempts, next_attempt_at FROM sa_outbox WHERE id = ?", (row_id,))
        row = cur.fetchone()
        assert row["attempts"] == 6
        assert row["next_attempt_at"] == now + 12


def test_attempts_limit_never_due(tmp_path: Path) -> None:
    """Rows with attempts >= 3 are never due, and do not block later rows."""
    db_path = _init_test_db(tmp_path)
    now = 3000000
    addr = {"platform": "telegram", "chat_id": "dead_chat"}

    with kbc.connect_closing(db_path) as conn:
        id_1 = outbox.queue_reply(conn, "t-1", "user", addr, "first", "Attempt exhausted", now)
        id_2 = outbox.queue_reply(conn, "t-1", "user", addr, "second", "Second queued", now)

        # Preset id_1 to status='failed', attempts=3, next_attempt_at in the past
        conn.execute(
            "UPDATE sa_outbox SET status = 'failed', attempts = 3, next_attempt_at = ? WHERE id = ?",
            (now - 10, id_1),
        )

        # id_1 has attempts >= 3 so it is never due, and it does not block id_2
        due = outbox.due_replies(conn, now=now)
        due_ids = [r["id"] for r in due]
        assert id_1 not in due_ids
        assert id_2 in due_ids


def test_mark_dropped_terminal(tmp_path: Path) -> None:
    """mark_dropped transitions row to 'dropped' with reason; never due, does not block."""
    db_path = _init_test_db(tmp_path)
    now = 4000000
    addr = {"platform": "telegram", "chat_id": "drop_chat"}

    with kbc.connect_closing(db_path) as conn:
        id_1 = outbox.queue_reply(conn, "t-1", "user", addr, "first", "To be dropped", now)
        id_2 = outbox.queue_reply(conn, "t-1", "user", addr, "second", "Followup", now)

        outbox.mark_dropped(conn, id_1, reason="User deactivated account")

        # Verify DB row status
        cur = conn.execute("SELECT status, dropped_reason FROM sa_outbox WHERE id = ?", (id_1,))
        row = cur.fetchone()
        assert row["status"] == "dropped"
        assert row["dropped_reason"] == "User deactivated account"

        # id_1 is terminal (not due), id_2 is due
        due = outbox.due_replies(conn, now=now)
        due_ids = [r["id"] for r in due]
        assert id_1 not in due_ids
        assert id_2 in due_ids


def test_connection_or_path_support(tmp_path: Path) -> None:
    """All APIs accept a Path or string database path directly."""
    db_path = _init_test_db(tmp_path)
    now = 5000000
    addr = {"platform": "slack", "chat_id": "general"}

    # Pass Path object
    row_id = outbox.queue_reply(
        db_path,
        ticket_id="ticket-path",
        source="system",
        address=addr,
        purpose="notice",
        body="Via path",
        now=now,
    )
    assert row_id > 0

    # due_replies via str path
    due = outbox.due_replies(str(db_path), now=now)
    assert len(due) == 1
    assert due[0]["id"] == row_id

    # mark_failed via Path
    outbox.mark_failed(db_path, row_id, error="temporary", now=now)

    # mark_sent via str path
    outbox.mark_sent(str(db_path), row_id, receipt={"ok": True}, now=now + 10)

    # mark_dropped via Path
    outbox.mark_dropped(db_path, row_id, reason="done")

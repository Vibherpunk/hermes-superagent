"""Unit tests for Superagent Outbox Drain (hermes_cli/superagent_outbox.py).

Verifies TODO 4.3 requirements:
1. Happy path: sends due rows in FIFO order and marks sent with receipt.
2. Unreachable errors: maps platform unreachable/dead recipient strings to mark_dropped
   (never due, never retried).
3. Flood errors: computes hint via delivery ledger flood helpers and marks failed
   with next_attempt_at matching the hint.
4. Generic errors: marks failed with doubling backoff (5s, 10s, 20s, ...).
5. 100-word cap on IM platforms: blocks send for IM sources (telegram, slack, discord, etc.),
   marks failed with last_error='over_limit', and increments sa_meta 'over_limit_notices'.
6. 100-word cap bypassed on non-IM sources (github): long body is transmitted successfully.
7. Crash safety / kill-mid-drain:
   - When sender raises BaseException mid-drain, row stays in 'sending'.
   - Re-running drain does not double-send already-sent rows.
   - Stale 'sending' row older than 10 minutes (600s) is recovered to 'failed' and sent exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import superagent_outbox as outbox


def _init_test_db(tmp_path: Path) -> Path:
    """Initialize a fresh temporary Kanban DB with all superagent schemas."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    return db_path


def test_happy_path_sends_in_order(tmp_path: Path) -> None:
    """Happy path drains in FIFO order per destination and records receipts."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000
    addr_a = {"platform": "slack", "chat_id": "C_A", "thread_id": "T1"}
    addr_b = {"platform": "slack", "chat_id": "C_B"}

    send_log: list[tuple[dict[str, Any], str, str]] = []

    def fake_sender(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
        send_log.append((address, body, purpose))
        return {"ok": True, "message_id": f"msg-{len(send_log)}"}

    with kbc.connect_closing(db_path) as conn:
        # Enqueue 3 messages: 2 for addr_a, 1 for addr_b
        id_a1 = outbox.queue_reply(conn, "ticket-1", "slack", addr_a, "ack", "Body A1", now)
        id_a2 = outbox.queue_reply(conn, "ticket-1", "slack", addr_a, "followup", "Body A2", now)
        id_b1 = outbox.queue_reply(conn, "ticket-1", "slack", addr_b, "notice", "Body B1", now)
        assert id_a1 < id_a2 < id_b1

        summary = outbox.drain_outbox(conn, now=now, sender=fake_sender)
        assert summary == {"sent": 3, "failed": 0, "dropped": 0}

        # Verify send_log received all 3 in order (id_a1 first, id_b1, then id_a2 after id_a1 sent)
        assert len(send_log) == 3
        assert send_log[0] == (addr_a, "Body A1", "ack")
        assert send_log[1] == (addr_b, "Body B1", "notice")
        assert send_log[2] == (addr_a, "Body A2", "followup")

        # Verify all 3 are marked 'sent' in database
        cur = conn.execute("SELECT id, status, sent_at, receipt_json FROM sa_outbox ORDER BY id ASC")
        rows = cur.fetchall()
        assert len(rows) == 3
        for r in rows:
            assert r["status"] == "sent"
            assert r["sent_at"] == now
            receipt = json.loads(r["receipt_json"])
            assert receipt["ok"] is True

        # Re-running drain when empty returns zeros and executes no sends
        summary_empty = outbox.drain_outbox(conn, now=now + 10, sender=fake_sender)
        assert summary_empty == {"sent": 0, "failed": 0, "dropped": 0}
        assert len(send_log) == 3


def test_unreachable_errors_marked_dropped(tmp_path: Path) -> None:
    """Unreachable recipient errors transition outbox rows to 'dropped' and are never retried."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000

    unreachable_samples = [
        ("telegram", {"platform": "telegram", "chat_id": "101"}, "Telegram error: Bot was blocked by the user"),
        ("discord", {"platform": "discord", "chat_id": "202"}, "Channel 202 not found"),
        ("slack", {"platform": "slack", "chat_id": "303"}, "Slack API call failed: channel_not_found"),
        ("telegram", {"platform": "telegram", "chat_id": "404"}, "Telegram: user is deactivated"),
        ("slack", {"platform": "slack", "chat_id": "505"}, "Slack error: not_in_channel"),
    ]

    with kbc.connect_closing(db_path) as conn:
        for source, addr, err_msg in unreachable_samples:
            row_id = outbox.queue_reply(conn, "ticket-drop", source, addr, "msg", "Hello", now)

            def failing_sender(address: dict[str, Any], body: str, purpose: str, _err=err_msg) -> dict[str, Any]:
                raise RuntimeError(_err)

            summary = outbox.drain_outbox(conn, now=now, sender=failing_sender)
            assert summary == {"sent": 0, "failed": 0, "dropped": 1}

            cur = conn.execute("SELECT status, dropped_reason FROM sa_outbox WHERE id = ?", (row_id,))
            row = cur.fetchone()
            assert row["status"] == "dropped"
            assert row["dropped_reason"] == err_msg

        # Ensure none of the dropped rows are ever due again
        assert outbox.due_replies(conn, now=now + 1000000) == []


def test_subchat_error_is_not_treated_as_dead_chat(tmp_path: Path) -> None:
    """Thread-level / sub-chat errors (e.g. 'thread not found') are not treated as whole-chat unreachability."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000
    addr = {"platform": "telegram", "chat_id": "100", "thread_id": "55"}

    with kbc.connect_closing(db_path) as conn:
        row_id = outbox.queue_reply(conn, "t-sub", "telegram", addr, "msg", "Thread message", now)

        def thread_err_sender(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
            raise RuntimeError("Telegram: message thread not found")

        summary = outbox.drain_outbox(conn, now=now, sender=thread_err_sender)
        # Should be marked failed (temporary / thread retry), NOT dropped
        assert summary == {"sent": 0, "failed": 1, "dropped": 0}

        cur = conn.execute("SELECT status, last_error FROM sa_outbox WHERE id = ?", (row_id,))
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert "thread not found" in row["last_error"]


def test_flood_error_matches_helper_hint(tmp_path: Path) -> None:
    """Flood and rate-limit errors extract delay hint and schedule next_attempt_at accordingly."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000

    with kbc.connect_closing(db_path) as conn:
        # Case 1: Standard normalized flood_control:<seconds>
        id_1 = outbox.queue_reply(conn, "t-1", "telegram", {"platform": "telegram", "chat_id": "1"}, "m", "Hi", now)

        def flood_sender_1(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
            raise RuntimeError("flood_control:45")

        summary_1 = outbox.drain_outbox(conn, now=now, sender=flood_sender_1)
        assert summary_1 == {"sent": 0, "failed": 1, "dropped": 0}

        cur = conn.execute("SELECT status, attempts, next_attempt_at FROM sa_outbox WHERE id = ?", (id_1,))
        r1 = cur.fetchone()
        assert r1["status"] == "failed"
        assert r1["attempts"] == 1
        assert r1["next_attempt_at"] == now + 45

        # Case 2: Raw Telegram flood wording
        id_2 = outbox.queue_reply(conn, "t-2", "telegram", {"platform": "telegram", "chat_id": "2"}, "m", "Hi", now)

        def flood_sender_2(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
            raise RuntimeError("Flood control exceeded. Retry in 185 seconds")

        summary_2 = outbox.drain_outbox(conn, now=now, sender=flood_sender_2)
        assert summary_2 == {"sent": 0, "failed": 1, "dropped": 0}

        cur = conn.execute("SELECT status, attempts, next_attempt_at FROM sa_outbox WHERE id = ?", (id_2,))
        r2 = cur.fetchone()
        assert r2["status"] == "failed"
        assert r2["attempts"] == 1
        assert r2["next_attempt_at"] == now + 185


def test_generic_error_doubling_backoff(tmp_path: Path) -> None:
    """Generic network errors use Card 4.2 exponential doubling backoff sequence."""
    db_path = _init_test_db(tmp_path)
    now = 1000000
    addr = {"platform": "slack", "chat_id": "C_GEN"}

    def generic_failing_sender(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
        raise ConnectionResetError("Connection reset by peer")

    with kbc.connect_closing(db_path) as conn:
        row_id = outbox.queue_reply(conn, "t-gen", "slack", addr, "ping", "Ping", now)

        # Attempt 1: backoff is 5 * 2^0 = 5s
        summary_1 = outbox.drain_outbox(conn, now=now, sender=generic_failing_sender)
        assert summary_1 == {"sent": 0, "failed": 1, "dropped": 0}

        cur = conn.execute("SELECT attempts, next_attempt_at, status FROM sa_outbox WHERE id = ?", (row_id,))
        r1 = cur.fetchone()
        assert r1["attempts"] == 1
        assert r1["next_attempt_at"] == now + 5

        # Attempt 2: at now + 5, backoff is 5 * 2^1 = 10s -> next is now + 5 + 10 = now + 15
        summary_2 = outbox.drain_outbox(conn, now=now + 5, sender=generic_failing_sender)
        assert summary_2 == {"sent": 0, "failed": 1, "dropped": 0}

        cur = conn.execute("SELECT attempts, next_attempt_at FROM sa_outbox WHERE id = ?", (row_id,))
        r2 = cur.fetchone()
        assert r2["attempts"] == 2
        assert r2["next_attempt_at"] == (now + 5) + 10

        # Attempt 3: at now + 15, backoff is 5 * 2^2 = 20s -> next is now + 15 + 20 = now + 35
        summary_3 = outbox.drain_outbox(conn, now=now + 15, sender=generic_failing_sender)
        assert summary_3 == {"sent": 0, "failed": 1, "dropped": 0}

        cur = conn.execute("SELECT attempts, next_attempt_at FROM sa_outbox WHERE id = ?", (row_id,))
        r3 = cur.fetchone()
        assert r3["attempts"] == 3

        # At attempts == 3, the row is permanently exhausted and never due
        due = outbox.due_replies(conn, now=now + 100000)
        assert [r["id"] for r in due] == []


def test_over_limit_im_cap_source(tmp_path: Path) -> None:
    """Body exceeding 100 words on an IM source is blocked, marked failed, and notice counter incremented."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000
    addr = {"platform": "telegram", "chat_id": "12345"}

    # Construct body with 105 words
    long_body = "word " * 105

    send_log: list[str] = []

    def tracking_sender(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
        send_log.append(body)
        return {"ok": True}

    with kbc.connect_closing(db_path) as conn:
        row_id = outbox.queue_reply(conn, "t-im-cap", "telegram", addr, "msg", long_body, now)

        summary = outbox.drain_outbox(conn, now=now, sender=tracking_sender)
        # Never sent!
        assert len(send_log) == 0
        assert summary == {"sent": 0, "failed": 1, "dropped": 0}

        # Check DB row state
        cur = conn.execute("SELECT status, last_error, attempts FROM sa_outbox WHERE id = ?", (row_id,))
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert row["last_error"] == "over_limit"
        assert row["attempts"] == 1

        # Check sa_meta counter incremented
        assert outbox.get_sa_meta(conn, "over_limit_notices") == "1"

        # Another over_limit message increments the counter to 2
        row_id2 = outbox.queue_reply(conn, "t-im-cap-2", "slack", {"platform": "slack", "chat_id": "C1"}, "msg", long_body, now)
        assert row_id2 > 0
        summary2 = outbox.drain_outbox(conn, now=now, sender=tracking_sender)
        assert summary2 == {"sent": 0, "failed": 1, "dropped": 0}
        assert outbox.get_sa_meta(conn, "over_limit_notices") == "2"


def test_over_limit_non_cap_source_github(tmp_path: Path) -> None:
    """Body exceeding 100 words on a non-IM source (github) is sent without restriction."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000
    addr = {"platform": "github", "repo": "nousresearch/hermes-agent", "issue_number": 42}
    long_body = "documentation details " * 60  # 120 words

    send_log: list[str] = []

    def tracking_sender(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
        send_log.append(body)
        return {"ok": True, "comment_id": 9999}

    with kbc.connect_closing(db_path) as conn:
        row_id = outbox.queue_reply(conn, "t-gh", "github", addr, "review", long_body, now)

        summary = outbox.drain_outbox(conn, now=now, sender=tracking_sender)
        assert summary == {"sent": 1, "failed": 0, "dropped": 0}
        assert len(send_log) == 1

        cur = conn.execute("SELECT status, sent_at FROM sa_outbox WHERE id = ?", (row_id,))
        row = cur.fetchone()
        assert row["status"] == "sent"
        assert row["sent_at"] == now

        # sa_meta counter was never touched
        assert outbox.get_sa_meta(conn, "over_limit_notices") is None


def test_kill_mid_drain_crash_safety(tmp_path: Path) -> None:
    """Mid-drain crash safety: does not double-send sent rows, recovers stuck 'sending' row exactly once."""
    db_path = _init_test_db(tmp_path)
    now = 1000000
    addr_1 = {"platform": "slack", "chat_id": "C1"}
    addr_2 = {"platform": "slack", "chat_id": "C2"}

    class SimulatedCrash(BaseException):
        """Simulates process termination or power loss."""

    send_history: list[str] = []
    crash_armed = True

    def flaky_sender(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
        send_history.append(body)
        if body == "Msg 2" and crash_armed:
            # Simulate crash after message was transmitted to the platform
            raise SimulatedCrash("Gateway process killed mid-drain after accepting send")
        return {"message_id": f"id-{body}"}

    with kbc.connect_closing(db_path) as conn:
        id_1 = outbox.queue_reply(conn, "t-crash", "slack", addr_1, "update", "Msg 1", now)
        id_2 = outbox.queue_reply(conn, "t-crash", "slack", addr_2, "update", "Msg 2", now)

        # 1. First drain crashes during row 2
        with pytest.raises(SimulatedCrash):
            outbox.drain_outbox(conn, now=now, sender=flaky_sender)

        # Disarm crash for future runs
        crash_armed = False

        # Verify DB state right after crash:
        # id_1 completed and was marked 'sent'
        r1 = conn.execute("SELECT status, sent_at FROM sa_outbox WHERE id = ?", (id_1,)).fetchone()
        assert r1["status"] == "sent"
        assert r1["sent_at"] == now

        # id_2 was left in 'sending'
        r2 = conn.execute("SELECT status, attempts FROM sa_outbox WHERE id = ?", (id_2,)).fetchone()
        assert r2["status"] == "sending"
        assert r2["attempts"] == 0

        # 2. Early restart: running drain only 60 seconds later (< 10m stale timeout)
        # id_2 is NOT stale yet, so it is not due and not sent
        summary_early = outbox.drain_outbox(conn, now=now + 60, sender=flaky_sender)
        assert summary_early == {"sent": 0, "failed": 0, "dropped": 0}
        assert send_history == ["Msg 1", "Msg 2"]  # no new send

        # 3. Full restart after 10 minutes (now + 601s):
        # Stale row id_2 must be recovered to 'failed' with attempts unchanged, and sent!
        summary_recovered = outbox.drain_outbox(conn, now=now + 601, sender=flaky_sender)
        assert summary_recovered == {"sent": 1, "failed": 0, "dropped": 0}

        # Verify id_1 was NOT double-sent (only in send_history once)
        assert send_history.count("Msg 1") == 1
        # id_2 was sent once more upon recovery
        assert send_history.count("Msg 2") == 2

        # Verify id_2 is now marked 'sent'
        r2_after = conn.execute("SELECT status, sent_at FROM sa_outbox WHERE id = ?", (id_2,)).fetchone()
        assert r2_after["status"] == "sent"
        assert r2_after["sent_at"] == now + 601

        # 4. Third drain: both rows are sent, nothing is due or re-sent
        summary_clean = outbox.drain_outbox(conn, now=now + 602, sender=flaky_sender)
        assert summary_clean == {"sent": 0, "failed": 0, "dropped": 0}
        assert len(send_history) == 3  # Msg 1, Msg 2 (crash), Msg 2 (recovered)


def test_drain_with_path_input(tmp_path: Path) -> None:
    """drain_outbox accepts a filesystem Path or string path seamlessly."""
    db_path = _init_test_db(tmp_path)
    now = 2000000

    row_id = outbox.queue_reply(
        db_path,
        "t-path",
        "slack",
        {"platform": "slack", "chat_id": "C_PATH"},
        "test",
        "Via Path",
        now,
    )

    def dummy_sender(address: dict[str, Any], body: str, purpose: str) -> dict[str, Any]:
        return {"receipt": "ok"}

    summary = outbox.drain_outbox(str(db_path), now=now, sender=dummy_sender)
    assert summary == {"sent": 1, "failed": 0, "dropped": 0}

    with kbc.connect_closing(db_path) as conn:
        cur = conn.execute("SELECT status FROM sa_outbox WHERE id = ?", (row_id,))
        assert cur.fetchone()["status"] == "sent"

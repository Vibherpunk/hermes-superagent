"""Tests for Superagent orchestrator read operations and query functions."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator_reads import (
    decisions,
    dropped_and_failed_replies,
    flow_counts_24h,
    health,
    needs_person,
    thread_timeline,
    ticket_detail,
)


@pytest.fixture
def conn(tmp_path: Path):
    """Temporary Kanban database initialized via repo schema-init path."""
    db_path = tmp_path / "orchestrator_reads_test.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as c:
        yield c


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    title: str = "Test Task",
    status: str = "todo",
    block_kind: str | None = None,
    created_at: int = 1000,
    priority: int = 0,
    assignee: str | None = None,
    ticket_kind: str | None = None,
    mandate_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, block_kind, created_at, priority,
            assignee, ticket_kind, mandate_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            title,
            status,
            block_kind,
            created_at,
            priority,
            assignee,
            ticket_kind,
            mandate_id,
        ),
    )


def test_needs_person_derived_stages(conn: sqlite3.Connection) -> None:
    """Requirement: one fixture per derived-stage row exercised through needs_person.

    Assert count == list length and stages are correct.
    """
    # 1. person/delivery: ticket with a failed outbox row
    _insert_task(conn, "t_deliv", title="Delivery Error Task", status="todo", created_at=100)
    conn.execute(
        """
        INSERT INTO sa_outbox (
            ticket_id, source, purpose, body, status, attempts, last_error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("t_deliv", "slack", "reply", "Failed msg", "failed", 1, "Connection refused", 110),
    )

    # 2. person/answer: blocked with block_kind='needs_input'
    _insert_task(
        conn,
        "t_answer",
        title="Needs Input Task",
        status="blocked",
        block_kind="needs_input",
        created_at=200,
    )

    # 3. person/review: blocked with block_kind='review'
    _insert_task(
        conn,
        "t_review",
        title="Review Task",
        status="blocked",
        block_kind="review",
        created_at=300,
    )

    # 4. person/blocked: blocked without specific input/review kind
    _insert_task(
        conn,
        "t_blocked",
        title="Stalled Task",
        status="blocked",
        block_kind="external_dependency",
        created_at=400,
    )

    # Non-person stages: should NOT appear in needs_person
    _insert_task(conn, "t_working", title="Running Task", status="running", created_at=500)
    _insert_task(conn, "t_queued", title="Queued Task", status="todo", created_at=600)
    _insert_task(conn, "t_closed", title="Done Task", status="done", created_at=700)

    results = needs_person(conn)

    # Must contain exactly the 4 tickets needing a person
    assert len(results) == 4
    result_stages = {r["stage"] for r in results}
    expected_stages = {
        "person/delivery",
        "person/answer",
        "person/review",
        "person/blocked",
    }
    assert result_stages == expected_stages

    # Verify pinned fields in all items
    stage_by_id = {r["ticket_id"]: r["stage"] for r in results}
    assert stage_by_id["t_deliv"] == "person/delivery"
    assert stage_by_id["t_answer"] == "person/answer"
    assert stage_by_id["t_review"] == "person/review"
    assert stage_by_id["t_blocked"] == "person/blocked"

    for r in results:
        assert "ticket_id" in r
        assert "stage" in r
        assert "title" in r
        assert "status" in r
        assert "block_kind" in r
        assert "assignee" in r
        assert "priority" in r
        assert "created_at" in r
        assert "ticket_kind" in r
        assert "mandate_id" in r


def test_health_counts_match_inserted_rows(conn: sqlite3.Connection) -> None:
    """Requirement: health counts match inserted rows."""
    now = 500_000

    # sa_meta: paused = true
    conn.execute("INSERT INTO sa_meta (key, value) VALUES ('paused', 'true')")

    # sa_outbox: 2 due (queued), 1 failed, 1 sent
    conn.execute(
        """
        INSERT INTO sa_outbox (source, purpose, body, status, next_attempt_at, created_at)
        VALUES
            ('slack', 'reply', 'due 1', 'queued', 0, ?),
            ('slack', 'reply', 'due 2', 'queued', ?, ?),
            ('slack', 'reply', 'failed', 'failed', 0, ?),
            ('slack', 'reply', 'sent', 'sent', 0, ?)
        """,
        (now, now - 100, now, now, now),
    )

    # sa_triggers: 3 queued, 1 processed
    conn.execute(
        """
        INSERT INTO sa_triggers (
            id, source, scope, thread_key, kind, idempotency_key, eval_state, created_at
        ) VALUES
            ('trig_1', 'slack', 'chan1', 'th1', 'msg', 'ik_1', 'queued', ?),
            ('trig_2', 'slack', 'chan1', 'th1', 'msg', 'ik_2', 'queued', ?),
            ('trig_3', 'slack', 'chan1', 'th1', 'msg', 'ik_3', 'queued', ?),
            ('trig_4', 'slack', 'chan1', 'th1', 'msg', 'ik_4', 'done', ?)
        """,
        (now, now, now, now),
    )

    # sa_decisions: 1 unavailable in 24h, 1 unavailable >24h old, 1 accept in 24h
    conn.execute(
        """
        INSERT INTO sa_decisions (
            thread_key, outcome, policy_version, created_at
        ) VALUES
            ('th1', 'unavailable', '1.0', ?),
            ('th1', 'unavailable', '1.0', ?),
            ('th1', 'accept', '1.0', ?)
        """,
        (now - 1000, now - 100_000, now - 500),
    )

    h = health(conn, now=now)

    assert h["paused"] is True
    assert h["outbox_due"] == 2
    assert h["outbox_failed"] == 1
    assert h["eval_queued"] == 3
    assert h["unavailable_24h"] == 1

    # Invert paused flag
    conn.execute("UPDATE sa_meta SET value = '0' WHERE key = 'paused'")
    h_unpaused = health(conn, now=now)
    assert h_unpaused["paused"] is False


def test_flow_counts_24h_excludes_25h_old_row(conn: sqlite3.Connection) -> None:
    """Requirement: flow_counts_24h excludes a 25h-old row."""
    now = 200_000
    recent_ts = now - 3600    # 1 hour ago (within 24h)
    old_ts = now - 90_000      # 25 hours ago (> 86400s)

    # Triggers: 1 recent, 1 25h-old
    conn.execute(
        """
        INSERT INTO sa_triggers (
            id, source, scope, thread_key, kind, idempotency_key, created_at
        ) VALUES
            ('tr_recent', 'slack', 'chan1', 'th1', 'msg', 'ik_rec', ?),
            ('tr_old', 'slack', 'chan1', 'th1', 'msg', 'ik_old', ?)
        """,
        (recent_ts, old_ts),
    )

    # Decisions: 2 recent (accept, reject), 1 25h-old (accept)
    conn.execute(
        """
        INSERT INTO sa_decisions (thread_key, outcome, policy_version, created_at)
        VALUES
            ('th1', 'accept', '1.0', ?),
            ('th1', 'reject', '1.0', ?),
            ('th1', 'accept', '1.0', ?)
        """,
        (recent_ts, recent_ts, old_ts),
    )

    # Tickets: 1 recent, 1 25h-old
    _insert_task(conn, "t_recent", created_at=recent_ts)
    _insert_task(conn, "t_old", created_at=old_ts)

    # Outbox: 1 sent recent, 1 failed recent, 1 dropped recent, 1 sent old
    conn.execute(
        """
        INSERT INTO sa_outbox (source, purpose, body, status, created_at)
        VALUES
            ('slack', 'reply', 'b1', 'sent', ?),
            ('slack', 'reply', 'b2', 'failed', ?),
            ('slack', 'reply', 'b3', 'dropped', ?),
            ('slack', 'reply', 'b4', 'sent', ?)
        """,
        (recent_ts, recent_ts, recent_ts, old_ts),
    )

    counts = flow_counts_24h(conn, now=now)

    assert counts["triggers_received"] == 1
    assert counts["decisions_by_outcome"] == {"accept": 1, "reject": 1}
    assert counts["tickets_created"] == 1
    assert counts["replies_sent"] == 1
    assert counts["replies_failed"] == 1
    assert counts["replies_dropped"] == 1
    assert counts["replies"] == {"sent": 1, "failed": 1, "dropped": 1}


def test_thread_timeline_ordering(conn: sqlite3.Connection) -> None:
    """Requirement: timeline ordering across the three sources (trigger, decision, input)."""
    thread_key = "slack:C123:msg42"

    _insert_task(conn, "t_timeline", title="Subscribed Task")
    conn.execute(
        "INSERT INTO sa_subscriptions (thread_key, ticket_id, created_at) VALUES (?, ?, ?)",
        (thread_key, "t_timeline", 50),
    )

    # Insert events at timestamps 100, 200, 300 in non-chronological order
    conn.execute(
        """
        INSERT INTO sa_decisions (thread_key, outcome, strength, ticket_id, policy_version, created_at)
        VALUES (?, 'accept', 'strong', 't_timeline', '1.0', 300)
        """,
        (thread_key,),
    )
    conn.execute(
        """
        INSERT INTO sa_triggers (
            id, source, scope, thread_key, kind, body, idempotency_key, created_at
        ) VALUES ('trig_th', 'slack', 'C123', ?, 'message', 'Initial thread message', 'ik_th', 100)
        """,
        (thread_key,),
    )
    conn.execute(
        """
        INSERT INTO sa_inputs (ticket_id, kind, body, created_at)
        VALUES ('t_timeline', 'reply', 'User follow-up message', 200)
        """,
    )

    timeline = thread_timeline(conn, thread_key)

    assert len(timeline) == 3
    assert timeline[0]["ts"] == 100
    assert timeline[0]["kind"] == "trigger"
    assert "Trigger [slack] message:" in timeline[0]["summary"]

    assert timeline[1]["ts"] == 200
    assert timeline[1]["kind"] == "input"
    assert "Input [reply] ticket=t_timeline:" in timeline[1]["summary"]

    assert timeline[2]["ts"] == 300
    assert timeline[2]["kind"] == "decision"
    assert "Decision outcome=accept" in timeline[2]["summary"]


def test_ticket_detail_and_missing_id(conn: sqlite3.Connection) -> None:
    """Requirement: ticket_detail None for a missing id; task row + mandate + inputs + outbox."""
    assert ticket_detail(conn, "t_nonexistent") is None

    _insert_task(
        conn,
        "t_full",
        title="Detail Ticket",
        status="running",
        assignee="worker1",
        created_at=1000,
    )
    conn.execute(
        """
        INSERT INTO sa_mandates (ticket_id, policy_version, scope, strength, created_at)
        VALUES ('t_full', 'v2', 'project-x', 'enforced', 1010)
        """,
    )
    conn.execute(
        """
        INSERT INTO sa_inputs (ticket_id, kind, body, created_at)
        VALUES
            ('t_full', 'context', 'Context line 1', 1020),
            ('t_full', 'instruction', 'Instruction line 2', 1030)
        """,
    )
    conn.execute(
        """
        INSERT INTO sa_outbox (ticket_id, source, purpose, body, status, created_at)
        VALUES ('t_full', 'slack', 'status_update', 'Working on it', 'sent', 1040)
        """,
    )

    detail = ticket_detail(conn, "t_full")
    assert detail is not None
    assert detail["id"] == "t_full"
    assert detail["title"] == "Detail Ticket"
    assert detail["status"] == "running"
    assert detail["mandate"] is not None
    assert detail["mandate"]["ticket_id"] == "t_full"
    assert detail["mandate"]["policy_version"] == "v2"
    assert len(detail["inputs"]) == 2
    assert detail["inputs"][0]["body"] == "Context line 1"
    assert len(detail["outbox"]) == 1
    assert detail["outbox"][0]["body"] == "Working on it"


def test_decisions_outcome_filter(conn: sqlite3.Connection) -> None:
    """Requirement: decisions outcome filter and ordering."""
    conn.execute(
        """
        INSERT INTO sa_decisions (thread_key, outcome, policy_version, created_at)
        VALUES
            ('th_a', 'accept', '1.0', 10),
            ('th_b', 'reject', '1.0', 20),
            ('th_c', 'accept', '1.0', 30),
            ('th_d', 'unavailable', '1.0', 40),
            ('th_e', 'accept', '1.0', 50)
        """,
    )

    all_decs = decisions(conn)
    assert len(all_decs) == 5
    # Newest first
    assert [d["created_at"] for d in all_decs] == [50, 40, 30, 20, 10]

    accept_only = decisions(conn, outcome="accept")
    assert len(accept_only) == 3
    assert all(d["outcome"] == "accept" for d in accept_only)
    assert [d["created_at"] for d in accept_only] == [50, 30, 10]

    limited = decisions(conn, limit=2)
    assert len(limited) == 2
    assert [d["created_at"] for d in limited] == [50, 40]


def test_dropped_and_failed_replies(conn: sqlite3.Connection) -> None:
    """Requirement: dropped_and_failed includes a 3-attempt failure and a dropped row

    but not a 1-attempt failure.
    """
    conn.execute(
        """
        INSERT INTO sa_outbox (source, purpose, body, status, attempts, created_at)
        VALUES
            ('slack', 'reply', 'fail-3', 'failed', 3, 100),
            ('slack', 'reply', 'dropped-1', 'dropped', 1, 200),
            ('slack', 'reply', 'fail-1', 'failed', 1, 300),
            ('slack', 'reply', 'fail-2', 'failed', 2, 400),
            ('slack', 'reply', 'sent-1', 'sent', 1, 500),
            ('slack', 'reply', 'queued-0', 'queued', 0, 600)
        """,
    )

    rows = dropped_and_failed_replies(conn)
    assert len(rows) == 2

    # Newest first: dropped at 200, failed-3 at 100
    assert rows[0]["body"] == "dropped-1"
    assert rows[0]["status"] == "dropped"
    assert rows[1]["body"] == "fail-3"
    assert rows[1]["status"] == "failed"
    assert rows[1]["attempts"] == 3

    # Assert 1-attempt failure and 2-attempt failure are strictly excluded
    bodies = {r["body"] for r in rows}
    assert "fail-1" not in bodies
    assert "fail-2" not in bodies
    assert "sent-1" not in bodies
    assert "queued-0" not in bodies

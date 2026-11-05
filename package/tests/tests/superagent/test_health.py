"""Unit tests for Superagent Health Triggers API (hermes_cli/superagent_health.py).

Verifies:
1. One event -> one trigger with the pinned shape per E-005 and SPEC-V2.
2. Same subject+kind twice in the same hour (two calls) -> still one row (hourly dedup).
3. Same subject in the next hour bucket -> a second row.
4. Two distinct subjects -> two rows.
5. All four kinds accepted ("reclaimed", "orphaned_running", "circuit_breaker", "gave_up").
6. Unknown kind raises ValueError.
7. Repo vocabulary aliases (stale, reconciled_orphans, auto_blocked) map and are accepted.
8. Connection and Path/str argument resolution.
9. Intra-batch duplicate deduplication.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import superagent_health as health


def _init_test_db(tmp_path: Path) -> Path:
    """Initialize a fresh temporary Kanban DB with all superagent schemas."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    return db_path


def test_one_event_pinned_shape(tmp_path: Path) -> None:
    """Requirement 1: one event -> one trigger with the pinned shape."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000.5
    detail = {"error": "worker lost connection", "worker_pid": 1234}
    events = [
        {
            "kind": "reclaimed",
            "subject": "task-alpha-100",
            "detail": detail,
        }
    ]

    with kbc.connect_closing(db_path) as conn:
        trigger_ids = health.emit_health_triggers(conn, events, now=now)
        assert len(trigger_ids) == 1
        trigger_id = trigger_ids[0]

        # Verify ID format trg_<uuid4hex12> per DECISIONS E-005
        assert trigger_id.startswith("trg_")
        assert len(trigger_id) == 16

        # Verify exact row shape in sa_triggers
        row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trigger_id,)).fetchone()
        assert row is not None
        assert row["id"] == trigger_id
        assert row["source"] == "internal"
        assert row["scope"] == "health"
        assert row["thread_key"] == "health:reclaimed:task-alpha-100"
        assert row["actor_source_id"] is None
        assert row["actor_is_bot"] == 0
        assert row["addressed"] == 0
        assert row["kind"] == "health"
        assert "worker lost connection" in row["body"]
        assert "\n" not in row["body"]

        # Meta json verification
        meta = json.loads(row["meta_json"])
        assert meta["error"] == "worker lost connection"
        assert meta["worker_pid"] == 1234

        assert row["correlation_json"] == "[]"
        expected_idem = f"health:reclaimed:task-alpha-100:{int(now) // 3600}"
        assert row["idempotency_key"] == expected_idem
        assert row["eval_state"] == "queued"
        assert row["eval_requeues"] == 0
        assert row["disposition"] is None
        assert row["created_at"] == int(now)


def test_hourly_dedup_same_hour_still_one_row(tmp_path: Path) -> None:
    """Requirement 2: same subject+kind twice in the same hour (two calls) -> still one row."""
    db_path = _init_test_db(tmp_path)
    base_now = 1700000000  # 1700000000 // 3600 = 472222

    with kbc.connect_closing(db_path) as conn:
        event = {"kind": "circuit_breaker", "subject": "task-dup-1", "detail": "tripped breaker"}

        # First call: creates row
        created1 = health.emit_health_triggers(conn, [event], now=base_now)
        assert len(created1) == 1

        # Second call 15 minutes later (same hour bucket): deduped
        second_call_time = base_now + 900
        assert (second_call_time // 3600) == (base_now // 3600)

        created2 = health.emit_health_triggers(conn, [event], now=second_call_time)
        assert created2 == []

        # Verify only 1 row exists in sa_triggers
        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 1

        row = conn.execute("SELECT id FROM sa_triggers").fetchone()
        assert row["id"] == created1[0]


def test_hourly_dedup_next_hour_second_row(tmp_path: Path) -> None:
    """Requirement 3: same subject in the NEXT hour bucket -> a second row."""
    db_path = _init_test_db(tmp_path)
    base_now = 1700000000  # bucket 472222
    next_hour_now = base_now + 3601  # bucket 472223

    with kbc.connect_closing(db_path) as conn:
        event = {"kind": "gave_up", "subject": "task-recur-1", "detail": "retry budget exceeded"}

        # First hour
        created1 = health.emit_health_triggers(conn, [event], now=base_now)
        assert len(created1) == 1

        # Next hour bucket: creates new row
        created2 = health.emit_health_triggers(conn, [event], now=next_hour_now)
        assert len(created2) == 1
        assert created2[0] != created1[0]

        # Verify 2 rows in DB with distinct idempotency keys
        rows = conn.execute(
            "SELECT id, idempotency_key FROM sa_triggers WHERE thread_key = ? ORDER BY created_at ASC",
            ("health:gave_up:task-recur-1",),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] == created1[0]
        assert rows[1]["id"] == created2[0]
        assert rows[0]["idempotency_key"] == f"health:gave_up:task-recur-1:{base_now // 3600}"
        assert rows[1]["idempotency_key"] == f"health:gave_up:task-recur-1:{next_hour_now // 3600}"


def test_two_distinct_subjects_two_rows(tmp_path: Path) -> None:
    """Requirement 4: two distinct subjects -> two rows."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000

    events = [
        {"kind": "orphaned_running", "subject": "task-subject-A", "detail": "lost worker"},
        {"kind": "orphaned_running", "subject": "task-subject-B", "detail": "lost worker"},
    ]

    with kbc.connect_closing(db_path) as conn:
        created = health.emit_health_triggers(conn, events, now=now)
        assert len(created) == 2
        assert created[0] != created[1]

        rows = conn.execute("SELECT id, thread_key FROM sa_triggers ORDER BY id").fetchall()
        assert len(rows) == 2
        thread_keys = {r["thread_key"] for r in rows}
        assert thread_keys == {
            "health:orphaned_running:task-subject-A",
            "health:orphaned_running:task-subject-B",
        }


def test_all_four_kinds_accepted(tmp_path: Path) -> None:
    """Requirement 5: all four kinds accepted."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000

    with kbc.connect_closing(db_path) as conn:
        for idx, kind in enumerate(health.FOUR_MAPPED_KINDS):
            subject = f"task-kind-{idx}"
            created = health.emit_health_triggers(
                conn,
                [{"kind": kind, "subject": subject, "detail": f"test payload for {kind}"}],
                now=now,
            )
            assert len(created) == 1

            row = conn.execute("SELECT thread_key, body FROM sa_triggers WHERE id = ?", (created[0],)).fetchone()
            assert row is not None
            assert row["thread_key"] == f"health:{kind}:{subject}"
            assert kind in row["body"]

        total = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert total == 4


def test_unknown_kind_raises_value_error(tmp_path: Path) -> None:
    """Requirement 6: unknown kind raises ValueError."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000

    with kbc.connect_closing(db_path) as conn:
        with pytest.raises(ValueError, match="Unknown health event kind"):
            health.emit_health_triggers(
                conn,
                [{"kind": "unsupported_kind", "subject": "task-err-1", "detail": None}],
                now=now,
            )

        # Confirm no row was written
        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 0


def test_repo_vocabulary_aliases_accepted(tmp_path: Path) -> None:
    """Verify repo vocabulary aliases (stale, reconciled_orphans, auto_blocked) map correctly."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000

    events = [
        {"kind": "stale", "subject": "task-alias-1", "detail": "stale heartbeat"},
        {"kind": "reconciled_orphans", "subject": "task-alias-2", "detail": "missing worker"},
        {"kind": "auto_blocked", "subject": "task-alias-3", "detail": "circuit tripped"},
        {"kind": "failure_budget_exhausted", "subject": "task-alias-4", "detail": "all attempts failed"},
    ]

    with kbc.connect_closing(db_path) as conn:
        created = health.emit_health_triggers(conn, events, now=now)
        assert len(created) == 4

        rows = conn.execute("SELECT thread_key FROM sa_triggers ORDER BY id").fetchall()
        thread_keys = {r["thread_key"] for r in rows}
        assert "health:reclaimed:task-alias-1" in thread_keys
        assert "health:orphaned_running:task-alias-2" in thread_keys
        assert "health:circuit_breaker:task-alias-3" in thread_keys
        assert "health:gave_up:task-alias-4" in thread_keys


def test_connection_or_path_support(tmp_path: Path) -> None:
    """Verify emit_health_triggers accepts Path or str or Connection."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000

    # Path object
    created_path = health.emit_health_triggers(
        db_path,
        [{"kind": "reclaimed", "subject": "task-p1"}],
        now=now,
    )
    assert len(created_path) == 1

    # String path
    created_str = health.emit_health_triggers(
        str(db_path),
        [{"kind": "circuit_breaker", "subject": "task-p2"}],
        now=now,
    )
    assert len(created_str) == 1

    # Connection
    with kbc.connect_closing(db_path) as conn:
        created_conn = health.emit_health_triggers(
            conn,
            [{"kind": "gave_up", "subject": "task-p3"}],
            now=now,
        )
        assert len(created_conn) == 1


def test_empty_events_and_validation(tmp_path: Path) -> None:
    """Verify empty events list and parameter validation."""
    db_path = _init_test_db(tmp_path)
    assert health.emit_health_triggers(db_path, [], now=1700000000) == []

    with pytest.raises(ValueError, match="must be a dict"):
        health.emit_health_triggers(db_path, ["not-a-dict"], now=1700000000)  # type: ignore[list-item]

    with pytest.raises(ValueError, match="non-empty 'subject'"):
        health.emit_health_triggers(
            db_path,
            [{"kind": "reclaimed", "subject": ""}],
            now=1700000000,
        )


def test_intra_batch_duplicate_deduped(tmp_path: Path) -> None:
    """Duplicate identical events within the same batch produce only one row."""
    db_path = _init_test_db(tmp_path)
    now = 1700000000
    events = [
        {"kind": "reclaimed", "subject": "task-same", "detail": "first"},
        {"kind": "reclaimed", "subject": "task-same", "detail": "second"},
    ]
    created = health.emit_health_triggers(db_path, events, now=now)
    assert len(created) == 1

    with kbc.connect_closing(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 1

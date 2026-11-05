"""Tests for VibeHard Source poller (Phase 6 / Card 6.2).

Verifies:
1. A new job emits exactly one 'submitted' trigger with correct metadata, actor, and cursor.
2. A status flip emits one 'state_change' trigger with from->to transition in body.
3. A flip to failed emits one 'failed' trigger with the error text in the body.
4. A second poll with no changes emits nothing (cursor idempotency).
5. enabled=false emits nothing.
6. Missing ledger file returns [] without raising.
7. Corrupt/unreadable ledger returns [] without raising.
8. Multiple jobs joined to sessions resolve client name correctly.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.superagent_vibehard import poll_vibehard


def init_vibehard_ledger(ledger_path: Path) -> None:
    """Create test fixture VibeHard SQLite database."""
    conn = sqlite3.connect(ledger_path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            client TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            spec TEXT,
            rigor TEXT DEFAULT 'standard',
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            updated_at TEXT,
            result_path TEXT,
            error TEXT,
            retries INTEGER DEFAULT 0,
            idempotency_key TEXT,
            runner_id TEXT,
            claimed_at TEXT,
            heartbeat_at TEXT,
            pipeline_version TEXT,
            kind TEXT DEFAULT 'build',
            target_dir TEXT,
            progress_revision INTEGER DEFAULT 0,
            output_dir TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _make_config(
    ledger_path: Path,
    enabled: bool = True,
    stuck_after_seconds: int = 1800,
) -> dict:
    return {
        "superagent": {
            "enabled": True,
            "vibehard": {
                "enabled": enabled,
                "ledger_path": str(ledger_path),
                "stuck_after_seconds": stuck_after_seconds,
            },
        }
    }


def test_new_job_emits_one_submitted(tmp_path: Path) -> None:
    """A new job emits exactly one 'submitted' trigger."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    # Insert session and job into ledger
    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-1', 'charlie', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at
        ) VALUES (
            'job-101', 'sess-1', 'Build the authentication gateway', 'pending', 'build', '/workspace/auth', '2026-11-03T10:00:05Z'
        )
        """
    )
    conn_ledger.commit()
    conn_ledger.close()

    cfg = _make_config(ledger_path)

    with kbc.connect_closing(board_db) as conn:
        trigger_ids = poll_vibehard(conn, cfg, now=1700000000.0)
        assert len(trigger_ids) == 1
        trg_id = trigger_ids[0]
        assert trg_id.startswith("trg_")

        row = conn.execute(
            """
            SELECT source, scope, thread_key, actor_source_id, actor_is_bot, kind, body, idempotency_key
            FROM sa_triggers WHERE id = ?
            """,
            (trg_id,),
        ).fetchone()

        assert row is not None
        assert row[0] == "vibehard"
        assert row[1] == "job-101"
        assert row[2] == "vibehard:job-job-101"
        assert row[3] == "vibehard:charlie"
        assert row[4] == 0
        assert row[5] == "submitted"
        assert "Kind: build" in row[6]
        assert "None -> pending" in row[6]
        assert "Build the authentication gateway" in row[6]
        assert "/workspace/auth" in row[6]
        assert row[7] == "vibehard:job-101:submitted"

        # Check cursor
        cursor_row = conn.execute(
            "SELECT cursor FROM sa_source_cursors WHERE source = 'vibehard' AND scope = 'job-101'"
        ).fetchone()
        assert cursor_row is not None
        cursor_data = json.loads(cursor_row[0])
        assert cursor_data["status"] == "pending"
        assert cursor_data["progress_revision"] == 0
        assert cursor_data["stuck_flag"] is False


def test_status_flip_emits_one_state_change(tmp_path: Path) -> None:
    """A status flip emits one 'state_change'."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-1', 'alice', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at
        ) VALUES (
            'job-201', 'sess-1', 'Refactor parser', 'pending', 'build', '/workspace/parser', '2026-11-03T10:00:05Z'
        )
        """
    )
    conn_ledger.commit()

    cfg = _make_config(ledger_path)

    with kbc.connect_closing(board_db) as conn:
        # First poll: emitted submitted
        first_poll = poll_vibehard(conn, cfg, now=1700000000.0)
        assert len(first_poll) == 1

        # Flip status to 'running'
        conn_ledger.execute(
            "UPDATE jobs SET status = 'running', progress_revision = 1, updated_at = '2026-11-03T10:02:00Z' WHERE id = 'job-201'"
        )
        conn_ledger.commit()
        conn_ledger.close()

        # Second poll: emits state_change
        second_poll = poll_vibehard(conn, cfg, now=1700000060.0)
        assert len(second_poll) == 1
        trg_id = second_poll[0]

        row = conn.execute(
            "SELECT kind, body FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "state_change"
        assert "pending -> running" in row[1]
        assert "Refactor parser" in row[1]

        # Verify cursor updated
        cursor_row = conn.execute(
            "SELECT cursor FROM sa_source_cursors WHERE source = 'vibehard' AND scope = 'job-201'"
        ).fetchone()
        cursor_data = json.loads(cursor_row[0])
        assert cursor_data["status"] == "running"
        assert cursor_data["progress_revision"] == 1


def test_flip_to_failed_emits_failed_with_error(tmp_path: Path) -> None:
    """A flip to failed emits one 'failed' with the error text in the body."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-1', 'bob', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at
        ) VALUES (
            'job-301', 'sess-1', 'Run migrations', 'running', 'build', '/workspace/db', '2026-11-03T10:00:05Z'
        )
        """
    )
    conn_ledger.commit()

    cfg = _make_config(ledger_path)

    with kbc.connect_closing(board_db) as conn:
        # First poll records initial 'running' state as submitted
        first_poll = poll_vibehard(conn, cfg, now=1700000000.0)
        assert len(first_poll) == 1

        # Flip status to 'failed' with error
        conn_ledger.execute(
            """
            UPDATE jobs SET
                status = 'failed',
                error = 'ConnectionRefusedError: database cluster down',
                progress_revision = 2,
                updated_at = '2026-11-03T10:05:00Z'
            WHERE id = 'job-301'
            """
        )
        conn_ledger.commit()
        conn_ledger.close()

        # Second poll: emits failed
        second_poll = poll_vibehard(conn, cfg, now=1700000100.0)
        assert len(second_poll) == 1
        trg_id = second_poll[0]

        row = conn.execute(
            "SELECT kind, body FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"
        assert "running -> failed" in row[1]
        assert "ConnectionRefusedError: database cluster down" in row[1]


def test_second_poll_with_no_changes_emits_nothing(tmp_path: Path) -> None:
    """A second poll with no changes emits nothing (cursor idempotency)."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-1', 'alice', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at
        ) VALUES (
            'job-401', 'sess-1', 'Deploy service', 'pending', 'deploy', '/workspace/srv', '2026-11-03T10:00:05Z'
        )
        """
    )
    conn_ledger.commit()
    conn_ledger.close()

    cfg = _make_config(ledger_path)

    with kbc.connect_closing(board_db) as conn:
        poll1 = poll_vibehard(conn, cfg, now=1700000000.0)
        assert len(poll1) == 1

        # Second poll without changing ledger
        poll2 = poll_vibehard(conn, cfg, now=1700000060.0)
        assert poll2 == []

        # Total rows in sa_triggers is still 1
        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 1


def test_enabled_false_emits_nothing(tmp_path: Path) -> None:
    """enabled=false emits nothing."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-1', 'alice', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at
        ) VALUES (
            'job-501', 'sess-1', 'Build package', 'pending', 'build', '/workspace/pkg', '2026-11-03T10:00:05Z'
        )
        """
    )
    conn_ledger.commit()
    conn_ledger.close()

    # 1. superagent.vibehard.enabled = False
    cfg_disabled = _make_config(ledger_path, enabled=False)

    with kbc.connect_closing(board_db) as conn:
        poll_res = poll_vibehard(conn, cfg_disabled, now=1700000000.0)
        assert poll_res == []

        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 0

        # 2. superagent.enabled = False
        cfg_sa_disabled = {
            "superagent": {
                "enabled": False,
                "vibehard": {
                    "enabled": True,
                    "ledger_path": str(ledger_path),
                },
            }
        }
        poll_res2 = poll_vibehard(conn, cfg_sa_disabled, now=1700000000.0)
        assert poll_res2 == []
        assert conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0] == 0


def test_missing_ledger_file_returns_empty_without_raising(tmp_path: Path) -> None:
    """Missing ledger file returns [] without raising."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    missing_path = tmp_path / "does_not_exist_ledger.sqlite"
    cfg = _make_config(missing_path, enabled=True)

    with kbc.connect_closing(board_db) as conn:
        res = poll_vibehard(conn, cfg, now=1700000000.0)
        assert res == []


def test_unreadable_or_corrupt_ledger_file_returns_empty_without_raising(tmp_path: Path) -> None:
    """Corrupt or non-sqlite ledger file returns [] without raising."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    corrupt_path = tmp_path / "corrupt_ledger.sqlite"
    corrupt_path.write_bytes(b"not a valid sqlite database header at all")

    cfg = _make_config(corrupt_path, enabled=True)

    with kbc.connect_closing(board_db) as conn:
        res = poll_vibehard(conn, cfg, now=1700000000.0)
        assert res == []


def test_multiple_jobs_and_client_resolution(tmp_path: Path) -> None:
    """Multiple jobs with and without session records resolve client properly."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-a', 'clientA', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at
        ) VALUES (
            'job-a', 'sess-a', 'Task A', 'pending', 'plan', '/workspace/a', '2026-11-03T10:00:01Z'
        )
        """
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at
        ) VALUES (
            'job-b', NULL, 'Task B without session', 'pending', 'build', '/workspace/b', '2026-11-03T10:00:02Z'
        )
        """
    )
    conn_ledger.commit()
    conn_ledger.close()

    cfg = _make_config(ledger_path)

    with kbc.connect_closing(board_db) as conn:
        triggers = poll_vibehard(conn, cfg, now=1700000000.0)
        assert len(triggers) == 2

        # Check actor for job-a
        row_a = conn.execute(
            "SELECT actor_source_id, thread_key FROM sa_triggers WHERE scope = 'job-a'"
        ).fetchone()
        assert row_a[0] == "vibehard:clientA"
        assert row_a[1] == "vibehard:job-job-a"

        # Check actor for job-b
        row_b = conn.execute(
            "SELECT actor_source_id, thread_key FROM sa_triggers WHERE scope = 'job-b'"
        ).fetchone()
        assert row_b[0] == "vibehard"
        assert row_b[1] == "vibehard:job-job-b"


def test_title_extracted_from_multiline_markdown_spec(tmp_path: Path) -> None:
    """Spec title extraction takes first line and strips markdown hashes."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at
        ) VALUES (
            'job-spec', NULL, '# Add Database Migrations\n\nDetailed breakdown of migrations...', 'pending', 'build', '/workspace/db', '2026-11-03T10:00:01Z'
        )
        """
    )
    conn_ledger.commit()
    conn_ledger.close()

    cfg = _make_config(ledger_path)

    with kbc.connect_closing(board_db) as conn:
        triggers = poll_vibehard(conn, cfg, now=1700000000.0)
        assert len(triggers) == 1

        body = conn.execute(
            "SELECT body FROM sa_triggers WHERE id = ?",
            (triggers[0],),
        ).fetchone()[0]
        assert "Title: Add Database Migrations" in body


def test_stuck_emits_exactly_once_across_two_polls(tmp_path: Path) -> None:
    """A working job whose heartbeat is older than stuck_after_seconds emits exactly ONE 'stuck' trigger across two polls."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    t0 = 1793700000.0  # 2026-11-03T10:00:00Z
    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-1', 'charlie', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at, updated_at, heartbeat_at
        ) VALUES (
            'job-stuck-1', 'sess-1', 'Build API server', 'running', 'build', '/workspace/api',
            '2026-11-03T10:00:00Z', '2026-11-03T10:00:00Z', '2026-11-03T10:00:00Z'
        )
        """
    )
    conn_ledger.commit()
    conn_ledger.close()

    # stuck_after_seconds = 1800
    cfg = _make_config(ledger_path, stuck_after_seconds=1800)

    with kbc.connect_closing(board_db) as conn:
        # First poll at t0: job is discovered and emits 'submitted'
        poll0 = poll_vibehard(conn, cfg, now=t0)
        assert len(poll0) == 1

        # Check cursor after initial poll: stuck_flag is False
        cursor_row = conn.execute(
            "SELECT cursor FROM sa_source_cursors WHERE source = 'vibehard' AND scope = 'job-stuck-1'"
        ).fetchone()
        assert cursor_row is not None
        cursor_data = json.loads(cursor_row[0])
        assert cursor_data["stuck_flag"] is False
        assert cursor_data["status"] == "running"

        # Advance time to t0 + 2000 (past 1800s threshold)
        # Poll 1: job is still in working status 'running' and heartbeat has not moved -> emits ONE 'stuck' trigger
        poll1 = poll_vibehard(conn, cfg, now=t0 + 2000)
        assert len(poll1) == 1
        stuck_trg_id = poll1[0]

        row = conn.execute(
            "SELECT kind, body, idempotency_key FROM sa_triggers WHERE id = ?",
            (stuck_trg_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "stuck"
        assert "running (stuck)" in row[1]
        assert "vibehard:job-stuck-1:stuck:" in row[2]

        # Verify cursor has stuck_flag set to True
        cursor_row = conn.execute(
            "SELECT cursor FROM sa_source_cursors WHERE source = 'vibehard' AND scope = 'job-stuck-1'"
        ).fetchone()
        cursor_data = json.loads(cursor_row[0])
        assert cursor_data["stuck_flag"] is True

        # Poll 2: job still stalled, heartbeat unchanged -> emits NOTHING (exactly once per episode)
        poll2 = poll_vibehard(conn, cfg, now=t0 + 2060)
        assert poll2 == []

        # Total triggers in sa_triggers for this job is 2 (1 submitted + 1 stuck)
        total_stuck = conn.execute(
            "SELECT COUNT(*) FROM sa_triggers WHERE scope = 'job-stuck-1' AND kind = 'stuck'"
        ).fetchone()[0]
        assert total_stuck == 1


def test_stuck_clears_on_heartbeat_advance_and_re_stalls(tmp_path: Path) -> None:
    """When heartbeat advances, stuck_flag clears; if it stalls again after moving, emits again."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    t0 = 1793700000.0  # 2026-11-03T10:00:00Z
    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-2', 'bob', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at, updated_at, heartbeat_at
        ) VALUES (
            'job-stuck-2', 'sess-2', 'Compile rust engine', 'running', 'build', '/workspace/engine',
            '2026-11-03T10:00:00Z', '2026-11-03T10:00:00Z', '2026-11-03T10:00:00Z'
        )
        """
    )
    conn_ledger.commit()

    cfg = _make_config(ledger_path, stuck_after_seconds=1800)

    with kbc.connect_closing(board_db) as conn:
        # First poll: submitted
        poll_vibehard(conn, cfg, now=t0)

        # Stalls: emits 1st stuck trigger
        poll1 = poll_vibehard(conn, cfg, now=t0 + 2000)
        assert len(poll1) == 1
        assert conn.execute("SELECT kind FROM sa_triggers WHERE id = ?", (poll1[0],)).fetchone()[0] == "stuck"

        # Heartbeat advances in ledger to 10:35:00Z (t0 + 2100)
        t_advance = t0 + 2100
        conn_ledger.execute(
            "UPDATE jobs SET heartbeat_at = '2026-11-03T10:35:00Z', updated_at = '2026-11-03T10:35:00Z' WHERE id = 'job-stuck-2'"
        )
        conn_ledger.commit()
        conn_ledger.close()

        # Poll shortly after advance: heartbeat moved, so stuck_flag clears and no trigger emitted
        poll_moving = poll_vibehard(conn, cfg, now=t_advance + 30)
        assert poll_moving == []

        cursor_row = conn.execute(
            "SELECT cursor FROM sa_source_cursors WHERE source = 'vibehard' AND scope = 'job-stuck-2'"
        ).fetchone()
        cursor_data = json.loads(cursor_row[0])
        assert cursor_data["stuck_flag"] is False

        # Now job stalls again: 2000 seconds after t_advance with no further heartbeat
        poll_restall = poll_vibehard(conn, cfg, now=t_advance + 2000)
        assert len(poll_restall) == 1
        stuck_trg_id2 = poll_restall[0]

        row2 = conn.execute("SELECT kind FROM sa_triggers WHERE id = ?", (stuck_trg_id2,)).fetchone()
        assert row2[0] == "stuck"

        # Verify cursor has stuck_flag set again
        cursor_row = conn.execute(
            "SELECT cursor FROM sa_source_cursors WHERE source = 'vibehard' AND scope = 'job-stuck-2'"
        ).fetchone()
        cursor_data = json.loads(cursor_row[0])
        assert cursor_data["stuck_flag"] is True

        # Total stuck triggers is now 2
        total_stuck = conn.execute(
            "SELECT COUNT(*) FROM sa_triggers WHERE scope = 'job-stuck-2' AND kind = 'stuck'"
        ).fetchone()[0]
        assert total_stuck == 2


def test_awaiting_human_emits_state_change_only_never_stuck(tmp_path: Path) -> None:
    """awaiting_human is daemon's person-gate (not a working status): transitions stay plain state_change, never stuck."""
    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    ledger_path = tmp_path / "ledger.sqlite"
    init_vibehard_ledger(ledger_path)

    t0 = 1793700000.0  # 2026-11-03T10:00:00Z
    conn_ledger = sqlite3.connect(ledger_path)
    conn_ledger.execute(
        "INSERT INTO sessions (id, client, created_at) VALUES ('sess-3', 'dave', '2026-11-03T10:00:00Z')"
    )
    conn_ledger.execute(
        """
        INSERT INTO jobs (
            id, session_id, spec, status, kind, target_dir, created_at, updated_at, heartbeat_at
        ) VALUES (
            'job-human-1', 'sess-3', 'Deploy to production', 'running', 'deploy', '/workspace/prod',
            '2026-11-03T10:00:00Z', '2026-11-03T10:00:00Z', '2026-11-03T10:00:00Z'
        )
        """
    )
    conn_ledger.commit()

    cfg = _make_config(ledger_path, stuck_after_seconds=1800)

    with kbc.connect_closing(board_db) as conn:
        # Initial poll: records running as submitted
        poll0 = poll_vibehard(conn, cfg, now=t0)
        assert len(poll0) == 1

        # Job flips to awaiting_human in ledger
        conn_ledger.execute(
            """
            UPDATE jobs SET
                status = 'awaiting_human',
                updated_at = '2026-11-03T10:01:00Z',
                heartbeat_at = '2026-11-03T10:01:00Z'
            WHERE id = 'job-human-1'
            """
        )
        conn_ledger.commit()
        conn_ledger.close()

        # Poll transition: emits state_change
        poll1 = poll_vibehard(conn, cfg, now=t0 + 60)
        assert len(poll1) == 1
        trg_id = poll1[0]

        row = conn.execute("SELECT kind, body FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert row[0] == "state_change"
        assert "running -> awaiting_human" in row[1]

        # Verify cursor has status awaiting_human and stuck_flag False
        cursor_row = conn.execute(
            "SELECT cursor FROM sa_source_cursors WHERE source = 'vibehard' AND scope = 'job-human-1'"
        ).fetchone()
        cursor_data = json.loads(cursor_row[0])
        assert cursor_data["status"] == "awaiting_human"
        assert cursor_data["stuck_flag"] is False

        # Advance time by 10,000 seconds (far older than stuck_after_seconds)
        poll2 = poll_vibehard(conn, cfg, now=t0 + 10000)
        assert poll2 == []

        # Confirm no stuck or failed triggers were ever emitted for this job
        stuck_or_failed = conn.execute(
            "SELECT COUNT(*) FROM sa_triggers WHERE scope = 'job-human-1' AND kind IN ('stuck', 'failed')"
        ).fetchone()[0]
        assert stuck_or_failed == 0


def test_vibehard_gate_admission_observe_mode(tmp_path: Path) -> None:
    """A vibehard trigger is admitted as first-party/observe and follows the would_accept path."""
    from hermes_cli.superagent_gate import admit
    from hermes_cli.orchestrator import assemble_batch, run_eval, receive_trigger

    board_db = tmp_path / "board.db"
    kbc.init_db(board_db)

    # 1. Unit gate admission check
    policy = {
        "accept_mode": "observe",
        "sources": {
            "vibehard": {
                "first_party": True,
            }
        },
        "first_party": ["vibehard"],
    }

    trigger = {
        "source": "vibehard",
        "scope": "job-gate-1",
        "actor_source_id": "vibehard:charlie",
        "actor_is_bot": False,
        "addressed": False,
        "kind": "submitted",
    }

    strength, basis = admit(trigger, policy)
    assert strength == "explicit"
    assert basis["via"] == "first_party"
    assert basis["source"] == "vibehard"
    assert basis["sender"] == "vibehard:charlie"

    # 2. Integration check with receive_trigger + assemble_batch + run_eval in observe mode
    cfg = {
        "superagent": {
            "enabled": True,
            "accept_mode": "observe",
            "sources": {
                "vibehard": {
                    "first_party": True,
                }
            },
            "first_party": ["vibehard"],
        }
    }

    trg_id, _ = receive_trigger(
        board_db,
        source="vibehard",
        scope="job-gate-1",
        thread_key="vibehard:job-gate-1",
        actor_source_id="vibehard:charlie",
        actor_is_bot=False,
        addressed=False,
        kind="submitted",
        body="Title: Test Gate\nStatus: None -> pending",
        idempotency_key="vibehard:gate:1",
        now=1700000000.0,
    )

    batch = assemble_batch(board_db, thread_key="vibehard:job-gate-1", now=1700000010.0, config=cfg)
    assert batch is not None

    decision = run_eval(batch, db_path_or_conn=board_db, config=cfg, now=1700000020.0)
    assert decision["outcome"] == "accept"
    assert decision["strength"] == "explicit"
    assert decision["would_accept"] == 1
    assert decision["reason"]["via"] == "first_party"

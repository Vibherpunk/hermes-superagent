"""Tests for Superagent Phase 2 SQLite database schema and migrations.

Verifies:
(1) Fresh DB creation via repo schema-init path contains all eight sa_* tables
    and tasks.mandate_id + tasks.ticket_kind.
(2) Opening a pre-Phase-2 DB cleanly applies additive migrations (tables and
    columns appear, existing rows remain intact).
(3) Re-running schema initialization is completely idempotent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


EXPECTED_SA_TABLES = {
    "sa_triggers",
    "sa_decisions",
    "sa_mandates",
    "sa_inputs",
    "sa_source_cursors",
    "sa_outbox",
    "sa_meta",
    "sa_subscriptions",
}

PRE_PHASE2_TASKS_DDL = """
CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    worker_started_at    INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    current_run_id       INTEGER,
    workflow_template_id TEXT,
    current_step_key     TEXT,
    skills               TEXT,
    model_override       TEXT,
    provider_override    TEXT,
    reasoning_effort     TEXT,
    max_retries          INTEGER,
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    goal_max_turns       INTEGER,
    session_id           TEXT,
    block_kind           TEXT,
    block_recurrences    INTEGER NOT NULL DEFAULT 0
);
"""


def _get_tables(conn: sqlite3.Connection) -> set[str]:
    """Return set of user table names in the database."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in cur.fetchall()}


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return set of column names for a given table."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in cur.fetchall()}


def _get_schema_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Capture snapshot of all table columns and index names for idempotency check."""
    snapshot: dict[str, list[tuple]] = {}
    tables = sorted(_get_tables(conn))
    for t in tables:
        cols = [
            (r["name"], (r["type"] or "").upper(), r["notnull"], r["pk"])
            for r in conn.execute(f"PRAGMA table_info({t})").fetchall()
        ]
        indexes = [
            (r["name"], r["unique"])
            for r in conn.execute(f"PRAGMA index_list({t})").fetchall()
            if not r["name"].startswith("sqlite_")
        ]
        snapshot[f"table:{t}"] = cols
        snapshot[f"indexes:{t}"] = sorted(indexes)
    return snapshot


def test_fresh_db_has_all_superagent_tables_and_columns(tmp_path: Path) -> None:
    """Requirement 1: A fresh temp DB created through the repo's schema-init
    path has all eight sa_* tables and tasks.mandate_id + tasks.ticket_kind.
    """
    db_path = tmp_path / "fresh_board.db"
    kbc.init_db(db_path)

    with kbc.connect_closing(db_path) as conn:
        tables = _get_tables(conn)
        assert EXPECTED_SA_TABLES.issubset(tables), (
            f"Missing sa_* tables: {EXPECTED_SA_TABLES - tables}"
        )
        assert "tasks" in tables

        task_cols = _get_column_names(conn, "tasks")
        assert "mandate_id" in task_cols, "tasks.mandate_id column missing"
        assert "ticket_kind" in task_cols, "tasks.ticket_kind column missing"

        # Verify sa_triggers table columns and index
        trigger_cols = _get_column_names(conn, "sa_triggers")
        for col in (
            "id", "source", "scope", "thread_key", "actor_source_id", "actor_is_bot",
            "addressed", "kind", "body", "meta_json", "correlation_json",
            "idempotency_key", "eval_state", "eval_requeues", "disposition", "created_at",
        ):
            assert col in trigger_cols, f"sa_triggers.{col} missing"

        # Verify sa_decisions columns
        decision_cols = _get_column_names(conn, "sa_decisions")
        for col in (
            "id", "thread_key", "trigger_ids_json", "outcome", "strength",
            "would_accept", "ignored_from_card", "matched_rules_json", "reason_json",
            "policy_version", "ticket_id", "dismissed", "created_at",
        ):
            assert col in decision_cols, f"sa_decisions.{col} missing"

        # Verify sa_mandates columns
        mandate_cols = _get_column_names(conn, "sa_mandates")
        for col in (
            "id", "ticket_id", "policy_version", "scope", "strength",
            "basis_json", "status", "amended_from", "created_at",
        ):
            assert col in mandate_cols, f"sa_mandates.{col} missing"

        # Verify sa_inputs columns
        input_cols = _get_column_names(conn, "sa_inputs")
        for col in (
            "id", "ticket_id", "trigger_id", "kind", "body",
            "is_person", "context_only", "read_at", "created_at",
        ):
            assert col in input_cols, f"sa_inputs.{col} missing"

        # Verify sa_source_cursors columns
        cursor_cols = _get_column_names(conn, "sa_source_cursors")
        for col in ("source", "scope", "cursor", "updated_at"):
            assert col in cursor_cols, f"sa_source_cursors.{col} missing"

        # Verify sa_outbox columns
        outbox_cols = _get_column_names(conn, "sa_outbox")
        for col in (
            "id", "ticket_id", "source", "address_json", "purpose", "body",
            "status", "attempts", "next_attempt_at", "last_error",
            "dropped_reason", "receipt_json", "created_at", "sent_at",
        ):
            assert col in outbox_cols, f"sa_outbox.{col} missing"

        # Verify sa_meta columns
        meta_cols = _get_column_names(conn, "sa_meta")
        for col in ("key", "value"):
            assert col in meta_cols, f"sa_meta.{col} missing"

        # Verify sa_subscriptions columns
        sub_cols = _get_column_names(conn, "sa_subscriptions")
        for col in ("thread_key", "ticket_id", "context_only", "created_at"):
            assert col in sub_cols, f"sa_subscriptions.{col} missing"

        # Verify expected indexes
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_sa_triggers_thread" in indexes
        assert "idx_sa_outbox_due" in indexes
        assert "idx_sa_subs_ticket" in indexes
        assert "idx_sa_mandates_ticket" in indexes


def test_pre_phase2_db_migrates_cleanly(tmp_path: Path) -> None:
    """Requirement 2: Opening a pre-Phase-2 DB (built with pre-existing schema only)
    migrates cleanly: all eight sa_* tables appear, tasks.mandate_id and tasks.ticket_kind
    appear, and existing rows remain completely intact.
    """
    db_path = tmp_path / "legacy_board.db"

    # Step A: Build a fixture DB with pre-Phase-2 schema only (no sa_* tables, no mandate_id/ticket_kind).
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(PRE_PHASE2_TASKS_DDL)
    raw_conn.executescript(
        """
        CREATE TABLE task_links (
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id)
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            profile TEXT,
            step_key TEXT,
            status TEXT NOT NULL,
            claim_lock TEXT,
            claim_expires INTEGER,
            worker_pid INTEGER,
            worker_started_at INTEGER,
            max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            outcome TEXT,
            summary TEXT,
            metadata TEXT,
            error TEXT
        );
        CREATE TABLE task_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            content_type TEXT,
            size INTEGER NOT NULL DEFAULT 0,
            uploaded_by TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE kanban_notify_subs (
            task_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            user_id TEXT,
            user_id_alt TEXT,
            chat_type TEXT,
            notifier_profile TEXT,
            delivery_mode TEXT NOT NULL DEFAULT 'notify',
            delivery_metadata TEXT,
            created_at INTEGER NOT NULL,
            last_event_id INTEGER NOT NULL DEFAULT 0,
            last_ping_event_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, platform, chat_id, thread_id)
        );
        """
    )

    # Insert existing pre-Phase-2 rows
    raw_conn.execute(
        """
        INSERT INTO tasks (
            id, title, body, assignee, status, priority, created_by, created_at,
            workspace_kind, consecutive_failures, goal_mode
        ) VALUES (
            'task-legacy-001', 'Implement legacy feature', 'Important background info',
            'agent-alpha', 'todo', 2, 'user-lead', 1700000000, 'scratch', 0, 0
        )
        """
    )
    raw_conn.execute(
        """
        INSERT INTO tasks (
            id, title, body, assignee, status, priority, created_by, created_at,
            workspace_kind, consecutive_failures, goal_mode
        ) VALUES (
            'task-legacy-002', 'Completed old feature', 'Already finished task',
            'agent-beta', 'done', 1, 'user-lead', 1700001000, 'scratch', 0, 0
        )
        """
    )
    raw_conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('task-legacy-001', 'alice', 'comment 1', 1700000500)"
    )
    raw_conn.commit()
    raw_conn.close()

    # Verify pre-condition: sa_* tables and new task columns do NOT exist
    probe_conn = sqlite3.connect(str(db_path))
    existing_tables = _get_tables(probe_conn)
    for table_name in EXPECTED_SA_TABLES:
        assert table_name not in existing_tables
    existing_cols = {r[1] for r in probe_conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "mandate_id" not in existing_cols
    assert "ticket_kind" not in existing_cols
    probe_conn.close()

    # Step B: Open the legacy DB through the repo's connection path (which triggers migration)
    with kbc.connect_closing(db_path) as conn:
        migrated_tables = _get_tables(conn)
        assert EXPECTED_SA_TABLES.issubset(migrated_tables)

        migrated_task_cols = _get_column_names(conn, "tasks")
        assert "mandate_id" in migrated_task_cols
        assert "ticket_kind" in migrated_task_cols

        # Step C: Verify existing rows remain intact
        cur = conn.execute(
            "SELECT id, title, body, assignee, status, priority, mandate_id, ticket_kind "
            "FROM tasks ORDER BY id"
        )
        rows = cur.fetchall()
        assert len(rows) == 2

        row1 = rows[0]
        assert row1["id"] == "task-legacy-001"
        assert row1["title"] == "Implement legacy feature"
        assert row1["body"] == "Important background info"
        assert row1["assignee"] == "agent-alpha"
        assert row1["status"] == "todo"
        assert row1["priority"] == 2
        assert row1["mandate_id"] is None
        assert row1["ticket_kind"] is None

        row2 = rows[1]
        assert row2["id"] == "task-legacy-002"
        assert row2["title"] == "Completed old feature"
        assert row2["body"] == "Already finished task"
        assert row2["status"] == "done"
        assert row2["mandate_id"] is None
        assert row2["ticket_kind"] is None

        # Verify related table data is preserved
        comment = conn.execute("SELECT body FROM task_comments WHERE task_id = 'task-legacy-001'").fetchone()
        assert comment is not None
        assert comment["body"] == "comment 1"

        # Step D: Verify new columns are fully operational
        conn.execute(
            "UPDATE tasks SET mandate_id = 999, ticket_kind = 'superagent_worker' WHERE id = 'task-legacy-001'"
        )
        updated = conn.execute(
            "SELECT mandate_id, ticket_kind FROM tasks WHERE id = 'task-legacy-001'"
        ).fetchone()
        assert updated["mandate_id"] == 999
        assert updated["ticket_kind"] == "superagent_worker"


def test_schema_init_is_idempotent(tmp_path: Path) -> None:
    """Requirement 3: Running schema init twice is a no-op (no error, same schema)."""
    db_path = tmp_path / "idempotent_board.db"

    # First init
    kbc.init_db(db_path)

    with kbc.connect_closing(db_path) as conn:
        snapshot_first = _get_schema_snapshot(conn)

        # Insert test records across fresh tables
        conn.execute(
            """
            INSERT INTO sa_triggers (
                id, source, scope, thread_key, kind, body, idempotency_key, created_at
            ) VALUES (
                'trig-1', 'telegram', 'main', 'tg:123:456', 'message', 'hello bot', 'idem-1', 1700000000
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sa_mandates (
                ticket_id, policy_version, scope, strength, created_at
            ) VALUES (
                't_test_task', 'v1.0', 'repo:hermes', 'must_execute', 1700000000
            )
            """
        )
        conn.execute("INSERT INTO sa_meta (key, value) VALUES ('version', '2.0')")
        conn.commit()

    # Invalidate cache so connect/init_db forces another migration/schema pass
    with kbc._INIT_LOCK:
        kbc._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    # Second init on the same database
    kbc.init_db(db_path)

    with kbc.connect_closing(db_path) as conn:
        snapshot_second = _get_schema_snapshot(conn)

        # Verify schema is identical
        assert snapshot_first == snapshot_second

        # Verify pre-existing data survives second init intact
        trig = conn.execute("SELECT id, body, idempotency_key FROM sa_triggers WHERE id = 'trig-1'").fetchone()
        assert trig is not None
        assert trig["id"] == "trig-1"
        assert trig["body"] == "hello bot"
        assert trig["idempotency_key"] == "idem-1"

        mandate = conn.execute("SELECT ticket_id, strength FROM sa_mandates WHERE ticket_id = 't_test_task'").fetchone()
        assert mandate is not None
        assert mandate["ticket_id"] == "t_test_task"
        assert mandate["strength"] == "must_execute"

        meta = conn.execute("SELECT value FROM sa_meta WHERE key = 'version'").fetchone()
        assert meta is not None
        assert meta["value"] == "2.0"


def test_sa_mandates_allows_duplicate_ticket_id_and_index_exists(tmp_path: Path) -> None:
    """Per DECISIONS E-044: sa_mandates.ticket_id is not unique and has idx_sa_mandates_ticket."""
    db_path = tmp_path / "mandates_e044.db"
    kbc.init_db(db_path)

    with kbc.connect_closing(db_path) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_sa_mandates_ticket" in indexes

        # Verify TWO mandate rows with the same ticket_id insert cleanly
        conn.execute(
            """
            INSERT INTO sa_mandates (
                ticket_id, policy_version, scope, strength, created_at
            ) VALUES (
                't_test_ticket_same', 'v1.0', 'repo:hermes', 'must_execute', 1700000000
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sa_mandates (
                ticket_id, policy_version, scope, strength, created_at
            ) VALUES (
                't_test_ticket_same', 'v1.1', 'repo:hermes', 'should_execute', 1700000100
            )
            """
        )
        conn.commit()

        rows = conn.execute(
            "SELECT id, ticket_id, policy_version FROM sa_mandates WHERE ticket_id = 't_test_ticket_same' ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["ticket_id"] == "t_test_ticket_same"
        assert rows[1]["ticket_id"] == "t_test_ticket_same"


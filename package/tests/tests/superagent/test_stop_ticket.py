"""Unit and integration tests for Super-agent Ticket Stop command (Card INT-3).

Verifies:
1. Stop an active ticket -> mandate withdrawn, ticket ended, fan-out afterwards delivers nothing to it.
2. Stop releases unread person Inputs to Eval per Card 3.7.
3. Double-stop is an idempotent no-op result (no duplicate side effects).
4. Unknown ticket returns an error string.
5. Withdrawn ticket is NOT resumed by Card 3.6's path when a person message arrives.
6. Stop an already-ended ticket transitions active mandate to withdrawn without duplicate end-path side effects.
7. Router tool orchestrator_ticket_stop mirrors orchestrator_ticket_retry shape and dispatches correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import (
    receive_trigger,
    release_on_end,
    stop_ticket,
)
from tools.orchestrator_tools import _handle_ticket_stop
from tools.registry import registry


@pytest.fixture
def kanban_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up an isolated HERMES_HOME and HERMES_KANBAN_DB for testing."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    cfg_yaml = """
superagent:
  enabled: true
  mode: auto
  accept_mode: auto
  router_profile: default
  owner_identities:
    - alice
  sources:
    telegram:
      allowlist:
        - alice
"""
    (hermes_home / "config.yaml").write_text(cfg_yaml)
    return db_path


def _insert_mandate(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    policy_ver: str = "v1",
    scope: str = "main",
    strength: str = "explicit",
    basis: dict[str, Any] | None = None,
    status: str = "active",
    now: int = 1000,
) -> int:
    """Helper to insert an sa_mandates row directly."""
    basis_json = json.dumps(basis if basis is not None else {})
    cur = conn.execute(
        """
        INSERT INTO sa_mandates (
            ticket_id, policy_version, scope, strength,
            basis_json, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            policy_ver,
            scope,
            strength,
            basis_json,
            status,
            now,
        ),
    )
    return int(cur.lastrowid)


def _add_subscription(
    conn: sqlite3.Connection,
    *,
    thread_key: str,
    ticket_id: str,
    context_only: int = 0,
    created_at: int = 1000,
) -> None:
    """Helper to insert an sa_subscriptions row."""
    with kbc.write_txn(conn):
        conn.execute(
            """
            INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (thread_key, ticket_id, int(context_only), int(created_at)),
        )


def test_stop_active_ticket(kanban_env: Path) -> None:
    """Stop an active ticket -> mandate withdrawn, ticket ended, fan-out afterwards delivers nothing."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = kb.create_task(conn, title="Active ticket to stop", assignee="coder", initial_status="running")
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        mid = _insert_mandate(conn, ticket_id=tid, status="active")
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, tid))
        _add_subscription(conn, thread_key="tg:chat:stop1", ticket_id=tid, context_only=0)

        # Initial state: task running, mandate active
        assert kb.get_task(conn, tid).status == "running"
        mrow = conn.execute("SELECT status FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mrow["status"] == "active"

        # Execute stop_ticket
        res = stop_ticket(conn, tid, reason="user requested stop")
        assert isinstance(res, dict)
        assert res.get("ok") is True
        assert res.get("status") == "stopped"
        assert res.get("already_stopped") is False
        assert res.get("mandate_id") == mid

        # 1. Mandate must be withdrawn
        mrow_after = conn.execute("SELECT status, basis_json FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mrow_after["status"] == "withdrawn"
        basis = json.loads(mrow_after["basis_json"])
        assert basis.get("stopped_reason") == "user requested stop"

        # 2. Ticket must be ended (status='done')
        task_after = kb.get_task(conn, tid)
        assert task_after.status == "done"
        assert task_after.result == "stopped"

        # 3. Events must record 'stopped' and 'completed'
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id ASC",
            (tid,),
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "stopped" in kinds
        assert "completed" in kinds

        # 4. Fan-out afterwards delivers nothing to it
        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:stop1",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Followup after stopped",
            idempotency_key="msg_stopped_1",
            now=2000,
        )
        # Because the only subscriber ticket is withdrawn, trigger is unowned -> queues for Eval
        assert disp == "queued"
        inputs = conn.execute("SELECT * FROM sa_inputs WHERE ticket_id = ?", (tid,)).fetchall()
        assert len(inputs) == 0


def test_stop_releases_unread_person_inputs_to_eval(kanban_env: Path) -> None:
    """Stop releases unread person inputs to Eval per Card 3.7."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = kb.create_task(conn, title="Ticket with unread input", assignee="coder", initial_status="running")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        mid = _insert_mandate(conn, ticket_id=tid, status="active")
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, tid))
        _add_subscription(conn, thread_key="tg:chat:input_test", ticket_id=tid, context_only=0)

        # Inbound person message arrives -> fanned out
        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:input_test",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Important unread instruction",
            idempotency_key="msg_unread_01",
            now=1000,
        )
        assert disp == "fanned_out"

        trg_before = conn.execute("SELECT eval_state, disposition FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_before["eval_state"] == "done"
        assert trg_before["disposition"] == "fanned_out"

        # Stop ticket
        res = stop_ticket(conn, tid, reason="stopping before read")
        assert isinstance(res, dict)
        assert res.get("ok") is True
        assert trg_id in res.get("released_inputs", [])

        # Trigger must now be re-queued for Eval
        trg_after = conn.execute("SELECT eval_state, disposition, meta_json FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_after["eval_state"] == "queued"
        assert trg_after["disposition"] is None
        meta = json.loads(trg_after["meta_json"])
        assert any(r.get("ticket_id") == tid and r.get("reason") == "release_on_end" for r in meta.get("releases", []))


def test_double_stop_is_noop(kanban_env: Path) -> None:
    """Double-stop is idempotent and returns a clear result with no duplicate side effects."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = kb.create_task(conn, title="Double stop ticket", assignee="coder", initial_status="running")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        mid = _insert_mandate(conn, ticket_id=tid, status="active")
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, tid))

        # First stop
        res1 = stop_ticket(conn, tid)
        assert isinstance(res1, dict)
        assert res1.get("ok") is True
        assert res1.get("already_stopped") is False
        assert res1.get("status") == "stopped"

        events_count_1 = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]

        # Second stop (idempotent no-op)
        res2 = stop_ticket(conn, tid)
        assert isinstance(res2, dict)
        assert res2.get("ok") is True
        assert res2.get("already_stopped") is True
        assert res2.get("status") == "already_stopped"

        events_count_2 = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
        assert events_count_1 == events_count_2  # No duplicate events appended


def test_unknown_ticket_errors(kanban_env: Path) -> None:
    """Stopping a nonexistent ticket returns an error string."""
    with kbc.connect_closing(kanban_env) as conn:
        res = stop_ticket(conn, "t_nonexistent_999")
        assert isinstance(res, str)
        assert "not found" in res.lower()

        # Also via default db path
        res_default = stop_ticket("t_nonexistent_999")
        assert isinstance(res_default, str)
        assert "not found" in res_default.lower()


def test_withdrawn_ticket_not_resumed_by_card_3_6(kanban_env: Path) -> None:
    """A ticket stopped via stop_ticket is NOT resumed by Card 3.6's path."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = kb.create_task(conn, title="Stopped ticket resume test", assignee="alice", initial_status="running")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        mid = _insert_mandate(conn, ticket_id=tid, status="active", strength="explicit")
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, tid))
        _add_subscription(conn, thread_key="tg:chat:resume_test", ticket_id=tid, context_only=0)

        # Stop ticket
        stop_ticket(conn, tid)
        assert kb.get_task(conn, tid).status == "done"
        mrow = conn.execute("SELECT status FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mrow["status"] == "withdrawn"

        # Person trigger arrives on the subscribed thread
        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:resume_test",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Can you resume this?",
            idempotency_key="msg_resume_attempt",
            now=3000,
        )

        # Must not resume: trigger queues for Eval, task remains done, mandate remains withdrawn
        assert disp == "queued"
        task_after = kb.get_task(conn, tid)
        assert task_after.status == "done"
        assert task_after.completed_at is not None

        # No new inputs delivered
        inputs_count = conn.execute("SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ?", (tid,)).fetchone()[0]
        assert inputs_count == 0


def test_stop_already_ended_ticket(kanban_env: Path) -> None:
    """Stopping an already-ended ticket withdraws its active mandate without duplicate end effects."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = kb.create_task(conn, title="Already ended ticket", assignee="coder", initial_status="running")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        mid = _insert_mandate(conn, ticket_id=tid, status="active")
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, tid))

        # Stop ticket
        res = stop_ticket(conn, tid, reason="person stopped ended ticket")
        assert isinstance(res, dict)
        assert res.get("ok") is True
        assert res.get("status") == "stopped"
        assert res.get("already_ended") is True
        assert res.get("already_stopped") is False

        # Mandate is withdrawn
        mrow = conn.execute("SELECT status FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mrow["status"] == "withdrawn"

        # Calling again reports already_stopped=True
        res2 = stop_ticket(conn, tid)
        assert res2.get("already_stopped") is True


def test_orchestrator_ticket_stop_tool(kanban_env: Path) -> None:
    """Router tool orchestrator_ticket_stop stops ticket and handles errors mirroring ticket_retry."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = kb.create_task(conn, title="Tool stop ticket", assignee="coder", initial_status="running")
        mid = _insert_mandate(conn, ticket_id=tid, status="active")
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, tid))

    # 1. Happy path stop via tool handler
    raw = _handle_ticket_stop({"task_id": tid, "reason": "router stop request"})
    res = json.loads(raw)
    assert res.get("ok") is True
    assert res.get("stopped") is True
    assert res.get("task_id") == tid
    assert res.get("status") == "stopped"

    with kbc.connect_closing(kanban_env) as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        mrow = conn.execute("SELECT status FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mrow["status"] == "withdrawn"

    # 2. Refusal on unknown ticket
    err_raw = _handle_ticket_stop({"task_id": "t_unknown_999"})
    err_res = json.loads(err_raw)
    assert err_res.get("ok") is not True
    assert "not found" in err_res.get("error", "").lower()

    # 3. Via registry dispatch
    with kbc.connect_closing(kanban_env) as conn:
        tid2 = kb.create_task(conn, title="Dispatch stop ticket", assignee="coder", initial_status="running")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid2,))
        mid2 = _insert_mandate(conn, ticket_id=tid2, status="active")
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid2, tid2))

    reg_raw = registry.dispatch("orchestrator_ticket_stop", {"task_id": tid2})
    reg_res = json.loads(reg_raw)
    assert reg_res.get("ok") is True
    assert reg_res.get("stopped") is True

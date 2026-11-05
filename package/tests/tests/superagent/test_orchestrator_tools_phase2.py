"""Unit tests for Phase 2 Super-agent orchestrator tools (Card 2.10).

Covers all 8 live implementations replacing Phase-1 stubs:
- orchestrator_eval (happy path + disabled gate refusal)
- orchestrator_eval_read (happy path + missing thread_key refusal)
- orchestrator_eval_decide (happy path accept + happy path dismiss + missing decision refusal)
- orchestrator_trigger_requeue (happy path + requeue while evaluating refusal)
- orchestrator_thread_unsubscribe (happy path + missing thread_key refusal)
- orchestrator_ticket_retry (happy path + retry on terminal ticket refusal)
- orchestrator_ticket_request_status (happy path + missing ticket refusal)
- orchestrator_ticket_note (happy path + missing ticket refusal)
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import receive_trigger
from tools.orchestrator_tools import (
    _handle_eval,
    _handle_eval_decide,
    _handle_eval_read,
    _handle_thread_unsubscribe,
    _handle_ticket_note,
    _handle_ticket_request_status,
    _handle_ticket_retry,
    _handle_ticket_stop,
    _handle_trigger_requeue,
)


def _setup_test_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    mode: str = "auto",
) -> Path:
    """Set up an isolated HERMES_HOME and HERMES_KANBAN_DB for testing."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    cfg_yaml = f"""
superagent:
  enabled: {"true" if enabled else "false"}
  mode: {mode}
  accept_mode: {mode}
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


# ── orchestrator_eval ────────────────────────────────────────────────────────


def test_orchestrator_eval_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run Eval for queued triggers on a thread, returning outcome counts."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    trg_id, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:1",
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Deploy the release",
        idempotency_key="idemp_eval_1",
        now=1000,
    )

    out = _handle_eval({"thread_key": "tg:chat:1"})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res["batches"] == 1
    assert res["triggers"] == 1
    assert res["summary"].get("accept") == 1

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT eval_state, disposition FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert row is not None
        assert row["eval_state"] == "done"
        assert row["disposition"] == "accept"


def test_orchestrator_eval_disabled_gate_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All tools refuse when superagent.enabled is false."""
    _setup_test_context(tmp_path, monkeypatch, enabled=False)

    out = _handle_eval({"thread_key": "tg:chat:1"})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "superagent is disabled" in res.get("error", "")


# ── orchestrator_eval_read ───────────────────────────────────────────────────


def test_orchestrator_eval_read_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read latest Decision records for a thread from sa_decisions, newest first."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, trigger_ids_json, outcome, strength, would_accept,
                    ignored_from_card, matched_rules_json, reason_json, policy_version,
                    ticket_id, dismissed, created_at
                ) VALUES (?, '[]', 'ignore', 'not_admitted', 0, 0, '[]', '{}', 'v1', NULL, 0, 1000)
                """,
                ("tg:chat:read",),
            )
            conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, trigger_ids_json, outcome, strength, would_accept,
                    ignored_from_card, matched_rules_json, reason_json, policy_version,
                    ticket_id, dismissed, created_at
                ) VALUES (?, '[]', 'accept', 'explicit', 0, 0, '[]', '{"via":"allowlist"}', 'v1', NULL, 0, 1010)
                """,
                ("tg:chat:read",),
            )

    out = _handle_eval_read({"thread_key": "tg:chat:read", "limit": 10})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res["count"] == 2
    decisions = res["decisions"]
    assert decisions[0]["created_at"] == 1010
    assert decisions[0]["outcome"] == "accept"
    assert decisions[1]["created_at"] == 1000
    assert decisions[1]["outcome"] == "ignore"


def test_orchestrator_eval_read_refusal_missing_thread_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse when thread_key parameter is missing."""
    _setup_test_context(tmp_path, monkeypatch, enabled=True)

    out = _handle_eval_read({})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "thread_key is required" in res.get("error", "")


# ── orchestrator_eval_decide ─────────────────────────────────────────────────


def test_orchestrator_eval_decide_happy_path_dismiss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mark pending Decision dismissed when decision='dismiss'."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, trigger_ids_json, outcome, strength, would_accept,
                    ignored_from_card, matched_rules_json, reason_json, policy_version,
                    ticket_id, dismissed, created_at
                ) VALUES (?, '[]', 'accept', 'explicit', 0, 0, '[]', '{}', 'v1', NULL, 0, 1000)
                """,
                ("tg:chat:decide",),
            )
            dec_id = cur.lastrowid

    out = _handle_eval_decide({"decision_id": dec_id, "decision": "dismiss"})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res.get("dismissed") is True

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT dismissed FROM sa_decisions WHERE id = ?", (dec_id,)).fetchone()
        assert row is not None
        assert row["dismissed"] == 1


def test_orchestrator_eval_decide_happy_path_accept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Call orchestrator.accept and instantiate ticket/mandate when decision='accept'."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    _trg_id, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:decide_accept",
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Implement feature X",
        idempotency_key="idemp_decide_acc",
        now=1000,
    )

    # Run eval to create accept Decision
    _handle_eval({"thread_key": "tg:chat:decide_accept"})

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute(
            "SELECT id, outcome FROM sa_decisions WHERE thread_key = ? ORDER BY id DESC LIMIT 1",
            ("tg:chat:decide_accept",),
        ).fetchone()
        assert row is not None
        assert row["outcome"] == "accept"
        dec_id = row["id"]

    out = _handle_eval_decide({"decision_id": dec_id, "decision": "accept"})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res.get("accepted") is True
    ticket_id = res.get("ticket_id")
    assert ticket_id is not None

    with kbc.connect_closing(db_path) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
        assert task is not None
        assert task["mandate_id"] is not None

        mandate = conn.execute("SELECT * FROM sa_mandates WHERE id = ?", (task["mandate_id"],)).fetchone()
        assert mandate is not None
        assert mandate["ticket_id"] == ticket_id


def test_orchestrator_eval_decide_refusal_missing_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse when the specified decision_id does not exist."""
    _setup_test_context(tmp_path, monkeypatch, enabled=True)

    out = _handle_eval_decide({"decision_id": 999999, "decision": "accept"})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "decision 999999 not found" in res.get("error", "")


def test_orchestrator_eval_decide_refusal_dismissed_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse eval_decide when the decision is already dismissed."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, trigger_ids_json, outcome, strength, would_accept,
                    ignored_from_card, matched_rules_json, reason_json, policy_version,
                    ticket_id, dismissed, created_at
                ) VALUES (?, '[]', 'accept', 'explicit', 0, 0, '[]', '{}', 'v1', NULL, 1, 1000)
                """,
                ("tg:chat:already_dismissed",),
            )
            dec_id = cur.lastrowid

    # Refuse accept on dismissed decision
    out = _handle_eval_decide({"decision_id": dec_id, "decision": "accept"})
    res = json.loads(out)
    assert res.get("ok") is not True
    assert "already dismissed" in res.get("error", "")

    # Refuse dismiss on already dismissed decision
    out2 = _handle_eval_decide({"decision_id": dec_id, "decision": "dismiss"})
    res2 = json.loads(out2)
    assert res2.get("ok") is not True
    assert "already dismissed" in res2.get("error", "")


def test_orchestrator_eval_decide_refusal_already_ticketed_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse eval_decide when the decision already has a ticket_id."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, trigger_ids_json, outcome, strength, would_accept,
                    ignored_from_card, matched_rules_json, reason_json, policy_version,
                    ticket_id, dismissed, created_at
                ) VALUES (?, '[]', 'accept', 'explicit', 0, 0, '[]', '{}', 'v1', 'task-existing-1', 0, 1000)
                """,
                ("tg:chat:already_ticketed",),
            )
            dec_id = cur.lastrowid

    # Refuse accept on already-ticketed decision
    out = _handle_eval_decide({"decision_id": dec_id, "decision": "accept"})
    res = json.loads(out)
    assert res.get("ok") is not True
    assert "already has a ticket" in res.get("error", "")

    # Refuse dismiss on already-ticketed decision
    out2 = _handle_eval_decide({"decision_id": dec_id, "decision": "dismiss"})
    res2 = json.loads(out2)
    assert res2.get("ok") is not True
    assert "already has a ticket" in res2.get("error", "")


def test_orchestrator_eval_decide_observe_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In observe mode, accept() returns None; handler reports accepted: false, would_accept: true, ticket_id: null."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True, mode="observe")

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, trigger_ids_json, outcome, strength, would_accept,
                    ignored_from_card, matched_rules_json, reason_json, policy_version,
                    ticket_id, dismissed, created_at
                ) VALUES (?, '[]', 'accept', 'explicit', 0, 0, '[]', '{}', 'v1', NULL, 0, 1000)
                """,
                ("tg:chat:observe_decide",),
            )
            dec_id = cur.lastrowid

    out = _handle_eval_decide({"decision_id": dec_id, "decision": "accept"})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res.get("decision") == "accept"
    assert res.get("accepted") is False
    assert res.get("would_accept") is True
    assert res.get("ticket_id") is None

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT would_accept, ticket_id FROM sa_decisions WHERE id = ?", (dec_id,)).fetchone()
        assert row is not None
        assert row["would_accept"] == 1
        assert row["ticket_id"] is None
        tasks_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert tasks_count == 0


# ── orchestrator_trigger_requeue ─────────────────────────────────────────────


def test_orchestrator_trigger_requeue_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset a completed trigger's eval_state back to 'queued'."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    trg_id, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:requeue",
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Trigger to requeue",
        idempotency_key="idemp_req_1",
        now=1000,
    )

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            conn.execute("UPDATE sa_triggers SET eval_state = 'done' WHERE id = ?", (trg_id,))

    out = _handle_trigger_requeue({"trigger_id": trg_id})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res["eval_state"] == "queued"
    assert res["requeued"] is True

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT eval_state, eval_requeues FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert row is not None
        assert row["eval_state"] == "queued"
        assert row["eval_requeues"] == 1


def test_orchestrator_trigger_requeue_refusal_while_evaluating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse requeue if trigger eval_state is 'evaluating'."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    trg_id, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:requeue_eval",
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Trigger evaluating",
        idempotency_key="idemp_req_eval",
        now=1000,
    )

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            conn.execute("UPDATE sa_triggers SET eval_state = 'evaluating' WHERE id = ?", (trg_id,))

    out = _handle_trigger_requeue({"trigger_id": trg_id})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "while it is evaluating" in res.get("error", "")


# ── orchestrator_thread_unsubscribe ──────────────────────────────────────────


def test_orchestrator_thread_unsubscribe_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete all subscription rows for thread_key directly from sa_subscriptions."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            conn.execute(
                """
                INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
                VALUES (?, 't_sub_1', 0, 1000)
                """,
                ("tg:chat:unsub",),
            )
            conn.execute(
                """
                INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
                VALUES (?, 't_sub_2', 1, 1000)
                """,
                ("tg:chat:unsub",),
            )

    out = _handle_thread_unsubscribe({"thread_key": "tg:chat:unsub"})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res["dropped"] == 2

    with kbc.connect_closing(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM sa_subscriptions WHERE thread_key = ?",
            ("tg:chat:unsub",),
        ).fetchone()[0]
        assert count == 0


def test_orchestrator_thread_unsubscribe_refusal_missing_thread_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse unsubscribe when thread_key is missing."""
    _setup_test_context(tmp_path, monkeypatch, enabled=True)

    out = _handle_thread_unsubscribe({})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "thread_key is required" in res.get("error", "")


def test_orchestrator_thread_unsubscribe_schema_no_task_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify ORCHESTRATOR_THREAD_UNSUBSCRIBE_SCHEMA has no task_id parameter and behavior is unchanged."""
    from tools.orchestrator_tools_schemas import ORCHESTRATOR_THREAD_UNSUBSCRIBE_SCHEMA

    props = ORCHESTRATOR_THREAD_UNSUBSCRIBE_SCHEMA["parameters"]["properties"]
    assert "task_id" not in props
    assert "thread_key" in props
    assert "thread_id" in props
    assert "board" in props

    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            conn.execute(
                """
                INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
                VALUES (?, 't_sub_1', 0, 1000)
                """,
                ("tg:chat:unsub_schema",),
            )
            conn.execute(
                """
                INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
                VALUES (?, 't_sub_2', 1, 1000)
                """,
                ("tg:chat:unsub_schema",),
            )

    # Behavior unchanged: drops all subscriptions for the thread
    out = _handle_thread_unsubscribe({"thread_key": "tg:chat:unsub_schema"})
    res = json.loads(out)
    assert res.get("ok") is True
    assert res["dropped"] == 2

    # Passing task_id is rejected as unknown parameter
    out_err = _handle_thread_unsubscribe({"thread_key": "tg:chat:unsub_schema", "task_id": "t_sub_1"})
    res_err = json.loads(out_err)
    assert res_err.get("ok") is not True
    assert "unknown parameter(s): task_id" in res_err.get("error", "")


# ── orchestrator_ticket_retry ────────────────────────────────────────────────


def test_orchestrator_ticket_retry_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End the ticket's current run via existing dispatcher retry path and mark ready."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Active task to retry", assignee="coder", initial_status="running")
        with kbc.write_txn(conn):
            run_cur = conn.execute(
                """
                INSERT INTO task_runs (task_id, profile, status, claim_lock, started_at)
                VALUES (?, 'coder', 'running', 'lock-1', ?)
                """,
                (tid, int(time.time())),
            )
            run_id = run_cur.lastrowid
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'lock-1', current_run_id = ? WHERE id = ?",
                (run_id, tid),
            )

    out = _handle_ticket_retry({"task_id": tid, "reason": "worker crashed"})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res["status"] == "ready"
    assert res["retried"] is True

    with kbc.connect_closing(db_path) as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"
        assert task.claim_lock is None

        # Verify the previous run ended
        run_row = conn.execute("SELECT outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert run_row is not None
        assert run_row["outcome"] is not None


def test_orchestrator_ticket_retry_refusal_terminal_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse retry on a terminal ticket (status='done' or 'archived')."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Done task", assignee="coder", initial_status="running")
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))

    out = _handle_ticket_retry({"task_id": tid})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "cannot retry terminal ticket" in res.get("error", "")


# ── orchestrator_ticket_stop ────────────────────────────────────────────────


def test_orchestrator_ticket_stop_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a ticket with person-stopped semantics via orchestrator_ticket_stop."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Active task to stop", assignee="coder", initial_status="running")
        with kbc.write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_mandates (ticket_id, policy_version, scope, strength, basis_json, status, created_at)
                VALUES (?, 'v1', 'main', 'explicit', '{}', 'active', ?)
                """,
                (tid, int(time.time())),
            )
            mid = cur.lastrowid
            conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, tid))

    out = _handle_ticket_stop({"task_id": tid, "reason": "router stop"})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res.get("stopped") is True
    assert res.get("task_id") == tid
    assert res.get("status") == "stopped"

    with kbc.connect_closing(db_path) as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        mrow = conn.execute("SELECT status FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mrow is not None
        assert mrow["status"] == "withdrawn"


def test_orchestrator_ticket_stop_refusal_missing_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse stop when ticket does not exist."""
    _setup_test_context(tmp_path, monkeypatch, enabled=True)

    out = _handle_ticket_stop({"task_id": "t_missing_ticket_999"})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "not found" in res.get("error", "").lower()


# ── orchestrator_ticket_request_status ───────────────────────────────────────


def test_orchestrator_ticket_request_status_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return ticket status summary: task status, mandate status, unread inputs, latest decision."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Task with status", assignee="coder", initial_status="running")
        with kbc.write_txn(conn):
            conn.execute(
                """
                INSERT INTO sa_mandates (
                    ticket_id, policy_version, scope, strength, basis_json, status, created_at
                ) VALUES (?, 'v1', 'main', 'explicit', '{}', 'active', 1000)
                """,
                (tid,),
            )
            conn.execute(
                """
                INSERT INTO sa_inputs (
                    ticket_id, trigger_id, kind, body, read_at, created_at
                ) VALUES (?, NULL, 'note', 'Unread note', NULL, 1001)
                """,
                (tid,),
            )
            conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, outcome, strength, policy_version, ticket_id, created_at
                ) VALUES (?, 'accept', 'explicit', 'v1', ?, 1002)
                """,
                ("tg:chat:status", tid),
            )

    out = _handle_ticket_request_status({"task_id": tid})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res["task_id"] == tid
    assert res["task_status"] == "ready"
    assert res["mandate_strength"] == "explicit"
    assert res["mandate_status"] == "active"
    assert res["unread_inputs_count"] == 1
    assert res["latest_decision"] is not None
    assert res["latest_decision"]["outcome"] == "accept"


def test_orchestrator_ticket_request_status_refusal_missing_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse request_status for a nonexistent ticket."""
    _setup_test_context(tmp_path, monkeypatch, enabled=True)

    out = _handle_ticket_request_status({"task_id": "nonexistent_tid"})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "ticket nonexistent_tid not found" in res.get("error", "")


# ── orchestrator_ticket_note ─────────────────────────────────────────────────


def test_orchestrator_ticket_note_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write an sa_inputs row kind 'note' for the ticket."""
    db_path = _setup_test_context(tmp_path, monkeypatch, enabled=True)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Ticket for note", assignee="coder", initial_status="running")

    out = _handle_ticket_note({"task_id": tid, "note": "Please check test coverage."})
    res = json.loads(out)

    assert res.get("ok") is True
    assert res["kind"] == "note"
    assert res["note"] == "Please check test coverage."

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT * FROM sa_inputs WHERE ticket_id = ?", (tid,)).fetchone()
        assert row is not None
        assert row["kind"] == "note"
        assert row["body"] == "Please check test coverage."
        assert row["read_at"] is None


def test_orchestrator_ticket_note_refusal_missing_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse writing a note on a nonexistent ticket."""
    _setup_test_context(tmp_path, monkeypatch, enabled=True)

    out = _handle_ticket_note({"task_id": "nonexistent_tid", "note": "Hello"})
    res = json.loads(out)

    assert res.get("ok") is not True
    assert "ticket nonexistent_tid not found" in res.get("error", "")

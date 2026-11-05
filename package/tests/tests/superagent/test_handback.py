"""Unit tests for Phase 3.5 Handback (E-027).

Verifies:
- kanban_handback(trigger_id, reason) tool and schema.
- Orchestrator records the handback on the Trigger's disposition.
- When no other Ticket owns the Trigger, queues it for fresh Eval with the reason attached to the batch context.
- Calling assemble_batch yields exactly one new Eval batch containing the reason.
- Second handback of the same Trigger by the same Ticket returns an error string and never raises.
- Ticket keeps its subscription after handback (assert row still present in sa_subscriptions).
- Multi-owner coordination: trigger remains unqueued until all owning tickets hand it back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import assemble_batch, receive_trigger, trigger_handback
from tools.kanban_tools import _handle_handback, kanban_handback


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


def _add_subscription(
    db_path: Path,
    *,
    thread_key: str,
    ticket_id: str,
    context_only: int = 0,
    created_at: int = 1000,
) -> None:
    """Insert an sa_subscriptions row directly for testing."""
    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            conn.execute(
                """
                INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (thread_key, ticket_id, int(context_only), int(created_at)),
            )


def test_handback_queues_single_eval_batch_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handback by sole owner records disposition and produces exactly one new Eval batch containing the reason."""
    db_path = _setup_test_context(tmp_path, monkeypatch)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Worker Task 1", assignee="alice", initial_status="running")

    thread_key = "tg:chat:100"
    _add_subscription(db_path, thread_key=thread_key, ticket_id=tid, context_only=0)

    # 1. Trigger arrives and fans out to the owning ticket
    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key=thread_key,
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Please refactor the payments API",
        idempotency_key="idemp_trg_1",
        now=1000,
    )
    assert disp == "fanned_out"

    # Prior to handback, trigger is done and nothing is queued for Eval
    assert assemble_batch(db_path, thread_key=thread_key, now=1001) is None

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT eval_state, disposition FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert row["eval_state"] == "done"
        assert row["disposition"] == "fanned_out"

    # 2. Worker hands back the trigger via kanban_handback
    reason = "Out of scope: this belongs to payments microservice team"
    res_str = kanban_handback(trigger_id=trg_id, reason=reason, task_id=tid)
    res = json.loads(res_str)
    assert res.get("ok") is True
    assert res.get("trigger_id") == trg_id
    assert res.get("ticket_id") == tid

    # 3. Check trigger state in DB: disposition is recorded, eval_state is queued
    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT eval_state, disposition, meta_json FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert row["disposition"] == "handed_back"
        assert row["eval_state"] == "queued"
        meta = json.loads(row["meta_json"])
        assert "handbacks" in meta
        assert meta["handbacks"][0]["ticket_id"] == tid
        assert meta["handbacks"][0]["reason"] == reason

    # 4. Assemble batch: exactly ONE new Eval batch containing the reason
    batch = assemble_batch(db_path, thread_key=thread_key, now=1002)
    assert batch is not None
    assert batch["trigger_ids"] == [trg_id]
    assert len(batch["triggers"]) == 1
    assert batch["triggers"][0]["eval_state"] == "pending"

    # Verify reason is attached to batch context
    assert "context" in batch
    assert reason in batch["context"]["handback_reasons"]
    assert batch["context"]["reason"] == reason
    assert reason in batch["context"]
    assert reason in batch["handback_reasons"]
    assert reason in batch["triggers"][0]["handback_reasons"]

    # Calling assemble_batch again returns None (no second batch)
    assert assemble_batch(db_path, thread_key=thread_key, now=1003) is None


def test_double_handback_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second handback of the same Trigger by the same Ticket returns an error string and never raises."""
    db_path = _setup_test_context(tmp_path, monkeypatch)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Worker Task Refuse", assignee="alice", initial_status="running")

    thread_key = "tg:chat:refuse"
    _add_subscription(db_path, thread_key=thread_key, ticket_id=tid, context_only=0)

    trg_id, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key=thread_key,
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Do task X",
        idempotency_key="idemp_trg_refuse",
        now=1000,
    )

    # First handback succeeds
    res1_str = kanban_handback(trigger_id=trg_id, reason="Not my job", task_id=tid)
    res1 = json.loads(res1_str)
    assert res1.get("ok") is True

    # Second handback of same trigger by same ticket -> returns error string, never raises
    res2_str = kanban_handback(trigger_id=trg_id, reason="Still not my job", task_id=tid)
    assert isinstance(res2_str, str)
    res2 = json.loads(res2_str)
    assert res2.get("ok") is not True
    assert "error" in res2
    assert "already handed back" in res2["error"]

    # Also verify direct trigger_handback API call returns error dict/string
    direct_res = trigger_handback(db_path, ticket_id=tid, trigger_id=trg_id, reason="Attempt 3")
    assert isinstance(direct_res, dict)
    assert direct_res.get("ok") is False
    assert "already handed back" in direct_res.get("error", "")


def test_ticket_keeps_subscription_after_handback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket keeps its subscription after handback (assert row still present)."""
    db_path = _setup_test_context(tmp_path, monkeypatch)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Subscribed Task", assignee="alice", initial_status="running")

    thread_key = "tg:chat:sub"
    _add_subscription(db_path, thread_key=thread_key, ticket_id=tid, context_only=0)

    trg_id, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key=thread_key,
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Work on Y",
        idempotency_key="idemp_trg_sub",
        now=1000,
    )

    kanban_handback(trigger_id=trg_id, reason="Handback reason", task_id=tid)

    # Assert subscription row is still present in sa_subscriptions
    with kbc.connect_closing(db_path) as conn:
        sub = conn.execute(
            "SELECT * FROM sa_subscriptions WHERE ticket_id = ? AND thread_key = ?",
            (tid, thread_key),
        ).fetchone()
        assert sub is not None, "Subscription row must remain present after handback"
        assert sub["ticket_id"] == tid
        assert sub["thread_key"] == thread_key
        assert sub["context_only"] == 0


def test_multiple_owners_handback_coordination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When multiple Tickets own a Trigger, fresh Eval queues only when all owners hand it back."""
    db_path = _setup_test_context(tmp_path, monkeypatch)

    with kbc.connect_closing(db_path) as conn:
        t1 = kb.create_task(conn, title="Backend Task", assignee="alice", initial_status="running")
        t2 = kb.create_task(conn, title="Frontend Task", assignee="bob", initial_status="running")

    thread_key = "tg:chat:multi_owners"
    _add_subscription(db_path, thread_key=thread_key, ticket_id=t1, context_only=0)
    _add_subscription(db_path, thread_key=thread_key, ticket_id=t2, context_only=0)

    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key=thread_key,
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Ambiguous cross-cutting question",
        idempotency_key="idemp_multi_1",
        now=1000,
    )
    assert disp == "fanned_out"

    # Ticket 1 hands it back
    res1_str = kanban_handback(trigger_id=trg_id, reason="Not backend work", task_id=t1)
    res1 = json.loads(res1_str)
    assert res1.get("ok") is True
    assert res1.get("queued_for_eval") is False

    # Since Ticket 2 still owns it, the trigger is NOT queued for eval yet
    assert assemble_batch(db_path, thread_key=thread_key, now=1001) is None

    # Ticket 2 also hands it back
    res2_str = kanban_handback(trigger_id=trg_id, reason="Not frontend work either", task_id=t2)
    res2 = json.loads(res2_str)
    assert res2.get("ok") is True
    assert res2.get("queued_for_eval") is True

    # Now no other ticket owns it -> queued for fresh eval with both reasons
    batch = assemble_batch(db_path, thread_key=thread_key, now=1002)
    assert batch is not None
    assert batch["trigger_ids"] == [trg_id]
    reasons = batch["context"]["handback_reasons"]
    assert "Not backend work" in reasons
    assert "Not frontend work either" in reasons


def test_handback_worker_env_scoping_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify HERMES_KANBAN_TASK env default and argument validation."""
    db_path = _setup_test_context(tmp_path, monkeypatch)

    with kbc.connect_closing(db_path) as conn:
        tid = kb.create_task(conn, title="Scoped Worker Task", assignee="alice", initial_status="running")

    thread_key = "tg:chat:scoped"
    _add_subscription(db_path, thread_key=thread_key, ticket_id=tid, context_only=0)

    trg_id, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key=thread_key,
        actor_source_id="alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Task in env",
        idempotency_key="idemp_scoped_1",
        now=1000,
    )

    # Set worker env var
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    # Omit task_id in call: resolves from HERMES_KANBAN_TASK
    res_str = _handle_handback({"trigger_id": trg_id, "reason": "Env scoped handback"})
    res = json.loads(res_str)
    assert res.get("ok") is True
    assert res.get("ticket_id") == tid

    # Validation: missing reason -> error string
    err_str = _handle_handback({"trigger_id": trg_id, "reason": ""})
    err = json.loads(err_str)
    assert "error" in err
    assert "reason is required" in err["error"]

    # Validation: missing trigger_id -> error string
    err_str2 = _handle_handback({"trigger_id": "", "reason": "some reason"})
    err2 = json.loads(err_str2)
    assert "error" in err2
    assert "trigger_id is required" in err2["error"]

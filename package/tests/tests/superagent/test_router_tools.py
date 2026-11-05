"""Tests for Super-agent Router (`orchestrator_*`) toolset (E-019).

Verifies:
  - orchestrator_route creates exactly one kanban task with created_by="router".
  - Tools are hidden (not registered / not visible) when superagent.enabled is false.
  - Tools are hidden on a non-router profile even when superagent.enabled is true.
  - Tools are visible on the configured router profile when superagent.enabled is true.
  - orchestrator_reply calls the notifier send path exactly once with the right address.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.orchestrator import receive_trigger
from hermes_cli.superagent_outbox import drain_outbox
from tools import orchestrator_tools as ot
from tools.orchestrator_tools import (
    _check_orchestrator_mode,
    _handle_read,
    _handle_reply,
    _handle_route,
)
from tools.registry import registry


@pytest.fixture(autouse=True)
def board_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with initialized Kanban database and clean env."""
    home = tmp_path / "hroot"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_PROFILE",
        "HERMES_PROFILE_NAME",
        "HERMES_SESSION_ID",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

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
    (home / "config.yaml").write_text(cfg_yaml)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Case 1: orchestrator_route creates exactly one task with created_by="router"
# ---------------------------------------------------------------------------


def test_orchestrator_route_creates_single_task_stamped_router(board_env):
    """orchestrator_route creates exactly one task with created_by='router'."""
    raw = _handle_route({
        "title": "Migrate auth service",
        "assignee": "backend-worker",
    })
    res = json.loads(raw)
    assert res.get("ok") is True, f"Route handler failed: {raw}"
    task_id = res.get("task_id")
    assert isinstance(task_id, str) and task_id.startswith("t_")

    conn = kbc.connect()
    try:
        tasks = kb.list_tasks(conn)
        assert len(tasks) == 1, f"Expected exactly 1 task, found {len(tasks)}"
        task = tasks[0]
        assert task.id == task_id
        assert task.created_by == "router"
        assert task.title == "Migrate auth service"
        assert task.assignee == "backend-worker"
        assert task.status == "ready"
    finally:
        conn.close()


def test_orchestrator_route_via_registry_dispatch(board_env):
    """Dispatching orchestrator_route through registry creates exactly one task with created_by='router'."""
    raw = registry.dispatch(
        "orchestrator_route",
        {"title": "Review PR #42", "assignee": "reviewer-bot"},
    )
    res = json.loads(raw)
    assert res.get("ok") is True

    conn = kbc.connect()
    try:
        tasks = kb.list_tasks(conn)
        assert len(tasks) == 1
        assert tasks[0].created_by == "router"
        assert tasks[0].title == "Review PR #42"
        assert tasks[0].assignee == "reviewer-bot"
    finally:
        conn.close()


def test_orchestrator_route_passes_optional_fields(board_env):
    """orchestrator_route correctly records optional task fields."""
    raw = _handle_route({
        "title": "Deploy canary",
        "assignee": "infra-agent",
        "body": "Canary deployment specification",
        "priority": 2,
        "skills": ["docker", "k8s"],
    })
    res = json.loads(raw)
    assert res.get("ok") is True

    conn = kbc.connect()
    try:
        task = kb.get_task(conn, res["task_id"])
        assert task is not None
        assert task.created_by == "router"
        assert task.body == "Canary deployment specification"
        assert task.priority == 2
        assert "docker" in (task.skills or "")
    finally:
        conn.close()


def test_orchestrator_route_validation_missing_required_fields(board_env):
    """orchestrator_route rejects calls with missing title or assignee without creating tasks."""
    res_no_title = json.loads(_handle_route({"assignee": "worker"}))
    assert "error" in res_no_title
    assert "title" in res_no_title["error"].lower()

    res_no_assignee = json.loads(_handle_route({"title": "Some title"}))
    assert "error" in res_no_assignee
    assert "assignee" in res_no_assignee["error"].lower()

    conn = kbc.connect()
    try:
        assert len(kb.list_tasks(conn)) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Case 2: Tools are hidden (not registered / not visible) when superagent.enabled is False
# ---------------------------------------------------------------------------


def test_tools_hidden_when_superagent_disabled(monkeypatch):
    """When superagent.enabled is False, _check_orchestrator_mode is False and tools are hidden."""
    monkeypatch.setattr(
        ot,
        "load_config",
        lambda: {"superagent": {"enabled": False, "router_profile": "router"}},
    )
    assert _check_orchestrator_mode() is False

    tool_names = set(registry.get_tool_names_for_toolset("orchestrator"))
    assert len(tool_names) == 12

    defs = registry.get_definitions(tool_names, quiet=True)
    assert len(defs) == 0, f"Expected 0 tool definitions when disabled, got: {defs}"


def test_tools_hidden_with_empty_or_missing_superagent_config(monkeypatch):
    """When superagent block is absent or defaults are used, tools remain hidden."""
    monkeypatch.setattr(ot, "load_config", dict)
    assert _check_orchestrator_mode() is False

    tool_names = set(registry.get_tool_names_for_toolset("orchestrator"))
    defs = registry.get_definitions(tool_names, quiet=True)
    assert len(defs) == 0


# ---------------------------------------------------------------------------
# Case 3: Tools are hidden on non-router profile even when enabled is True
# ---------------------------------------------------------------------------


def test_tools_hidden_on_non_router_profile_when_enabled(monkeypatch):
    """When superagent.enabled is True, non-router profiles cannot see the tools."""
    monkeypatch.setattr(
        ot,
        "load_config",
        lambda: {"superagent": {"enabled": True, "router_profile": "router"}},
    )
    monkeypatch.setattr(ot, "_active_profile_name", lambda: "worker")

    assert _check_orchestrator_mode() is False

    tool_names = set(registry.get_tool_names_for_toolset("orchestrator"))
    defs = registry.get_definitions(tool_names, quiet=True)
    assert len(defs) == 0


def test_tools_hidden_when_router_profile_is_unconfigured(monkeypatch):
    """When superagent.enabled is True but router_profile is None or empty, tools are hidden."""
    for empty_val in (None, "", "   "):
        monkeypatch.setattr(
            ot,
            "load_config",
            lambda val=empty_val: {"superagent": {"enabled": True, "router_profile": val}},
        )
        monkeypatch.setattr(ot, "_active_profile_name", lambda: "default")
        assert _check_orchestrator_mode() is False

        tool_names = set(registry.get_tool_names_for_toolset("orchestrator"))
        defs = registry.get_definitions(tool_names, quiet=True)
        assert len(defs) == 0


def test_tools_visible_when_enabled_on_matching_router_profile(monkeypatch):
    """When superagent.enabled is True and active profile matches router_profile, tools are visible."""
    monkeypatch.setattr(
        ot,
        "load_config",
        lambda: {"superagent": {"enabled": True, "router_profile": "lead-router"}},
    )
    monkeypatch.setattr(ot, "_active_profile_name", lambda: "lead-router")

    assert _check_orchestrator_mode() is True

    tool_names = set(registry.get_tool_names_for_toolset("orchestrator"))
    defs = registry.get_definitions(tool_names, quiet=True)
    assert len(defs) == 12
    names_in_defs = {d["function"]["name"] for d in defs}
    assert names_in_defs == tool_names


def test_tools_hidden_in_delegated_child_or_worker_context(monkeypatch):
    """Tools remain hidden in delegated child contexts and dispatcher worker processes."""
    monkeypatch.setattr(
        ot,
        "load_config",
        lambda: {"superagent": {"enabled": True, "router_profile": "router"}},
    )
    monkeypatch.setattr(ot, "_active_profile_name", lambda: "router")

    # In delegated child context
    monkeypatch.setattr(ot, "_is_delegated_child_context", lambda: True)
    assert _check_orchestrator_mode() is False

    # Dispatcher-owned worker
    monkeypatch.setattr(ot, "_is_delegated_child_context", lambda: False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    monkeypatch.setattr(ot, "_is_dispatcher_owned_worker", lambda: True)
    assert _check_orchestrator_mode() is False


# ---------------------------------------------------------------------------
# Case 4: orchestrator_reply enqueues Outbox row only, performs NO inline send, and drains once
# ---------------------------------------------------------------------------


def test_orchestrator_reply_explicit_target_calls_notifier_send():
    """orchestrator_reply with explicit platform and chat_id enqueues to outbox, does not send inline, and drains once."""
    mock_runner = MagicMock()
    mock_runner.config = None
    mock_adapter = MagicMock()
    mock_adapter.send = AsyncMock(return_value=MagicMock(success=True))
    mock_runner._authorization_adapter.return_value = mock_adapter

    raw = _handle_reply(
        {
            "text": "Routing notification text",
            "platform": "telegram",
            "chat_id": "123456789",
        },
        runner=mock_runner,
    )
    res = json.loads(raw)
    assert res.get("ok") is True
    assert res.get("queued") == 1
    assert len(res.get("deliveries", [])) == 1
    assert res["deliveries"][0]["queued"] is True
    assert res["deliveries"][0]["outbox_id"] is not None

    # Performs NO adapter send inline
    mock_adapter.send.assert_not_called()

    # Leaves exactly one 'queued' Outbox row
    conn = kbc.connect()
    try:
        cur = conn.execute("SELECT * FROM sa_outbox WHERE source = 'telegram'")
        rows = cur.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "queued"
        assert row["body"] == "Routing notification text"
        addr = json.loads(row["address_json"])
        assert addr["platform"] == "telegram"
        assert addr["chat_id"] == "123456789"
    finally:
        conn.close()

    # After drain_outbox runs with a fake sender, exactly one send occurs
    sent_messages: list[dict] = []

    def fake_sender(address: dict, body: str, purpose: str) -> dict:
        sent_messages.append({"address": address, "body": body, "purpose": purpose})
        return {"ok": True, "message_id": "fake-tg-1"}

    conn = kbc.connect()
    try:
        summary = drain_outbox(conn, now=time.time(), sender=fake_sender)
        assert summary.get("sent") == 1
        assert len(sent_messages) == 1
        assert sent_messages[0]["address"]["chat_id"] == "123456789"
        assert sent_messages[0]["body"] == "Routing notification text"

        cur = conn.execute("SELECT status FROM sa_outbox WHERE id = ?", (row["id"],))
        assert cur.fetchone()["status"] == "sent"
    finally:
        conn.close()


def test_orchestrator_reply_task_subscription_calls_notifier_send(board_env):
    """orchestrator_reply targeting a task_id resolves subscription, enqueues to outbox, and drains once."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Subscribed Task", assignee="worker-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="chan-98765")
    finally:
        conn.close()

    mock_runner = MagicMock()
    mock_runner.config = None
    mock_adapter = MagicMock()
    mock_adapter.send = AsyncMock(return_value=MagicMock(success=True))
    mock_runner._authorization_adapter.return_value = mock_adapter

    raw = _handle_reply(
        {"text": "Task resolution status update", "task_id": tid},
        runner=mock_runner,
    )
    res = json.loads(raw)
    assert res.get("ok") is True
    assert res.get("queued") == 1

    # Performs NO adapter send inline
    mock_adapter.send.assert_not_called()

    # Leaves exactly one 'queued' Outbox row
    conn = kbc.connect()
    try:
        cur = conn.execute("SELECT * FROM sa_outbox WHERE ticket_id = ?", (tid,))
        rows = cur.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "queued"
        assert row["body"] == "Task resolution status update"
    finally:
        conn.close()

    # After drain_outbox runs with a fake sender, exactly one send occurs
    sent_messages: list[dict] = []

    def fake_sender(address: dict, body: str, purpose: str) -> dict:
        sent_messages.append({"address": address, "body": body, "purpose": purpose})
        return {"ok": True, "message_id": "fake-discord-1"}

    conn = kbc.connect()
    try:
        summary = drain_outbox(conn, now=time.time(), sender=fake_sender)
        assert summary.get("sent") == 1
        assert len(sent_messages) == 1
        assert sent_messages[0]["address"]["chat_id"] == "chan-98765"
        assert sent_messages[0]["body"] == "Task resolution status update"

        cur = conn.execute("SELECT status FROM sa_outbox WHERE id = ?", (row["id"],))
        assert cur.fetchone()["status"] == "sent"
    finally:
        conn.close()


def test_orchestrator_reply_via_registry_dispatch():
    """orchestrator_reply invoked via registry.dispatch enqueues to outbox, does not send inline, and drains once."""
    mock_runner = MagicMock()
    mock_runner.config = None
    mock_adapter = MagicMock()
    mock_adapter.send = AsyncMock(return_value=MagicMock(success=True))
    mock_runner._authorization_adapter.return_value = mock_adapter

    raw = registry.dispatch(
        "orchestrator_reply",
        {"text": "Dispatched message", "platform": "slack", "chat_id": "C012345"},
        runner=mock_runner,
    )
    res = json.loads(raw)
    assert res.get("ok") is True
    assert res.get("queued") == 1

    # Performs NO adapter send inline
    mock_adapter.send.assert_not_called()

    # Leaves exactly one 'queued' Outbox row
    conn = kbc.connect()
    try:
        cur = conn.execute("SELECT * FROM sa_outbox WHERE source = 'slack'")
        rows = cur.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "queued"
        assert row["body"] == "Dispatched message"
    finally:
        conn.close()

    # After drain_outbox runs with a fake sender, exactly one send occurs
    sent_messages: list[dict] = []

    def fake_sender(address: dict, body: str, purpose: str) -> dict:
        sent_messages.append({"address": address, "body": body, "purpose": purpose})
        return {"ok": True, "message_id": "fake-slack-1"}

    conn = kbc.connect()
    try:
        summary = drain_outbox(conn, now=time.time(), sender=fake_sender)
        assert summary.get("sent") == 1
        assert len(sent_messages) == 1
        assert sent_messages[0]["address"]["chat_id"] == "C012345"
        assert sent_messages[0]["body"] == "Dispatched message"

        cur = conn.execute("SELECT status FROM sa_outbox WHERE id = ?", (row["id"],))
        assert cur.fetchone()["status"] == "sent"
    finally:
        conn.close()


def test_orchestrator_reply_queue_reply_error_handling(monkeypatch):
    """orchestrator_reply propagates/reports queue_reply failures instead of swallowing them."""
    mock_runner = MagicMock()
    mock_runner.config = None

    import hermes_cli.superagent_outbox as outbox_mod

    def bad_queue(*args, **kw):
        raise RuntimeError("simulated DB disk I/O error")

    monkeypatch.setattr(outbox_mod, "queue_reply", bad_queue)

    raw = _handle_reply(
        {
            "text": "Failing enqueue",
            "platform": "telegram",
            "chat_id": "12345",
        },
        runner=mock_runner,
    )
    res = json.loads(raw)
    assert res.get("ok") is False
    assert res.get("queued") == 0
    assert "queue_reply failed" in res.get("error", "")
    assert "simulated DB disk I/O error" in res.get("error", "")


def test_orchestrator_reply_validation_errors():
    """orchestrator_reply validates required parameters and active runner."""
    mock_runner = MagicMock()
    mock_runner.config = None

    # Empty / missing text
    res_no_text = json.loads(_handle_reply({}, runner=mock_runner))
    assert "error" in res_no_text
    assert "text must be a non-empty string" in res_no_text["error"]

    # No target or subscription found
    res_no_sub = json.loads(_handle_reply({"text": "Hello"}, runner=mock_runner))
    assert "error" in res_no_sub
    assert "no notification subscription" in res_no_sub["error"]

    # No gateway runner
    res_no_runner = json.loads(_handle_reply({
        "text": "Hello",
        "platform": "telegram",
        "chat_id": "123",
    }))
    assert "error" in res_no_runner
    assert "gateway runner is not active" in res_no_runner["error"]


# ---------------------------------------------------------------------------
# Additional router tools: orchestrator_read and stubs
# ---------------------------------------------------------------------------


def test_orchestrator_read_delegates_to_show_and_list(board_env):
    """orchestrator_read delegates to show when task_id given and list otherwise."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Read Task", assignee="worker-1")
    finally:
        conn.close()

    # Read single task
    show_out = json.loads(_handle_read({"task_id": tid}))
    assert "task" in show_out
    assert show_out["task"]["id"] == tid
    assert show_out["task"]["title"] == "Read Task"

    # List tasks
    list_out = json.loads(_handle_read({"action": "list"}))
    assert "tasks" in list_out
    assert any(t["id"] == tid for t in list_out["tasks"])


def test_orchestrator_phase_1_stubs(monkeypatch):
    """The 8 tools that were stubs in Phase 1 execute real Phase 2 implementations (Card 2.10, E-016/E-017)."""
    conn = kbc.connect()
    try:
        # 1. orchestrator_eval & orchestrator_eval_read
        trg_id, _ = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:stubs",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Review release notes",
            idempotency_key="idemp_stubs_eval",
            now=1000,
        )
        eval_raw = registry.dispatch("orchestrator_eval", {"thread_key": "tg:chat:stubs"})
        eval_res = json.loads(eval_raw)
        assert eval_res.get("ok") is True
        assert eval_res.get("batches") == 1
        assert eval_res.get("triggers") == 1
        assert eval_res.get("summary", {}).get("accept") == 1

        eval_read_raw = registry.dispatch("orchestrator_eval_read", {"thread_key": "tg:chat:stubs"})
        eval_read_res = json.loads(eval_read_raw)
        assert eval_read_res.get("ok") is True
        assert eval_read_res.get("count") >= 1
        assert len(eval_read_res.get("decisions", [])) >= 1
        dec = eval_read_res["decisions"][0]
        assert dec["outcome"] == "accept"
        dec_id = dec["id"]

        # 2. orchestrator_eval_decide enforces pending batches & observe mode
        # 2a. Refuses missing decision
        missing_dec_raw = registry.dispatch("orchestrator_eval_decide", {"decision_id": 999999, "decision": "accept"})
        missing_dec = json.loads(missing_dec_raw)
        assert missing_dec.get("ok") is not True
        assert "not found" in missing_dec.get("error", "")

        # 2b. Observe-mode returns would_accept (per DECISIONS E-016/E-017 / Card 2.10)
        monkeypatch.setattr(
            ot,
            "load_config",
            lambda: {
                "superagent": {
                    "enabled": True,
                    "mode": "observe",
                    "accept_mode": "observe",
                    "router_profile": "default",
                    "owner_identities": ["alice"],
                }
            },
        )
        decide_obs_raw = registry.dispatch("orchestrator_eval_decide", {"decision_id": dec_id, "decision": "accept"})
        decide_obs = json.loads(decide_obs_raw)
        assert decide_obs.get("ok") is True
        assert decide_obs.get("accepted") is False
        assert decide_obs.get("would_accept") is True
        assert decide_obs.get("ticket_id") is None

        # 2c. Dismiss decision
        with write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_decisions (
                    thread_key, trigger_ids_json, outcome, strength, would_accept,
                    ignored_from_card, matched_rules_json, reason_json, policy_version,
                    ticket_id, dismissed, created_at
                ) VALUES (?, '[]', 'accept', 'explicit', 0, 0, '[]', '{}', 'v1', NULL, 0, 1000)
                """,
                ("tg:chat:dismiss_me",),
            )
            dismiss_dec_id = cur.lastrowid
        dismiss_raw = registry.dispatch("orchestrator_eval_decide", {"decision_id": dismiss_dec_id, "decision": "dismiss"})
        dismiss_res = json.loads(dismiss_raw)
        assert dismiss_res.get("ok") is True
        assert dismiss_res.get("dismissed") is True
        # Cannot decide on already dismissed
        re_dismiss_raw = registry.dispatch("orchestrator_eval_decide", {"decision_id": dismiss_dec_id, "decision": "dismiss"})
        assert "already dismissed" in json.loads(re_dismiss_raw).get("error", "")

        # 3. orchestrator_trigger_requeue
        # Reset monkeypatch config back to auto
        monkeypatch.setattr(
            ot,
            "load_config",
            lambda: {
                "superagent": {
                    "enabled": True,
                    "mode": "auto",
                    "accept_mode": "auto",
                    "router_profile": "default",
                    "owner_identities": ["alice"],
                }
            },
        )
        # Requeue completed trigger back to queued
        req_raw = registry.dispatch("orchestrator_trigger_requeue", {"trigger_id": trg_id})
        req_res = json.loads(req_raw)
        assert req_res.get("ok") is True
        assert req_res.get("eval_state") == "queued"
        assert req_res.get("requeued") is True

        # Refuses requeue while evaluating
        with write_txn(conn):
            conn.execute("UPDATE sa_triggers SET eval_state = 'evaluating' WHERE id = ?", (trg_id,))
        req_eval_raw = registry.dispatch("orchestrator_trigger_requeue", {"trigger_id": trg_id})
        assert "while it is evaluating" in json.loads(req_eval_raw).get("error", "")

        # 4. orchestrator_thread_unsubscribe
        with write_txn(conn):
            conn.execute(
                """
                INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
                VALUES (?, 't_sub_test', 0, 1000)
                """,
                ("tg:chat:unsub_test",),
            )
        unsub_raw = registry.dispatch("orchestrator_thread_unsubscribe", {"thread_key": "tg:chat:unsub_test"})
        unsub_res = json.loads(unsub_raw)
        assert unsub_res.get("ok") is True
        assert unsub_res.get("dropped") == 1

        # 5. orchestrator_ticket_retry works via kanban path
        tid = kb.create_task(conn, title="Retry test task", assignee="worker-1", initial_status="running")
        with write_txn(conn):
            run_cur = conn.execute(
                """
                INSERT INTO task_runs (task_id, profile, status, claim_lock, started_at)
                VALUES (?, 'worker-1', 'running', 'lock-retry-test', ?)
                """,
                (tid, int(time.time())),
            )
            run_id = run_cur.lastrowid
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'lock-retry-test', current_run_id = ? WHERE id = ?",
                (run_id, tid),
            )
        retry_raw = registry.dispatch("orchestrator_ticket_retry", {"task_id": tid, "reason": "worker timeout"})
        retry_res = json.loads(retry_raw)
        assert retry_res.get("ok") is True
        assert retry_res.get("status") == "ready"
        assert retry_res.get("retried") is True
        task_after_retry = kb.get_task(conn, tid)
        assert task_after_retry.status == "ready"

        # 5b. Refuses terminal ticket retry
        with write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        term_retry_raw = registry.dispatch("orchestrator_ticket_retry", {"task_id": tid})
        assert "cannot retry terminal ticket" in json.loads(term_retry_raw).get("error", "")

        # 5c. orchestrator_ticket_stop works via stop_ticket path
        tid_stop = kb.create_task(conn, title="Stop test task", assignee="worker-1", initial_status="running")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid_stop,))
        stop_raw = registry.dispatch("orchestrator_ticket_stop", {"task_id": tid_stop, "reason": "router stop test"})
        stop_res = json.loads(stop_raw)
        assert stop_res.get("ok") is True
        assert stop_res.get("stopped") is True
        assert stop_res.get("status") == "stopped"
        assert kb.get_task(conn, tid_stop).status == "done"

        # 6. orchestrator_ticket_request_status
        status_raw = registry.dispatch("orchestrator_ticket_request_status", {"task_id": tid})
        status_res = json.loads(status_raw)
        assert status_res.get("ok") is True
        assert status_res.get("task_id") == tid
        assert status_res.get("task_status") == "done"

        # 7. orchestrator_ticket_note
        note_raw = registry.dispatch("orchestrator_ticket_note", {"task_id": tid, "note": "Integration note"})
        note_res = json.loads(note_raw)
        assert note_res.get("ok") is True
        assert note_res.get("kind") == "note"
        assert note_res.get("note") == "Integration note"
        note_row = conn.execute("SELECT * FROM sa_inputs WHERE id = ?", (note_res["input_id"],)).fetchone()
        assert note_row is not None
        assert note_row["body"] == "Integration note"
    finally:
        conn.close()

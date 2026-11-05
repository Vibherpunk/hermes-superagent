"""Tests for Super-agent blocked-with-diagnosis auto-resume rule (Card P2)."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from gateway.kanban_watchers_dispatcher import reset_slot_pool
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import profiles
from tools import kanban_tools as kt


@pytest.fixture
def kanban_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with initialized kanban db and mocked profile resolver."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    reset_slot_pool(None)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _create_running_task(conn: sqlite3.Connection, title: str = "task") -> tuple[str, int]:
    tid = kb.create_task(conn, title=title, assignee="test-worker")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    return tid, int(claimed.current_run_id)


def test_diagnosed_block_auto_resumes_once_with_comment_and_event(kanban_env: Path) -> None:
    """1. Block with the triple (kind='transient', within_mandate=True) ->
    auto_resume_diagnosed_blocks returns [tid], status is resumable ('ready'),
    dispatcher comment contains diagnosis+next_step, and 'auto_resumed' event exists."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "diagnosed-transient")
        ok = kb.block_task(
            conn,
            tid,
            reason="transient build failure",
            kind="transient",
            diagnosis="missing fixture import in conftest",
            next_step="import fixture in tests/conftest.py and re-run pytest",
            next_step_within_mandate=True,
            expected_run_id=run_id,
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "blocked"

        resumed = kb.auto_resume_diagnosed_blocks(conn)
        assert resumed == [tid]

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"

        comments = kb.list_comments(conn, tid)
        dispatcher_comments = [c for c in comments if c.author == "dispatcher"]
        assert len(dispatcher_comments) == 1
        expected_body = (
            "Auto-resume (1 of 1): the previous worker filed the diagnosis and next step below. "
            "Continue from them — do not re-diagnose from scratch.\n\n"
            "Diagnosis: missing fixture import in conftest\n\n"
            "Next step: import fixture in tests/conftest.py and re-run pytest"
        )
        assert dispatcher_comments[0].body == expected_body

        events = kb.list_events(conn, tid)
        auto_events = [e for e in events if e.kind == "auto_resumed"]
        assert len(auto_events) == 1
        assert auto_events[0].payload == {
            "diagnosis": "missing fixture import in conftest",
            "next_step": "import fixture in tests/conftest.py and re-run pytest",
            "kind": "transient",
        }


def test_second_diagnosed_block_does_not_auto_resume(kanban_env: Path) -> None:
    """2. After one auto-resume, blocking the task AGAIN with the full triple
    returns [] (once-ever) and the task stays blocked/triage (not resumed)."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "once-only")
        kb.block_task(
            conn,
            tid,
            reason="first block",
            kind="transient",
            diagnosis="root cause 1",
            next_step="fix 1",
            next_step_within_mandate=True,
            expected_run_id=run_id,
        )
        assert kb.auto_resume_diagnosed_blocks(conn) == [tid]
        assert kb.get_task(conn, tid).status == "ready"

        # Claim again and block with a different kind ('capability') so it lands in
        # 'blocked' rather than hitting BLOCK_RECURRENCE_LIMIT -> 'triage'.
        claimed2 = kb.claim_task(conn, tid)
        assert claimed2 is not None
        kb.block_task(
            conn,
            tid,
            reason="second block",
            kind="capability",
            diagnosis="root cause 2",
            next_step="fix 2",
            next_step_within_mandate=True,
            expected_run_id=claimed2.current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        assert kb.auto_resume_diagnosed_blocks(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_block_with_reason_only_stays_blocked(kanban_env: Path) -> None:
    """3. Block with reason only (no triple) -> [] and still blocked."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "reason-only")
        kb.block_task(conn, tid, reason="stuck on something", kind="transient", expected_run_id=run_id)
        assert kb.auto_resume_diagnosed_blocks(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_block_without_within_mandate_true_stays_blocked(kanban_env: Path) -> None:
    """4. Block with diagnosis+next_step but within_mandate False -> [];
    with within_mandate omitted -> []."""
    with kbc.connect() as conn:
        t_false, run_false = _create_running_task(conn, "mandate-false")
        kb.block_task(
            conn,
            t_false,
            reason="out of scope fix",
            kind="transient",
            diagnosis="root cause",
            next_step="rewrite auth service",
            next_step_within_mandate=False,
            expected_run_id=run_false,
        )

        t_omitted, run_omitted = _create_running_task(conn, "mandate-omitted")
        kb.block_task(
            conn,
            t_omitted,
            reason="unspecified mandate",
            kind="transient",
            diagnosis="root cause",
            next_step="apply patch",
            expected_run_id=run_omitted,
        )

        assert kb.auto_resume_diagnosed_blocks(conn) == []
        assert kb.get_task(conn, t_false).status == "blocked"
        assert kb.get_task(conn, t_omitted).status == "blocked"


def test_block_with_empty_next_step_stays_blocked(kanban_env: Path) -> None:
    """5. Block with diagnosis but empty next_step -> []."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "empty-next-step")
        kb.block_task(
            conn,
            tid,
            reason="diagnosed only",
            kind="transient",
            diagnosis="found the bug",
            next_step="",
            next_step_within_mandate=True,
            expected_run_id=run_id,
        )
        assert kb.auto_resume_diagnosed_blocks(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_needs_input_and_dependency_never_auto_resume(kanban_env: Path) -> None:
    """6. Block kind='needs_input' with the full triple -> []."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "needs-human")
        kb.block_task(
            conn,
            tid,
            reason="which prod bucket should we target?",
            kind="needs_input",
            diagnosis="bucket name missing from config",
            next_step="set bucket name in config.yaml",
            next_step_within_mandate=True,
            expected_run_id=run_id,
        )
        assert kb.auto_resume_diagnosed_blocks(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_ready_and_running_tasks_untouched_by_scanner(kanban_env: Path) -> None:
    """7. A task in 'ready'/'running' is untouched by the scanner."""
    with kbc.connect() as conn:
        t_ready = kb.create_task(conn, title="ready-task", assignee="test-worker")
        t_running, _ = _create_running_task(conn, "running-task")
        ready_events_before = kb.list_events(conn, t_ready)
        running_events_before = kb.list_events(conn, t_running)

        assert kb.auto_resume_diagnosed_blocks(conn) == []
        assert kb.get_task(conn, t_ready).status == "ready"
        assert kb.get_task(conn, t_running).status == "running"
        assert kb.list_events(conn, t_ready) == ready_events_before
        assert kb.list_events(conn, t_running) == running_events_before


def test_kanban_block_handler_accepts_triple_and_populates_event_payload(
    kanban_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8. kanban_block handler level: the new args are accepted and land in the
    blocked event payload when supplied, and are absent when omitted."""
    with kbc.connect() as conn:
        t_with, run_with = _create_running_task(conn, "handler-with-triple")
        t_without, run_without = _create_running_task(conn, "handler-without-triple")

    monkeypatch.setenv("HERMES_KANBAN_TASK", t_with)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_with))
    res_with = json.loads(
        kt._handle_block(
            {
                "reason": "flaky test failure",
                "kind": "transient",
                "diagnosis": "race condition in cache init",
                "next_step": "serialize init with lock",
                "next_step_within_mandate": True,
            }
        )
    )
    assert res_with["ok"] is True

    monkeypatch.setenv("HERMES_KANBAN_TASK", t_without)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_without))
    res_without = json.loads(
        kt._handle_block(
            {
                "reason": "waiting on operator",
                "kind": "needs_input",
            }
        )
    )
    assert res_without["ok"] is True

    with kbc.connect() as conn:
        ev_with = [e for e in kb.list_events(conn, t_with) if e.kind == "blocked"][-1]
        assert ev_with.payload["diagnosis"] == "race condition in cache init"
        assert ev_with.payload["next_step"] == "serialize init with lock"
        assert ev_with.payload["next_step_within_mandate"] is True

        ev_without = [e for e in kb.list_events(conn, t_without) if e.kind == "blocked"][-1]
        assert "diagnosis" not in ev_without.payload
        assert "next_step" not in ev_without.payload
        assert "next_step_within_mandate" not in ev_without.payload


def test_dispatch_tick_auto_resumes_and_spawns_in_same_tick(kanban_env: Path) -> None:
    """A diagnosed-blocked card is auto-resumed during _dispatch_once_locked and
    becomes spawnable in the same tick."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "dispatch-same-tick")
        kb.block_task(
            conn,
            tid,
            reason="transient error",
            kind="transient",
            diagnosis="port already bound",
            next_step="use ephemeral port 0",
            next_step_within_mandate=True,
            expected_run_id=run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        spawned_ids: list[str] = []

        def fake_spawn(task: Any, workspace: str, board: str | None = None) -> int:
            spawned_ids.append(task.id)
            return 4321

        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn, reconcile_orphans=False)
        assert tid in spawned_ids
        assert any(s[0] == tid for s in res.spawned)
        assert kb.get_task(conn, tid).status == "running"


def test_stale_diagnosis_after_unblock_and_gave_up_does_not_auto_resume(kanban_env: Path) -> None:
    """9a. Stale-diagnosis guard: block with triple -> manual unblock_task ->
    breaker parks back in 'blocked' with a 'gave_up' event (no new 'blocked'
    event, block_kind left as-is) -> scanner returns [] and task stays blocked
    with no 'auto_resumed' event and no dispatcher comment."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "stale-diagnosis")
        assert kb.block_task(
            conn,
            tid,
            reason="transient glitch",
            kind="transient",
            diagnosis="old root cause",
            next_step="old next step",
            next_step_within_mandate=True,
            expected_run_id=run_id,
        )
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"

        # Simulate breaker park: back to 'blocked' with 'gave_up' event, no new
        # 'blocked' event, block_kind preserved.
        with kbc.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked', current_run_id = NULL WHERE id = ?",
                (tid,),
            )
            kb._append_event(
                conn,
                tid,
                "gave_up",
                {"reason": "consecutive_failures", "failures": 3},
            )
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_task(conn, tid).block_kind == "transient"

        assert kb.auto_resume_diagnosed_blocks(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "auto_resumed"] == []
        assert [c for c in kb.list_comments(conn, tid) if c.author == "dispatcher"] == []


def test_fresh_diagnosis_after_stale_breaker_park_auto_resumes_once(kanban_env: Path) -> None:
    """9b. Freshness-positive control: after (a)'s stale setup on a fresh task,
    filing a NEW diagnosed block from a running state (newer than both 'unblocked'
    and 'gave_up') fires the scanner once."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "fresh-after-gave-up")
        assert kb.block_task(
            conn,
            tid,
            reason="first transient glitch",
            kind="transient",
            diagnosis="stale root cause",
            next_step="stale next step",
            next_step_within_mandate=True,
            expected_run_id=run_id,
        )
        assert kb.unblock_task(conn, tid) is True
        with kbc.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked', current_run_id = NULL WHERE id = ?",
                (tid,),
            )
            kb._append_event(
                conn,
                tid,
                "gave_up",
                {"reason": "consecutive_failures", "failures": 3},
            )
        assert kb.auto_resume_diagnosed_blocks(conn) == []

        # Return to running and file a NEW diagnosed block via block_task (use
        # kind='capability' so BLOCK_RECURRENCE_LIMIT on 'transient' does not
        # route to 'triage').
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        claimed2 = kb.claim_task(conn, tid)
        assert claimed2 is not None
        assert kb.block_task(
            conn,
            tid,
            reason="new capability block with diagnosis",
            kind="capability",
            diagnosis="fresh root cause",
            next_step="fresh next step",
            next_step_within_mandate=True,
            expected_run_id=claimed2.current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        assert kb.auto_resume_diagnosed_blocks(conn) == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        dispatcher_comments = [c for c in kb.list_comments(conn, tid) if c.author == "dispatcher"]
        assert len(dispatcher_comments) == 1
        assert "Diagnosis: fresh root cause" in dispatcher_comments[0].body
        assert "Next step: fresh next step" in dispatcher_comments[0].body
        auto_events = [e for e in kb.list_events(conn, tid) if e.kind == "auto_resumed"]
        assert len(auto_events) == 1


def test_auto_resume_rolls_back_when_unblock_task_fails(
    kanban_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """9c. Rollback case: if unblock_task returns False, auto_resume_diagnosed_blocks
    rolls back the comment and 'auto_resumed' event, returns [], and leaves the
    task in 'blocked'."""
    with kbc.connect() as conn:
        tid, run_id = _create_running_task(conn, "rollback-on-unblock-failure")
        assert kb.block_task(
            conn,
            tid,
            reason="transient error",
            kind="transient",
            diagnosis="known cause",
            next_step="known fix",
            next_step_within_mandate=True,
            expected_run_id=run_id,
        )
        monkeypatch.setattr(kb, "unblock_task", lambda _conn, _tid: False)

        assert kb.auto_resume_diagnosed_blocks(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.list_comments(conn, tid) == []
        assert [e for e in kb.list_events(conn, tid) if e.kind in ("auto_resumed", "commented")] == []


def test_auto_resume_isolates_per_task_exception_and_resumes_remaining(
    kanban_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """9d. Isolation case: two diagnosed blocked tasks; when processing of the
    FIRST (in scan order) raises an unexpected exception, ONLY the first task is
    rolled back, a warning is logged, and the second task still resumes and is
    returned."""
    with kbc.connect() as conn:
        t1, run1 = _create_running_task(conn, "first-scan-task")
        t2, run2 = _create_running_task(conn, "second-scan-task")
        # Ensure deterministic scan order (ORDER BY created_at ASC, id ASC).
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET created_at = created_at - 10 WHERE id = ?", (t1,))

        assert kb.block_task(
            conn,
            t1,
            reason="transient 1",
            kind="transient",
            diagnosis="diag 1",
            next_step="step 1",
            next_step_within_mandate=True,
            expected_run_id=run1,
        )
        assert kb.block_task(
            conn,
            t2,
            reason="transient 2",
            kind="transient",
            diagnosis="diag 2",
            next_step="step 2",
            next_step_within_mandate=True,
            expected_run_id=run2,
        )

        real_unblock = kb.unblock_task

        def failing_first_unblock(c: sqlite3.Connection, task_id: str) -> bool:
            if task_id == t1:
                raise RuntimeError("simulated per-task explosion")
            return real_unblock(c, task_id)

        monkeypatch.setattr(kb, "unblock_task", failing_first_unblock)

        with caplog.at_level("WARNING", logger=kb._log.name):
            resumed = kb.auto_resume_diagnosed_blocks(conn)

        assert resumed == [t2]
        assert any(t1 in rec.getMessage() for rec in caplog.records)

        # First task rolled back completely.
        assert kb.get_task(conn, t1).status == "blocked"
        assert kb.list_comments(conn, t1) == []
        assert [e for e in kb.list_events(conn, t1) if e.kind in ("auto_resumed", "commented")] == []

        # Second task resumed normally.
        assert kb.get_task(conn, t2).status == "ready"
        assert len([c for c in kb.list_comments(conn, t2) if c.author == "dispatcher"]) == 1
        assert len([e for e in kb.list_events(conn, t2) if e.kind == "auto_resumed"]) == 1


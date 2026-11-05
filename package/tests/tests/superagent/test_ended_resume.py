"""Tests for Super-agent Ended-Ticket resume via revalidate (Card 3.6).

Verifies:
1. Done-criterion-3 Case 1 (Pass):
   Owning Ticket ended + person trigger + revalidate() pass -> reopen (new task_runs
   row via dispatcher path, resume Input written, disposition fanned_out).
2. Done-criterion-3 Case 2 (Fail):
   Owning Ticket ended + person trigger + revalidate() fail -> no resume, message
   treated as unowned (Eval path: queued), NO notice to Router that it reached a worker.
3. Done-criterion-3 Case 3 (Bot/CI trigger on ended ticket):
   Owning Ticket ended + bot/CI trigger -> never resumes, ticket stays ended, queues for Eval.
4. Stopped-ticket Case:
   Person-stopped Ticket (mandate status withdrawn) receives nothing, ever, even with
   a person's message on its subscribed thread.
5. Context-only subscription on ended ticket:
   Context-only subscriptions never resume an ended ticket.
6. Subsequent trigger after resume:
   Resumed ticket accepts subsequent triggers and dispatcher delivers all pending inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from gateway.kanban_watchers_dispatcher import (
    SlotPool,
    reset_slot_pool,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import profiles
from hermes_cli.orchestrator import (
    receive_trigger,
    reopen_ended_ticket,
    reopen_ticket,
    revalidate,
)


@pytest.fixture
def kanban_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with initialized kanban db and mocked profile resolver."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    reset_slot_pool(None)
    kb.init_db()
    return home


def _fake_spawn_factory(spawns: list[Any]):
    """Return a fake spawn function that records task objects and returns synthetic pids."""
    def fake_spawn(task: Any, workspace: str, board: str | None = None) -> int:
        spawns.append(task)
        return 1000 + len(spawns)
    return fake_spawn


def _insert_mandate(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    policy_ver: str = "v1",
    scope: str = "main",
    strength: str = "explicit",
    basis: dict[str, Any] | None = None,
    status: str = "active",
    amended_from: int | None = None,
    now: int = 1000,
) -> int:
    """Helper to insert an sa_mandates row."""
    basis_json = json.dumps(basis if basis is not None else {})
    cur = conn.execute(
        """
        INSERT INTO sa_mandates (
            ticket_id, policy_version, scope, strength,
            basis_json, status, amended_from, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            policy_ver,
            scope,
            strength,
            basis_json,
            status,
            amended_from,
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
    conn.execute(
        """
        INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (thread_key, ticket_id, int(context_only), int(created_at)),
    )


def _create_ended_ticket(
    conn: sqlite3.Connection,
    *,
    ticket_id: str = "t_ended_1",
    title: str = "Ended ticket",
    assignee: str = "alice",
    mandate_status: str = "active",
    mandate_strength: str = "explicit",
    mandate_basis: dict[str, Any] | None = None,
    now: int = 1000,
) -> tuple[str, int, int]:
    """Create a ticket, complete its initial run so it is ended (status='done').

    Returns (ticket_id, mandate_id, initial_run_id).
    """
    tid = kb.create_task(conn, title=title, assignee=assignee)
    mid = _insert_mandate(
        conn,
        ticket_id=tid,
        strength=mandate_strength,
        basis=mandate_basis or {"source": "telegram", "sender": "alice", "via": "explicit"},
        status=mandate_status,
        now=now,
    )
    conn.execute("UPDATE tasks SET id = ?, mandate_id = ? WHERE id = ?", (ticket_id, mid, tid))

    # Claim and complete an initial run so the ticket is legitimately ended
    claimed = kb.claim_task(conn, ticket_id)
    assert claimed is not None
    initial_run_id = claimed.current_run_id
    assert initial_run_id is not None

    kb.complete_task(
        conn,
        ticket_id,
        result="Initial task implementation completed",
        expected_run_id=initial_run_id,
    )
    task = kb.get_task(conn, ticket_id)
    assert task.status == "done"
    return ticket_id, mid, initial_run_id


# ============================================================================
# Done-criterion-3 Case 1: Revalidate PASS -> Reopen, new task_run via dispatcher
# ============================================================================


def test_ended_ticket_resume_revalidate_pass(kanban_env: Path) -> None:
    """Owning Ticket ended + person trigger + revalidate() pass:

    - Reopens ticket (status='ready', promoted event).
    - Writes resume Input in sa_inputs (kind='resume', is_person=1, context_only=0).
    - Disposition fanned_out, eval_state='done'.
    - New task_runs row created via dispatcher path (kbd.dispatch_once),
      and resume input is delivered to worker run.
    """
    spawns: list[Any] = []
    pool = SlotPool(worker_slots=2, reserve_slots=1)

    with kbc.connect() as conn:
        tid, mid, old_run_id = _create_ended_ticket(
            conn,
            ticket_id="t_ended_pass",
            assignee="alice",
            mandate_strength="explicit",
            mandate_basis={"source": "telegram", "sender": "alice", "via": "explicit"},
        )
        _add_subscription(conn, thread_key="tg:chat:100", ticket_id=tid, context_only=0)

        # Inbound trigger is a person's message on the subscribed thread
        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:100",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Can you please fix the login button too?",
            idempotency_key="msg_resume_01",
            now=1700001000,
        )

        assert disp == "fanned_out"
        assert trg_id.startswith("trg_")

        # 1. Trigger marked terminal fanned_out
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row["eval_state"] == "done"
        assert trg_row["disposition"] == "fanned_out"

        # 2. Ticket reopened to ready status
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.completed_at is None

        # Verify promoted event on task
        events = kb.list_events(conn, tid)
        promoted = [e for e in events if e.kind == "promoted" and (e.payload or {}).get("reason") == "resume"]
        assert len(promoted) == 1

        # 3. Resume Input written with kind='resume'
        inputs = conn.execute(
            "SELECT * FROM sa_inputs WHERE ticket_id = ?",
            (tid,),
        ).fetchall()
        assert len(inputs) == 1
        inp = inputs[0]
        assert inp["trigger_id"] == trg_id
        assert inp["kind"] == "resume"
        assert inp["body"] == "Can you please fix the login button too?"
        assert inp["is_person"] == 1
        assert inp["context_only"] == 0
        assert inp["read_at"] is None

        # 4. Dispatcher path: new task_runs row created via normal dispatch
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=pool)
        assert len(res.spawned) == 1
        assert len(spawns) == 1

        claimed_task = spawns[0]
        assert claimed_task.id == tid
        new_run_id = claimed_task.current_run_id
        assert new_run_id is not None
        assert new_run_id != old_run_id

        # Verify task_runs table has both the old ended run and the new active run
        runs = conn.execute(
            "SELECT id, status, started_at, ended_at FROM task_runs WHERE task_id = ? ORDER BY id ASC",
            (tid,),
        ).fetchall()
        assert len(runs) == 2
        assert runs[0]["id"] == old_run_id
        assert runs[0]["status"] == "done"
        assert runs[0]["ended_at"] is not None

        assert runs[1]["id"] == new_run_id
        assert runs[1]["status"] == "running"
        assert runs[1]["ended_at"] is None

        # Resume input delivered to worker run and marked read
        assert claimed_task.run_input is not None
        assert "Can you please fix the login button too?" in claimed_task.run_input
        inp_updated = conn.execute("SELECT read_at FROM sa_inputs WHERE id = ?", (inp["id"],)).fetchone()
        assert inp_updated["read_at"] is not None


# ============================================================================
# Done-criterion-3 Case 2: Revalidate FAIL -> No resume, message unowned (Eval)
# ============================================================================


def test_ended_ticket_resume_revalidate_fail(kanban_env: Path) -> None:
    """Owning Ticket ended + person trigger + revalidate() fail:

    - No resume: Ticket stays ended (status='done').
    - Message treated as unowned: Trigger enters Eval path (eval_state='queued', disposition None).
    - No inputs written for the ended ticket (sa_inputs empty).
    - NO notice to the Router that it reached a worker (0 rows in sa_outbox).
    - Mandate status is revoked.
    """
    with kbc.connect() as conn:
        # Inferred mandate requires mode='auto' and matched rules still enabled
        tid, mid, old_run_id = _create_ended_ticket(
            conn,
            ticket_id="t_ended_fail",
            assignee="alice",
            mandate_strength="inferred",
            mandate_basis={
                "source": "telegram",
                "sender": "bob",
                "via": "inferred",
                "matched_rules": ["rule_auto_deploy"],
            },
        )
        _add_subscription(conn, thread_key="tg:chat:200", ticket_id=tid, context_only=0)

        # Current policy is 'observe' mode (revalidate fails for inferred mandate)
        policy_observe = {
            "accept_mode": "observe",
            "sources": {"telegram": {"allowlist": ["bob"]}},
            "rules": [{"id": "rule_auto_deploy", "name": "rule_auto_deploy"}],
        }

        # Verify revalidate returns False for this mandate
        assert revalidate(conn, mid, policy=policy_observe) is False

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:200",
            actor_source_id="bob",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Deploy immediately to production",
            idempotency_key="msg_fail_01",
            now=1700002000,
            policy=policy_observe,
        )

        # Message is treated as unowned (Eval path)
        assert disp == "queued"
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        # Ticket stays ended: no status change
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.current_run_id is None

        # No task_runs row created
        runs = conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (tid,)).fetchall()
        assert len(runs) == 1
        assert runs[0]["id"] == old_run_id

        # No inputs written for the ended ticket
        inputs_count = conn.execute(
            "SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ?",
            (tid,),
        ).fetchone()[0]
        assert inputs_count == 0

        # Mandate is revoked
        mandate_row = conn.execute("SELECT status FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mandate_row["status"] == "revoked"

        # NO notice to Router that it reached a worker (no outbox rows)
        outbox_count = conn.execute("SELECT COUNT(*) FROM sa_outbox").fetchone()[0]
        assert outbox_count == 0


# ============================================================================
# Done-criterion-3 Case 3: Bot / CI Trigger never resumes ended ticket
# ============================================================================


def test_ended_ticket_bot_trigger_never_resumes(kanban_env: Path) -> None:
    """Owning Ticket ended + bot/CI trigger:

    - Bot and CI messages never resume an ended Ticket.
    - Ticket stays ended (status='done').
    - No resume input written.
    - Trigger queues for Eval normally.
    """
    with kbc.connect() as conn:
        tid, mid, old_run_id = _create_ended_ticket(
            conn,
            ticket_id="t_ended_bot",
            assignee="alice",
            mandate_strength="explicit",
        )
        _add_subscription(conn, thread_key="gh:repo:alerts", ticket_id=tid, context_only=0)

        # Trigger from bot (actor_is_bot=True)
        trg_id, disp = receive_trigger(
            conn,
            source="github",
            scope="main",
            thread_key="gh:repo:alerts",
            actor_source_id="github-actions[bot]",
            actor_is_bot=True,
            addressed=False,
            kind="check_run",
            body="Build passed for commit sha 123",
            idempotency_key="bot_trg_01",
            now=1700003000,
        )

        assert disp == "queued"
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        # Ticket stays ended
        task = kb.get_task(conn, tid)
        assert task.status == "done"

        # No input written
        inputs_count = conn.execute(
            "SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ?",
            (tid,),
        ).fetchone()[0]
        assert inputs_count == 0

        # No new run created
        runs = conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (tid,)).fetchall()
        assert len(runs) == 1
        assert runs[0]["id"] == old_run_id


# ============================================================================
# Stopped-ticket Case: Person-stopped Ticket receives nothing, ever
# ============================================================================


def test_person_stopped_ticket_receives_nothing_ever(kanban_env: Path) -> None:
    """Person-stopped Tickets (mandate status withdrawn) receive nothing, ever:

    - Even when a person's message arrives on its subscribed thread.
    - Ticket is NOT revalidated, NOT reopened.
    - No sa_inputs row is written.
    - Trigger is treated as unowned and queues for Eval.
    """
    with kbc.connect() as conn:
        # Create ticket with withdrawn mandate (person-stopped)
        tid, mid, old_run_id = _create_ended_ticket(
            conn,
            ticket_id="t_stopped",
            assignee="alice",
            mandate_status="withdrawn",
            mandate_strength="explicit",
        )
        _add_subscription(conn, thread_key="tg:chat:400", ticket_id=tid, context_only=0)

        # Person's message arrives
        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:400",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Please resume the stopped task",
            idempotency_key="msg_stopped_01",
            now=1700004000,
        )

        assert disp == "queued"
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        # Ticket receives NOTHING, EVER:
        inputs_count = conn.execute(
            "SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ?",
            (tid,),
        ).fetchone()[0]
        assert inputs_count == 0

        # Ticket remains done
        task = kb.get_task(conn, tid)
        assert task.status == "done"

        # Mandate remains withdrawn
        mrow = conn.execute("SELECT status FROM sa_mandates WHERE id = ?", (mid,)).fetchone()
        assert mrow["status"] == "withdrawn"


def test_person_stopped_live_ticket_receives_nothing_ever(kanban_env: Path) -> None:
    """Even a live ticket (status='running') with withdrawn mandate receives nothing, ever."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Stopped live ticket", initial_status="running")
        mid = _insert_mandate(conn, ticket_id=tid, status="withdrawn")
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, tid))
        _add_subscription(conn, thread_key="tg:live:stopped", ticket_id=tid, context_only=0)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:live:stopped",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="New instructions",
            idempotency_key="live_stopped_01",
            now=1700004500,
        )

        assert disp == "queued"
        inputs_count = conn.execute(
            "SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ?",
            (tid,),
        ).fetchone()[0]
        assert inputs_count == 0


# ============================================================================
# Additional Edge Cases: Context-only on ended ticket & Reopen API helper
# ============================================================================


def test_ended_ticket_context_only_subscription_never_resumes(kanban_env: Path) -> None:
    """Context-only subscription on ended ticket never resumes."""
    with kbc.connect() as conn:
        tid, mid, old_run_id = _create_ended_ticket(
            conn,
            ticket_id="t_ctx_only",
            assignee="alice",
            mandate_strength="explicit",
        )
        _add_subscription(conn, thread_key="tg:chat:ctx", ticket_id=tid, context_only=1)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:ctx",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="FYI update to thread",
            idempotency_key="msg_ctx_01",
            now=1700005000,
        )

        assert disp == "queued"
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        inputs_count = conn.execute(
            "SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ?",
            (tid,),
        ).fetchone()[0]
        assert inputs_count == 0


def test_reopen_ticket_api_helper(kanban_env: Path) -> None:
    """reopen_ticket and reopen_ended_ticket helpers transition ended ticket to ready."""
    with kbc.connect() as conn:
        tid, _, _ = _create_ended_ticket(conn, ticket_id="t_reopen_api")
        assert kb.get_task(conn, tid).status == "done"

        ok = reopen_ticket(conn, tid)
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.completed_at is None
        assert task.current_run_id is None

        events = kb.list_events(conn, tid)
        assert any(e.kind == "promoted" and (e.payload or {}).get("reason") == "resume" for e in events)


def test_subsequent_person_trigger_after_resume(kanban_env: Path) -> None:
    """After resume reopens ticket to ready, a subsequent person trigger delivers trigger input."""
    spawns: list[Any] = []
    pool = SlotPool(worker_slots=2, reserve_slots=1)

    with kbc.connect() as conn:
        tid, mid, old_run_id = _create_ended_ticket(
            conn,
            ticket_id="t_subsequent",
            assignee="alice",
            mandate_strength="explicit",
        )
        _add_subscription(conn, thread_key="tg:chat:multi_msg", ticket_id=tid, context_only=0)

        # Trigger 1: resumes the ended ticket
        trg_id1, disp1 = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:multi_msg",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Resume message",
            idempotency_key="msg_seq_01",
            now=1700006000,
        )
        assert disp1 == "fanned_out"

        # Trigger 2: arrives while ticket is ready
        trg_id2, disp2 = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:multi_msg",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Follow-up instruction",
            idempotency_key="msg_seq_02",
            now=1700006010,
        )
        assert disp2 == "fanned_out"

        # Check inputs: one 'resume' and one 'trigger'
        inputs = conn.execute(
            "SELECT kind, body, is_person FROM sa_inputs WHERE ticket_id = ? ORDER BY id ASC",
            (tid,),
        ).fetchall()
        assert len(inputs) == 2
        assert inputs[0]["kind"] == "resume"
        assert inputs[0]["body"] == "Resume message"
        assert inputs[1]["kind"] == "trigger"
        assert inputs[1]["body"] == "Follow-up instruction"

        # Dispatcher claims and spawns the reopened ticket
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=pool)
        assert len(res.spawned) == 1
        assert spawns[0].id == tid

        # Both inputs delivered
        assert "Resume message" in spawns[0].run_input
        assert "Follow-up instruction" in spawns[0].run_input

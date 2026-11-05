"""Tests for Super-agent Release-on-end for unread Inputs (Card 3.7).

Verifies:
1. End with one unread Input -> one Eval batch.
2. End with all read -> none.
3. End with read but unacknowledged Input (no run comment/report after delivery) -> one Eval batch.
4. End with read Input acknowledged by comment -> none.
5. End with unread Input owned by another active Ticket -> none.
6. End with unread Input when other owning Ticket is also ended -> one Eval batch.
7. Unread Input already handed back -> none.
8. Bot Input (is_person=0) -> not re-queued.
9. Direct API call to release_on_end(db_path_or_conn, ticket_id).
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import (
    assemble_batch,
    deliver_inputs,
    receive_trigger,
    release_on_end,
    trigger_handback,
)


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
    """Helper to insert an sa_mandates row."""
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
    conn.execute(
        """
        INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (thread_key, ticket_id, int(context_only), int(created_at)),
    )


def _create_active_ticket(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    title: str = "Active ticket",
    assignee: str = "alice",
    status: str = "running",
    now: int = 1000,
) -> str:
    """Create a ticket in running status with a mandate and claim."""
    tid = kb.create_task(conn, title=title, assignee=assignee, initial_status="running")
    mid = _insert_mandate(
        conn,
        ticket_id=tid,
        strength="explicit",
        basis={"source": "telegram", "sender": "alice", "via": "explicit"},
        status="active",
        now=now,
    )
    conn.execute(
        "UPDATE tasks SET id = ?, mandate_id = ?, status = ? WHERE id = ?",
        (ticket_id, mid, status, tid),
    )
    return ticket_id


# ============================================================================
# Card 3.7 Core Test 1: end with one unread Input -> one Eval batch
# ============================================================================


def test_end_with_one_unread_input_creates_one_eval_batch(kanban_env: Path) -> None:
    """When a Ticket ends with one unread person Input, it is re-queued for Eval:

    assemble_batch produces exactly one Eval batch containing the trigger.
    """
    with kbc.connect_closing(kanban_env) as conn:
        tid = _create_active_ticket(conn, "t_unread_1")
        _add_subscription(conn, thread_key="tg:chat:100", ticket_id=tid, context_only=0)

        # Receive person trigger on thread
        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:100",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Please do something urgent",
            idempotency_key="msg_unr_01",
            now=1000,
        )
        assert disp == "fanned_out"

        # Verify sa_inputs has 1 unread row
        inps = conn.execute(
            "SELECT id, read_at, is_person FROM sa_inputs WHERE ticket_id = ?",
            (tid,),
        ).fetchall()
        assert len(inps) == 1
        assert inps[0]["read_at"] is None
        assert inps[0]["is_person"] == 1

        # Trigger is currently eval_state='done' and disposition='fanned_out'
        trg_before = conn.execute(
            "SELECT eval_state, disposition FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert trg_before["eval_state"] == "done"
        assert trg_before["disposition"] == "fanned_out"

        # End ticket via complete_task
        kb.complete_task(conn, tid, result="Done with initial task")
        assert kb.get_task(conn, tid).status == "done"

        # Trigger must now be re-queued for Eval
        trg_after = conn.execute(
            "SELECT eval_state, disposition, meta_json FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert trg_after["eval_state"] == "queued"
        assert trg_after["disposition"] is None

        meta = json.loads(trg_after["meta_json"])
        assert any(r.get("ticket_id") == tid and r.get("reason") == "release_on_end" for r in meta.get("releases", []))

        # assemble_batch yields exactly one Eval batch
        batch = assemble_batch(conn)
        assert batch is not None
        assert batch["thread_key"] == "tg:chat:100"
        assert batch["trigger_ids"] == [trg_id]


# ============================================================================
# Card 3.7 Core Test 2: end with all read -> none
# ============================================================================


def test_end_with_all_read_creates_none(kanban_env: Path) -> None:
    """When a Ticket ends with all inputs delivered (read_at set) and acknowledged by completion,

    no triggers are re-queued for Eval (assemble_batch returns None).
    """
    with kbc.connect_closing(kanban_env) as conn:
        tid = _create_active_ticket(conn, "t_all_read_1")
        _add_subscription(conn, thread_key="tg:chat:200", ticket_id=tid, context_only=0)

        # Receive person trigger on thread
        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:200",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Task instruction for worker",
            idempotency_key="msg_read_01",
            now=1000,
        )
        assert disp == "fanned_out"

        # Worker run reads/delivers the input
        msg, delivered_ids = deliver_inputs(conn, tid, now=1010)
        assert len(delivered_ids) == 1
        assert delivered_ids[0] == 1

        inp = conn.execute("SELECT read_at FROM sa_inputs WHERE id = ?", (delivered_ids[0],)).fetchone()
        assert inp["read_at"] == 1010

        # Complete task after delivery
        kb.complete_task(conn, tid, result="Addressed all instructions")
        assert kb.get_task(conn, tid).status == "done"

        # Trigger must remain done (not re-queued)
        trg_after = conn.execute(
            "SELECT eval_state, disposition FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert trg_after["eval_state"] == "done"
        assert trg_after["disposition"] == "fanned_out"

        # assemble_batch yields None
        batch = assemble_batch(conn)
        assert batch is None


# ============================================================================
# Read but unacknowledged input (e.g. archived without run report/comment)
# ============================================================================


def test_end_with_read_but_unacknowledged_input_requeues_for_eval(kanban_env: Path) -> None:
    """When an input was delivered (read_at is set) but the run never commented or reported

    after delivery (e.g. task archived mid-run), it is unacknowledged and re-queued for Eval.
    """
    with kbc.connect_closing(kanban_env) as conn:
        tid = _create_active_ticket(conn, "t_unack_1")
        _add_subscription(conn, thread_key="tg:chat:300", ticket_id=tid, context_only=0)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:300",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Instruction that worker crashed before answering",
            idempotency_key="msg_unack_01",
            now=1000,
        )
        assert disp == "fanned_out"

        # Delivery happens at t=1010
        deliver_inputs(conn, tid, now=1010)

        # Task is archived mid-run without any worker comment or report
        kb.archive_task(conn, tid)
        assert kb.get_task(conn, tid).status == "archived"

        # Because the run never commented or reported after delivery, input was unacknowledged
        # -> Trigger is re-queued for Eval
        trg_after = conn.execute(
            "SELECT eval_state, disposition FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert trg_after["eval_state"] == "queued"
        assert trg_after["disposition"] is None

        # Yields one Eval batch
        batch = assemble_batch(conn)
        assert batch is not None
        assert batch["trigger_ids"] == [trg_id]


def test_end_with_read_input_acknowledged_by_comment(kanban_env: Path) -> None:
    """When an input was delivered and the worker commented after delivery,

    it is acknowledged and not re-queued even if the task is later archived.
    """
    with kbc.connect_closing(kanban_env) as conn:
        tid = _create_active_ticket(conn, "t_ack_comment")
        _add_subscription(conn, thread_key="tg:chat:400", ticket_id=tid, context_only=0)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:400",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Instruction to check",
            idempotency_key="msg_ack_c_01",
            now=1000,
        )
        assert disp == "fanned_out"

        deliver_inputs(conn, tid, now=1010)

        # Worker comments at t=1020
        kb.add_comment(conn, tid, author="alice", body="Acknowledged and recorded")

        # Task is archived later
        kb.archive_task(conn, tid)

        # Trigger remains done
        trg_after = conn.execute(
            "SELECT eval_state, disposition FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert trg_after["eval_state"] == "done"

        batch = assemble_batch(conn)
        assert batch is None


# ============================================================================
# Multi-owner coordination
# ============================================================================


def test_unread_input_with_another_active_owner_not_requeued(kanban_env: Path) -> None:
    """When a Ticket ends with an unread input, but another Ticket still owns the trigger,

    the trigger is NOT re-queued for Eval.
    """
    with kbc.connect_closing(kanban_env) as conn:
        tid1 = _create_active_ticket(conn, "t_multi_1")
        tid2 = _create_active_ticket(conn, "t_multi_2", status="ready")

        # Both subscribe to thread
        _add_subscription(conn, thread_key="tg:chat:multi_own", ticket_id=tid1, context_only=0)
        _add_subscription(conn, thread_key="tg:chat:multi_own", ticket_id=tid2, context_only=0)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:multi_own",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Message to shared thread",
            idempotency_key="msg_m_01",
            now=1000,
        )
        assert disp == "fanned_out"

        # Ticket 1 ends with unread input
        kb.complete_task(conn, tid1, result="T1 done")

        # Ticket 2 is still active ('ready') -> owns the trigger -> NOT re-queued
        trg_after = conn.execute(
            "SELECT eval_state FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert trg_after["eval_state"] == "done"

        batch = assemble_batch(conn)
        assert batch is None


def test_unread_input_requeued_when_all_owners_ended(kanban_env: Path) -> None:
    """When both owning Tickets end with unread inputs, the trigger is re-queued once."""
    with kbc.connect_closing(kanban_env) as conn:
        tid1 = _create_active_ticket(conn, "t_both_1")
        tid2 = _create_active_ticket(conn, "t_both_2")

        _add_subscription(conn, thread_key="tg:chat:both_end", ticket_id=tid1, context_only=0)
        _add_subscription(conn, thread_key="tg:chat:both_end", ticket_id=tid2, context_only=0)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:both_end",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Message to both",
            idempotency_key="msg_b_01",
            now=1000,
        )
        assert disp == "fanned_out"

        # Ticket 1 ends
        kb.complete_task(conn, tid1, result="T1 finished")
        # Still not queued because Ticket 2 is running
        trg_mid = conn.execute("SELECT eval_state FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_mid["eval_state"] == "done"

        # Now Ticket 2 ends
        kb.complete_task(conn, tid2, result="T2 finished")

        # Now no active owner remains -> re-queued
        trg_after = conn.execute(
            "SELECT eval_state, disposition FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert trg_after["eval_state"] == "queued"
        assert trg_after["disposition"] is None

        batch = assemble_batch(conn)
        assert batch is not None
        assert batch["trigger_ids"] == [trg_id]


# ============================================================================
# Handback and Bot exclusion
# ============================================================================


def test_unread_input_already_handed_back_not_requeued(kanban_env: Path) -> None:
    """If a Ticket already handed back the trigger, ending the Ticket does not re-process it."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = _create_active_ticket(conn, "t_hb_end")
        _add_subscription(conn, thread_key="tg:chat:hb_end", ticket_id=tid, context_only=0)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:hb_end",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Out of scope message",
            idempotency_key="msg_hb_01",
            now=1000,
        )
        assert disp == "fanned_out"

        # Hand back the trigger
        hb_res = trigger_handback(conn, ticket_id=tid, trigger_id=trg_id, reason="Not my scope", now=1020)
        assert hb_res["ok"] is True
        assert hb_res["queued_for_eval"] is True

        # Consume the eval batch produced by handback
        batch1 = assemble_batch(conn)
        assert batch1 is not None

        # Now ticket ends
        kb.complete_task(conn, tid, result="Completed task")

        # No additional eval batch is created
        batch2 = assemble_batch(conn)
        assert batch2 is None


def test_bot_input_not_requeued_on_end(kanban_env: Path) -> None:
    """Bot inputs (is_person=0) are never re-queued for Eval on ticket end."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = _create_active_ticket(conn, "t_bot_end")
        _add_subscription(conn, thread_key="tg:chat:bot_end", ticket_id=tid, context_only=0)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:bot_end",
            actor_source_id="ci_bot",
            actor_is_bot=True,
            addressed=False,
            kind="ci_status",
            body="CI build passed",
            idempotency_key="bot_ci_01",
            now=1000,
        )
        # Bot trigger starts in queued; consume it and mark it done (as if evaluated or ignored)
        conn.execute("UPDATE sa_triggers SET eval_state = 'done', disposition = 'ignore' WHERE id = ?", (trg_id,))

        # Verify input has is_person=0
        inps = conn.execute("SELECT id, is_person, read_at FROM sa_inputs WHERE ticket_id = ?", (tid,)).fetchall()
        assert len(inps) == 1
        assert inps[0]["is_person"] == 0
        assert inps[0]["read_at"] is None

        # End ticket with the bot input still unread
        kb.complete_task(conn, tid, result="CI verified")

        # Bot trigger is NOT re-queued on ticket end
        trg_after = conn.execute("SELECT eval_state, disposition FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_after["eval_state"] == "done"
        assert trg_after["disposition"] == "ignore"

        batch = assemble_batch(conn)
        assert batch is None


# ============================================================================
# Direct release_on_end API
# ============================================================================


def test_direct_release_on_end_api(kanban_env: Path) -> None:
    """Direct invocation of release_on_end returns the list of re-queued trigger ids."""
    with kbc.connect_closing(kanban_env) as conn:
        tid = _create_active_ticket(conn, "t_api_direct")
        _add_subscription(conn, thread_key="tg:chat:api_dir", ticket_id=tid, context_only=0)

        trg_id, disp = receive_trigger(
            conn,
            source="telegram",
            scope="main",
            thread_key="tg:chat:api_dir",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Direct API test",
            idempotency_key="msg_api_01",
            now=1000,
        )
        assert disp == "fanned_out"

        requeued = release_on_end(conn, tid, now=1050)
        assert requeued == [trg_id]

        # Second call is idempotent
        requeued_second = release_on_end(kanban_env, tid, now=1060)
        assert requeued_second == []

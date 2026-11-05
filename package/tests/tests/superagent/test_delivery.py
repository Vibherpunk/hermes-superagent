"""Tests for Super-agent Input delivery and wake coordination (Card 3.3).

Verifies:
- Trigger card rendering (source, actor, body).
- Unread Inputs rendered as one message.
- Bodies >2,000 chars: full body delivered when no read tool exists (recorded as observation),
  pointer format when force_pointer=True.
- Delivery happens once per Input (marks read_at as integer epoch timestamp).
- Fake run harness receives rendered message on claimed.run_input and claimed.delivered_inputs.
- A second tick does not redeliver read Inputs.
- Delivery to a slot-full dispatcher waits.
- Person Inputs may use the human reserve slot (Phase 1 reserve-slot rule).
- When all slots (including reserve) are full, person Inputs wait.
- Unread inputs promote inactive/non-queued tasks to ready.
- Wakes take priority over new launches in tick ordering.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
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
from hermes_cli.superagent_delivery import (
    BODY_TRUNCATE_LIMIT,
    deliver_inputs,
    fetch_unread_inputs,
    get_ticket_wake_kind,
    has_input_read_tool,
    mark_inputs_read,
    render_inputs_message,
    render_trigger_card,
    request_launch_for_unread_inputs,
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


def _insert_trigger(
    conn: sqlite3.Connection,
    *,
    trigger_id: str,
    source: str = "telegram",
    scope: str = "default",
    thread_key: str = "tg:chat:100",
    actor_source_id: str = "user_42",
    kind: str = "message",
    idempotency_key: str | None = None,
    created_at: int = 1000,
) -> None:
    """Helper to insert an sa_triggers row."""
    conn.execute(
        """
        INSERT INTO sa_triggers (id, source, scope, thread_key, actor_source_id, kind, idempotency_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (trigger_id, source, scope, thread_key, actor_source_id, kind, idempotency_key or trigger_id, created_at),
    )


def _insert_input(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    trigger_id: str | None = None,
    kind: str = "trigger",
    body: str = "Test input body",
    is_person: int = 0,
    context_only: int = 0,
    read_at: int | None = None,
    created_at: int = 1000,
) -> int:
    """Helper to insert an sa_inputs row."""
    cur = conn.execute(
        """
        INSERT INTO sa_inputs (ticket_id, trigger_id, kind, body, is_person, context_only, read_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, trigger_id, kind, body, is_person, context_only, read_at, created_at),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Unit tests: Card rendering and truncation
# ---------------------------------------------------------------------------


def test_render_trigger_card_formatting() -> None:
    """Trigger cards format source, actor, and body."""
    inp = {
        "id": 1,
        "kind": "trigger",
        "body": "Fix the bug in auth",
        "is_person": 1,
        "source": "telegram",
        "actor_source_id": "alice",
    }
    rendered = render_trigger_card(inp)
    assert "[Trigger Card: id=1]" in rendered
    assert "Source: telegram" in rendered
    assert "Actor: alice" in rendered
    assert "Body:\nFix the bug in auth" in rendered


def test_render_trigger_card_defaults() -> None:
    """Trigger cards fall back to kind and person/system actor when unjoined."""
    inp_person = {"id": 2, "kind": "note", "body": "A note", "is_person": 1}
    r1 = render_trigger_card(inp_person)
    assert "Source: note" in r1
    assert "Actor: person" in r1

    inp_sys = {"id": 3, "kind": "trigger", "body": "CI failed", "is_person": 0}
    r2 = render_trigger_card(inp_sys)
    assert "Source: unknown" in r2
    assert "Actor: system" in r2


def test_render_inputs_message_combines_cards() -> None:
    """Multiple unread inputs render as one message separated by dividers."""
    inputs = [
        {"id": 1, "kind": "trigger", "body": "First", "is_person": 1, "source": "github", "actor_source_id": "octocat"},
        {"id": 2, "kind": "note", "body": "Second", "is_person": 0, "source": "cron", "actor_source_id": "system"},
    ]
    combined = render_inputs_message(inputs)
    assert "[Trigger Card: id=1]" in combined
    assert "[Trigger Card: id=2]" in combined
    assert "\n\n---\n\n" in combined


def test_body_truncation_and_deviation_observation() -> None:
    """When body > 2000 chars: full body delivered when no read tool exists (recorded as observation),
    pointer format when force_pointer=True.
    """
    assert has_input_read_tool() is False

    long_body = "x" * 2500
    inp = {"id": 42, "kind": "trigger", "body": long_body, "is_person": 1, "source": "chat", "actor_source_id": "bob"}

    # Default: includes full body (deviation recorded as observation)
    card_full = render_trigger_card(inp)
    assert len(card_full) > 2500
    assert long_body in card_full
    assert "[pointer:" not in card_full

    # With force_pointer=True: truncated with sa_inputs.id reference pointer
    card_ptr = render_trigger_card(inp, force_pointer=True)
    assert "[pointer: sa_inputs.id=42]" in card_ptr
    assert len(card_ptr) < 2500
    assert "x" * BODY_TRUNCATE_LIMIT in card_ptr


# ---------------------------------------------------------------------------
# Database level tests: fetch, deliver, mark read
# ---------------------------------------------------------------------------


def test_fetch_and_mark_read_inputs(kanban_env: Path) -> None:
    """fetch_unread_inputs queries unread inputs in order, mark_inputs_read updates read_at."""
    with kbc.connect() as conn:
        _insert_trigger(conn, trigger_id="trg_1", source="github", actor_source_id="octocat")
        id1 = _insert_input(conn, ticket_id="t1", trigger_id="trg_1", body="msg 1", created_at=100)
        id2 = _insert_input(conn, ticket_id="t1", trigger_id=None, body="msg 2", created_at=200)
        _insert_input(conn, ticket_id="t1", trigger_id=None, body="msg 3", read_at=500, created_at=50)

        unread = fetch_unread_inputs(conn, "t1")
        assert len(unread) == 2
        assert [u["id"] for u in unread] == [id1, id2]
        assert unread[0]["source"] == "github"
        assert unread[0]["actor_source_id"] == "octocat"

        msg, delivered = deliver_inputs(conn, "t1", now=1500)
        assert delivered == [id1, id2]
        assert msg is not None
        assert "msg 1" in msg and "msg 2" in msg

        # After delivery, sa_inputs.read_at is set
        rows = conn.execute("SELECT id, read_at FROM sa_inputs WHERE id IN (?, ?)", (id1, id2)).fetchall()
        for r in rows:
            assert r["read_at"] == 1500

        # Second deliver returns nothing
        msg2, delivered2 = deliver_inputs(conn, "t1")
        assert msg2 is None
        assert delivered2 == []


# ---------------------------------------------------------------------------
# Fake run harness tests: Dispatcher delivery integration
# ---------------------------------------------------------------------------


def test_delivery_happens_once_per_input(kanban_env: Path) -> None:
    """When a task launches with unread Inputs, they render as one message on the run's input and mark read_at."""
    spawns: list[Any] = []
    with kbc.connect() as conn:
        t1 = kb.create_task(conn, title="task-1", assignee="alice")
        _insert_trigger(conn, trigger_id="trg_10", source="slack", actor_source_id="carol")
        inp_id = _insert_input(conn, ticket_id=t1, trigger_id="trg_10", body="Please fix auth", is_person=1)

        res = kbd.dispatch_once(
            conn,
            spawn_fn=_fake_spawn_factory(spawns),
            slot_pool=None,
        )
        assert len(res.spawned) == 1
        assert len(spawns) == 1
        spawned_task = spawns[0]
        assert spawned_task.id == t1

        # Rendered inputs available on run_input and delivered_inputs
        assert spawned_task.run_input is not None
        assert "[Trigger Card: id=" in spawned_task.run_input
        assert "Source: slack" in spawned_task.run_input
        assert "Actor: carol" in spawned_task.run_input
        assert "Please fix auth" in spawned_task.run_input
        assert spawned_task.delivered_inputs == spawned_task.run_input

        # sa_inputs.read_at is marked with integer epoch timestamp
        inp_row = conn.execute("SELECT read_at FROM sa_inputs WHERE id = ?", (inp_id,)).fetchone()
        assert inp_row["read_at"] is not None
        assert isinstance(inp_row["read_at"], int)

        # Comment recorded from dispatcher:inbound
        comments = kb.list_comments(conn, t1)
        inbound = [c for c in comments if c.author == "dispatcher:inbound"]
        assert len(inbound) == 1
        assert inbound[0].body == spawned_task.run_input

        # Event recorded: input_delivered
        events = kb.list_events(conn, t1)
        deliv_events = [e for e in events if e.kind == "input_delivered"]
        assert len(deliv_events) == 1
        assert deliv_events[0].payload == {"input_ids": [inp_id], "count": 1}


def test_second_tick_does_not_redeliver_read_inputs(kanban_env: Path) -> None:
    """A second tick does not redeliver already-read inputs."""
    spawns: list[Any] = []
    with kbc.connect() as conn:
        t1 = kb.create_task(conn, title="task-1", assignee="alice")
        inp_id = _insert_input(conn, ticket_id=t1, body="First input", is_person=1)

        # Tick 1: delivers input
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=None)
        assert len(spawns) == 1
        assert spawns[0].run_input is not None
        assert "First input" in spawns[0].run_input

        # Complete task 1 so its claim is released
        kb.complete_task(conn, t1, force=True, result="Finished first run")

        # Now create a second task t2 with no unread inputs
        t2 = kb.create_task(conn, title="task-2", assignee="alice")
        spawns.clear()

        # Tick 2: t2 spawns with no unread inputs
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=None)
        assert len(spawns) == 1
        assert spawns[0].id == t2
        assert getattr(spawns[0], "run_input", None) is None

        # Verify t1's input was not redelivered and read_at remains set
        inp_row = conn.execute("SELECT read_at FROM sa_inputs WHERE id = ?", (inp_id,)).fetchone()
        assert inp_row["read_at"] is not None

        # Complete t2
        kb.complete_task(conn, t2, force=True, result="Finished second run")

        # Now create t3 with a new unread input
        t3 = kb.create_task(conn, title="task-3", assignee="alice")
        _insert_input(conn, ticket_id=t3, body="Second input arrived", is_person=0)

        spawns.clear()
        # Tick 3: delivers ONLY the new input for t3
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=None)
        assert len(spawns) == 1
        assert spawns[0].id == t3
        assert spawns[0].run_input is not None
        assert "Second input arrived" in spawns[0].run_input
        assert "First input" not in spawns[0].run_input


# ---------------------------------------------------------------------------
# Slot controller tests: slot-full wait and person reserve slot
# ---------------------------------------------------------------------------


def test_delivery_to_slot_full_dispatcher_waits(kanban_env: Path) -> None:
    """When worker slots are full, a non-person launch or wake waits in skipped_slot_wait."""
    pool = SlotPool(worker_slots=1, reserve_slots=1)
    spawns: list[Any] = []

    with kbc.connect() as conn:
        t1 = kb.create_task(conn, title="task-1", assignee="alice")
        # Tick 1: t1 spawns and consumes worker slot
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=pool)
        assert len(spawns) == 1
        assert spawns[0].id == t1
        assert pool.in_use() == 1

        # Now t2 arrives with a non-person input (system trigger)
        t2 = kb.create_task(conn, title="task-2", assignee="alice")
        inp2 = _insert_input(conn, ticket_id=t2, body="System alert", is_person=0)

        spawns.clear()
        res = kbd.dispatch_once(
            conn,
            spawn_fn=_fake_spawn_factory(spawns),
            slot_pool=pool,
        )

        # t2 waits because worker_slots=1 is full and system wake cannot use reserve slot
        assert len(spawns) == 0
        assert res.skipped_slot_wait == [t2]
        t2_events = [e.kind for e in kb.list_events(conn, t2)]
        assert "slot_wait" in t2_events

        # Input for t2 is NOT delivered while waiting
        row = conn.execute("SELECT read_at FROM sa_inputs WHERE id = ?", (inp2,)).fetchone()
        assert row["read_at"] is None


def test_person_input_uses_reserve_slot(kanban_env: Path) -> None:
    """When worker slots are full, a person Input taps the human reserve slot and spawns."""
    pool = SlotPool(worker_slots=1, reserve_slots=1)
    spawns: list[Any] = []

    with kbc.connect() as conn:
        t1 = kb.create_task(conn, title="task-1", assignee="alice")
        # First dispatch: t1 spawns using worker slot
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=pool)
        assert len(spawns) == 1
        assert spawns[0].id == t1
        assert pool.in_use() == 1

        # Now t2 arrives with a person input
        t2 = kb.create_task(conn, title="task-2", assignee="alice")
        inp2 = _insert_input(conn, ticket_id=t2, body="Human message", is_person=1)

        spawns.clear()
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=pool)
        assert len(res.spawned) == 1

        # t2 spawns because person input acquires the reserve slot!
        assert len(spawns) == 1
        assert spawns[0].id == t2
        assert pool.in_use() == 2

        # t2 input was delivered and marked read_at
        assert spawns[0].run_input is not None
        assert "Human message" in spawns[0].run_input
        row = conn.execute("SELECT read_at FROM sa_inputs WHERE id = ?", (inp2,)).fetchone()
        assert row["read_at"] is not None


def test_slot_full_both_worker_and_reserve_person_input_waits(kanban_env: Path) -> None:
    """When both worker and reserve slots are full (capacity exhausted), person Input waits."""
    pool = SlotPool(worker_slots=1, reserve_slots=1)
    spawns: list[Any] = []

    with kbc.connect() as conn:
        # t1 created and launched (takes worker slot)
        t1 = kb.create_task(conn, title="task-1", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=pool)
        assert len(spawns) == 1
        assert spawns[0].id == t1
        assert pool.in_use() == 1

        # t2 created with person input and launched (takes reserve slot)
        t2 = kb.create_task(conn, title="task-2", assignee="alice")
        _insert_input(conn, ticket_id=t2, body="Human 1", is_person=1)
        spawns.clear()
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=pool)
        assert len(spawns) == 1
        assert spawns[0].id == t2
        assert pool.in_use() == 2

        # t3 created with person input: total capacity (2) is full
        t3 = kb.create_task(conn, title="task-3", assignee="alice")
        inp3 = _insert_input(conn, ticket_id=t3, body="Human 2", is_person=1)

        spawns.clear()
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=pool)
        assert t3 in res.skipped_slot_wait
        assert len(spawns) == 0

        # t3 input remains unread
        row = conn.execute("SELECT read_at FROM sa_inputs WHERE id = ?", (inp3,)).fetchone()
        assert row["read_at"] is None


# ---------------------------------------------------------------------------
# Wake promotion and priority tests
# ---------------------------------------------------------------------------


def test_unread_input_promotes_inactive_ticket(kanban_env: Path) -> None:
    """When no run is active or queued, delivery requests normal launch by promoting ticket to ready."""
    spawns: list[Any] = []
    with kbc.connect() as conn:
        t1 = kb.create_task(conn, title="inactive-task", assignee="alice")
        kb.block_task(conn, t1, reason="waiting on input")
        assert kb.get_task(conn, t1).status == "blocked"

        # Unread input arrives for t1
        inp_id = _insert_input(conn, ticket_id=t1, body="Wake up inactive task", is_person=1)

        # Dispatch tick runs
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=None)
        assert len(res.spawned) == 1

        # t1 was promoted and spawned!
        assert len(spawns) == 1
        assert spawns[0].id == t1
        assert spawns[0].run_input is not None
        assert "Wake up inactive task" in spawns[0].run_input

        # Check 'promoted' event was recorded
        events = kb.list_events(conn, t1)
        promoted_events = [e for e in events if e.kind == "promoted"]
        assert len(promoted_events) == 1
        assert promoted_events[0].payload == {"reason": "inbound_input", "status": "ready"}

        # sa_inputs.read_at is marked
        row = conn.execute("SELECT read_at FROM sa_inputs WHERE id = ?", (inp_id,)).fetchone()
        assert row["read_at"] is not None


def test_wake_priority_over_new_launches(kanban_env: Path) -> None:
    """Person wake takes priority over system wake, which takes priority over new launch."""
    spawns: list[Any] = []
    with kbc.connect() as conn:
        # Create 3 ready tasks
        t_new = kb.create_task(conn, title="new-launch", assignee="alice")
        t_sys = kb.create_task(conn, title="sys-wake", assignee="alice")
        t_per = kb.create_task(conn, title="person-wake", assignee="alice")

        _insert_input(conn, ticket_id=t_sys, body="system wake", is_person=0)
        _insert_input(conn, ticket_id=t_per, body="person wake", is_person=1)

        # Tick with max_spawn=1 -> must pick t_per first
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=None, max_spawn=1)
        assert len(spawns) == 1
        assert spawns[0].id == t_per

        # Complete t_per
        kb.complete_task(conn, t_per, force=True, result="done")

        # Second tick with max_spawn=1 -> must pick t_sys second
        spawns.clear()
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=None, max_spawn=1)
        assert len(spawns) == 1
        assert spawns[0].id == t_sys

        # Complete t_sys
        kb.complete_task(conn, t_sys, force=True, result="done")

        # Third tick with max_spawn=1 -> picks t_new
        spawns.clear()
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), slot_pool=None, max_spawn=1)
        assert len(spawns) == 1
        assert spawns[0].id == t_new

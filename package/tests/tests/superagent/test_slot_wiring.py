"""Tests for SlotPool dispatcher integration per E-023 and Card 1.6."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from gateway.kanban_watchers_dispatcher import (
    SlotPool,
    _KanbanDispatcher,
    _resolve_dispatcher_settings,
    get_slot_pool,
    reset_slot_pool,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import profiles


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


def _fake_spawn_factory(spawns: list[str]):
    """Return a fake spawn function that records task ids and returns synthetic pids."""
    def fake_spawn(task: Any, workspace: str, board: str | None = None) -> int:
        spawns.append(task.id)
        return 1000 + len(spawns)
    return fake_spawn


def _spawned_ids(res: kbd.DispatchResult) -> list[str]:
    """Extract task IDs from DispatchResult.spawned."""
    return [t[0] if isinstance(t, tuple) else t for t in res.spawned]


class TestSlotWiring:
    """Tests for SlotPool integration into dispatch ticks."""

    def test_slots_disabled_no_slot_wait_events(self, kanban_env: Path) -> None:
        """When superagent is disabled, behavior is identical to today (no slot_wait events)."""
        spawns: list[str] = []
        with kbc.connect() as conn:
            t1 = kb.create_task(conn, title="task-1", assignee="alice")
            t2 = kb.create_task(conn, title="task-2", assignee="alice")

            # dispatch_once with slot_pool=None (disabled)
            res = kbd.dispatch_once(
                conn,
                spawn_fn=_fake_spawn_factory(spawns),
                slot_pool=None,
            )

            spawned = _spawned_ids(res)
            assert t1 in spawned
            assert t2 in spawned
            assert not res.skipped_slot_wait

            # Neither task should have a slot_wait event in task_events
            t1_events = [e.kind for e in kb.list_events(conn, t1)]
            t2_events = [e.kind for e in kb.list_events(conn, t2)]
            assert "slot_wait" not in t1_events
            assert "slot_wait" not in t2_events

    def test_slots_enabled_worker_slots_1_second_launch_waits(self, kanban_env: Path) -> None:
        """When worker_slots=1, second concurrent launch waits with a slot_wait event."""
        pool = SlotPool(worker_slots=1, reserve_slots=1)
        spawns: list[str] = []

        with kbc.connect() as conn:
            t1 = kb.create_task(conn, title="task-1", assignee="alice")
            t2 = kb.create_task(conn, title="task-2", assignee="alice")

            res = kbd.dispatch_once(
                conn,
                spawn_fn=_fake_spawn_factory(spawns),
                slot_pool=pool,
            )

            spawned = _spawned_ids(res)
            # Exactly one task should spawn, and the other should wait
            assert len(spawned) == 1
            assert len(res.skipped_slot_wait) == 1
            spawned_id = spawned[0]
            waiting_id = res.skipped_slot_wait[0]

            assert spawned_id == t1
            assert waiting_id == t2

            # Spawned task is running; waiting task remains ready
            t1_task = kb.get_task(conn, t1)
            t2_task = kb.get_task(conn, t2)
            assert t1_task is not None and t1_task.status == "running"
            assert t2_task is not None and t2_task.status == "ready"

            # Waiting task has a slot_wait event
            t1_events = [e.kind for e in kb.list_events(conn, t1)]
            t2_events = kb.list_events(conn, t2)
            assert "slot_wait" not in t1_events

            slot_wait_events = [e for e in t2_events if e.kind == "slot_wait"]
            assert len(slot_wait_events) == 1
            assert slot_wait_events[0].payload == {"lane": "ready", "kind": "launch"}

            # SlotPool in_use reflects the single active worker
            assert pool.in_use() == 1

    def test_completion_releases_and_waiting_task_spawns_next_tick(self, kanban_env: Path) -> None:
        """Completing a running task releases its slot token so the waiting task spawns next tick."""
        pool = SlotPool(worker_slots=1, reserve_slots=1)
        spawns: list[str] = []

        with kbc.connect() as conn:
            t1 = kb.create_task(conn, title="task-1", assignee="alice")
            t2 = kb.create_task(conn, title="task-2", assignee="alice")

            # Tick 1: t1 spawns, t2 waits
            res1 = kbd.dispatch_once(
                conn,
                spawn_fn=_fake_spawn_factory(spawns),
                slot_pool=pool,
            )
            spawned1 = _spawned_ids(res1)
            assert spawned1 == [t1]
            assert res1.skipped_slot_wait == [t2]
            assert pool.in_use() == 1

            # Complete task 1
            completed = kb.complete_task(conn, t1, force=True, result="Job done")
            assert completed is True
            t1_task = kb.get_task(conn, t1)
            assert t1_task is not None and t1_task.status == "done"

            # Tick 2: reclaim phase releases t1's slot; t2 acquires slot and spawns
            res2 = kbd.dispatch_once(
                conn,
                spawn_fn=_fake_spawn_factory(spawns),
                slot_pool=pool,
            )
            assert t1 in res2.released_slots
            spawned2 = _spawned_ids(res2)
            assert spawned2 == [t2]
            assert not res2.skipped_slot_wait

            # Task 2 is now running
            t2_task = kb.get_task(conn, t2)
            assert t2_task is not None and t2_task.status == "running"
            assert pool.in_use() == 1

    def test_slot_pool_lifecycle_and_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_slot_pool respects superagent.enabled and sizes from superagent_settings."""
        reset_slot_pool(None)

        # Disabled config returns None
        pool_disabled = get_slot_pool(reload=True, config={"superagent": {"enabled": False}})
        assert pool_disabled is None

        # Enabled config constructs SlotPool with configured slots (reserve slots must be 0 or 1)
        cfg = {
            "superagent": {
                "enabled": True,
                "worker_slots": 5,
                "human_reserve_slots": 1,
            }
        }
        pool_enabled = get_slot_pool(reload=True, config=cfg)
        assert pool_enabled is not None
        assert pool_enabled.worker_slots == 5
        assert pool_enabled.reserve_slots == 1
        assert pool_enabled.capacity == 6

        # Singleton reuse
        assert get_slot_pool() is pool_enabled

        # Clean reset
        reset_slot_pool(None)
        assert get_slot_pool(reload=True, config={"superagent": {"enabled": False}}) is None

    def test_dispatcher_tick_once_passes_slot_pool(
        self, kanban_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_KanbanDispatcher.tick_once_for_board passes slot_pool to dispatch_once."""
        pool = SlotPool(worker_slots=2, reserve_slots=1)
        passed_kwargs: dict[str, Any] = {}

        def mock_dispatch_once(conn: sqlite3.Connection, **kwargs: Any) -> kbd.DispatchResult:
            passed_kwargs.update(kwargs)
            return kbd.DispatchResult()

        monkeypatch.setattr(kbd, "dispatch_once", mock_dispatch_once)

        settings = _resolve_dispatcher_settings({}, kb)
        dispatcher = _KanbanDispatcher(kb, settings, slot_pool=pool)

        dispatcher.tick_once_for_board("")

        assert passed_kwargs.get("slot_pool") is pool

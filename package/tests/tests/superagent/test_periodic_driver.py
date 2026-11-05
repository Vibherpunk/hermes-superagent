"""Unit tests for periodic Superagent driver (INT-2).

Verifies:
1. run_superagent_tick with enabled=false touches nothing (no eval expiry, no polls).
2. run_superagent_tick with enabled=true asserts expire_evals and enabled polls are each called once.
3. Throttle test: two calls 10s apart -> second is a no-op; call at +61s runs.
4. Source config gating for GitHub and VibeHard.
5. Graceful handling of missing VibeHard module.
6. Watcher loop integration in gateway/kanban_watchers.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import reset_tick_throttle, run_superagent_tick


@pytest.fixture
def kanban_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an initialized kanban db."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    return db_path


def test_run_superagent_tick_disabled_touches_nothing(kanban_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When superagent.enabled is false, run_superagent_tick touches nothing."""
    reset_tick_throttle()

    spy_expire = MagicMock()
    spy_gh = MagicMock()
    spy_vh = MagicMock()

    monkeypatch.setattr("hermes_cli.orchestrator.expire_evals", spy_expire)
    monkeypatch.setattr("hermes_cli.superagent_github.poll_github", spy_gh)
    monkeypatch.setattr("hermes_cli.superagent_vibehard.poll_vibehard", spy_vh)

    cfg = {
        "superagent": {
            "enabled": False,
            "github": {"repository": "owner/repo", "enabled": True},
            "vibehard": {"enabled": True},
        }
    }

    res = run_superagent_tick(kanban_env, cfg, now=1000.0)
    assert res == {"status": "disabled"}

    spy_expire.assert_not_called()
    spy_gh.assert_not_called()
    spy_vh.assert_not_called()


def test_run_superagent_tick_enabled_calls_expire_and_polls(kanban_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When superagent.enabled is true, expire_evals and enabled polls are called once."""
    reset_tick_throttle()

    spy_expire = MagicMock(return_value=[{"trigger_id": "trg_1", "action": "expired"}])
    spy_gh = MagicMock(return_value=3)
    spy_vh = MagicMock(return_value=["trg_vh_1"])

    monkeypatch.setattr("hermes_cli.orchestrator.expire_evals", spy_expire)
    monkeypatch.setattr("hermes_cli.superagent_github.poll_github", spy_gh)
    monkeypatch.setattr("hermes_cli.superagent_vibehard.poll_vibehard", spy_vh)

    cfg = {
        "superagent": {
            "enabled": True,
            "github": {"repository": "owner/repo", "enabled": True},
            "vibehard": {"enabled": True},
        }
    }

    res = run_superagent_tick(kanban_env, cfg, now=1000.0)
    assert res["status"] == "ok"
    assert res["expired_evals"] == [{"trigger_id": "trg_1", "action": "expired"}]
    assert res["github_triggers"] == 3
    assert res["vibehard_triggers"] == ["trg_vh_1"]

    spy_expire.assert_called_once()
    spy_gh.assert_called_once()
    spy_vh.assert_called_once()


def test_run_superagent_tick_throttle_60s(kanban_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls 10s apart -> second is a no-op; call at +61s runs."""
    reset_tick_throttle()

    spy_expire = MagicMock(return_value=[])
    spy_gh = MagicMock(return_value=0)
    spy_vh = MagicMock(return_value=[])

    monkeypatch.setattr("hermes_cli.orchestrator.expire_evals", spy_expire)
    monkeypatch.setattr("hermes_cli.superagent_github.poll_github", spy_gh)
    monkeypatch.setattr("hermes_cli.superagent_vibehard.poll_vibehard", spy_vh)

    cfg = {
        "superagent": {
            "enabled": True,
            "github": {"repository": "owner/repo", "enabled": True},
            "vibehard": {"enabled": True},
        }
    }

    # Call 1 at now = 1000.0: runs
    res1 = run_superagent_tick(kanban_env, cfg, now=1000.0)
    assert res1["status"] == "ok"
    assert spy_expire.call_count == 1
    assert spy_gh.call_count == 1
    assert spy_vh.call_count == 1

    # Call 2 at now = 1010.0 (10s later): throttled / no-op
    res2 = run_superagent_tick(kanban_env, cfg, now=1010.0)
    assert res2 == {"status": "throttled"}
    assert spy_expire.call_count == 1
    assert spy_gh.call_count == 1
    assert spy_vh.call_count == 1

    # Call 3 at now = 1061.0 (61s after Call 1): runs
    res3 = run_superagent_tick(kanban_env, cfg, now=1061.0)
    assert res3["status"] == "ok"
    assert spy_expire.call_count == 2
    assert spy_gh.call_count == 2
    assert spy_vh.call_count == 2


def test_run_superagent_tick_selective_source_enabling(kanban_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub and VibeHard are only polled when enabled in their respective source blocks."""
    reset_tick_throttle()

    spy_expire = MagicMock(return_value=[])
    spy_gh = MagicMock(return_value=0)
    spy_vh = MagicMock(return_value=[])

    monkeypatch.setattr("hermes_cli.orchestrator.expire_evals", spy_expire)
    monkeypatch.setattr("hermes_cli.superagent_github.poll_github", spy_gh)
    monkeypatch.setattr("hermes_cli.superagent_vibehard.poll_vibehard", spy_vh)

    # VibeHard disabled, GitHub without repository
    cfg = {
        "superagent": {
            "enabled": True,
            "github": {"enabled": True},  # Missing repository
            "vibehard": {"enabled": False},
        }
    }

    res = run_superagent_tick(kanban_env, cfg, now=1000.0)
    assert res["status"] == "ok"
    spy_expire.assert_called_once()
    spy_gh.assert_not_called()
    spy_vh.assert_not_called()


def test_run_superagent_tick_vibehard_missing_import_handled(kanban_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If superagent_vibehard is absent/unimportable, it is caught and does not crash."""
    reset_tick_throttle()

    spy_expire = MagicMock(return_value=[])
    monkeypatch.setattr("hermes_cli.orchestrator.expire_evals", spy_expire)

    import sys
    monkeypatch.setitem(sys.modules, "hermes_cli.superagent_vibehard", None)
    import hermes_cli
    monkeypatch.delattr(hermes_cli, "superagent_vibehard", raising=False)

    cfg = {
        "superagent": {
            "enabled": True,
            "vibehard": {"enabled": True},
        }
    }

    res = run_superagent_tick(kanban_env, cfg, now=1000.0)
    assert res["status"] == "ok"
    spy_expire.assert_called_once()
    assert res["vibehard_triggers"] == []


class DummyWatcherRunner(GatewayKanbanWatchersMixin):
    """Test runner implementing GatewayKanbanWatchersMixin."""

    def __init__(self):
        self._running = True
        self.adapters = {}
        self._kanban_sub_fail_counts = {}

    def _active_profile_name(self) -> str:
        return "default"


def test_watcher_loop_superagent_disabled_skips_step(kanban_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In _kanban_notifier_watcher with superagent.enabled=false, drain and tick are skipped."""
    runner = DummyWatcherRunner()

    drain_called = False
    tick_called = False

    def fake_drain(*args, **kwargs):
        nonlocal drain_called
        drain_called = True
        return {}

    def fake_tick(*args, **kwargs):
        nonlocal tick_called
        tick_called = True
        return {}

    runner._drain_kanban_outbox = fake_drain
    monkeypatch.setattr("hermes_cli.orchestrator.run_superagent_tick", fake_tick)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"superagent": {"enabled": False}})
    monkeypatch.setattr("gateway.kanban_watchers_notifier._notifier_collect", lambda *a, **kw: [])

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))

    assert not drain_called
    assert not tick_called


def test_watcher_loop_superagent_enabled_runs_step(kanban_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In _kanban_notifier_watcher with superagent.enabled=true, drain and driver tick run."""
    runner = DummyWatcherRunner()

    drain_called = False
    tick_called = False

    def fake_drain(*args, **kwargs):
        nonlocal drain_called
        drain_called = True
        return {"sent": 1}

    def fake_tick(*args, **kwargs):
        nonlocal tick_called
        tick_called = True
        return {"status": "ok"}

    runner._drain_kanban_outbox = fake_drain
    monkeypatch.setattr("hermes_cli.orchestrator.run_superagent_tick", fake_tick)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"superagent": {"enabled": True}})
    monkeypatch.setattr("gateway.kanban_watchers_notifier._notifier_collect", lambda *a, **kw: [])

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))

    assert drain_called
    assert tick_called

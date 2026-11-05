"""Tests for draining the eval queue on the superagent tick (punch P1a).

Verifies that ``run_superagent_tick`` assembles, evaluates and (outside observe
mode) accepts queued triggers itself, without any Router tool call, and that it
respects the operator pause and is idempotent across ticks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import (
    receive_trigger,
    reset_tick_throttle,
    run_superagent_tick,
)
from hermes_cli.superagent_pause import set_paused

ACTOR = "owner_alice"


def _config(mode: str) -> dict[str, Any]:
    return {
        "superagent": {
            "enabled": True,
            "mode": mode,
            "owner_identities": [ACTOR],
            "sources": {"telegram": {"allowlist": [ACTOR]}},
        }
    }


def _seed(tmp_path: Path) -> tuple[Path, str]:
    """Create a temp board with one explicitly admitted queued trigger."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:drain",
        actor_source_id=ACTOR,
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Deploy the latest release",
        idempotency_key="drain_msg_1",
        now=1000,
    )
    assert disp == "queued"
    return db_path, trg_id


def _tick(db_path: Path, cfg: dict[str, Any], now: float) -> dict[str, Any]:
    reset_tick_throttle()
    return run_superagent_tick(db_path, cfg, now=now, force=True)


def _count(db_path: Path, table: str) -> int:
    with kbc.connect_closing(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_auto_mode_tick_evaluates_and_accepts(tmp_path: Path) -> None:
    db_path, trg_id = _seed(tmp_path)

    res = _tick(db_path, _config("auto"), now=2000)

    assert res["status"] == "ok"
    assert res["eval_drain"]["status"] == "ok"
    assert res["eval_drain"]["batches"] == 1
    assert res["eval_drain"]["decisions"] == {"accept": 1}
    assert res["eval_drain"]["accepted"] == 1
    assert res["eval_drain"]["errors"] == 0

    with kbc.connect_closing(db_path) as conn:
        decs = conn.execute("SELECT * FROM sa_decisions").fetchall()
        assert len(decs) == 1
        assert decs[0]["outcome"] == "accept"

        tasks = conn.execute("SELECT * FROM tasks").fetchall()
        assert len(tasks) == 1
        assert tasks[0]["mandate_id"] is not None
        assert decs[0]["ticket_id"] == tasks[0]["id"]

        trg = conn.execute(
            "SELECT eval_state FROM sa_triggers WHERE id = ?", (trg_id,)
        ).fetchone()
        assert trg["eval_state"] == "done"


def test_observe_mode_tick_records_would_accept_without_task(tmp_path: Path) -> None:
    db_path, trg_id = _seed(tmp_path)
    tasks_before = _count(db_path, "tasks")

    res = _tick(db_path, _config("observe"), now=2000)

    assert res["eval_drain"]["status"] == "ok"
    assert res["eval_drain"]["decisions"] == {"accept": 1}
    assert res["eval_drain"]["accepted"] == 0

    assert _count(db_path, "tasks") == tasks_before
    with kbc.connect_closing(db_path) as conn:
        decs = conn.execute("SELECT * FROM sa_decisions").fetchall()
        assert len(decs) == 1
        assert decs[0]["outcome"] == "accept"
        assert decs[0]["would_accept"] == 1
        assert decs[0]["ticket_id"] is None
        trg = conn.execute(
            "SELECT eval_state FROM sa_triggers WHERE id = ?", (trg_id,)
        ).fetchone()
        assert trg["eval_state"] == "done"


def test_empty_queue_tick_is_ok_and_idle(tmp_path: Path) -> None:
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    res = _tick(db_path, _config("auto"), now=2000)

    assert res["status"] == "ok"
    assert res["eval_drain"]["status"] == "ok"
    assert res["eval_drain"]["batches"] == 0
    assert res["eval_drain"]["accepted"] == 0
    assert _count(db_path, "sa_decisions") == 0
    assert _count(db_path, "tasks") == 0


def test_paused_tick_leaves_queue_untouched(tmp_path: Path) -> None:
    db_path, trg_id = _seed(tmp_path)
    with kbc.connect_closing(db_path) as conn:
        set_paused(conn, True, 1500)

    res = _tick(db_path, _config("auto"), now=2000)

    assert res["eval_drain"]["status"] == "paused"
    assert res["eval_drain"]["batches"] == 0
    assert res["eval_drain"]["accepted"] == 0
    assert _count(db_path, "sa_decisions") == 0
    assert _count(db_path, "tasks") == 0
    with kbc.connect_closing(db_path) as conn:
        trg = conn.execute(
            "SELECT eval_state FROM sa_triggers WHERE id = ?", (trg_id,)
        ).fetchone()
        assert trg["eval_state"] == "queued"


def test_second_tick_is_idempotent(tmp_path: Path) -> None:
    db_path, _trg_id = _seed(tmp_path)
    cfg = _config("auto")

    first = _tick(db_path, cfg, now=2000)
    assert first["eval_drain"]["accepted"] == 1
    decisions_after_first = _count(db_path, "sa_decisions")
    tasks_after_first = _count(db_path, "tasks")
    assert decisions_after_first == 1
    assert tasks_after_first == 1

    second = _tick(db_path, cfg, now=2100)

    assert second["eval_drain"]["status"] == "ok"
    assert second["eval_drain"]["batches"] == 0
    assert second["eval_drain"]["accepted"] == 0
    assert _count(db_path, "sa_decisions") == decisions_after_first
    assert _count(db_path, "tasks") == tasks_after_first

"""Unit tests for Superagent Pause API (hermes_cli/superagent_pause.py).

Verifies:
1. set_paused / is_paused roundtrip and paused_at recorded in sa_meta.
2. pause_delta computation: seconds since paused_at when paused, 0 when running.
3. double-pause does not reset paused_at (second set_paused(True) keeps original paused_at).
4. shifting while still paused is a no-op (apply only via resume).
5. apply_pause_shift on resume shifts only triggers carrying eval_deadline, preserves
   other meta_json keys, and returns the count of shifted triggers.
6. pure shifted_deadline arithmetic.
7. connection or path/str support across all APIs.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import superagent_pause as pause


def _init_test_db(tmp_path: Path) -> Path:
    """Initialize a fresh temporary Kanban DB with all superagent schemas."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    return db_path


def _insert_trigger(
    conn: kbc.sqlite3.Connection,
    trigger_id: str,
    eval_state: str = "queued",
    meta: dict | None = None,
) -> None:
    """Helper to insert a minimal trigger row for testing deadline shifts."""
    meta_json = json.dumps(meta if meta is not None else {})
    conn.execute(
        """
        INSERT INTO sa_triggers (
            id, source, scope, thread_key, kind, idempotency_key, eval_state, meta_json, created_at
        ) VALUES (?, 'telegram', 'main', 'tg:123:456', 'message', ?, ?, ?, 1700000000)
        """,
        (trigger_id, f"idem-{trigger_id}", eval_state, meta_json),
    )


def test_pause_roundtrip_and_paused_at(tmp_path: Path) -> None:
    """Verify set_paused/is_paused roundtrip and paused_at persistence in sa_meta."""
    db_path = _init_test_db(tmp_path)

    with kbc.connect_closing(db_path) as conn:
        assert not pause.is_paused(conn)
        assert pause.pause_delta(conn, now=1700000000) == 0

        # Pause at epoch 1700000000
        res = pause.set_paused(conn, True, now=1700000000)
        assert res == 0
        assert pause.is_paused(conn)

        # Inspect sa_meta table directly
        cur = conn.execute("SELECT key, value FROM sa_meta WHERE key IN ('paused', 'paused_at')")
        meta_dict = dict(cur.fetchall())
        assert meta_dict.get("paused") == "1"
        assert meta_dict.get("paused_at") == "1700000000"

        # Resume at epoch 1700000100
        res = pause.set_paused(conn, False, now=1700000100)
        assert res == 0
        assert not pause.is_paused(conn)

        # Inspect sa_meta table directly after resume
        cur = conn.execute("SELECT key, value FROM sa_meta WHERE key IN ('paused', 'paused_at')")
        meta_dict = dict(cur.fetchall())
        assert meta_dict.get("paused") == "0"
        assert meta_dict.get("paused_at") == ""


def test_double_pause_does_not_reset_paused_at(tmp_path: Path) -> None:
    """Verify double-pause does not reset paused_at (second set_paused(True) keeps original paused_at)."""
    db_path = _init_test_db(tmp_path)

    with kbc.connect_closing(db_path) as conn:
        # First pause at 1700000000
        pause.set_paused(conn, True, now=1700000000)
        assert pause.is_paused(conn)

        cur = conn.execute("SELECT value FROM sa_meta WHERE key = 'paused_at'")
        assert cur.fetchone()[0] == "1700000000"

        # Second pause call at 1700000500 MUST NOT overwrite original paused_at
        pause.set_paused(conn, True, now=1700000500)
        cur = conn.execute("SELECT value FROM sa_meta WHERE key = 'paused_at'")
        assert cur.fetchone()[0] == "1700000000"

        # Delta at 1700000600 should be 600 (computed from original 1700000000, not 1700000500)
        assert pause.pause_delta(conn, now=1700000600) == 600


def test_pause_delta_computation(tmp_path: Path) -> None:
    """Verify pause_delta computes seconds since paused_at when paused, 0 when running."""
    db_path = _init_test_db(tmp_path)

    with kbc.connect_closing(db_path) as conn:
        # Before pause
        assert pause.pause_delta(conn, now=1700000050) == 0

        # Paused at 1700000000
        pause.set_paused(conn, True, now=1700000000)
        assert pause.pause_delta(conn, now=1700000000) == 0
        assert pause.pause_delta(conn, now=1700000025) == 25
        assert pause.pause_delta(conn, now=1700000100) == 100

        # Inverted / past time clamps to 0
        assert pause.pause_delta(conn, now=1699999900) == 0

        # After resume
        pause.set_paused(conn, False, now=1700000100)
        assert pause.pause_delta(conn, now=1700000200) == 0


def test_shifted_deadline_pure() -> None:
    """Verify shifted_deadline is pure arithmetic deadline + delta."""
    assert pause.shifted_deadline(100, 50) == 150
    assert pause.shifted_deadline(0, 0) == 0
    assert pause.shifted_deadline(1700000000, 3600) == 1700003600
    assert pause.shifted_deadline(500, -100) == 400


def test_shifting_while_paused_is_noop(tmp_path: Path) -> None:
    """Verify shifting while still paused is a no-op (apply only via resume)."""
    db_path = _init_test_db(tmp_path)

    with kbc.connect_closing(db_path) as conn:
        _insert_trigger(conn, "trig-queued", eval_state="queued", meta={"eval_deadline": 2000, "foo": "bar"})
        _insert_trigger(conn, "trig-eval", eval_state="evaluating", meta={"eval_deadline": 3000})

        # Pause at 1000
        pause.set_paused(conn, True, now=1000)
        assert pause.is_paused(conn)

        # Attempting apply_pause_shift directly while paused must be a no-op
        shifted = pause.apply_pause_shift(conn, now=1100)
        assert shifted == 0

        # Verify deadlines remain untouched
        rows = dict(conn.execute("SELECT id, meta_json FROM sa_triggers").fetchall())
        meta_q = json.loads(rows["trig-queued"])
        assert meta_q["eval_deadline"] == 2000
        assert meta_q["foo"] == "bar"

        meta_e = json.loads(rows["trig-eval"])
        assert meta_e["eval_deadline"] == 3000

        # paused_at in sa_meta is still 1000
        cur = conn.execute("SELECT value FROM sa_meta WHERE key = 'paused_at'")
        assert cur.fetchone()[0] == "1000"


def test_apply_pause_shift_on_resume(tmp_path: Path) -> None:
    """Verify apply_pause_shift on resume shifts only triggers carrying eval_deadline, preserves other keys."""
    db_path = _init_test_db(tmp_path)

    with kbc.connect_closing(db_path) as conn:
        # 1. Queued with deadline and extra keys
        _insert_trigger(conn, "t1", eval_state="queued", meta={"eval_deadline": 2000, "priority": "high", "num": 42})
        # 2. Evaluating with deadline and extra keys
        _insert_trigger(conn, "t2", eval_state="evaluating", meta={"eval_deadline": 3000, "note": "active"})
        # 3. Queued without eval_deadline
        _insert_trigger(conn, "t3", eval_state="queued", meta={"task": "no_deadline", "flag": True})
        # 4. Done with eval_deadline (should not be shifted)
        _insert_trigger(conn, "t4", eval_state="done", meta={"eval_deadline": 4000, "done_flag": True})
        # 5. Dropped with eval_deadline (should not be shifted)
        _insert_trigger(conn, "t5", eval_state="dropped", meta={"eval_deadline": 5000})
        # 6. Evaluating with empty meta_json
        _insert_trigger(conn, "t6", eval_state="evaluating", meta={})

        # Pause at 1000
        pause.set_paused(conn, True, now=1000)

        # Resume at 1150 -> delta is 150 seconds
        shifted_count = pause.set_paused(conn, False, now=1150)
        assert shifted_count == 2

        # Check DB state
        rows = dict(conn.execute("SELECT id, meta_json FROM sa_triggers").fetchall())

        # t1 shifted: 2000 + 150 = 2150, extra keys preserved
        meta_t1 = json.loads(rows["t1"])
        assert meta_t1["eval_deadline"] == 2150
        assert meta_t1["priority"] == "high"
        assert meta_t1["num"] == 42

        # t2 shifted: 3000 + 150 = 3150, extra keys preserved
        meta_t2 = json.loads(rows["t2"])
        assert meta_t2["eval_deadline"] == 3150
        assert meta_t2["note"] == "active"

        # t3 untouched (no eval_deadline)
        meta_t3 = json.loads(rows["t3"])
        assert "eval_deadline" not in meta_t3
        assert meta_t3["task"] == "no_deadline"
        assert meta_t3["flag"] is True

        # t4 untouched (eval_state is 'done')
        meta_t4 = json.loads(rows["t4"])
        assert meta_t4["eval_deadline"] == 4000
        assert meta_t4["done_flag"] is True

        # t5 untouched (eval_state is 'dropped')
        meta_t5 = json.loads(rows["t5"])
        assert meta_t5["eval_deadline"] == 5000

        # t6 untouched (empty meta)
        meta_t6 = json.loads(rows["t6"])
        assert meta_t6 == {}

        # sa_meta is cleared
        cur = conn.execute("SELECT key, value FROM sa_meta WHERE key IN ('paused', 'paused_at')")
        meta_dict = dict(cur.fetchall())
        assert meta_dict.get("paused") == "0"
        assert meta_dict.get("paused_at") == ""


def test_connection_or_path_interface(tmp_path: Path) -> None:
    """Verify all APIs accept sqlite3.Connection, Path, or str interchangeably."""
    db_path = _init_test_db(tmp_path)

    # Path instance
    assert not pause.is_paused(db_path)
    pause.set_paused(db_path, True, now=1000)
    assert pause.is_paused(db_path)
    assert pause.pause_delta(db_path, now=1040) == 40

    # str path
    str_path = str(db_path)
    assert pause.is_paused(str_path)
    assert pause.pause_delta(str_path, now=1050) == 50
    pause.set_paused(str_path, False, now=1050)
    assert not pause.is_paused(str_path)
    assert pause.pause_delta(str_path, now=1100) == 0

    # Calling apply_pause_shift when already unpaused returns 0
    assert pause.apply_pause_shift(db_path, now=1100) == 0


def test_resume_when_not_paused_is_noop(tmp_path: Path) -> None:
    """Verify set_paused(False) on an unpaused database safely does nothing and returns 0."""
    db_path = _init_test_db(tmp_path)

    with kbc.connect_closing(db_path) as conn:
        assert not pause.is_paused(conn)
        res = pause.set_paused(conn, False, now=1700000000)
        assert res == 0
        assert not pause.is_paused(conn)

"""Surfaces Rev 3 — shared inbound helpers (hermes_cli/superagent_inbound.py).

Covers WRD U3 step 1 / §3/U3.1: the full config-resolution chain (incl.
the in-memory steps), the DB-path chain, scope_for_profile_home, the
bounded synchronous record_local_turn (incl. the held-lock bound and the
no-background-landing semantics), and alias_thread_key's A31-1 move
semantics (read inside the txn, per-row delete, no blanket drop).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import superagent_inbound as inbound
from hermes_cli.superagent_subscriptions import subscribe, subscriptions_for


def _cfg(db_path: Path, *, enabled: bool = True, pilot=("local",)) -> dict:
    return {
        "superagent": {
            "enabled": enabled,
            "pilot_surfaces": list(pilot),
            "db_path": str(db_path),
        }
    }


@pytest.fixture
def sa_config(monkeypatch, tmp_path):
    """Point the file-config step of the chain at a temp kanban DB."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    state = {"cfg": _cfg(db_path)}
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: state["cfg"]
    )
    return db_path, state


def _rows(db_path: Path):
    with kbc.connect_closing(db_path) as conn:
        return conn.execute("SELECT * FROM sa_triggers").fetchall()


# ---------------------------------------------------------------------------
# resolve_superagent_config — the full chain
# ---------------------------------------------------------------------------


def test_config_chain_in_memory_config_wins():
    cfg = {"superagent": {"enabled": True, "marker": "from-config"}}
    assert inbound.resolve_superagent_config(config=cfg) == {"enabled": True, "marker": "from-config"}


def test_config_chain_in_memory_superagent_second():
    assert inbound.resolve_superagent_config(superagent={"enabled": False}) == {"enabled": False}
    # A config dict WITHOUT a superagent dict does not shadow step (ii).
    assert inbound.resolve_superagent_config(config={"other": {}}, superagent={"x": 1}) == {"x": 1}


def test_config_chain_file_fallback_and_failure_swallowed(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"superagent": {"enabled": True, "marker": "file"}},
    )
    assert inbound.resolve_superagent_config() == {"enabled": True, "marker": "file"}

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", _boom)
    assert inbound.resolve_superagent_config() is None  # never raises


# ---------------------------------------------------------------------------
# resolve_kanban_db_path
# ---------------------------------------------------------------------------


def test_db_path_chain_order(tmp_path):
    a, b, c = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "c.db"
    assert inbound.resolve_kanban_db_path({"db_path": str(a)}, b, c) == a
    assert inbound.resolve_kanban_db_path({"kanban_db": str(b)}, c) == b
    assert inbound.resolve_kanban_db_path({}, str(c)) == c
    assert inbound.resolve_kanban_db_path(None, Path(c)) == c


def test_db_path_default_and_failure(monkeypatch, tmp_path):
    target = tmp_path / "default.db"
    monkeypatch.setattr("hermes_cli.kanban_db.kanban_db_path", lambda: target)
    assert inbound.resolve_kanban_db_path({}) == target

    def _boom():
        raise RuntimeError("no path")

    monkeypatch.setattr("hermes_cli.kanban_db.kanban_db_path", _boom)
    assert inbound.resolve_kanban_db_path({}) is None  # never raises


# ---------------------------------------------------------------------------
# scope_for_profile_home
# ---------------------------------------------------------------------------


def test_scope_for_profile_home(tmp_path):
    assert inbound.scope_for_profile_home(None) == "default"
    assert inbound.scope_for_profile_home("") == "default"
    assert inbound.scope_for_profile_home(tmp_path / "profiles" / "sa-worker") == "sa-worker"
    assert inbound.scope_for_profile_home(tmp_path / "somewhere" / "else") == "default"
    # No produced scope ever contains a path separator.
    for value in (None, tmp_path, tmp_path / "profiles" / "x", "/a/b/c"):
        assert "/" not in inbound.scope_for_profile_home(value)


# ---------------------------------------------------------------------------
# record_local_turn
# ---------------------------------------------------------------------------


def test_record_local_turn_records_pinned_fields(sa_config):
    db_path, _ = sa_config
    inbound.record_local_turn(thread_key="sess-key-1", body="do the thing", scope="default")
    rows = _rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "local"
    assert row["scope"] == "default"
    assert row["thread_key"] == "sess-key-1"
    assert row["actor_source_id"] == "local:desktop"
    assert row["actor_is_bot"] == 0
    assert row["addressed"] == 1
    assert row["kind"] == "text"
    assert row["body"] == "do the thing"
    assert row["id"].startswith("trg_")
    assert row["idempotency_key"].startswith("local:sess-key-1:")


def test_record_local_turn_gating(sa_config):
    db_path, state = sa_config
    state["cfg"] = _cfg(db_path, enabled=False)
    inbound.record_local_turn(thread_key="k", body="x", scope="default")
    state["cfg"] = _cfg(db_path, pilot=("telegram",))
    inbound.record_local_turn(thread_key="k", body="x", scope="default")
    inbound.record_local_turn(thread_key="", body="x", scope="default")
    state["cfg"] = _cfg(db_path)
    inbound.record_local_turn(thread_key="k", body="   ", scope="default")
    assert _rows(db_path) == []


def test_record_local_turn_never_raises_on_receive_failure(sa_config, monkeypatch):
    db_path, _ = sa_config
    monkeypatch.setattr(
        "hermes_cli.orchestrator.receive_trigger",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    inbound.record_local_turn(thread_key="k", body="x", scope="default")  # no raise
    assert _rows(db_path) == []


def test_record_local_turn_held_lock_bound_and_no_background_landing(sa_config):
    """WRD U3 latency contract: with the writer lock held >= 5 s the
    bounded attempt returns < 10 s, drops the write, and nothing lands
    afterwards in the background; a later attempt records normally."""
    db_path, _ = sa_config
    holder = kbc.connect(db_path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO sa_triggers (id, source, scope, thread_key, actor_source_id,"
            " actor_is_bot, addressed, kind, body, meta_json, correlation_json,"
            " idempotency_key, eval_state, eval_requeues, disposition, created_at)"
            " VALUES ('trg_holder0001', 'x', 'x', 'x', NULL, 0, 0, 'text', 'hold', '{}', '[]',"
            " 'hold:1', 'queued', 0, NULL, 1)"
        )
        started = time.time()
        inbound.record_local_turn(thread_key="locked-key", body="lost candidacy", scope="default")
        elapsed = time.time() - started
        assert elapsed < 10, f"bounded attempt took {elapsed:.1f}s"
    finally:
        holder.execute("COMMIT")  # the holder's own row persists; the lost candidacy does not
        holder.close()
    # No background landing after the lock clears.
    time.sleep(0.5)
    assert [r["thread_key"] for r in _rows(db_path)] == ["x"]
    # A fresh attempt after the lock clears records normally.
    inbound.record_local_turn(thread_key="locked-key", body="lost candidacy", scope="default")
    assert [r["thread_key"] for r in _rows(db_path)] == ["x", "locked-key"]


# ---------------------------------------------------------------------------
# alias_thread_key — A31-1 move semantics
# ---------------------------------------------------------------------------


def _seed_subscriptions(db_path: Path) -> None:
    with kbc.connect_closing(db_path) as conn:
        subscribe(conn, ticket_id="t_one", thread_key="old-key", context_only=False, now=100)
        subscribe(conn, ticket_id="t_two", thread_key="old-key", context_only=True, now=101)


def test_alias_moves_rows_and_preserves_flags(sa_config):
    db_path, _ = sa_config
    _seed_subscriptions(db_path)
    inbound.alias_thread_key("old-key", "new-key")
    with kbc.connect_closing(db_path) as conn:
        assert subscriptions_for(conn, "old-key") == []
        moved = subscriptions_for(conn, "new-key")
        assert [(r["ticket_id"], r["context_only"]) for r in moved] == [
            ("t_one", False),
            ("t_two", True),
        ]
        # A second rotation moves them again — no accumulation.
        inbound.alias_thread_key("new-key", "third-key")
        assert subscriptions_for(conn, "new-key") == []
        assert len(subscriptions_for(conn, "third-key")) == 2


def test_alias_gated_off_writes_nothing(sa_config):
    db_path, state = sa_config
    _seed_subscriptions(db_path)
    state["cfg"] = _cfg(db_path, enabled=False)
    inbound.alias_thread_key("old-key", "new-key")
    state["cfg"] = _cfg(db_path, pilot=("telegram",))
    inbound.alias_thread_key("old-key", "new-key")
    inbound.alias_thread_key("old-key", "old-key")
    inbound.alias_thread_key("", "new-key")
    with kbc.connect_closing(db_path) as conn:
        assert len(subscriptions_for(conn, "old-key")) == 2
        assert subscriptions_for(conn, "new-key") == []


def test_alias_raced_row_landing_after_read_survives(sa_config, monkeypatch):
    """A31-1 raced-move case: a subscription that lands on the old key
    AFTER the move's in-transaction read (driven here by inserting one
    from inside the move's own subscribe step — the only interleaving a
    test can drive deterministically) is NOT deleted by the move: the
    per-row delete removes exactly the rows the move read. A blanket
    drop_thread_subscriptions would delete the fresh row and fail this.
    """
    db_path, _ = sa_config
    _seed_subscriptions(db_path)
    import hermes_cli.superagent_subscriptions as subs_mod

    real_subscribe = subs_mod.subscribe
    planted = {"done": False}

    def _planting_subscribe(conn, **kwargs):
        real_subscribe(conn, **kwargs)
        if not planted["done"]:
            planted["done"] = True
            conn.execute(
                "INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)"
                " VALUES ('old-key', 't_fresh', 0, 999)"
            )

    monkeypatch.setattr(subs_mod, "subscribe", _planting_subscribe)
    inbound.alias_thread_key("old-key", "new-key")
    with kbc.connect_closing(db_path) as conn:
        assert [r["ticket_id"] for r in subscriptions_for(conn, "old-key")] == ["t_fresh"]
        assert {r["ticket_id"] for r in subscriptions_for(conn, "new-key")} == {"t_one", "t_two"}

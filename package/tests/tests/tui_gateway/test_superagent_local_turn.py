"""Surfaces Rev 3 — WRD U3 TUI seam + U3b rotation integration.

prompt.submit records a `local` Super Agent turn ONLY at the three
mutually exclusive accepted-dispatch points, with the exclusion set from
WRD U3 (hidden / hosted / relay / refused / empty). Rotation moves the
ticket's subscriptions to the new session key (A31-1) so follow-ups fan
out to the same ticket.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import tui_gateway.server as server
from tools.bot_relay import DeliveryAuthor
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.superagent_subscriptions import subscriptions_for


# ---------------------------------------------------------------------------
# Harness (pattern: tests/tui_gateway/test_closed_session_turn_lease.py)
# ---------------------------------------------------------------------------


def _busy_agent():
    return SimpleNamespace(
        steer=lambda text: True,
        redirect=lambda text: True,
        interrupt=lambda *a, **k: None,
        clear_interrupt=lambda: None,
        session_id="agent-busy-1",
    )


def _make_session(monkeypatch, server_module, *, running=False, key="local-sess-1"):
    sid = "sa-local-1"
    session = {
        "session_key": key,
        "session_id": "db-row-1",
        "agent": None,
        "agent_ready": threading.Event(),
        "agent_building": False,
        "agent_error": None,
        "history": [],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "running": running,
        "inflight_turn_id": "turn-1" if running else None,
        "attached_images": [],
        "image_counter": 0,
        "cwd": "/tmp",
        "profile_home": None,
        "cols": 80,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "slash_worker": None,
        "transport": None,
        "created_at": 1_700_000_000.0,
        "last_active": 1_700_000_000.0,
    }
    session["agent_ready"].set()
    server_module._sessions[sid] = session
    monkeypatch.setattr(server_module, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server_module, "_ensure_session_db_row", lambda s: True)
    monkeypatch.setattr(server_module, "_persist_branch_seed", lambda s: None)
    monkeypatch.setattr(server_module, "_start_agent_build", lambda s, k: None)
    monkeypatch.setattr(server_module, "_run_after_agent_ready", lambda *a, **k: None)
    return sid, session


def _submit(server_module, sid, text, **extra):
    params = {"session_id": sid, "text": text}
    params.update(extra)
    return server_module._methods["prompt.submit"](1, params)


@pytest.fixture
def captured(monkeypatch):
    """Capture record_local_turn calls instead of writing."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "hermes_cli.superagent_inbound.record_local_turn",
        lambda **kw: calls.append(kw),
    )
    return calls


# ---------------------------------------------------------------------------
# Exclusions (capture-stub): nothing records
# ---------------------------------------------------------------------------


def test_hidden_submit_records_nothing(monkeypatch, captured):
    sid, _s = _make_session(monkeypatch, server)
    resp = _submit(server, sid, "onboarding opening", display_kind="hidden")
    assert "error" not in resp
    assert captured == []


def test_hosted_submit_records_nothing(monkeypatch, captured):
    sid, session = _make_session(monkeypatch, server)
    session["source"] = "bot_room"
    resp = _submit(
        server, sid, "hosted turn",
        _hosted_task={
            "room_id": "room-1", "task_id": "task-1", "thread_id": "thread-1",
            "turn_id": "turn-1", "execution_generation": 1, "member_id": "member-1",
        },
        _hosted_terminal_callback=lambda *a, **k: None,
    )
    assert "error" not in resp
    assert captured == []


def test_relay_submit_records_nothing(monkeypatch, captured):
    sid, _s = _make_session(monkeypatch, server)
    resp = _submit(
        server, sid, "relayed words",
        _turn_author=DeliveryAuthor({"user_id": "u9", "display_name": "Relayed"}),
    )
    assert "error" not in resp
    assert captured == []


def test_room_plumbing_session_records_nothing(monkeypatch, captured):
    sid, session = _make_session(monkeypatch, server)
    session["room_plumbing"] = True
    resp = _submit(server, sid, "plumbing text")
    assert "error" not in resp
    assert captured == []


def test_pending_hidden_session_records_nothing(monkeypatch, captured):
    sid, session = _make_session(monkeypatch, server)
    session["pending_hidden"] = True
    resp = _submit(server, sid, "pending hidden text")
    assert "error" not in resp
    assert captured == []


def test_empty_text_records_nothing(monkeypatch, captured):
    sid, _s = _make_session(monkeypatch, server)
    _submit(server, sid, "   ")
    assert captured == []


def test_slot_refused_records_nothing(monkeypatch, captured):
    sid, _s = _make_session(monkeypatch, server)
    monkeypatch.setattr(
        server, "_ensure_active_session_slot",
        lambda sid_, session_: "session slot is taken",
    )
    resp = _submit(server, sid, "refused text")
    assert resp["error"]["code"] == 4090
    assert captured == []


def test_owner_forwarded_records_exactly_once(monkeypatch, captured):
    """A submit the desktop refuses and forwards to the owner records
    NOTHING on the forwarding side; the owner's own accepting submit
    records exactly once — one row total across both invocations."""
    sid, _s = _make_session(monkeypatch, server)
    import tui_gateway.owner_forward as owner_forward

    refusal = SimpleNamespace(ledger_status="running", reason="SESSION_NOT_OWNED")
    monkeypatch.setattr(
        server, "_ensure_active_session_slot", lambda sid_, session_: refusal
    )
    monkeypatch.setattr(
        owner_forward, "forwardable", lambda refusal_, session_, text_, params_: True
    )
    forwarded: list = []
    monkeypatch.setattr(
        owner_forward, "forward_prompt",
        lambda sid_, session_, text_: forwarded.append(text_) or {"admission_id": "adm-1"},
    )
    resp = _submit(server, sid, "forward me")
    assert "error" not in resp
    assert resp["result"]["forwarded"] is True
    assert forwarded == ["forward me"]
    assert captured == []  # the forwarder records nothing

    # The owner's own submit (a fresh, owned session) records once.
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid_, session_: None)
    sid2, _s2 = _make_session(monkeypatch, server, key="local-sess-2")
    resp2 = _submit(server, sid2, "forward me")
    assert "error" not in resp2
    assert len(captured) == 1
    assert captured[0]["thread_key"] == "local-sess-2"
    assert captured[0]["body"] == "forward me"


# ---------------------------------------------------------------------------
# Real records: exactly one row per accepted submit
# ---------------------------------------------------------------------------


@pytest.fixture
def live_db(monkeypatch, tmp_path):
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    cfg = {
        "superagent": {
            "enabled": True,
            "pilot_surfaces": ["local"],
            "db_path": str(db_path),
            "mode": "auto",
            "owner_identities": ["local:desktop"],
            "sources": {"local": {"allowlist": ["local:desktop"]}},
        }
    }
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
    return db_path


def _trigger_rows(db_path):
    with kbc.connect_closing(db_path) as conn:
        return conn.execute("SELECT * FROM sa_triggers ORDER BY created_at, rowid").fetchall()


def test_idle_submit_records_one_row(monkeypatch, live_db):
    sid, _s = _make_session(monkeypatch, server)
    resp = _submit(server, sid, "build the thing")
    assert "error" not in resp
    rows = _trigger_rows(live_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "local"
    assert row["thread_key"] == "local-sess-1"
    assert row["actor_source_id"] == "local:desktop"
    assert row["addressed"] == 1
    assert row["kind"] == "text"
    assert row["body"] == "build the thing"
    assert row["scope"] == "default"


def test_busy_queued_records_one_row(monkeypatch, live_db):
    sid, session = _make_session(monkeypatch, server, running=True)
    session["agent"] = _busy_agent()
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    resp = _submit(server, sid, "queued while busy")
    assert resp["result"]["status"] == "queued"
    rows = _trigger_rows(live_db)
    assert len(rows) == 1
    assert rows[0]["body"] == "queued while busy"


def test_busy_steered_records_one_row(monkeypatch, live_db):
    sid, session = _make_session(monkeypatch, server, running=True)
    session["agent"] = _busy_agent()
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    resp = _submit(server, sid, "steer the turn")
    assert resp["result"]["status"] == "steered"
    rows = _trigger_rows(live_db)
    assert len(rows) == 1
    assert rows[0]["body"] == "steer the turn"


def test_busy_self_duplicate_records_nothing(monkeypatch, live_db):
    """A text-only self-copy of the LIVE turn's original prompt is
    dropped by _enqueue_prompt (returns None): the busy response still
    says queued, but no envelope lands — and per A31-8 nothing records."""
    sid, session = _make_session(monkeypatch, server, running=True, key="dup-key")
    session["agent"] = _busy_agent()
    session["inflight_turn"] = {"user": "same text", "assistant": "partial reply"}
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    resp = _submit(server, sid, "same text")
    assert resp["result"]["status"] == "queued"
    assert session.get("queued_prompt") is None
    assert _trigger_rows(live_db) == []


def test_busy_merge_onto_nonempty_head_records_one(monkeypatch, live_db):
    sid, session = _make_session(monkeypatch, server, running=True)
    session["agent"] = _busy_agent()
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    _submit(server, sid, "head text")
    _submit(server, sid, "appended text")
    rows = _trigger_rows(live_db)
    assert [r["body"] for r in rows] == ["head text", "appended text"]


def test_busy_merge_onto_empty_head_records_one(monkeypatch, live_db):
    """A31-8 landed condition (iii): the head envelope's text was empty
    at snapshot time and now holds exactly the submitted text."""
    sid, session = _make_session(monkeypatch, server, running=True)
    session["agent"] = _busy_agent()
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    session["queued_prompt"] = {"text": "", "queued_at": 1.0, "attachments": []}
    resp = _submit(server, sid, "fills the empty head")
    assert resp["result"]["status"] == "queued"
    rows = _trigger_rows(live_db)
    assert len(rows) == 1
    assert rows[0]["body"] == "fills the empty head"


def test_two_identical_consecutive_messages_record_two_rows(monkeypatch, live_db):
    sid, session = _make_session(monkeypatch, server)
    _submit(server, sid, "repeat me")
    session["running"] = False
    session["inflight_turn_id"] = None
    _submit(server, sid, "repeat me")
    rows = _trigger_rows(live_db)
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]


# ---------------------------------------------------------------------------
# Rotation integration: submit -> accept -> rotate -> follow-up fans out
# ---------------------------------------------------------------------------


def test_rotation_moves_subscription_and_followup_fans_out(monkeypatch, live_db):
    from hermes_cli.orchestrator import accept, assemble_batch, run_eval

    sid, session = _make_session(monkeypatch, server, key="rot-key-1")
    _submit(server, sid, "start the ticket work")
    rows = _trigger_rows(live_db)
    assert len(rows) == 1
    trigger_id = rows[0]["id"]

    # Accept the trigger through the real Eval path (auto mode + owner).
    eval_config = {
        "superagent": {
            "mode": "auto",
            "owner_identities": ["local:desktop"],
            "sources": {"local": {"allowlist": ["local:desktop"]}},
        }
    }
    batch = assemble_batch(live_db, thread_key="rot-key-1", now=2000, config=eval_config)
    assert batch is not None
    decision = run_eval(batch, db_path_or_conn=live_db, config=eval_config, now=2001)
    assert decision["outcome"] == "accept"
    with kbc.connect_closing(live_db) as conn:
        ticket_id = accept(conn, decision["id"], now=2002, config=eval_config)
    assert ticket_id
    with kbc.connect_closing(live_db) as conn:
        assert [r["ticket_id"] for r in subscriptions_for(conn, "rot-key-1")] == [ticket_id]

    # Rotate: session_key rot-key-1 -> rot-key-2 via the real seam caller.
    session["agent"] = SimpleNamespace(session_id="rot-key-2")
    # The split-module function is rebound onto the server namespace
    # (bind_module); only that copy resolves its bare helper names.
    server._sync_session_key_after_compress(sid, session, restart_slash_worker=False)
    assert session["session_key"] == "rot-key-2"
    with kbc.connect_closing(live_db) as conn:
        assert subscriptions_for(conn, "rot-key-1") == []
        moved = subscriptions_for(conn, "rot-key-2")
        assert [(r["ticket_id"], r["context_only"]) for r in moved] == [(ticket_id, False)]

    # A follow-up on the rotated key fans out to the SAME ticket.
    session["running"] = False
    session["inflight_turn_id"] = None
    _submit(server, sid, "follow-up after rotation")
    rows = _trigger_rows(live_db)
    assert len(rows) == 2
    follow = rows[1]
    assert follow["thread_key"] == "rot-key-2"
    assert follow["disposition"] == "fanned_out"
    assert follow["eval_state"] == "done"
    with kbc.connect_closing(live_db) as conn:
        inputs = conn.execute(
            "SELECT * FROM sa_inputs WHERE ticket_id = ?", (ticket_id,)
        ).fetchall()
        assert len(inputs) == 1
        assert inputs[0]["body"] == "follow-up after rotation"

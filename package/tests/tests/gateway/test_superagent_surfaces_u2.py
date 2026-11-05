"""Surfaces Rev 3 — WRD U2 hook acceptance tests (gateway hook rule).

Direct hook-level tests for GatewayRunner._hm_maybe_record_superagent_trigger
as amended by U2/U2b + addendum A31-5/A31-7/A31-9. The real-chain wake
test lives in test_superagent_surfaces_wake_chain.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.superagent_subscriptions import subscribe


@pytest.fixture(autouse=True)
def _allow_all_env(monkeypatch, tmp_path_factory):
    temp_home = tmp_path_factory.mktemp("hermes_home")
    monkeypatch.setenv("HERMES_HOME", str(temp_home))
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")


def _runner(db_path: Path, pilot) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = {"superagent": {"enabled": True, "pilot_surfaces": pilot}}
    runner._superagent_db_path = db_path
    return runner


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    return db_path


def _rows(db_path: Path):
    with kbc.connect_closing(db_path) as conn:
        return conn.execute("SELECT * FROM sa_triggers").fetchall()


def _api_source(*, user_id="api", chat_id="sess-1") -> SessionSource:
    # Built EXACTLY as gateway/session_api.py:185 builds the drain source.
    return SessionSource(
        platform=Platform.API_SERVER, chat_id=chat_id, user_id=user_id, chat_type="dm"
    )


def _event(source: SessionSource, *, text="hello", internal=False, message_id="m-1", **kw) -> MessageEvent:
    event = MessageEvent(
        text=text, source=source, message_id=message_id,
        message_type=MessageType.TEXT, internal=internal, **kw,
    )
    return event


# (i) Drain-shaped API user turn records with pinned identity.
def test_api_user_turn_records_pinned_identity(tmp_path):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["api_server"])
    source = _api_source()
    event = _event(source, text="murmur work request")
    event._sa_user_turn = True
    event.internal = True
    runner._hm_maybe_record_superagent_trigger(event, source)
    rows = _rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "api_server"
    assert row["actor_source_id"] == "api"
    assert row["addressed"] == 1
    assert row["kind"] == "text"
    assert row["body"] == "murmur work request"
    assert row["scope"] == "sess-1"
    assert row["thread_key"] == build_session_key(source)


# (v) Internal API event without the flag records nothing.
def test_api_internal_without_flag_no_row(tmp_path):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["api_server"])
    source = _api_source()
    event = _event(source, internal=True)
    runner._hm_maybe_record_superagent_trigger(event, source)
    assert _rows(db_path) == []


# (vii) Bot-flagged api_server events record nothing — all three flag sources.
@pytest.mark.parametrize("variant", ["source", "event", "metadata"], ids=["source-is_bot", "event-is_bot", "metadata-is_bot"])
def test_api_bot_flagged_no_row(tmp_path, variant):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["api_server"])
    source = _api_source()
    event = _event(source)
    event._sa_user_turn = True
    event.internal = True
    if variant == "source":
        source.is_bot = True
    elif variant == "event":
        event.is_bot = True
    else:
        event.metadata = {"is_bot": True}
    runner._hm_maybe_record_superagent_trigger(event, source)
    assert _rows(db_path) == []


# (viii) api_server source with user_id=None records the "api" fallback.
def test_api_none_actor_fallback(tmp_path):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["api_server"])
    source = _api_source(user_id=None)
    event = _event(source)
    event._sa_user_turn = True
    event.internal = True
    runner._hm_maybe_record_superagent_trigger(event, source)
    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["actor_source_id"] == "api"


# (ix) An owned-thread API follow-up fans out and is NOT left queued.
def test_api_owned_followup_fanned_out_not_queued(tmp_path):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["api_server"])
    source = _api_source()
    thread_key = build_session_key(source)
    with kbc.connect_closing(db_path) as conn:
        subscribe(conn, ticket_id="t_owned1", thread_key=thread_key, context_only=False, now=100)
    event = _event(source, text="follow-up on the ticket")
    event._sa_user_turn = True
    event.internal = True
    runner._hm_maybe_record_superagent_trigger(event, source)
    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["disposition"] == "fanned_out"
    assert rows[0]["eval_state"] == "done"
    with kbc.connect_closing(db_path) as conn:
        inputs = conn.execute(
            "SELECT * FROM sa_inputs WHERE ticket_id = 't_owned1'"
        ).fetchall()
        assert len(inputs) == 1


# (x) Gateway-local events (desktop-fix, 2026-11-04): a genuine user turn
# on the gateway local adapter RECORDS (live evidence: the owner's desktop
# turns arrive on this path); machine deliveries are excluded by marker.
def _local_source(*, user_id="uid:501", chat_id="local-abc"):
    return SessionSource(platform=Platform.LOCAL, chat_id=chat_id, user_id=user_id, chat_type="dm")


def test_local_user_turn_records(tmp_path):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["local"])
    source = _local_source()
    event = _event(source, text="desktop work request")
    runner._hm_maybe_record_superagent_trigger(event, source)
    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["source"] == "local"
    assert rows[0]["actor_source_id"] == "uid:501"


@pytest.mark.parametrize("marker", ["bot_id", "turn_author", "notification_category", "internal_text"])
def test_local_machine_markers_no_row(tmp_path, marker):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["local"])
    source = _local_source()
    if marker == "bot_id":
        event = _event(source, message_id="bot:xyz")
    elif marker == "turn_author":
        event = _event(source, metadata={"turn_author": {"subject": "someone"}})
    elif marker == "notification_category":
        event = _event(source, metadata={"notification_category": "diagnostic"})
    else:
        event = _event(source, text="result text [INTERNAL NOTIFICATION — generated by the Hermes gateway, not written by a human user.]")
    runner._hm_maybe_record_superagent_trigger(event, source)
    assert _rows(db_path) == []


# (xi) Telegram internal classes — A31-7: one named case per class, each
# an internal telegram event that must record NO trigger row.
_TELEGRAM_INTERNAL_CLASSES = [
    ("startup_resume_replay", "", {}),
    ("handoff_synthetic", "[Session was just handed off from CLI to telegram]", {}),
    ("process_notification", "Process watch: job finished", {"notification_category": "result"}),
    ("plugin_injection", "plugin-injected context", {}),
    ("goal_wakeup", "Goal wakeup: continue the plan", {}),
    ("kanban_wake_automation", "Kanban wake: ticket updated", {"notification_category": "result"}),
    ("loop_wake_synthetic", "Loop tick: check the thing", {"notification_category": "result"}),
]


@pytest.mark.parametrize(
    "text,metadata",
    [pytest.param(text, meta, id=name) for name, text, meta in _TELEGRAM_INTERNAL_CLASSES],
)
def test_telegram_internal_classes_no_row(tmp_path, text, metadata):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["telegram"])
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="7078451053",
        user_id="7078451053", chat_type="dm",
    )
    event = _event(source, text=text, internal=True, metadata=metadata)
    runner._hm_maybe_record_superagent_trigger(event, source)
    assert _rows(db_path) == []


# (xii) Positive control: a human telegram DM records, unchanged.
def test_telegram_human_dm_records(tmp_path):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["telegram"])
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="7078451053",
        user_id="7078451053", chat_type="dm",
    )
    event = _event(source, text="please build the report", internal=False)
    runner._hm_maybe_record_superagent_trigger(event, source)
    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["source"] == "telegram"
    assert rows[0]["actor_source_id"] == "7078451053"
    assert rows[0]["body"] == "please build the report"


# A31-5 read-order: a non-piloted armed surface reads the flag, then
# returns at the pilot check with no writes.
def test_non_piloted_armed_surface_no_row(tmp_path):
    db_path = _db(tmp_path)
    runner = _runner(db_path, ["telegram"])  # api_server NOT piloted
    source = _api_source()
    event = _event(source)
    event._sa_user_turn = True
    event.internal = True
    runner._hm_maybe_record_superagent_trigger(event, source)
    assert _rows(db_path) == []


# Disabled superagent: the hook returns before any flag read / write.
def test_disabled_no_row(tmp_path):
    db_path = _db(tmp_path)
    runner = object.__new__(GatewayRunner)
    runner.config = {"superagent": {"enabled": False, "pilot_surfaces": ["telegram"]}}
    runner._superagent_db_path = db_path
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="1", user_id="7078451053", chat_type="dm"
    )
    event = _event(source, internal=False)
    runner._hm_maybe_record_superagent_trigger(event, source)
    assert _rows(db_path) == []

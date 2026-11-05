"""Tests for Superagent Phase 2 inbound hook in gateway/run_inbound.py (Card 2.7).

Verifies:
(1) enabled=false -> no Trigger row, normal reply happens.
(2) enabled=true + pilot surface fixture event -> exactly one Trigger row with pinned fields
    AND normal reply happens.
(3) enabled=true + non-pilot surface -> no Trigger row, normal reply happens.
(4) Bot flags, sender-less identity, addressing detection, and idempotency key derivations.
(5) receive_trigger failure is swallowed and does not break the turn.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import kanban_db_connect as kbc


def _make_runner(*, sa_config: dict | None = None, db_path: Path | None = None):
    """Construct a minimal GatewayRunner instance for testing _handle_message."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True),
            Platform.SLACK: PlatformConfig(enabled=True),
        }
    )
    if sa_config is not None:
        runner.config = {"superagent": sa_config}

    if db_path is not None:
        runner._superagent_db_path = db_path

    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {
        Platform.TELEGRAM: adapter,
        Platform.SLACK: adapter,
    }
    runner.pairing_store = MagicMock()
    runner.session_store = MagicMock()
    runner._is_user_authorized_for_source = lambda _s: True
    runner._admit_bot_message_for_source = lambda _s: True
    runner._delivery_adapter_for = lambda _s: adapter
    runner._intake_adapter_for = lambda _s: adapter
    runner._rescue_orphaned_overflow = lambda _k, _a: None
    runner._enqueue_fifo = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="agent-turn-ok")
    runner._run_post_turn_hooks = AsyncMock()
    return runner


def _make_telegram_event(
    *,
    text: str = "hello superagent",
    message_id: str = "tel-msg-001",
    user_id: str | None = "user_111",
    chat_id: str = "-100123456",
    chat_type: str = "group",
    is_bot: bool = False,
    addressed: bool | None = True,
    reply_expected: bool | None = None,
) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id=user_id,
        user_name="alice" if user_id else None,
        chat_type=chat_type,
        is_bot=is_bot,
    )
    event = MessageEvent(
        text=text,
        message_id=message_id,
        source=source,
        message_type=MessageType.TEXT,
        reply_expected=reply_expected,
    )
    if addressed is not None:
        event.addressed = addressed
    return event


@pytest.fixture(autouse=True)
def _allow_all_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    temp_home = tmp_path_factory.mktemp("hermes_home")
    monkeypatch.setenv("HERMES_HOME", str(temp_home))
    for key in ("TELEGRAM_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")


@pytest.mark.asyncio
async def test_inbound_hook_disabled_no_trigger_reply_happens(tmp_path: Path) -> None:
    """When superagent.enabled is false, no trigger row is created and the turn runs normally."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    runner = _make_runner(
        sa_config={"enabled": False, "pilot_surfaces": ["telegram"]},
        db_path=db_path,
    )
    event = _make_telegram_event(text="check disabled")

    res = await runner._handle_message(event)

    assert res == "agent-turn-ok"
    with kbc.connect_closing(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 0


@pytest.mark.asyncio
async def test_inbound_hook_pilot_surface_records_trigger_and_reply_happens(tmp_path: Path) -> None:
    """When superagent.enabled is true and surface is a pilot surface, exactly one trigger row is
    created with pinned fields AND the normal turn reply still happens."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    runner = _make_runner(
        sa_config={"enabled": True, "pilot_surfaces": ["telegram"]},
        db_path=db_path,
    )
    event = _make_telegram_event(
        text="launch deployment",
        message_id="msg-999",
        user_id="alice_42",
        chat_id="-100987654",
        chat_type="group",
        is_bot=False,
        addressed=True,
    )

    res = await runner._handle_message(event)

    assert res == "agent-turn-ok"
    with kbc.connect_closing(db_path) as conn:
        rows = conn.execute("SELECT * FROM sa_triggers").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "telegram"
        assert row["scope"] == "-100987654"
        expected_thread_key = build_session_key(event.source)
        assert row["thread_key"] == expected_thread_key
        assert row["actor_source_id"] == "alice_42"
        assert row["actor_is_bot"] == 0
        assert row["addressed"] == 1
        assert row["kind"] == "text"
        assert row["body"] == "launch deployment"
        assert row["idempotency_key"] == "telegram:msg-999"
        assert row["eval_state"] == "queued"


@pytest.mark.asyncio
async def test_inbound_hook_non_pilot_surface_no_trigger(tmp_path: Path) -> None:
    """When surface is not in pilot_surfaces, no Trigger is created and normal reply still happens."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # Only slack is in pilot_surfaces; incoming event is telegram
    runner = _make_runner(
        sa_config={"enabled": True, "pilot_surfaces": ["slack"]},
        db_path=db_path,
    )
    event = _make_telegram_event(text="should not trigger")

    res = await runner._handle_message(event)

    assert res == "agent-turn-ok"
    with kbc.connect_closing(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 0


@pytest.mark.asyncio
async def test_inbound_hook_bot_flags_and_senderless(tmp_path: Path) -> None:
    """Verifies actor_is_bot propagation and sender-less actor_source_id None handling."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    runner = _make_runner(
        sa_config={"enabled": True, "pilot_surfaces": ["telegram"]},
        db_path=db_path,
    )

    # 1. Bot event
    bot_event = _make_telegram_event(
        text="bot notification",
        message_id="bot-msg-1",
        user_id="bot_user",
        is_bot=True,
    )
    assert await runner._handle_message(bot_event) == "agent-turn-ok"

    # 2. Sender-less event (e.g. channel forward / service message)
    senderless_event = _make_telegram_event(
        text="channel post",
        message_id="chan-msg-2",
        user_id=None,
        is_bot=False,
    )
    assert await runner._handle_message(senderless_event) == "agent-turn-ok"

    with kbc.connect_closing(db_path) as conn:
        # Surfaces Rev 3 (U2 sub-rule 3): bot-flagged events on the armed
        # surfaces {telegram, api_server, local} no longer record at all —
        # the gate checks first-party before its bot check, so the recorder
        # enforces the exclusion. This supersedes the pre-Rev-3 assertion
        # below's original form (bot row with actor_is_bot=1).
        row_bot = conn.execute(
            "SELECT actor_source_id, actor_is_bot FROM sa_triggers WHERE idempotency_key = 'telegram:bot-msg-1'"
        ).fetchone()
        assert row_bot is None

        row_chan = conn.execute(
            "SELECT actor_source_id, actor_is_bot FROM sa_triggers WHERE idempotency_key = 'telegram:chan-msg-2'"
        ).fetchone()
        assert row_chan is not None
        assert row_chan["actor_source_id"] is None
        assert row_chan["actor_is_bot"] == 0


@pytest.mark.asyncio
async def test_inbound_hook_dm_inherently_addressed(tmp_path: Path) -> None:
    """DMs without explicit addressed flag are marked addressed=1."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    runner = _make_runner(
        sa_config={"enabled": True, "pilot_surfaces": ["telegram"]},
        db_path=db_path,
    )

    dm_event = _make_telegram_event(
        text="dm to assistant",
        message_id="dm-msg-1",
        chat_type="dm",
        addressed=None,
    )
    assert await runner._handle_message(dm_event) == "agent-turn-ok"

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT addressed FROM sa_triggers WHERE idempotency_key = 'telegram:dm-msg-1'").fetchone()
        assert row is not None
        assert row["addressed"] == 1


@pytest.mark.asyncio
async def test_inbound_hook_unaddressed_group_message(tmp_path: Path) -> None:
    """Group messages with reply_expected=False are marked addressed=0."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    runner = _make_runner(
        sa_config={"enabled": True, "pilot_surfaces": ["telegram"]},
        db_path=db_path,
    )

    group_event = _make_telegram_event(
        text="chatting with someone else",
        message_id="grp-msg-1",
        chat_type="group",
        addressed=None,
        reply_expected=False,
    )
    assert await runner._handle_message(group_event) == "agent-turn-ok"

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT addressed FROM sa_triggers WHERE idempotency_key = 'telegram:grp-msg-1'").fetchone()
        assert row is not None
        assert row["addressed"] == 0


@pytest.mark.asyncio
async def test_inbound_hook_receive_trigger_failure_does_not_break_turn(tmp_path: Path) -> None:
    """When receive_trigger raises an exception, the turn does NOT fail: the error is logged and
    the agent reply is returned as usual."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    runner = _make_runner(
        sa_config={"enabled": True, "pilot_surfaces": ["telegram"]},
        db_path=db_path,
    )
    event = _make_telegram_event(text="failing trigger attempt")

    with patch("hermes_cli.orchestrator.receive_trigger", side_effect=RuntimeError("database locked simulated")):
        res = await runner._handle_message(event)

    # Normal reply must still happen
    assert res == "agent-turn-ok"

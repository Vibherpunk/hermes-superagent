"""Tests for Super-agent Card 4.5: Cron migration to Outbox (E-031).

Contract:
- Agent-originated cron deliveries enqueue Outbox rows (source = the delivery target's platform).
- Pairing/decline/system notices stay on the old path (E-031).
- Outbox module is hermes_cli/superagent_outbox.py.
- Tests: cron fixture job -> Outbox row -> drain -> one send;
         a pairing notice fixture -> old path, zero Outbox rows.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cron import scheduler
from cron import scheduler_delivery
from gateway.config import Platform
from gateway.pairing import PairingStore
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


class RecordingAdapter:
    def __init__(self):
        self.sent: list[dict] = []
        self.handled: list = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": str(chat_id), "text": str(text), "metadata": metadata or {}})
        return SimpleNamespace(success=True, message_id=f"msg-{len(self.sent)}")

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _make_pairing_runner(store, adapter):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._pairing_store_for = lambda _source: store
    runner._delivery_adapter_for = lambda _source: adapter
    runner.config = None
    return runner


def test_cron_fixture_job_enqueues_outbox_row_and_drains_to_one_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Cron fixture job -> Outbox row -> drain -> one send (Card 4.5, E-031)."""
    db_path = tmp_path / "cron-outbox.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    job = {
        "id": "cron-daily-brief",
        "name": "Daily Brief",
        "prompt": "Morning brief",
        "deliver": "telegram:chat-cron-1",
        "no_agent": False,
    }

    # Simulate execution of cron fixture job via run_one_job with a mock run_job
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *args, **kwargs: (True, "output", "Morning weather: 72F and sunny.", None),
    )

    ok = scheduler.run_one_job(job, adapters=runner.adapters, loop=None)
    assert ok is True

    # 1. Before drain: exactly one Outbox row exists with status='queued', source='telegram'
    conn = kbc.connect()
    try:
        rows = conn.execute("SELECT * FROM sa_outbox").fetchall()
        assert len(rows) == 1
        outbox_row = rows[0]
        assert outbox_row["source"] == "telegram"
        assert outbox_row["status"] == "queued"
        assert outbox_row["purpose"] == "result"
        assert "Morning weather: 72F and sunny." in outbox_row["body"]

        addr = json.loads(outbox_row["address_json"])
        assert addr["platform"] == "telegram"
        assert addr["chat_id"] == "chat-cron-1"
    finally:
        conn.close()

    # 2. Before drain: zero messages sent to adapter
    assert len(adapter.sent) == 0

    # 3. Drain outbox
    drain_summary = runner._drain_kanban_outbox()
    assert drain_summary.get("sent") == 1

    # 4. After drain: exactly one platform send
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "chat-cron-1"
    assert "Morning weather: 72F and sunny." in adapter.sent[0]["text"]

    # 5. Row status updated to 'sent'
    conn = kbc.connect()
    try:
        updated = conn.execute("SELECT * FROM sa_outbox WHERE id = ?", (outbox_row["id"],)).fetchone()
        assert updated["status"] == "sent"
        assert updated["sent_at"] is not None
    finally:
        conn.close()


def test_pairing_notice_fixture_uses_old_path_zero_outbox_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Pairing notice fixture -> old path, zero Outbox rows (Card 4.5, E-031)."""
    db_path = tmp_path / "pairing-outbox.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    adapter = RecordingAdapter()
    store = PairingStore()
    runner = _make_pairing_runner(store, adapter)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-unauth-1",
        chat_type="dm",
        user_id="stranger-42",
    )

    asyncio.run(runner._hm_offer_pairing_code(source))

    # Old path: adapter received the message directly
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "chat-unauth-1"
    assert "pairing approve" in adapter.sent[0]["text"]

    # Zero Outbox rows in sa_outbox
    conn = kbc.connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM sa_outbox").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_decline_notice_fixture_uses_old_path_zero_outbox_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Decline notice fixture -> old path, zero Outbox rows (Card 4.5, E-031)."""
    db_path = tmp_path / "decline-outbox.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    adapter = RecordingAdapter()
    store = PairingStore()
    runner = _make_pairing_runner(store, adapter)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-unauth-2",
        chat_type="dm",
        user_id="stranger-99",
    )

    asyncio.run(runner._hm_send_unauthorized_decline(source))

    # Old path: adapter received decline directly
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "chat-unauth-2"

    # Zero Outbox rows in sa_outbox
    conn = kbc.connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM sa_outbox").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_cron_system_failure_notice_stays_on_old_path_zero_outbox_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Cron system failure notices stay on old path, zero Outbox rows (Card 4.5, E-031)."""
    db_path = tmp_path / "failure-outbox.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    job = {
        "id": "cron-failing-job",
        "name": "Failing Job",
        "deliver": "telegram:chat-fail-1",
        "no_agent": False,
    }

    # Mock standalone delivery fallback on old path
    monkeypatch.setattr(
        scheduler_delivery,
        "_deliver_standalone",
        lambda *args, **kwargs: adapter.sent.append({"chat_id": "chat-fail-1", "text": "Job execution timed out."}),
    )

    scheduler_delivery._deliver_result(
        job, "Job execution timed out.", adapters=runner.adapters, for_failure=True,
    )

    # Delivered directly via live adapter on old path (1 send)
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "chat-fail-1"
    assert "Job execution timed out." in adapter.sent[0]["text"]

    # Zero Outbox rows in sa_outbox
    conn = kbc.connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM sa_outbox").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_cron_drain_delivery_queue_drains_outbox_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """scheduler.drain_delivery_queue drains sa_outbox rows through adapters."""
    db_path = tmp_path / "drain-queue-outbox.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    adapter = RecordingAdapter()
    job = {
        "id": "cron-drain-job",
        "name": "Drain Job",
        "deliver": "telegram:chat-drain-1",
        "no_agent": False,
    }

    scheduler_delivery._deliver_result(job, "Delivery via drain_delivery_queue")
    assert len(adapter.sent) == 0

    drained = scheduler.drain_delivery_queue(adapters={Platform.TELEGRAM: adapter}, loop=None)
    assert drained == 1
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "chat-drain-1"

"""Tests for superagent accept path and D-1 amend_mandate (Card 2.6).

Verifies:
(1) Happy path creates task + mandate + stamps + acknowledgement row.
(2) Ticket kind is 'coding' for github source.
(3) Ticket kind is 'personal' for non-github sources.
(4) Observe mode creates nothing and sets would_accept=1.
(5) Amend mandate happy path (chain + superseded).
(6) Amend refusals: coding ticket, non-owner approver, empty owner_identities config (three separate tests).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import (
    accept,
    amend_mandate,
    assemble_batch,
    receive_trigger,
    run_eval,
)


def _setup_accept_scenario(
    tmp_path: Path,
    *,
    source: str = "telegram",
    scope: str = "main_scope",
    thread_key: str = "tg:chat:1",
    actor: str = "owner_alice",
    body: str = "Deploy the latest release",
    mode: str = "auto",
) -> tuple[Path, int, dict[str, Any]]:
    """Helper to ingest a trigger and evaluate it to an accept Decision."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    config = {
        "superagent": {
            "mode": mode,
            "owner_identities": [actor],
            "sources": {source: {"allowlist": [actor]}},
        }
    }

    _trg_id, _ = receive_trigger(
        db_path,
        source=source,
        scope=scope,
        thread_key=thread_key,
        actor_source_id=actor,
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body=body,
        idempotency_key=f"idemp_{source}_{thread_key}",
        now=1000,
    )

    batch = assemble_batch(db_path, thread_key=thread_key, now=1001, config=config)
    assert batch is not None
    dec = run_eval(batch, db_path_or_conn=db_path, config=config, now=1002)
    assert dec["outcome"] == "accept"

    return db_path, dec["id"], config


def test_accept_happy_path_creates_task_mandate_stamps_and_outbox(tmp_path: Path) -> None:
    """Happy path creates task via create_task, sa_mandates row, stamps tasks and decisions, and inserts outbox acknowledgement."""
    db_path, dec_id, config = _setup_accept_scenario(tmp_path, source="telegram", body="Deploy the latest release\nDetails here")

    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1003, config=config)

    assert ticket_id is not None
    assert isinstance(ticket_id, str)

    with kbc.connect_closing(db_path) as conn:
        # 1. Kanban task created via create_task
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
        assert task is not None
        assert task["title"] == "Deploy the latest release"
        assert task["body"].startswith("Deploy the latest release\nDetails here")
        assert task["status"] == "ready"
        assert task["ticket_kind"] == "personal"  # v1 rule: telegram -> personal
        assert task["mandate_id"] is not None

        # 2. sa_mandates row created
        mandate = conn.execute("SELECT * FROM sa_mandates WHERE ticket_id = ?", (ticket_id,)).fetchone()
        assert mandate is not None
        assert mandate["id"] == task["mandate_id"]
        assert mandate["scope"] == "main_scope"
        assert mandate["strength"] == "explicit"
        assert mandate["status"] == "active"
        assert mandate["amended_from"] is None
        assert mandate["created_at"] == 1003

        basis = json.loads(mandate["basis_json"])
        assert basis["sender"] == "owner_alice"
        assert basis["source"] == "telegram"
        assert basis["scope"] == "main_scope"

        # 3. sa_decisions stamped with ticket_id
        dec = conn.execute("SELECT * FROM sa_decisions WHERE id = ?", (dec_id,)).fetchone()
        assert dec["ticket_id"] == ticket_id

        # 4. sa_outbox acknowledgement row created
        outbox = conn.execute("SELECT * FROM sa_outbox WHERE ticket_id = ?", (ticket_id,)).fetchone()
        assert outbox is not None
        assert outbox["source"] == "telegram"
        assert outbox["purpose"] == "acknowledgement"
        assert outbox["status"] == "queued"
        assert outbox["created_at"] == 1003
        assert f"ticket {ticket_id}" in outbox["body"]

        addr = json.loads(outbox["address_json"])
        assert addr["thread_key"] == "tg:chat:1"
        assert addr["actor_source_id"] == "owner_alice"
        # Canonical delivery address (fix 2026-11-04): the gateway outbox
        # sender and the dedupe identity both key on platform/chat_id —
        # without them every acknowledgement failed undeliverable.
        assert addr["platform"] == "telegram"
        assert addr["chat_id"] == "main_scope"

        # 5. The card body carries the worker sign-off contract, appended
        # to the requester's own words (fix 2026-11-04): SA cards used to
        # carry the bare prompt, and workers exited without the terminal
        # kanban call that makes a run count.
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
        assert "WORK ORDER" in task["body"]
        assert "kanban_complete" in task["body"]


def test_accept_ticket_kind_coding_for_github_source(tmp_path: Path) -> None:
    """Trigger with Source 'github' stamps tasks.ticket_kind = 'coding' per v1 rule."""
    db_path, dec_id, config = _setup_accept_scenario(
        tmp_path,
        source="github",
        scope="hermes-agent",
        thread_key="gh:issue:101",
        actor="octocat",
        body="Fix memory leak in runner",
    )

    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1003, config=config)

    assert ticket_id is not None
    with kbc.connect_closing(db_path) as conn:
        task = conn.execute("SELECT ticket_kind FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
        assert task["ticket_kind"] == "coding"


def test_accept_ticket_kind_personal_for_non_github_source(tmp_path: Path) -> None:
    """Trigger with non-github source (e.g. slack) stamps tasks.ticket_kind = 'personal' per v1 rule."""
    db_path, dec_id, config = _setup_accept_scenario(
        tmp_path,
        source="slack",
        scope="general",
        thread_key="slack:channel:C123",
        actor="slack_user",
        body="Order team lunch",
    )

    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1003, config=config)

    assert ticket_id is not None
    with kbc.connect_closing(db_path) as conn:
        task = conn.execute("SELECT ticket_kind FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
        assert task["ticket_kind"] == "personal"


def test_accept_observe_mode_creates_nothing_and_sets_would_accept(tmp_path: Path) -> None:
    """In observe mode (superagent.mode == 'observe'), accept() sets would_accept=1 and creates NOTHING."""
    db_path, dec_id, _ = _setup_accept_scenario(tmp_path, mode="auto")

    observe_config = {"superagent": {"mode": "observe"}}

    with kbc.connect_closing(db_path) as conn:
        result = accept(conn, dec_id, now=1003, config=observe_config)

    assert result is None

    with kbc.connect_closing(db_path) as conn:
        # Decision has would_accept=1 and no ticket_id
        dec = conn.execute("SELECT * FROM sa_decisions WHERE id = ?", (dec_id,)).fetchone()
        assert dec["would_accept"] == 1
        assert dec["ticket_id"] is None

        # NOTHING created
        tasks_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert tasks_count == 0

        mandates_count = conn.execute("SELECT COUNT(*) FROM sa_mandates").fetchone()[0]
        assert mandates_count == 0

        outbox_count = conn.execute("SELECT COUNT(*) FROM sa_outbox").fetchone()[0]
        assert outbox_count == 0


def test_amend_mandate_happy_path_chain_and_superseded(tmp_path: Path) -> None:
    """Amend happy path creates new mandate row, chains amended_from, marks old superseded, and updates tasks."""
    db_path, dec_id, config = _setup_accept_scenario(tmp_path, source="telegram", actor="owner_alice")

    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1003, config=config)
        assert ticket_id is not None

        old_mandate = conn.execute("SELECT * FROM sa_mandates WHERE ticket_id = ?", (ticket_id,)).fetchone()
        assert old_mandate["status"] == "active"
        old_id = old_mandate["id"]
        old_scope = old_mandate["scope"]

        # 1. First amendment
        new_mandate_id = amend_mandate(
            conn,
            ticket_id=ticket_id,
            added_scope=";additional_privileges",
            approver="owner_alice",
            now=1004,
            config=config,
        )

        assert new_mandate_id != old_id

        # Verify old mandate is superseded
        old_updated = conn.execute("SELECT * FROM sa_mandates WHERE id = ?", (old_id,)).fetchone()
        assert old_updated["status"] == "superseded"

        # Verify new mandate
        new_m = conn.execute("SELECT * FROM sa_mandates WHERE id = ?", (new_mandate_id,)).fetchone()
        assert new_m["ticket_id"] == ticket_id
        assert new_m["status"] == "active"
        assert new_m["amended_from"] == old_id
        assert new_m["scope"] == old_scope + ";additional_privileges"
        assert new_m["created_at"] == 1004

        new_basis = json.loads(new_m["basis_json"])
        assert new_basis["old_scope"] == old_scope
        assert new_basis["added_scope"] == ";additional_privileges"
        assert new_basis["scope"] == new_m["scope"]
        assert new_basis["approver"] == "owner_alice"

        # Verify task mandate_id is updated
        task = conn.execute("SELECT mandate_id FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
        assert task["mandate_id"] == new_mandate_id

        # 2. Second amendment (chaining)
        third_mandate_id = amend_mandate(
            conn,
            ticket_id=ticket_id,
            added_scope=";even_more_scope",
            approver="owner_alice",
            now=1005,
            config=config,
        )

        assert third_mandate_id != new_mandate_id

        # Second mandate is now superseded
        second_m = conn.execute("SELECT * FROM sa_mandates WHERE id = ?", (new_mandate_id,)).fetchone()
        assert second_m["status"] == "superseded"

        # Third mandate is active and points to second
        third_m = conn.execute("SELECT * FROM sa_mandates WHERE id = ?", (third_mandate_id,)).fetchone()
        assert third_m["status"] == "active"
        assert third_m["amended_from"] == new_mandate_id
        assert third_m["scope"] == new_m["scope"] + ";even_more_scope"

        task = conn.execute("SELECT mandate_id FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
        assert task["mandate_id"] == third_mandate_id


def test_amend_mandate_refuses_coding_ticket(tmp_path: Path) -> None:
    """D-1 Refusal 1: Cannot amend mandate when ticket_kind is 'coding'."""
    db_path, dec_id, config = _setup_accept_scenario(
        tmp_path,
        source="github",
        actor="octocat",
        body="Refactor database layer",
    )

    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1003, config=config)
        assert ticket_id is not None

        with pytest.raises(ValueError, match="only 'personal' tickets may be amended"):
            amend_mandate(
                conn,
                ticket_id=ticket_id,
                added_scope=";extra",
                approver="octocat",
                now=1004,
                config=config,
            )


def test_amend_mandate_refuses_non_owner_approver(tmp_path: Path) -> None:
    """D-1 Refusal 2: Cannot amend mandate when approver does not match owner identity."""
    db_path, dec_id, config = _setup_accept_scenario(
        tmp_path,
        source="telegram",
        actor="owner_alice",
        body="Personal reminder task",
    )

    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1003, config=config)
        assert ticket_id is not None

        with pytest.raises(ValueError, match="does not match owner identity"):
            amend_mandate(
                conn,
                ticket_id=ticket_id,
                added_scope=";extra",
                approver="mallory_intruder",
                now=1004,
                config=config,
            )


def test_amend_mandate_refuses_empty_owner_identities_config(tmp_path: Path) -> None:
    """D-1 Refusal 3: Cannot amend mandate when superagent.owner_identities is empty or missing."""
    db_path, dec_id, config = _setup_accept_scenario(
        tmp_path,
        source="telegram",
        actor="owner_alice",
        body="Personal reminder task",
    )

    empty_config = {
        "superagent": {
            "mode": "auto",
            "owner_identities": [],  # explicitly empty
        }
    }

    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=1003, config=config)
        assert ticket_id is not None

        with pytest.raises(ValueError, match="owner_identities is empty or not configured"):
            amend_mandate(
                conn,
                ticket_id=ticket_id,
                added_scope=";extra",
                approver="owner_alice",
                now=1004,
                config=empty_config,
            )

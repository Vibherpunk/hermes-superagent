"""Tests for superagent orchestrator trigger persistence and policy versioning (E-007, E-013).

Verifies:
(1) Duplicate event -> one row + 'duplicate' disposition (silent no-op, never raises).
(2) Two distinct events -> two rows + 'queued' disposition.
(3) Policy version stable across identical configs.
(4) Policy version changes on:
    (a) rule edit
    (b) mode edit
    (c) allowlist edit
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import (
    policy_snapshot,
    policy_version,
    receive_trigger,
)


def test_duplicate_event_produces_one_row_and_duplicate(tmp_path: Path) -> None:
    """Duplicate idempotency_key returns existing id and 'duplicate' with no new row."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # First event: fresh insert
    trg_id1, disp1 = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:12345:thread1",
        actor_source_id="user_999",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="hello world",
        meta={"chat_type": "group"},
        correlation=["corr_1"],
        idempotency_key="telegram:msg_001",
        now=1700000000,
    )

    assert trg_id1.startswith("trg_")
    assert len(trg_id1) == 16  # 'trg_' (4) + 12 hex chars
    assert disp1 == "queued"

    # Second event: duplicate idempotency key
    trg_id2, disp2 = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:12345:thread1",
        actor_source_id="user_999",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="hello world duplicate attempt",
        meta={"chat_type": "group"},
        correlation=["corr_1"],
        idempotency_key="telegram:msg_001",
        now=1700000001,
    )

    assert trg_id2 == trg_id1
    assert disp2 == "duplicate"

    # Confirm exactly ONE row exists in sa_triggers with original content intact
    with kbc.connect_closing(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 1

        row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id1,)).fetchone()
        assert row is not None
        assert row["id"] == trg_id1
        assert row["source"] == "telegram"
        assert row["scope"] == "main"
        assert row["thread_key"] == "tg:12345:thread1"
        assert row["actor_source_id"] == "user_999"
        assert row["actor_is_bot"] == 0
        assert row["addressed"] == 1
        assert row["kind"] == "message"
        assert row["body"] == "hello world"
        assert row["idempotency_key"] == "telegram:msg_001"
        assert row["eval_state"] == "queued"
        assert row["created_at"] == 1700000000


def test_two_distinct_events_produce_two_rows(tmp_path: Path) -> None:
    """Distinct idempotency keys produce two separate queued rows."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    trg_id1, disp1 = receive_trigger(
        db_path,
        source="github",
        scope="repo:org/hermes",
        thread_key="gh:issue:100",
        actor_source_id="gh_user_1",
        actor_is_bot=False,
        addressed=True,
        kind="issue_opened",
        body="Feature request",
        meta={"number": 100},
        correlation=[],
        idempotency_key="github:issue:100:created",
        now=1700000000,
    )

    trg_id2, disp2 = receive_trigger(
        db_path,
        source="github",
        scope="repo:org/hermes",
        thread_key="gh:issue:101",
        actor_source_id="gh_user_2",
        actor_is_bot=True,
        addressed=False,
        kind="issue_comment",
        body="Automated comment",
        meta={"number": 101},
        correlation=["corr_2"],
        idempotency_key="github:issue:101:comment:555",
        now=1700000005,
    )

    assert trg_id1 != trg_id2
    assert disp1 == "queued"
    assert disp2 == "queued"

    with kbc.connect_closing(db_path) as conn:
        rows = conn.execute("SELECT id, idempotency_key, actor_is_bot FROM sa_triggers ORDER BY created_at").fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] == trg_id1
        assert rows[0]["idempotency_key"] == "github:issue:100:created"
        assert rows[0]["actor_is_bot"] == 0

        assert rows[1]["id"] == trg_id2
        assert rows[1]["idempotency_key"] == "github:issue:101:comment:555"
        assert rows[1]["actor_is_bot"] == 1


def test_receive_trigger_with_live_connection(tmp_path: Path) -> None:
    """receive_trigger works when passed an active sqlite3.Connection directly."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    with kbc.connect_closing(db_path) as conn:
        trg_id1, disp1 = receive_trigger(
            conn,
            source="slack",
            scope="channel:general",
            thread_key="slack:C123:T456",
            actor_source_id="U789",
            actor_is_bot=0,
            addressed=1,
            kind="mention",
            body="ping router",
            meta=None,
            correlation=None,
            idempotency_key="slack:ev_1",
            now=1700000010,
        )
        assert disp1 == "queued"

        # Duplicate via same connection
        trg_id2, disp2 = receive_trigger(
            conn,
            source="slack",
            scope="channel:general",
            thread_key="slack:C123:T456",
            actor_source_id="U789",
            actor_is_bot=0,
            addressed=1,
            kind="mention",
            body="ping router",
            meta=None,
            correlation=None,
            idempotency_key="slack:ev_1",
            now=1700000020,
        )
        assert trg_id2 == trg_id1
        assert disp2 == "duplicate"

        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 1


def test_policy_version_stable_across_identical_configs() -> None:
    """Identical configs generate identical snapshot and version (12 hex chars)."""
    cfg_a = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [
                {"name": "rule1", "instruction": "Do task", "priority": 1},
                {"name": "rule2", "instruction": "Check tests", "priority": 2},
            ],
            "allowlists": ["user1", "user2"],
        }
    }
    cfg_b = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [
                # Notice reversed dict keys in rule objects to prove canonical key sorting
                {"priority": 1, "instruction": "Do task", "name": "rule1"},
                {"instruction": "Check tests", "priority": 2, "name": "rule2"},
            ],
            "allowlists": ["user1", "user2"],
        }
    }

    snap_a = policy_snapshot(cfg_a)
    snap_b = policy_snapshot(cfg_b)

    assert snap_a == snap_b
    ver_a = policy_version(snap_a)
    ver_b = policy_version(snap_b)

    assert ver_a == ver_b
    assert len(ver_a) == 12
    assert all(c in "0123456789abcdef" for c in ver_a)


def test_policy_version_changes_on_rule_edit() -> None:
    """Case (a): changing rules changes the policy version."""
    base_cfg = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [{"name": "rule1", "instruction": "Build feature"}],
            "allowlists": ["dev_lead"],
        }
    }
    snap_base = policy_snapshot(base_cfg)
    ver_base = policy_version(snap_base)

    # 1. Edit an instruction in existing rule
    edited_instruction_cfg = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [{"name": "rule1", "instruction": "Build and test feature"}],
            "allowlists": ["dev_lead"],
        }
    }
    ver_edited = policy_version(policy_snapshot(edited_instruction_cfg))
    assert ver_edited != ver_base

    # 2. Add an additional rule
    added_rule_cfg = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [
                {"name": "rule1", "instruction": "Build feature"},
                {"name": "rule2", "instruction": "Review PRs"},
            ],
            "allowlists": ["dev_lead"],
        }
    }
    ver_added = policy_version(policy_snapshot(added_rule_cfg))
    assert ver_added != ver_base
    assert ver_added != ver_edited


def test_policy_version_changes_on_mode_edit() -> None:
    """Case (b): changing accept_mode changes the policy version."""
    base_cfg = {
        "superagent": {
            "accept_mode": "observe",
            "rules": [{"name": "rule1", "instruction": "Build feature"}],
            "allowlists": ["dev_lead"],
        }
    }
    snap_base = policy_snapshot(base_cfg)
    ver_base = policy_version(snap_base)

    # Change to 'auto'
    auto_cfg = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [{"name": "rule1", "instruction": "Build feature"}],
            "allowlists": ["dev_lead"],
        }
    }
    ver_auto = policy_version(policy_snapshot(auto_cfg))
    assert ver_auto != ver_base

    # Change to 'off'
    off_cfg = {
        "superagent": {
            "accept_mode": "off",
            "rules": [{"name": "rule1", "instruction": "Build feature"}],
            "allowlists": ["dev_lead"],
        }
    }
    ver_off = policy_version(policy_snapshot(off_cfg))
    assert ver_off != ver_base
    assert ver_off != ver_auto


def test_policy_version_changes_on_allowlist_edit() -> None:
    """Case (c): changing allowlist entries changes the policy version."""
    base_cfg = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [{"name": "rule1"}],
            "allowlists": ["user_alpha"],
        }
    }
    ver_base = policy_version(policy_snapshot(base_cfg))

    # Edit direct allowlist
    edited_allowlist_cfg = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [{"name": "rule1"}],
            "allowlists": ["user_alpha", "user_beta"],
        }
    }
    ver_edited = policy_version(policy_snapshot(edited_allowlist_cfg))
    assert ver_edited != ver_base

    # Source-scoped allowlist change
    source_cfg1 = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [{"name": "rule1"}],
            "sources": {"telegram": {"allowlist": ["tg_100"]}},
        }
    }
    source_cfg2 = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [{"name": "rule1"}],
            "sources": {"telegram": {"allowlist": ["tg_200"]}},
        }
    }
    ver_src1 = policy_version(policy_snapshot(source_cfg1))
    ver_src2 = policy_version(policy_snapshot(source_cfg2))
    assert ver_src1 != ver_src2


def test_policy_snapshot_owner_identities_presence() -> None:
    """Owner identities are captured only when present in config."""
    cfg_without_owner = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [],
            "allowlists": [],
        }
    }
    snap_no_owner = policy_snapshot(cfg_without_owner)
    assert "owner_identities" not in snap_no_owner

    cfg_with_owner = {
        "superagent": {
            "accept_mode": "auto",
            "rules": [],
            "allowlists": [],
        },
        "owner": "admin_alice",
    }
    snap_with_owner = policy_snapshot(cfg_with_owner)
    assert "owner_identities" in snap_with_owner
    assert snap_with_owner["owner_identities"] == "admin_alice"

    assert policy_version(snap_no_owner) != policy_version(snap_with_owner)

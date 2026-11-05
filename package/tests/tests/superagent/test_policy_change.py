"""Tests for superagent policy change and revalidation (Card 2.9).

Verifies per SPEC-V2 §2.5:
1. Revalidation matrix:
   - explicit-followup True
   - inferred still-admitted True
   - inferred no-longer-listed False
   - admission strength-dropped False
   - admission strength-held True
   - bot unlisted False
2. Policy changed behavior:
   - Affected ticket gets exactly one policy_change Input and its task status is unchanged
   - Unaffected ticket gets no Input
   - A running/queued Eval requeues (eval_state back to 'queued', eval_requeues unchanged)
   - Terminal tasks are not affected
   - Scope rule removal triggers policy_change Input
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db import create_task
from hermes_cli.orchestrator import (
    assemble_batch,
    policy_changed,
    policy_version,
    receive_trigger,
    revalidate,
)


def _make_policy_snapshot(
    *,
    mode: str = "auto",
    allowlists: dict[str, list[str]] | None = None,
    bots: dict[str, list[str]] | None = None,
    rules: list[Any] | None = None,
    owner_identities: list[str] | dict[str, str] | None = None,
) -> dict[str, Any]:
    """Helper to synthesize a canonical policy snapshot dict."""
    sources: dict[str, Any] = {}
    if allowlists:
        for src, al in allowlists.items():
            sources.setdefault(src, {})["allowlist"] = al
    if bots:
        for src, bl in bots.items():
            sources.setdefault(src, {})["bots"] = bl

    snap: dict[str, Any] = {
        "accept_mode": mode,
        "rules": rules or [],
        "allowlists": allowlists or {},
    }
    if sources:
        snap["sources"] = sources
    if owner_identities:
        snap["owner_identities"] = owner_identities
    return snap


def _insert_mandate(
    conn: Any,
    *,
    ticket_id: str,
    policy_ver: str,
    strength: str,
    basis: dict[str, Any],
    scope: str = "default",
    status: str = "active",
    now: int = 1000,
) -> int:
    """Helper to insert an sa_mandates row directly."""
    cur = conn.execute(
        """
        INSERT INTO sa_mandates (
            ticket_id, policy_version, scope, strength,
            basis_json, status, amended_from, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            ticket_id,
            policy_ver,
            scope,
            strength,
            json.dumps(basis, sort_keys=True),
            status,
            now,
        ),
    )
    return cur.lastrowid


# --- Revalidate matrix tests ---


def test_revalidate_explicit_followup_true(tmp_path: Path) -> None:
    """Matrix row: explicit + followup basis -> True (even if unlisted or mode is observe)."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    policy = _make_policy_snapshot(mode="observe", allowlists={"telegram": []})

    with kbc.connect_closing(db_path) as conn:
        t1 = create_task(conn, title="Explicit Task", created_by="superagent")
        m1 = _insert_mandate(
            conn,
            ticket_id=t1,
            policy_ver="v1",
            strength="explicit",
            basis={"source": "telegram", "sender": "alice", "via": "addressed"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (m1, t1))

        t2 = create_task(conn, title="Followup Task", created_by="superagent")
        m2 = _insert_mandate(
            conn,
            ticket_id=t2,
            policy_ver="v1",
            strength="inferred",
            basis={"source": "telegram", "sender": "bob", "via": "followup"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (m2, t2))

        assert revalidate(conn, m1, policy=policy) is True
        assert revalidate(conn, m2, policy=policy) is True


def test_revalidate_inferred_still_admitted_true(tmp_path: Path) -> None:
    """Matrix row: inferred -> True only if current policy admits sender under mode 'auto' with matching rules."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    policy = _make_policy_snapshot(
        mode="auto",
        allowlists={"telegram": ["alice"]},
        rules=[{"id": "auto_deploy", "name": "auto_deploy"}],
    )

    with kbc.connect_closing(db_path) as conn:
        t = create_task(conn, title="Inferred Task", created_by="superagent")
        mid = _insert_mandate(
            conn,
            ticket_id=t,
            policy_ver="v1",
            strength="inferred",
            basis={
                "source": "telegram",
                "sender": "alice",
                "via": "inferred",
                "matched_rules": ["auto_deploy"],
            },
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, t))

        assert revalidate(conn, mid, policy=policy) is True


def test_revalidate_inferred_no_longer_listed_false(tmp_path: Path) -> None:
    """Matrix row: inferred no-longer-listed False."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # Policy does NOT list alice
    policy = _make_policy_snapshot(
        mode="auto",
        allowlists={"telegram": ["bob"]},
    )

    with kbc.connect_closing(db_path) as conn:
        t = create_task(conn, title="Inferred Task", created_by="superagent")
        mid = _insert_mandate(
            conn,
            ticket_id=t,
            policy_ver="v1",
            strength="inferred",
            basis={"source": "telegram", "sender": "alice", "via": "inferred"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, t))

        assert revalidate(conn, mid, policy=policy) is False


def test_revalidate_admission_strength_dropped_false(tmp_path: Path) -> None:
    """Matrix row: admission strength-dropped False (sender removed -> strength becomes not_admitted)."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # charlie was removed from allowlist
    policy = _make_policy_snapshot(mode="auto", allowlists={"telegram": []})

    with kbc.connect_closing(db_path) as conn:
        t = create_task(conn, title="Admission Task", created_by="superagent")
        mid = _insert_mandate(
            conn,
            ticket_id=t,
            policy_ver="v1",
            strength="inferred",
            basis={"source": "telegram", "sender": "charlie", "via": "admission"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, t))

        assert revalidate(conn, mid, policy=policy) is False


def test_revalidate_admission_strength_held_true(tmp_path: Path) -> None:
    """Matrix row: admission strength-held True (today's computed strength >= original strength)."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # charlie is still on allowlist
    policy = _make_policy_snapshot(mode="auto", allowlists={"telegram": ["charlie"]})

    with kbc.connect_closing(db_path) as conn:
        t = create_task(conn, title="Admission Task", created_by="superagent")
        mid = _insert_mandate(
            conn,
            ticket_id=t,
            policy_ver="v1",
            strength="inferred",
            basis={"source": "telegram", "sender": "charlie", "via": "admission"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, t))

        assert revalidate(conn, mid, policy=policy) is True


def test_revalidate_bot_unlisted_false(tmp_path: Path) -> None:
    """Matrix row: bot unlisted False (sender no longer in bot list)."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # dependabot is not listed
    policy_without_bot = _make_policy_snapshot(mode="auto", bots={"github": []})
    policy_with_bot = _make_policy_snapshot(mode="auto", bots={"github": ["dependabot"]})

    with kbc.connect_closing(db_path) as conn:
        t = create_task(conn, title="Bot Task", created_by="superagent")
        mid = _insert_mandate(
            conn,
            ticket_id=t,
            policy_ver="v1",
            strength="inferred",
            basis={"source": "github", "sender": "dependabot", "via": "bot"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, t))

        assert revalidate(conn, mid, policy=policy_without_bot) is False
        assert revalidate(conn, mid, policy=policy_with_bot) is True


# --- policy_changed behavior tests ---


def test_policy_changed_affected_ticket_gets_exactly_one_input_and_task_status_unchanged(
    tmp_path: Path,
) -> None:
    """Affected ticket gets exactly one policy_change Input and its task status is unchanged."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    old_policy = _make_policy_snapshot(mode="auto", allowlists={"telegram": ["eve"]})
    old_ver = policy_version(old_policy)

    new_policy = _make_policy_snapshot(mode="auto", allowlists={"telegram": []})

    with kbc.connect_closing(db_path) as conn:
        t = create_task(conn, title="Running Task", created_by="superagent")
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (t,))
        mid = _insert_mandate(
            conn,
            ticket_id=t,
            policy_ver=old_ver,
            strength="inferred",
            basis={"source": "telegram", "sender": "eve", "via": "inferred"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, t))

        summary = policy_changed(conn, old_version=old_ver, new_snapshot=new_policy, now=2000)

        assert t in summary["affected_tickets"]

        # 1. Task status is unchanged
        task_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (t,)).fetchone()
        assert task_row["status"] == "running"

        # 2. Exactly one policy_change Input exists
        inputs = conn.execute("SELECT * FROM sa_inputs WHERE ticket_id = ?", (t,)).fetchall()
        assert len(inputs) == 1
        assert inputs[0]["kind"] == "policy_change"
        assert "no longer listed" in inputs[0]["body"]
        assert inputs[0]["created_at"] == 2000

        # 3. Repeated call is idempotent (ticket not notified again)
        summary2 = policy_changed(conn, old_version=old_ver, new_snapshot=new_policy, now=2001)
        inputs2 = conn.execute("SELECT * FROM sa_inputs WHERE ticket_id = ?", (t,)).fetchall()
        assert len(inputs2) == 1
        assert t not in summary2["affected_tickets"]


def test_policy_changed_unaffected_ticket_gets_no_input(tmp_path: Path) -> None:
    """Unaffected ticket gets no Input."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    old_ver = "v_old_explicit"
    new_policy = _make_policy_snapshot(mode="auto", allowlists={"telegram": []})

    with kbc.connect_closing(db_path) as conn:
        t = create_task(conn, title="Explicit Task", created_by="superagent")
        mid = _insert_mandate(
            conn,
            ticket_id=t,
            policy_ver=old_ver,
            strength="explicit",
            basis={"source": "telegram", "sender": "owner_alice", "via": "addressed"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, t))

        summary = policy_changed(conn, old_version=old_ver, new_snapshot=new_policy, now=2000)

        assert t in summary["unaffected_tickets"]
        assert t not in summary["affected_tickets"]

        inputs = conn.execute("SELECT * FROM sa_inputs WHERE ticket_id = ?", (t,)).fetchall()
        assert len(inputs) == 0

        task_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (t,)).fetchone()
        assert task_row["status"] == "ready"


def test_policy_changed_queued_eval_requeues(tmp_path: Path) -> None:
    """Running/queued Eval requeues: triggers' eval_state back to 'queued', eval_requeues unchanged."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    cfg = {"superagent": {"sources": {"telegram": {"allowlist": ["user1"]}}}}

    # Ingest trigger and assemble batch (putting it into pending / in-flight eval state)
    trg_id, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:th:1",
        actor_source_id="user1",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Trigger in eval",
        idempotency_key="idemp_eval_1",
        now=1000,
    )

    batch = assemble_batch(db_path, thread_key="tg:th:1", now=1001, config=cfg)
    assert batch is not None
    assert batch["triggers"][0]["eval_state"] == "pending"

    new_policy = _make_policy_snapshot(mode="auto")

    with kbc.connect_closing(db_path) as conn:
        summary = policy_changed(conn, old_version="v_old", new_snapshot=new_policy, now=1002)
        assert summary["requeued_evals"] >= 1

        row = conn.execute(
            "SELECT eval_state, eval_requeues FROM sa_triggers WHERE id = ?",
            (trg_id,),
        ).fetchone()
        assert row["eval_state"] == "queued"
        assert row["eval_requeues"] == 0  # unchanged per SPEC-V2 §2.5


def test_policy_changed_terminal_task_ignored(tmp_path: Path) -> None:
    """Terminal tasks (done, archived) are ignored and get no policy_change Input."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    old_ver = "v_old"
    new_policy = _make_policy_snapshot(mode="auto", allowlists={"telegram": []})

    with kbc.connect_closing(db_path) as conn:
        t_done = create_task(conn, title="Done Task", created_by="superagent")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t_done,))
        m_done = _insert_mandate(
            conn,
            ticket_id=t_done,
            policy_ver=old_ver,
            strength="inferred",
            basis={"source": "telegram", "sender": "removed_user", "via": "inferred"},
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (m_done, t_done))

        summary = policy_changed(conn, old_version=old_ver, new_snapshot=new_policy, now=2000)

        assert t_done not in summary["affected_tickets"]
        inputs = conn.execute("SELECT * FROM sa_inputs WHERE ticket_id = ?", (t_done,)).fetchall()
        assert len(inputs) == 0


def test_policy_changed_scope_rule_removed_triggers_affected(tmp_path: Path) -> None:
    """Removing a required scope rule marks an inferred ticket as affected."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    old_policy = _make_policy_snapshot(
        mode="auto",
        allowlists={"telegram": ["alice"]},
        rules=[{"id": "auto_review", "name": "auto_review"}],
    )
    old_ver = policy_version(old_policy)

    # New policy keeps alice on allowlist but drops auto_review rule
    new_policy = _make_policy_snapshot(
        mode="auto",
        allowlists={"telegram": ["alice"]},
        rules=[],
    )

    with kbc.connect_closing(db_path) as conn:
        t = create_task(conn, title="Rule Dependent Task", created_by="superagent")
        mid = _insert_mandate(
            conn,
            ticket_id=t,
            policy_ver=old_ver,
            strength="inferred",
            basis={
                "source": "telegram",
                "sender": "alice",
                "via": "inferred",
                "matched_rules": ["auto_review"],
            },
        )
        conn.execute("UPDATE tasks SET mandate_id = ? WHERE id = ?", (mid, t))

        summary = policy_changed(conn, old_version=old_ver, new_snapshot=new_policy, now=2000)

        assert t in summary["affected_tickets"]
        inputs = conn.execute("SELECT * FROM sa_inputs WHERE ticket_id = ?", (t,)).fetchall()
        assert len(inputs) == 1
        assert "scope rule removed" in inputs[0]["body"]

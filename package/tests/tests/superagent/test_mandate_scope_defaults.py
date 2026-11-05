"""Tests for P2b: restore-to-intended repairs are in-mandate by default.

Verifies the owner's ruling is encoded in the versioned policy snapshot, the issued
mandate's basis, and the inferred-admission judge prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import (
    accept,
    assemble_batch,
    policy_snapshot,
    policy_version,
    receive_trigger,
    run_eval,
)

EXCLUDED_CATEGORIES = [
    "behavior changes or new features",
    "schema or data changes",
    "deletions of real data",
    "external sends",
    "money movement",
    "anything irreversible",
]


def _assert_restore_to_intended(entry: dict[str, Any]) -> None:
    assert entry["name"] == "restore_to_intended"
    assert "smallest patch" in entry["definition"]
    assert "tenant containers" in entry["applies_to"]
    assert "ambiguous" in entry["ambiguity_rule"]
    assert "escalate" in entry["ambiguity_rule"]
    for category in EXCLUDED_CATEGORIES:
        assert category in entry["excludes"]
    assert len(entry["excludes"]) == len(EXCLUDED_CATEGORIES)
    assert len(entry["includes"]) == 4


def test_policy_snapshot_default_includes_restore_to_intended() -> None:
    snap = policy_snapshot({})
    defaults = snap["mandate_scope_defaults"]
    assert isinstance(defaults, list) and len(defaults) == 1
    _assert_restore_to_intended(defaults[0])
    # Pre-existing keys are preserved.
    for key in ("accept_mode", "allowlists", "rules"):
        assert key in snap


def test_policy_snapshot_config_override_and_version_changes() -> None:
    override_cfg = {"superagent": {"mandate_scope_defaults": [{"name": "custom"}]}}
    default_snap = policy_snapshot({})
    override_snap = policy_snapshot(override_cfg)
    assert override_snap["mandate_scope_defaults"] == [{"name": "custom"}]
    assert policy_version(default_snap) != policy_version(override_snap)

    # Empty list falls back to the built-in default.
    empty_snap = policy_snapshot({"superagent": {"mandate_scope_defaults": []}})
    assert empty_snap["mandate_scope_defaults"] == default_snap["mandate_scope_defaults"]
    assert policy_version(empty_snap) == policy_version(default_snap)


def test_accepted_mandate_basis_carries_scope_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    actor = "owner_alice"
    config = {
        "superagent": {
            "mode": "auto",
            "owner_identities": [actor],
            "sources": {"telegram": {"allowlist": [actor]}},
        }
    }
    receive_trigger(
        db_path,
        source="telegram",
        scope="main_scope",
        thread_key="tg:chat:sd",
        actor_source_id=actor,
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Fix the broken container",
        idempotency_key="idemp_sd_1",
        now=1000,
    )
    batch = assemble_batch(db_path, thread_key="tg:chat:sd", now=1001, config=config)
    assert batch is not None
    dec = run_eval(batch, db_path_or_conn=db_path, config=config, now=1002)
    assert dec["outcome"] == "accept"

    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec["id"], now=1003, config=config)
        assert ticket_id is not None
        row = conn.execute(
            "SELECT basis_json FROM sa_mandates WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
    basis = json.loads(row["basis_json"])
    assert len(basis["scope_defaults"]) == 1
    _assert_restore_to_intended(basis["scope_defaults"][0])


def test_eval_prompt_includes_scope_defaults(tmp_path: Path, monkeypatch: Any) -> None:
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    config = {
        "superagent": {
            "mode": "auto",
            "sources": {"telegram": {"allowlist": ["member_alice"]}},
        }
    }
    receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:sd:eval",
        actor_source_id="member_alice",
        actor_is_bot=False,
        addressed=False,
        kind="message",
        body="the dashboard is down again",
        idempotency_key="sd_eval_1",
        now=1000,
    )
    batch = assemble_batch(db_path, thread_key="tg:sd:eval", now=1001, config=config)
    assert batch is not None

    captured: dict[str, Any] = {}

    def fake_call_llm(**kw: Any) -> Any:
        captured.update(kw)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"outcome": "ignore", "reason": "x", "matched_rules": []})
                    )
                )
            ]
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    run_eval(batch, db_path_or_conn=db_path, config=config, now=1002)

    messages = captured["messages"]
    system = next(m["content"] for m in messages if m["role"] == "system")
    user = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
    assert "restore-to-intended" in system
    assert len(user["mandate_scope_defaults"]) == 1
    _assert_restore_to_intended(user["mandate_scope_defaults"][0])


def test_evaluate_inferred_without_scope_defaults_still_works(monkeypatch: Any) -> None:
    from hermes_cli.orchestrator import _evaluate_inferred_with_llm

    captured: dict[str, Any] = {}

    def fake_call_llm(**kw: Any) -> Any:
        captured.update(kw)
        return json.dumps({"outcome": "ignore", "reason": "x", "matched_rules": []})

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    _evaluate_inferred_with_llm("tk", [{"id": 1, "body": "b"}], [])
    user = json.loads(next(m["content"] for m in captured["messages"] if m["role"] == "user"))
    assert "mandate_scope_defaults" not in user

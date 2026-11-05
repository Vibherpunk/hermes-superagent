"""End-to-end pilot fixture test for Super-agent PR comment -> Input -> wake (Card 3.8).

Verifies:
(1) With pass-through (superagent.passthrough = True, default):
    - Inbound PR comment arrives on worker-owned PR thread (github:pr-<n>).
    - Fan-out directly creates sa_inputs row and marks trigger fanned_out / eval_state done.
    - Kanban dispatcher tick delivers input and wakes/spawns worker with rendered Trigger Card.
    - ZERO Router/Eval model calls are made (0 prompt tokens, 0 completion tokens).
(2) Without pass-through (superagent.passthrough = False):
    - Inbound PR comment does NOT fan out directly (0 sa_inputs rows initially).
    - Trigger is queued for Eval (eval_state = queued).
    - Eval batch assembly and run_eval evaluates trigger via Router/Eval model call.
    - Exactly ONE Router/Eval model call is made with measured prompt and completion tokens.
(3) Token comparison between with-pass-through and without-pass-through paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest

from agent.model_metadata import estimate_tokens_rough
import agent.auxiliary_client as aux
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import profiles
from hermes_cli.orchestrator import (
    assemble_batch,
    receive_trigger,
    run_eval,
    set_configured_github_repo,
    subscribe_ticket_to_pr,
)
from hermes_cli.superagent_delivery import deliver_inputs


CONFIGURED_REPO = "nousresearch/pilot-repo"


@pytest.fixture
def pilot_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Isolated environment with kanban DB and profile resolver."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "profile_exists", lambda _: True)
    db_path = home / "board.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as conn:
        set_configured_github_repo(conn, CONFIGURED_REPO)
    return home, db_path


def _create_pilot_worker_ticket(conn: Any, ticket_id: str, pr_number: int = 101) -> str:
    """Create a worker coding task subscribed to a PR."""
    tid = kb.create_task(conn, title=f"Worker task for PR #{pr_number}", assignee="sa-worker")
    with kbc.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET id = ?, mandate_id = 1, ticket_kind = 'coding' WHERE id = ?",
            (ticket_id, tid),
        )
    subscribe_ticket_to_pr(conn, ticket_id, pr_number, opened=True)
    kb.block_task(conn, ticket_id, reason="waiting on review")
    return ticket_id


def test_pilot_fixture_with_passthrough_zero_model_calls(
    pilot_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With pass-through: PR comment -> Input -> wake with ZERO Router model calls (Card 3.8)."""
    _, db_path = pilot_env

    # Intercept model calls to record calls and tokens
    model_calls: list[dict[str, Any]] = []

    def mock_call_llm(task=None, messages=None, **kw):
        prompt_text = "".join(m.get("content", "") for m in messages or [])
        prompt_tokens = estimate_tokens_rough(prompt_text)
        resp_text = json.dumps({"outcome": "accept", "reason": "PR review feedback", "matched_rules": []})
        completion_tokens = estimate_tokens_rough(resp_text)
        model_calls.append({
            "task": task,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        })
        return resp_text

    monkeypatch.setattr(aux, "call_llm", mock_call_llm)

    with kbc.connect_closing(db_path) as conn:
        tid = _create_pilot_worker_ticket(conn, "t_pilot_worker_pt", pr_number=101)

    cfg = {
        "superagent": {
            "enabled": True,
            "passthrough": True,
            "pilot_surfaces": ["github"],
            "github": {"repository": CONFIGURED_REPO},
            "sources": {"github": {"allowlist": ["alice"]}},
        }
    }

    # 1. Inbound PR comment arrives
    with kbc.connect_closing(db_path) as conn:
        trg_id, disp = receive_trigger(
            conn,
            source="github",
            scope="comments",
            thread_key="github:pr-101",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=False,
            kind="comment",
            body="Please address the review comments: check boundary conditions on line 42 before merging.",
            idempotency_key="github:pr-101:c101_pt",
            now=1762190000.0,
            config=cfg,
        )

    # Trigger fanned out directly
    assert disp == "fanned_out"
    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row["eval_state"] == "done"
        assert trg_row["disposition"] == "fanned_out"

        inp_row = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchone()
        assert inp_row is not None
        assert inp_row["ticket_id"] == "t_pilot_worker_pt"
        assert inp_row["is_person"] == 1
        assert inp_row["read_at"] is None

    # NO model calls during trigger ingestion
    assert len(model_calls) == 0

    # 2. Wake phase: dispatcher tick
    spawns: list[Any] = []

    def fake_spawn(task, ws, board=None):
        spawns.append(task)
        return 9901

    with kbc.connect_closing(db_path) as conn:
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn, slot_pool=None)

    assert len(res.spawned) == 1
    assert len(spawns) == 1
    assert spawns[0].id == "t_pilot_worker_pt"
    assert spawns[0].run_input is not None
    assert "Please address the review comments" in spawns[0].run_input

    # Input marked read
    with kbc.connect_closing(db_path) as conn:
        inp_after = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchone()
        assert inp_after["read_at"] is not None

    # End-to-end model calls remain ZERO
    assert len(model_calls) == 0


def test_pilot_fixture_without_passthrough_evaluates_via_router_model(
    pilot_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without pass-through: PR comment requires Router/Eval model call (Card 3.8)."""
    _, db_path = pilot_env

    model_calls: list[dict[str, Any]] = []

    def mock_call_llm(task=None, messages=None, **kw):
        prompt_text = "".join(m.get("content", "") for m in messages or [])
        prompt_tokens = estimate_tokens_rough(prompt_text)
        resp_text = json.dumps({"outcome": "accept", "reason": "PR review feedback from alice", "matched_rules": []})
        completion_tokens = estimate_tokens_rough(resp_text)
        model_calls.append({
            "task": task,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        })
        return resp_text

    monkeypatch.setattr(aux, "call_llm", mock_call_llm)

    with kbc.connect_closing(db_path) as conn:
        tid = _create_pilot_worker_ticket(conn, "t_pilot_worker_nopt", pr_number=102)

    cfg_no_pt = {
        "superagent": {
            "enabled": True,
            "passthrough": False,
            "pilot_surfaces": ["github"],
            "github": {"repository": CONFIGURED_REPO},
            "sources": {"github": {"allowlist": ["alice"]}},
        }
    }

    # 1. Inbound PR comment arrives with passthrough disabled
    with kbc.connect_closing(db_path) as conn:
        trg_id, disp = receive_trigger(
            conn,
            source="github",
            scope="comments",
            thread_key="github:pr-102",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=False,
            kind="comment",
            body="Please address the review comments: check boundary conditions on line 42 before merging.",
            idempotency_key="github:pr-102:c102_nopt",
            now=1762190000.0,
            config=cfg_no_pt,
        )

    # Fan-out is disabled -> trigger queued for Eval
    assert disp == "queued"
    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        # Zero inputs created before Eval
        inps = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchall()
        assert len(inps) == 0

    # 2. Eval phase runs
    with kbc.connect_closing(db_path) as conn:
        batch = assemble_batch(conn, thread_key="github:pr-102", now=1762190005.0, config=cfg_no_pt)
        assert batch is not None
        dec = run_eval(batch, db_path_or_conn=conn, config=cfg_no_pt, now=1762190008.0)

    assert dec["outcome"] == "accept"
    assert dec["strength"] == "inferred"

    # Exactly 1 Router/Eval model call occurred
    assert len(model_calls) == 1
    call = model_calls[0]
    assert call["task"] == "superagent_eval"
    assert call["prompt_tokens"] > 0
    assert call["completion_tokens"] > 0
    assert call["total_tokens"] == call["prompt_tokens"] + call["completion_tokens"]


def test_with_vs_without_passthrough_token_comparison(
    pilot_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compare token consumption between with-pass-through and without-pass-through (Card 3.8)."""
    _, db_path = pilot_env

    recorded_tokens = {"with_pt": 0, "without_pt": 0}

    def mock_call_llm(task=None, messages=None, **kw):
        prompt_text = "".join(m.get("content", "") for m in messages or [])
        resp_text = json.dumps({"outcome": "accept", "reason": "PR review feedback", "matched_rules": []})
        return resp_text

    monkeypatch.setattr(aux, "call_llm", mock_call_llm)

    # Case A: with pass-through
    with kbc.connect_closing(db_path) as conn:
        tid_a = _create_pilot_worker_ticket(conn, "t_compare_a", pr_number=201)
        calls_a: list[Any] = []
        monkeypatch.setattr(aux, "call_llm", lambda **kw: calls_a.append(kw) or "{}")

        receive_trigger(
            conn,
            source="github",
            scope="comments",
            thread_key="github:pr-201",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=False,
            kind="comment",
            body="Review comment A",
            idempotency_key="cmp_a",
            now=1762190000.0,
            config={"superagent": {"enabled": True, "passthrough": True}},
        )
        deliver_inputs(conn, tid_a, now=1762190010.0)
        assert len(calls_a) == 0
        recorded_tokens["with_pt"] = 0

    # Case B: without pass-through
    with kbc.connect_closing(db_path) as conn:
        tid_b = _create_pilot_worker_ticket(conn, "t_compare_b", pr_number=202)
        calls_b: list[Any] = []

        def tracking_call(**kw):
            calls_b.append(kw)
            resp = json.dumps({"outcome": "accept", "reason": "Review comment B", "matched_rules": []})
            p_tok = estimate_tokens_rough("".join(m.get("content", "") for m in kw.get("messages", [])))
            c_tok = estimate_tokens_rough(resp)
            recorded_tokens["without_pt"] += (p_tok + c_tok)
            return resp

        monkeypatch.setattr(aux, "call_llm", tracking_call)

        cfg_b = {
            "superagent": {
                "enabled": True,
                "passthrough": False,
                "pilot_surfaces": ["github"],
                "github": {"repository": CONFIGURED_REPO},
                "sources": {"github": {"allowlist": ["alice"]}},
            }
        }
        receive_trigger(
            conn,
            source="github",
            scope="comments",
            thread_key="github:pr-202",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=False,
            kind="comment",
            body="Review comment B",
            idempotency_key="cmp_b",
            now=1762190000.0,
            config=cfg_b,
        )
        batch = assemble_batch(conn, thread_key="github:pr-202", now=1762190005.0, config=cfg_b)
        run_eval(batch, db_path_or_conn=conn, config=cfg_b, now=1762190008.0)

        assert len(calls_b) == 1
        assert recorded_tokens["without_pt"] > 0

    # With pass-through uses strictly 0 tokens, saving 100% of Router decisions and tokens
    assert recorded_tokens["with_pt"] == 0
    assert recorded_tokens["without_pt"] >= 100

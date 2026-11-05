"""Tests for superagent eval engine per E-016, E-017 (Card 2.5).

Verifies:
(1) Batch assembly merges triggers arriving mid-Eval into the NEXT batch.
(2) Batch cap 20 is enforced.
(3) Third expiry records outcome 'unavailable' (eval_requeues up to 2).
(4) Unparseable model JSON records outcome 'unavailable' (E-016).
(5) Observe-mode positive outcome records would_accept=1 and tasks table is unchanged.
(6) Explicit outcome records Decision accept with correct strength ('explicit').
(7) Decision rows exist for ignore and stop outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import (
    assemble_batch,
    expire_evals,
    receive_trigger,
    run_eval,
)


def _count_tasks(db_path: Path) -> int:
    """Return the total number of rows in the tasks table."""
    with kbc.connect_closing(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def test_batch_merges_triggers_arriving_mid_eval_into_next_batch(tmp_path: Path) -> None:
    """Triggers arriving while an Eval runs join the NEXT batch, never the running one (E-017)."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    config = {
        "superagent": {
            "sources": {"telegram": {"allowlist": ["user_1"]}},
        }
    }

    # 1. Trigger 1 arrives
    trg1, disp1 = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:thread:1",
        actor_source_id="user_1",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="first trigger",
        idempotency_key="trg_key_1",
        now=1000,
    )
    assert disp1 == "queued"

    # 2. Assemble Batch 1 for this thread
    batch1 = assemble_batch(db_path, thread_key="tg:thread:1", now=1001, config=config)
    assert batch1 is not None
    assert batch1["trigger_ids"] == [trg1]
    assert len(batch1["triggers"]) == 1
    assert batch1["triggers"][0]["eval_state"] == "pending"

    # 3. Trigger 2 arrives while Eval for Batch 1 is in-flight
    trg2, disp2 = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:thread:1",
        actor_source_id="user_1",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="second trigger mid-eval",
        idempotency_key="trg_key_2",
        now=1002,
    )
    assert disp2 == "queued"
    assert trg2 != trg1

    # Batch 1 in memory remains unmodified
    assert batch1["trigger_ids"] == [trg1]

    # Attempting to assemble a new batch while batch 1 is in-flight returns None
    assert assemble_batch(db_path, thread_key="tg:thread:1", now=1003, config=config) is None

    # 4. Complete Eval for Batch 1
    dec1 = run_eval(batch1, db_path_or_conn=db_path, config=config, now=1004)
    assert dec1["outcome"] == "accept"
    assert dec1["trigger_ids"] == [trg1]

    # 5. Now Batch 1 is done; assemble the NEXT batch for this thread
    batch2 = assemble_batch(db_path, thread_key="tg:thread:1", now=1005, config=config)
    assert batch2 is not None
    assert batch2["trigger_ids"] == [trg2]
    assert len(batch2["triggers"]) == 1

    dec2 = run_eval(batch2, db_path_or_conn=db_path, config=config, now=1006)
    assert dec2["outcome"] == "accept"
    assert dec2["trigger_ids"] == [trg2]


def test_batch_cap_20_enforced(tmp_path: Path) -> None:
    """Enforces batch size cap of ≤20 triggers per batch (E-017)."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # Insert 25 triggers for the same thread
    inserted_ids: list[str] = []
    for i in range(25):
        tid, _ = receive_trigger(
            db_path,
            source="slack",
            scope="main",
            thread_key="slack:channel:batch-cap",
            actor_source_id=f"user_{i}",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body=f"batch item {i}",
            idempotency_key=f"slack_msg_{i}",
            now=1000 + i,
        )
        inserted_ids.append(tid)

    # Assemble batch
    batch = assemble_batch(db_path, thread_key="slack:channel:batch-cap", now=2000)
    assert batch is not None
    assert len(batch["triggers"]) == 20
    assert len(batch["trigger_ids"]) == 20
    assert batch["trigger_ids"] == inserted_ids[:20]

    # Verify 5 remain queued in the database
    with kbc.connect_closing(db_path) as conn:
        queued_count = conn.execute(
            "SELECT COUNT(*) FROM sa_triggers WHERE thread_key = ? AND eval_state = 'queued'",
            ("slack:channel:batch-cap",),
        ).fetchone()[0]
        assert queued_count == 5

        pending_count = conn.execute(
            "SELECT COUNT(*) FROM sa_triggers WHERE thread_key = ? AND eval_state = 'pending'",
            ("slack:channel:batch-cap",),
        ).fetchone()[0]
        assert pending_count == 20

    # Finish batch 1
    run_eval(batch, db_path_or_conn=db_path, now=2001)

    # Assemble batch 2 -> remaining 5 triggers are assembled
    batch2 = assemble_batch(db_path, thread_key="slack:channel:batch-cap", now=2002)
    assert batch2 is not None
    assert len(batch2["triggers"]) == 5
    assert batch2["trigger_ids"] == inserted_ids[20:]


def test_third_expiry_records_unavailable(tmp_path: Path) -> None:
    """Expiry requeues batch up to 2 times; the third expiry records outcome 'unavailable'."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    tid, disp = receive_trigger(
        db_path,
        source="discord",
        scope="main",
        thread_key="discord:ch:exp",
        actor_source_id="member_1",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="test expiry",
        idempotency_key="disc_msg_1",
        now=1000,
    )
    assert disp == "queued"

    # --- Attempt 1 (requeues = 0) ---
    batch1 = assemble_batch(db_path, thread_key="discord:ch:exp", now=1000, timeout_seconds=60)
    assert batch1 is not None
    assert batch1["eval_deadline"] == 1060

    # Expiry 1 fires at t=1070
    exp1 = expire_evals(now=1070, db_path_or_conn=db_path)
    assert len(exp1) == 1
    assert exp1[0]["action"] == "requeued"
    assert exp1[0]["eval_requeues"] == 1

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT eval_state, eval_requeues FROM sa_triggers WHERE id = ?", (tid,)).fetchone()
        assert row["eval_state"] == "queued"
        assert row["eval_requeues"] == 1
        dec_count = conn.execute("SELECT COUNT(*) FROM sa_decisions").fetchone()[0]
        assert dec_count == 0

    # --- Attempt 2 (requeues = 1) ---
    batch2 = assemble_batch(db_path, thread_key="discord:ch:exp", now=1080, timeout_seconds=60)
    assert batch2 is not None
    assert batch2["eval_deadline"] == 1140

    # Expiry 2 fires at t=1150
    exp2 = expire_evals(now=1150, db_path_or_conn=db_path)
    assert len(exp2) == 1
    assert exp2[0]["action"] == "requeued"
    assert exp2[0]["eval_requeues"] == 2

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT eval_state, eval_requeues FROM sa_triggers WHERE id = ?", (tid,)).fetchone()
        assert row["eval_state"] == "queued"
        assert row["eval_requeues"] == 2
        dec_count = conn.execute("SELECT COUNT(*) FROM sa_decisions").fetchone()[0]
        assert dec_count == 0

    # --- Attempt 3 (requeues = 2) ---
    batch3 = assemble_batch(db_path, thread_key="discord:ch:exp", now=1160, timeout_seconds=60)
    assert batch3 is not None
    assert batch3["eval_deadline"] == 1220

    # Expiry 3 fires at t=1230 -> 3rd expiry!
    exp3 = expire_evals(now=1230, db_path_or_conn=db_path)
    assert len(exp3) == 1
    assert exp3[0]["action"] == "unavailable"
    assert exp3[0]["eval_requeues"] == 3

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT eval_state, eval_requeues, disposition FROM sa_triggers WHERE id = ?", (tid,)).fetchone()
        assert row["eval_state"] == "done"
        assert row["disposition"] == "unavailable"
        assert row["eval_requeues"] == 3

        dec_rows = conn.execute("SELECT * FROM sa_decisions").fetchall()
        assert len(dec_rows) == 1
        d = dec_rows[0]
        assert d["outcome"] == "unavailable"
        assert d["thread_key"] == "discord:ch:exp"
        assert tid in json.loads(d["trigger_ids_json"])


def test_unparseable_model_json_records_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    """Unparseable model JSON leads to outcome 'unavailable' per E-016."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    config = {
        "superagent": {
            "sources": {"telegram": {"allowlist": ["member_alice"]}},
        }
    }

    # addressed=False with listed user -> Pure gate produces 'inferred'
    tid, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:unparse",
        actor_source_id="member_alice",
        actor_is_bot=False,
        addressed=False,
        kind="message",
        body="deploy the service",
        idempotency_key="unp_key_1",
        now=1000,
    )

    batch = assemble_batch(db_path, thread_key="tg:unparse", now=1001, config=config)
    assert batch is not None

    # Mock call_llm returning unparseable text
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kw: "NOT VALID JSON {missing quotes",
    )

    decision = run_eval(batch, db_path_or_conn=db_path, config=config, now=1002)
    assert decision["outcome"] == "unavailable"
    assert decision["strength"] == "inferred"
    assert decision["would_accept"] == 0

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT outcome, would_accept FROM sa_decisions WHERE id = ?", (decision["id"],)).fetchone()
        assert row["outcome"] == "unavailable"
        assert row["would_accept"] == 0

        trg = conn.execute("SELECT eval_state, disposition FROM sa_triggers WHERE id = ?", (tid,)).fetchone()
        assert trg["eval_state"] == "done"
        assert trg["disposition"] == "unavailable"


def test_observe_mode_positive_records_would_accept_and_tasks_unchanged(tmp_path: Path, monkeypatch: Any) -> None:
    """In observe mode, positive outcomes record would_accept=1 and tasks table is unchanged."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # Pre-condition: tasks table has 0 rows
    assert _count_tasks(db_path) == 0

    # 1. Test explicit admission with accept_mode='observe'
    config_explicit = {
        "superagent": {
            "accept_mode": "observe",
            "sources": {"telegram": {"allowlist": ["user_admin"]}},
        }
    }
    _tid1, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:obs:1",
        actor_source_id="user_admin",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="build component",
        idempotency_key="obs_1",
        now=1000,
    )

    batch1 = assemble_batch(db_path, thread_key="tg:obs:1", now=1001, config=config_explicit)
    assert batch1 is not None

    dec1 = run_eval(batch1, db_path_or_conn=db_path, config=config_explicit, now=1002)
    assert dec1["outcome"] == "accept"
    assert dec1["would_accept"] == 1
    assert dec1["strength"] == "explicit"

    # Tasks table must still be 0!
    assert _count_tasks(db_path) == 0

    # 2. Test inferred admission with mode='observe'
    config_inferred = {
        "superagent": {
            "mode": "observe",
            "sources": {"telegram": {"allowlist": ["user_member"]}},
        }
    }
    _tid2, _ = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:obs:2",
        actor_source_id="user_member",
        actor_is_bot=False,
        addressed=False,
        kind="message",
        body="fix typo in readme",
        idempotency_key="obs_2",
        now=1003,
    )

    batch2 = assemble_batch(db_path, thread_key="tg:obs:2", now=1004, config=config_inferred)
    assert batch2 is not None

    mock_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps({
                        "outcome": "accept",
                        "matched_rules": ["auto_fix"],
                        "reason": {"text": "safe fix"},
                    })
                )
            )
        ]
    )
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: mock_resp)

    dec2 = run_eval(batch2, db_path_or_conn=db_path, config=config_inferred, now=1005)
    assert dec2["outcome"] == "accept"
    assert dec2["would_accept"] == 1
    assert dec2["strength"] == "inferred"
    assert dec2["matched_rules"] == ["auto_fix"]

    # Tasks table must remain completely untouched (0 rows)
    assert _count_tasks(db_path) == 0

    with kbc.connect_closing(db_path) as conn:
        rows = conn.execute("SELECT id, outcome, would_accept, strength FROM sa_decisions ORDER BY id ASC").fetchall()
        assert len(rows) == 2
        assert rows[0]["outcome"] == "accept"
        assert rows[0]["would_accept"] == 1
        assert rows[0]["strength"] == "explicit"
        assert rows[1]["outcome"] == "accept"
        assert rows[1]["would_accept"] == 1
        assert rows[1]["strength"] == "inferred"


def test_explicit_outcome_records_accept_with_correct_strength(tmp_path: Path) -> None:
    """Explicit outcome records Decision accept with correct strength ('explicit')."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    config = {
        "superagent": {
            "accept_mode": "auto",
            "owner_identities": ["owner_dan"],
            "sources": {"github": {"allowlist": ["owner_dan"]}},
        }
    }

    _tid, _ = receive_trigger(
        db_path,
        source="github",
        scope="main",
        thread_key="gh:issue:42",
        actor_source_id="owner_dan",
        actor_is_bot=False,
        addressed=True,
        kind="issue",
        body="critical bug in payment handler",
        idempotency_key="gh_issue_42",
        now=1000,
    )

    batch = assemble_batch(db_path, thread_key="gh:issue:42", now=1001, config=config)
    assert batch is not None

    dec = run_eval(batch, db_path_or_conn=db_path, config=config, now=1002)
    assert dec["outcome"] == "accept"
    assert dec["strength"] == "explicit"
    assert dec["would_accept"] == 0  # auto mode, not observe

    with kbc.connect_closing(db_path) as conn:
        row = conn.execute("SELECT * FROM sa_decisions WHERE id = ?", (dec["id"],)).fetchone()
        assert row["outcome"] == "accept"
        assert row["strength"] == "explicit"
        assert row["would_accept"] == 0
        assert row["thread_key"] == "gh:issue:42"


def test_decision_rows_exist_for_ignore_and_stop_outcomes(tmp_path: Path, monkeypatch: Any) -> None:
    """Decision rows are recorded for ignore and stop outcomes too."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    config = {
        "superagent": {
            "sources": {"telegram": {"allowlist": ["member_sam"]}},
        }
    }

    # Case A: Model verdict is 'stop'
    receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:stop:thread",
        actor_source_id="member_sam",
        actor_is_bot=False,
        addressed=False,
        kind="message",
        body="stop worker execution immediately",
        idempotency_key="tg_stop_1",
        now=1000,
    )
    batch_stop = assemble_batch(db_path, thread_key="tg:stop:thread", now=1001, config=config)
    assert batch_stop is not None

    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kw: json.dumps({"outcome": "stop", "reason": "emergency stop command"}),
    )
    dec_stop = run_eval(batch_stop, db_path_or_conn=db_path, config=config, now=1002)
    assert dec_stop["outcome"] == "stop"
    assert dec_stop["strength"] == "inferred"

    # Case B: Model verdict is 'ignore'
    receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:ignore:thread",
        actor_source_id="member_sam",
        actor_is_bot=False,
        addressed=False,
        kind="message",
        body="just a casual comment",
        idempotency_key="tg_ignore_1",
        now=1003,
    )
    batch_ignore = assemble_batch(db_path, thread_key="tg:ignore:thread", now=1004, config=config)
    assert batch_ignore is not None

    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kw: json.dumps({"outcome": "ignore", "reason": "conversation only"}),
    )
    dec_ignore = run_eval(batch_ignore, db_path_or_conn=db_path, config=config, now=1005)
    assert dec_ignore["outcome"] == "ignore"
    assert dec_ignore["strength"] == "inferred"

    # Case C: Not admitted trigger (unlisted user)
    receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:unlisted:thread",
        actor_source_id="unlisted_stranger",
        actor_is_bot=False,
        addressed=False,
        kind="message",
        body="hello stranger",
        idempotency_key="tg_unlisted_1",
        now=1006,
    )
    batch_unlisted = assemble_batch(db_path, thread_key="tg:unlisted:thread", now=1007, config=config)
    assert batch_unlisted is not None

    dec_unlisted = run_eval(batch_unlisted, db_path_or_conn=db_path, config=config, now=1008)
    assert dec_unlisted["outcome"] == "ignore"
    assert dec_unlisted["strength"] == "not_admitted"

    with kbc.connect_closing(db_path) as conn:
        rows = conn.execute("SELECT thread_key, outcome, strength FROM sa_decisions ORDER BY id ASC").fetchall()
        assert len(rows) == 3
        assert rows[0]["outcome"] == "stop"
        assert rows[0]["strength"] == "inferred"
        assert rows[1]["outcome"] == "ignore"
        assert rows[1]["strength"] == "inferred"
        assert rows[2]["outcome"] == "ignore"
        assert rows[2]["strength"] == "not_admitted"

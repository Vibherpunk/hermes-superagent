"""Tests for superagent orchestrator trigger fan-out and subscription delivery (Phase 3.2).

Verifies:
(1) Person + owning subscription -> inputs written, disposition fanned_out, eval_state done (NOT queued).
(2) Person + context-only subscription -> inputs written, eval_state queued.
(3) Bot + owning subscription -> inputs written, eval_state queued.
(4) Actor NULL + owning subscription -> inputs written, eval_state queued.
(5) Person + no subscription -> no inputs written, eval_state queued.
(6) Correlation thread delivers context-only to primary thread's subscribers and creates no ownership on correlated thread.
(7) Correlation never fans out beyond primary thread's subscribers.
(8) Duplicate trigger (same idempotency key) writes no second inputs.
(9) Multiple subscribers on thread (owning + context-only) with person trigger -> disposition fanned_out.
"""

from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import receive_trigger


def _add_subscription(
    db_path: Path,
    *,
    thread_key: str,
    ticket_id: str,
    context_only: int = 0,
    created_at: int = 1000,
) -> None:
    """Helper to insert an sa_subscriptions row directly for testing."""
    with kbc.connect_closing(db_path) as conn:
        with kbc.write_txn(conn):
            conn.execute(
                """
                INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (thread_key, ticket_id, int(context_only), int(created_at)),
            )


def test_fanout_person_with_owning_subscription(tmp_path: Path) -> None:
    """Person message + owning subscription writes inputs, marks disposition fanned_out and eval_state done."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    _add_subscription(db_path, thread_key="tg:chat:100", ticket_id="t_task_1", context_only=0)

    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:100",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Please resolve bug #42",
        idempotency_key="msg_001",
        now=1700000000,
    )

    assert disp == "fanned_out"
    assert trg_id.startswith("trg_")

    with kbc.connect_closing(db_path) as conn:
        # Verify trigger table terminal marking
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row is not None
        assert trg_row["eval_state"] == "done"
        assert trg_row["disposition"] == "fanned_out"

        # Verify sa_inputs row written
        inputs = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchall()
        assert len(inputs) == 1
        inp = inputs[0]
        assert inp["ticket_id"] == "t_task_1"
        assert inp["trigger_id"] == trg_id
        assert inp["kind"] == "trigger"
        assert inp["body"] == "Please resolve bug #42"
        assert inp["is_person"] == 1
        assert inp["context_only"] == 0
        assert inp["read_at"] is None
        assert inp["created_at"] == 1700000000


def test_fanout_person_with_context_only_subscription(tmp_path: Path) -> None:
    """Person message + context-only subscription writes inputs as context and still queues for Eval."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    _add_subscription(db_path, thread_key="tg:chat:100", ticket_id="t_task_1", context_only=1)

    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:100",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Just FYI update",
        idempotency_key="msg_002",
        now=1700000001,
    )

    assert disp == "queued"

    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row is not None
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        inputs = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchall()
        assert len(inputs) == 1
        inp = inputs[0]
        assert inp["ticket_id"] == "t_task_1"
        assert inp["is_person"] == 1
        assert inp["context_only"] == 1


def test_fanout_bot_with_owning_subscription(tmp_path: Path) -> None:
    """Bot trigger + owning subscription writes inputs with is_person=0 and still queues for Eval."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    _add_subscription(db_path, thread_key="tg:chat:100", ticket_id="t_task_1", context_only=0)

    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:100",
        actor_source_id="ci_bot",
        actor_is_bot=True,
        addressed=False,
        kind="status",
        body="Tests passed on main",
        idempotency_key="msg_003",
        now=1700000002,
    )

    assert disp == "queued"

    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row is not None
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        inputs = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchall()
        assert len(inputs) == 1
        inp = inputs[0]
        assert inp["ticket_id"] == "t_task_1"
        assert inp["is_person"] == 0
        assert inp["context_only"] == 0


def test_fanout_actor_null_with_owning_subscription(tmp_path: Path) -> None:
    """Actor NULL trigger + owning subscription is treated as non-person (is_person=0) and queues for Eval."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    _add_subscription(db_path, thread_key="tg:chat:100", ticket_id="t_task_1", context_only=0)

    trg_id, disp = receive_trigger(
        db_path,
        source="system",
        scope="main",
        thread_key="tg:chat:100",
        actor_source_id=None,
        actor_is_bot=False,
        addressed=False,
        kind="webhook",
        body="Anonymous webhook event",
        idempotency_key="msg_004",
        now=1700000003,
    )

    assert disp == "queued"

    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row is not None
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        inputs = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchall()
        assert len(inputs) == 1
        inp = inputs[0]
        assert inp["ticket_id"] == "t_task_1"
        assert inp["is_person"] == 0
        assert inp["context_only"] == 0


def test_fanout_person_with_no_subscription(tmp_path: Path) -> None:
    """Person message with no subscriptions writes no inputs and queues for Eval normally."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:unsubscribed",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="New prompt from human",
        idempotency_key="msg_005",
        now=1700000004,
    )

    assert disp == "queued"

    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row is not None
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        # Zero inputs written
        inputs = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchall()
        assert len(inputs) == 0


def test_fanout_correlation_delivers_context_only_to_primary_subscribers(tmp_path: Path) -> None:
    """Correlation thread delivers context-only to primary thread's subscribers and creates no ownership on correlated thread."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # Ticket subscribed to primary thread with context_only=0 (owning)
    _add_subscription(db_path, thread_key="primary:thread:1", ticket_id="t_primary", context_only=0)

    # Trigger arrives on correlated thread with primary thread in correlation
    trg_id, disp = receive_trigger(
        db_path,
        source="github",
        scope="main",
        thread_key="correlated:thread:pr42",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=False,
        kind="pull_request",
        body="PR #42 opened for issue",
        correlation=["primary:thread:1"],
        idempotency_key="msg_006",
        now=1700000005,
    )

    # Trigger queues for Eval normally (correlation creates NO ownership on correlated thread)
    assert disp == "queued"

    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row is not None
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        # Delivered to primary subscriber as CONTEXT-ONLY (context_only=1)
        inputs = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchall()
        assert len(inputs) == 1
        inp = inputs[0]
        assert inp["ticket_id"] == "t_primary"
        assert inp["trigger_id"] == trg_id
        assert inp["kind"] == "trigger"
        assert inp["body"] == "PR #42 opened for issue"
        assert inp["is_person"] == 1
        assert inp["context_only"] == 1

        # No ownership row created in sa_subscriptions for correlated:thread:pr42
        subs_corr = conn.execute(
            "SELECT * FROM sa_subscriptions WHERE thread_key = 'correlated:thread:pr42'"
        ).fetchall()
        assert len(subs_corr) == 0


def test_fanout_correlation_never_fans_out_beyond_primary_subscribers(tmp_path: Path) -> None:
    """Correlation delivery only delivers to primary thread subscribers, not to unrelated threads."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    _add_subscription(db_path, thread_key="primary:thread:1", ticket_id="t_primary", context_only=0)
    _add_subscription(db_path, thread_key="unrelated:thread:99", ticket_id="t_unrelated", context_only=0)

    trg_id, disp = receive_trigger(
        db_path,
        source="github",
        scope="main",
        thread_key="correlated:thread:ci",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=False,
        kind="check_run",
        body="Check run completed",
        correlation=["primary:thread:1"],
        idempotency_key="msg_007",
        now=1700000006,
    )

    assert disp == "queued"

    with kbc.connect_closing(db_path) as conn:
        inputs = conn.execute("SELECT * FROM sa_inputs WHERE trigger_id = ?", (trg_id,)).fetchall()
        assert len(inputs) == 1
        assert inputs[0]["ticket_id"] == "t_primary"

        unrelated_inputs = conn.execute(
            "SELECT * FROM sa_inputs WHERE ticket_id = 't_unrelated'"
        ).fetchall()
        assert len(unrelated_inputs) == 0


def test_fanout_duplicate_trigger_writes_no_second_inputs(tmp_path: Path) -> None:
    """Duplicate trigger (same idempotency key) writes no second inputs."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    _add_subscription(db_path, thread_key="tg:chat:100", ticket_id="t_task_1", context_only=0)

    # First trigger: fresh insert
    trg_id1, disp1 = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:100",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Original command",
        idempotency_key="msg_idem_dup",
        now=1700000010,
    )

    assert disp1 == "fanned_out"

    with kbc.connect_closing(db_path) as conn:
        inputs_count1 = conn.execute("SELECT COUNT(*) FROM sa_inputs").fetchone()[0]
        assert inputs_count1 == 1

    # Second trigger: duplicate idempotency key
    trg_id2, disp2 = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:100",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Duplicate command",
        idempotency_key="msg_idem_dup",
        now=1700000011,
    )

    assert trg_id2 == trg_id1
    assert disp2 == "duplicate"

    # Confirms no second inputs written
    with kbc.connect_closing(db_path) as conn:
        inputs_count2 = conn.execute("SELECT COUNT(*) FROM sa_inputs").fetchone()[0]
        assert inputs_count2 == 1


def test_fanout_multiple_subscribers_on_thread(tmp_path: Path) -> None:
    """Multiple subscribers on thread: owning + context-only both receive inputs, disposition fanned_out."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    _add_subscription(db_path, thread_key="tg:chat:multi", ticket_id="t_owning", context_only=0)
    _add_subscription(db_path, thread_key="tg:chat:multi", ticket_id="t_context", context_only=1)

    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:multi",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Message to multi-subscribed thread",
        idempotency_key="msg_multi_001",
        now=1700000020,
    )

    assert disp == "fanned_out"

    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row is not None
        assert trg_row["eval_state"] == "done"
        assert trg_row["disposition"] == "fanned_out"

        inputs = conn.execute(
            "SELECT ticket_id, context_only, is_person FROM sa_inputs WHERE trigger_id = ? ORDER BY ticket_id",
            (trg_id,),
        ).fetchall()
        assert len(inputs) == 2
        # t_context
        assert inputs[0]["ticket_id"] == "t_context"
        assert inputs[0]["context_only"] == 1
        assert inputs[0]["is_person"] == 1
        # t_owning
        assert inputs[1]["ticket_id"] == "t_owning"
        assert inputs[1]["context_only"] == 0
        assert inputs[1]["is_person"] == 1


def test_fanout_disabled_via_passthrough_false(tmp_path: Path) -> None:
    """When superagent.passthrough is False, fan-out is disabled and trigger queues for Eval (Card 3.8)."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    _add_subscription(db_path, thread_key="tg:chat:pt_off", ticket_id="t_pt_task", context_only=0)

    cfg_disabled = {"superagent": {"passthrough": False}}

    trg_id, disp = receive_trigger(
        db_path,
        source="telegram",
        scope="main",
        thread_key="tg:chat:pt_off",
        actor_source_id="user_alice",
        actor_is_bot=False,
        addressed=True,
        kind="message",
        body="Message with passthrough disabled",
        idempotency_key="msg_pt_001",
        now=1700000030,
        config=cfg_disabled,
    )

    # Fan-out was disabled: trigger enters normal Eval queue, no inputs written
    assert disp == "queued"

    with kbc.connect_closing(db_path) as conn:
        trg_row = conn.execute("SELECT * FROM sa_triggers WHERE id = ?", (trg_id,)).fetchone()
        assert trg_row is not None
        assert trg_row["eval_state"] == "queued"
        assert trg_row["disposition"] is None

        inputs = conn.execute(
            "SELECT * FROM sa_inputs WHERE trigger_id = ?",
            (trg_id,),
        ).fetchall()
        assert len(inputs) == 0

"""Surfaces Rev 3 — WRD U4 accept restructure (addendum A31-2/A31-3/A31-11).

_accept_on_conn creates the task INSIDE the single mandate transaction:
task + mandate + decision link + outbox ack + owning subscription commit
or roll back together. The A31-2 entry guard fails closed when the
caller already holds a transaction. The A31-11 raced double-accept runs
two top-level connections, barrier-synchronized inside the first
caller's transaction, and asserts BOTH callers passed the pre-txn
early-return while ticket_id was still NULL and both entered the txn
path (counted via a write_txn spy), converging on exactly one of each
artifact.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kanban_db_module
import hermes_cli.orchestrator as orchestrator
import hermes_cli.superagent_outbox as outbox_module
import hermes_cli.superagent_subscriptions as subscriptions_module
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import accept, assemble_batch, run_eval

NOW = 1000

CONFIG = {
    "superagent": {
        "mode": "auto",
        "owner_identities": {"telegram": "owner-1"},
        "sources": {"telegram": {"allowlist": ["owner-1"]}},
    }
}


def _setup_decision(tmp_path: Path) -> tuple[Path, str]:
    """One accepted-by-Eval decision, not yet accepted into a ticket."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as conn:
        from hermes_cli.orchestrator import receive_trigger

        receive_trigger(
            db_path,
            source="telegram",
            scope="chat-1",
            thread_key="thread-accept",
            actor_source_id="owner-1",
            actor_is_bot=False,
            addressed=True,
            kind="message",
            body="Build the report\nFull details here",
            idempotency_key="accept-scenario-1",
            now=NOW,
            config=CONFIG,
        )
    batch = assemble_batch(db_path, thread_key="thread-accept", now=NOW + 1, config=CONFIG)
    assert batch is not None
    decision = run_eval(batch, db_path_or_conn=db_path, config=CONFIG, now=NOW + 2)
    assert decision["outcome"] == "accept"
    return db_path, decision["id"]


def _counts(db_path: Path) -> dict:
    with kbc.connect_closing(db_path) as conn:
        return {
            "tasks": conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"],
            "mandates": conn.execute("SELECT COUNT(*) c FROM sa_mandates").fetchone()["c"],
            "subscriptions": conn.execute(
                "SELECT COUNT(*) c FROM sa_subscriptions WHERE context_only = 0"
            ).fetchone()["c"],
            "outbox": conn.execute("SELECT COUNT(*) c FROM sa_outbox").fetchone()["c"],
            "ticket_id": conn.execute("SELECT ticket_id FROM sa_decisions").fetchone()["ticket_id"],
        }


def test_accept_commits_task_mandate_subscription_outbox_together(tmp_path):
    db_path, dec_id = _setup_decision(tmp_path)
    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=NOW + 3, config=CONFIG)
    assert ticket_id
    counts = _counts(db_path)
    assert counts == {
        "tasks": 1, "mandates": 1, "subscriptions": 1, "outbox": 1,
        "ticket_id": ticket_id,
    }
    # Idempotent re-accept: same ticket, still exactly one of each.
    with kbc.connect_closing(db_path) as conn:
        again = accept(conn, dec_id, now=NOW + 4, config=CONFIG)
    assert again == ticket_id
    assert _counts(db_path)["tasks"] == 1


def test_subscribe_failure_rolls_back_everything_then_retry_succeeds(tmp_path, monkeypatch):
    db_path, dec_id = _setup_decision(tmp_path)

    def _boom(conn, **kwargs):
        raise RuntimeError("subscribe exploded")

    monkeypatch.setattr(subscriptions_module, "subscribe", _boom)
    with kbc.connect_closing(db_path) as conn:
        with pytest.raises(RuntimeError, match="subscribe exploded"):
            accept(conn, dec_id, now=NOW + 3, config=CONFIG)
    counts = _counts(db_path)
    assert counts["tasks"] == 0
    assert counts["mandates"] == 0
    assert counts["subscriptions"] == 0
    assert counts["ticket_id"] is None

    monkeypatch.undo()
    with kbc.connect_closing(db_path) as conn:
        ticket_id = accept(conn, dec_id, now=NOW + 4, config=CONFIG)
    assert ticket_id
    counts = _counts(db_path)
    assert counts["tasks"] == 1
    assert counts["mandates"] == 1
    assert counts["subscriptions"] == 1
    assert counts["outbox"] == 1


def test_outbox_failure_after_create_rolls_back_task(tmp_path, monkeypatch):
    """A throw AFTER create_task inside the txn (here: queue_reply) must
    leave NO task row — the pre-restructure code leaked exactly that."""
    db_path, dec_id = _setup_decision(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("outbox exploded")

    monkeypatch.setattr(outbox_module, "queue_reply", _boom)
    with kbc.connect_closing(db_path) as conn:
        with pytest.raises(RuntimeError, match="outbox exploded"):
            accept(conn, dec_id, now=NOW + 3, config=CONFIG)
    counts = _counts(db_path)
    assert counts["tasks"] == 0
    assert counts["mandates"] == 0
    assert counts["ticket_id"] is None


def test_entry_guard_fails_closed_inside_caller_transaction(tmp_path):
    """A31-2: accept attempted while the caller already holds a
    transaction raises and writes nothing (the in-txn re-read's
    serialization guarantee only holds for an outermost write_txn)."""
    db_path, dec_id = _setup_decision(tmp_path)
    conn = kbc.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError):
            accept(conn, dec_id, now=NOW + 3, config=CONFIG)
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    counts = _counts(db_path)
    assert counts["tasks"] == 0
    assert counts["mandates"] == 0
    assert counts["ticket_id"] is None
    # After the caller releases, accept works normally.
    with kbc.connect_closing(db_path) as conn:
        assert accept(conn, dec_id, now=NOW + 4, config=CONFIG)


def test_raced_double_accept_converges(tmp_path, monkeypatch):
    """A31-11: two top-level connections race one decision. The first
    caller is held INSIDE its write_txn (via a create_task barrier)
    while the second caller runs: the second must pass the pre-txn
    early-return with ticket_id still NULL and enter the txn path
    (write_txn spy entries = 2, each observing committed ticket_id
    NULL), block on the writer lock (its busy_timeout is set explicitly
    to 120000 and read back), then re-read inside its own txn and
    return the SAME ticket id. Exactly one of each artifact exists."""
    db_path, dec_id = _setup_decision(tmp_path)

    real_create_task = kanban_db_module.create_task
    real_write_txn = orchestrator.write_txn
    first_inside_txn = threading.Event()
    release_first = threading.Event()
    state = {"create_calls": 0}
    lock = threading.Lock()

    def _barrier_create_task(conn, **kwargs):
        with lock:
            state["create_calls"] += 1
            first = state["create_calls"] == 1
        if first:
            first_inside_txn.set()
            assert release_first.wait(timeout=30), "test barrier never released"
        return real_create_task(conn, **kwargs)

    txn_entries: list = []

    @contextmanager
    def _spy_write_txn(conn, *, allow_nested=False):
        # Record the COMMITTED ticket_id visible to a fresh reader at
        # the moment this caller enters the accept txn path.
        import sqlite3

        probe = sqlite3.connect(str(db_path))
        try:
            row = probe.execute(
                "SELECT ticket_id FROM sa_decisions WHERE id = ?", (dec_id,)
            ).fetchone()
        finally:
            probe.close()
        txn_entries.append(row[0] if row else "missing")
        with real_write_txn(conn, allow_nested=allow_nested) as inner:
            yield inner

    monkeypatch.setattr(kanban_db_module, "create_task", _barrier_create_task)
    monkeypatch.setattr(orchestrator, "write_txn", _spy_write_txn)

    results: dict = {}
    errors: dict = {}

    def _caller(name: str) -> None:
        conn = kbc.connect(db_path)  # created inside the thread (sqlite affinity)
        try:
            if name == "second":
                conn.execute("PRAGMA busy_timeout = 120000")
                readback = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                assert readback == 120000, f"busy_timeout read-back {readback}"
                results["second_busy_timeout"] = readback
            else:
                conn.execute("PRAGMA busy_timeout = 120000")
            results[name] = accept(conn, dec_id, now=NOW + 3, config=CONFIG)
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors[name] = exc
        finally:
            conn.close()

    t1 = threading.Thread(target=_caller, args=("first",))
    t1.start()
    assert first_inside_txn.wait(timeout=30), "first caller never entered its txn"
    t2 = threading.Thread(target=_caller, args=("second",))
    t2.start()
    # Wait until the second caller has entered the txn path (its spy
    # entry) while the first still holds the writer lock.
    deadline = threading.Event()
    waited = 0.0
    while len(txn_entries) < 2 and waited < 20:
        deadline.wait(0.05)
        waited += 0.05
    assert len(txn_entries) == 2, f"txn entries: {txn_entries}"
    assert txn_entries == [None, None], (
        f"both callers must enter with committed ticket_id NULL, got {txn_entries}"
    )
    release_first.set()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive()
    assert errors == {}, errors
    assert results["first"] is not None
    assert results["first"] == results["second"]
    assert results["second_busy_timeout"] == 120000
    assert state["create_calls"] == 1  # the loser re-read and returned, never created

    counts = _counts(db_path)
    assert counts["tasks"] == 1
    assert counts["mandates"] == 1
    assert counts["subscriptions"] == 1
    assert counts["outbox"] == 1
    assert counts["ticket_id"] == results["first"]

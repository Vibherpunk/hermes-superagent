"""Tests for Superagent thread-ticket subscription storage API."""

from __future__ import annotations

from pathlib import Path

import pytest
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.superagent_subscriptions import (
    drop_thread_subscriptions,
    subscribe,
    subscriptions_for,
    threads_for,
    unsubscribe,
)


@pytest.fixture
def conn(tmp_path: Path):
    """Temporary Kanban database initialized via repo schema-init path."""
    db_path = tmp_path / "subscriptions_test.db"
    kbc.init_db(db_path)
    with kbc.connect_closing(db_path) as c:
        yield c


def test_idempotent_subscribe(conn) -> None:
    """Subscribing the same (thread_key, ticket_id) twice yields one row."""
    thread_key = "slack:C123:1700000000.000100"
    ticket_id = "t_alpha01"

    subscribe(conn, ticket_id, thread_key, context_only=False, now=1000)
    subscribe(conn, ticket_id, thread_key, context_only=False, now=2000)

    rows = conn.execute(
        "SELECT thread_key, ticket_id, context_only, created_at FROM sa_subscriptions "
        "WHERE thread_key = ? AND ticket_id = ?",
        (thread_key, ticket_id),
    ).fetchall()

    assert len(rows) == 1
    row = dict(rows[0])
    assert row["thread_key"] == thread_key
    assert row["ticket_id"] == ticket_id
    assert row["context_only"] == 0
    assert row["created_at"] == 1000


def test_context_only_flip_on_resubscribe(conn) -> None:
    """If the pair exists with a different context_only, the new call's context_only wins."""
    thread_key = "discord:ch456:th789"
    ticket_id = "t_alpha02"

    # Start as owning subscription (context_only=False)
    subscribe(conn, ticket_id, thread_key, context_only=False, now=1000)
    subs = subscriptions_for(conn, thread_key)
    assert len(subs) == 1
    assert subs[0]["context_only"] is False

    # Flip to context_only=True -> new flag wins, still one row
    subscribe(conn, ticket_id, thread_key, context_only=True, now=2000)
    subs = subscriptions_for(conn, thread_key)
    assert len(subs) == 1
    assert subs[0]["context_only"] is True
    assert subs[0]["created_at"] == 1000

    # Flip back to context_only=False -> flag updates, still one row
    subscribe(conn, ticket_id, thread_key, context_only=False, now=3000)
    subs = subscriptions_for(conn, thread_key)
    assert len(subs) == 1
    assert subs[0]["context_only"] is False


def test_unsubscribe(conn) -> None:
    """unsubscribe returns True if a row was removed, False otherwise."""
    thread_key = "tg:chat999:th1"
    ticket_id = "t_alpha03"

    # Unsubscribe when nonexistent returns False
    assert unsubscribe(conn, ticket_id, thread_key) is False

    subscribe(conn, ticket_id, thread_key, context_only=False)
    assert len(subscriptions_for(conn, thread_key)) == 1

    # Unsubscribe existing row returns True
    assert unsubscribe(conn, ticket_id, thread_key) is True
    assert len(subscriptions_for(conn, thread_key)) == 0

    # Second unsubscribe returns False
    assert unsubscribe(conn, ticket_id, thread_key) is False

    # Nonexistent ticket or thread returns False
    assert unsubscribe(conn, "t_nonexistent", thread_key) is False
    assert unsubscribe(conn, ticket_id, "unknown_thread") is False


def test_drop_thread_subscriptions(conn) -> None:
    """drop_thread_subscriptions removes owning AND context-only rows for that thread only.

    A second thread's rows survive intact, and the function returns the removed row count.
    """
    thread_a = "matrix:roomA:thread1"
    thread_b = "matrix:roomB:thread2"

    # Populate thread_a with owning and context-only subscriptions
    subscribe(conn, "t_ticket1", thread_a, context_only=False, now=100)
    subscribe(conn, "t_ticket2", thread_a, context_only=True, now=101)

    # Populate thread_b with subscriptions
    subscribe(conn, "t_ticket3", thread_b, context_only=False, now=200)
    subscribe(conn, "t_ticket1", thread_b, context_only=True, now=201)

    assert len(subscriptions_for(conn, thread_a)) == 2
    assert len(subscriptions_for(conn, thread_b)) == 2

    # Drop thread_a subscriptions
    removed_count = drop_thread_subscriptions(conn, thread_a)
    assert removed_count == 2

    # thread_a subscriptions are gone
    assert subscriptions_for(conn, thread_a) == []

    # thread_b subscriptions survive intact
    surviving_b = subscriptions_for(conn, thread_b)
    assert len(surviving_b) == 2
    assert [s["ticket_id"] for s in surviving_b] == ["t_ticket3", "t_ticket1"]

    # Calling drop again returns 0
    assert drop_thread_subscriptions(conn, thread_a) == 0


def test_subscriptions_for(conn) -> None:
    """subscriptions_for returns all subscriptions for a thread with boolean context_only flags."""
    thread_key = "slack:C555:th001"

    assert subscriptions_for(conn, thread_key) == []

    subscribe(conn, "t_sub1", thread_key, context_only=False, now=150)
    subscribe(conn, "t_sub2", thread_key, context_only=True, now=160)

    subs = subscriptions_for(conn, thread_key)
    assert len(subs) == 2

    assert subs[0] == {
        "thread_key": thread_key,
        "ticket_id": "t_sub1",
        "context_only": False,
        "created_at": 150,
    }
    assert subs[1] == {
        "thread_key": thread_key,
        "ticket_id": "t_sub2",
        "context_only": True,
        "created_at": 160,
    }


def test_threads_for(conn) -> None:
    """threads_for lists both owning and context-only threads subscribed to a ticket."""
    ticket_id = "t_shared01"

    assert threads_for(conn, ticket_id) == []

    # Subscribe ticket to thread 1 (owning) and thread 2 (context-only)
    subscribe(conn, ticket_id, "thread_primary", context_only=False, now=10)
    subscribe(conn, ticket_id, "thread_context", context_only=True, now=20)

    # Another ticket on thread 3 to ensure ticket isolation
    subscribe(conn, "t_other", "thread_unrelated", context_only=False, now=30)

    threads = threads_for(conn, ticket_id)
    assert threads == ["thread_primary", "thread_context"]

    # Other ticket only sees its thread
    assert threads_for(conn, "t_other") == ["thread_unrelated"]

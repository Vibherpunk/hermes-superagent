"""Tests for Super-agent PR following and follows-done rule (Phase 3, Card 3.4).

Verifies:
1. Worker's report path hooks:
   - Run summary naming PR number for configured repo subscribes Ticket (owning) to github:pr-<n>.
   - Run metadata naming PR number (int, digit str, or dict) for configured repo subscribes Ticket.
   - Worker calls kanban_comment containing PR URL (parse github.com/<owner>/<repo>/pull/<n>) subscribes Ticket.
   - PR for a different (non-configured) repo does NOT subscribe Ticket.
2. GitHub Source close/merge trigger kinds:
   - Closed PR with merged_at or merged=True maps to trigger kind 'merge'.
   - Closed PR without merge maps to trigger kind 'close'.
   - Open PR maps to 'pull_request'.
3. Follows-done rule:
   - open -> comment -> merge lifecycle ends Ticket with outcome 'merged'.
   - close with an unread person Input does not end Ticket.
   - when unread person Input is subsequently marked read, follows-done ends Ticket.
   - multiple opened PRs: Ticket ends only when all opened PRs close.
   - Ticket following a PR it did not open ends when all followed PRs close.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.orchestrator import (
    apply_follows_done_rule,
    get_ticket_followed_prs,
    get_ticket_opened_prs,
    receive_trigger,
    set_configured_github_repo,
    subscribe_ticket_to_pr,
)
from hermes_cli.superagent_delivery import deliver_inputs, mark_inputs_read
from hermes_cli.superagent_github import _extract_kind, SCOPE_PRS, SCOPE_NOTIFICATIONS
from hermes_cli.superagent_pr import (
    extract_pr_numbers_from_comment,
    extract_pr_numbers_from_report,
    get_configured_github_repo,
)
from tools.kanban_tools import _handle_comment


CONFIGURED_REPO = "testowner/testrepo"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Initialize a fresh kanban DB with superagent schema and configured repo."""
    path = tmp_path / "board.db"
    kbc.init_db(path)
    with kbc.connect_closing(path) as conn:
        set_configured_github_repo(conn, CONFIGURED_REPO)
    return path


def _create_test_ticket(conn: sqlite3.Connection, ticket_id: str = "t_pr_1") -> str:
    """Helper to create a ticket task in the database."""
    tid = kb.create_task(
        conn,
        title="Test Superagent PR task",
        initial_status="running",
    )
    with kbc.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET id = ?, mandate_id = 1, ticket_kind = 'coding' WHERE id = ?",
            (ticket_id, tid),
        )
    return ticket_id


# ============================================================================
# 1. URL and Report Parsing Unit Tests
# ============================================================================

def test_extract_pr_numbers_from_comment() -> None:
    """Parse github.com/<owner>/<repo>/pull/<n> matching the configured repo."""
    repo = "testowner/testrepo"

    # Exact matching URLs
    body1 = "Created PR: https://github.com/testowner/testrepo/pull/42 for review."
    assert extract_pr_numbers_from_comment(body1, repo) == [42]

    # Without protocol, different casing
    body2 = "See github.com/TESTOWNER/TESTREPO/pull/108"
    assert extract_pr_numbers_from_comment(body2, repo) == [108]

    # Non-configured repo is ignored
    body3 = "https://github.com/otherorg/otherrepo/pull/42"
    assert extract_pr_numbers_from_comment(body3, repo) == []

    # Multiple PRs in one comment
    body4 = "PRs https://github.com/testowner/testrepo/pull/1 and https://github.com/testowner/testrepo/pull/2"
    assert extract_pr_numbers_from_comment(body4, repo) == [1, 2]


def test_extract_pr_numbers_from_report() -> None:
    """Extract PR numbers from summary and metadata for configured repo."""
    repo = "testowner/testrepo"

    # Metadata int
    assert extract_pr_numbers_from_report(metadata={"pr": 42}, configured_repo=repo) == [42]
    assert extract_pr_numbers_from_report(metadata={"pr_number": 43}, configured_repo=repo) == [43]
    assert extract_pr_numbers_from_report(metadata={"pull_request": 44}, configured_repo=repo) == [44]

    # Metadata published_pr URL
    meta_pub = {"published_pr": "https://github.com/testowner/testrepo/pull/50"}
    assert extract_pr_numbers_from_report(metadata=meta_pub, configured_repo=repo) == [50]

    # Metadata with explicit other repo is ignored
    meta_other = {"pr": 42, "repo": "otherorg/otherrepo"}
    assert extract_pr_numbers_from_report(metadata=meta_other, configured_repo=repo) == []

    # Summary naming PR number
    assert extract_pr_numbers_from_report(summary="Fixed issue in PR #60", configured_repo=repo) == [60]
    assert extract_pr_numbers_from_report(summary="Opened pull request 61", configured_repo=repo) == [61]
    assert extract_pr_numbers_from_report(summary="PR 62 submitted", configured_repo=repo) == [62]

    # Without configured repo, nothing is returned
    assert extract_pr_numbers_from_report(summary="PR #60", metadata={"pr": 42}, configured_repo=None) == []


# ============================================================================
# 2. Worker Report Path Hooks
# ============================================================================

def test_kanban_comment_with_pr_url_subscribes_ticket(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker calling kanban_comment with PR URL subscribes the Ticket (owning)."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_comm_pr")
    with kbc.connect_closing(db_path) as conn:
        _create_test_ticket(conn, "t_comm_pr")

        # Comment with PR URL
        body = "I opened https://github.com/testowner/testrepo/pull/77"
        kb.add_comment(conn, "t_comm_pr", author="worker:sa", body=body)

        # Verify subscribed to github:pr-77 with context_only = 0 (owning)
        subs = conn.execute(
            "SELECT thread_key, ticket_id, context_only FROM sa_subscriptions WHERE ticket_id = 't_comm_pr'"
        ).fetchall()
        assert len(subs) == 1
        assert subs[0][0] == "github:pr-77"
        assert subs[0][1] == "t_comm_pr"
        assert subs[0][2] == 0  # owning

        # Verify recorded as opened PR
        assert get_ticket_opened_prs(conn, "t_comm_pr") == ["github:pr-77"]


def test_kanban_comment_tool_handler_subscribes_ticket(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling kanban_comment tool handler directly subscribes the owning ticket."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_tool_pr")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with kbc.connect_closing(db_path) as conn:
        _create_test_ticket(conn, "t_tool_pr")

    resp_str = _handle_comment({"task_id": "t_tool_pr", "body": "See https://github.com/testowner/testrepo/pull/88"})
    resp = json.loads(resp_str)
    assert resp.get("ok") is True

    with kbc.connect_closing(db_path) as conn:
        subs = get_ticket_followed_prs(conn, "t_tool_pr")
        assert subs == ["github:pr-88"]
        assert get_ticket_opened_prs(conn, "t_tool_pr") == ["github:pr-88"]


def test_run_report_summary_and_metadata_subscribes_ticket(db_path: Path) -> None:
    """Run summary/metadata naming a PR number subscribes Ticket (owning)."""
    with kbc.connect_closing(db_path) as conn:
        _create_test_ticket(conn, "t_run_pr")

        # End run with summary and metadata
        kb.complete_task(
            conn,
            "t_run_pr",
            summary="All tests pass, opened PR #99",
            metadata={"pr": 99},
        )

        subs = conn.execute(
            "SELECT thread_key, ticket_id, context_only FROM sa_subscriptions WHERE ticket_id = 't_run_pr'"
        ).fetchall()
        assert len(subs) == 1
        assert subs[0][0] == "github:pr-99"
        assert subs[0][2] == 0  # owning
        assert get_ticket_opened_prs(conn, "t_run_pr") == ["github:pr-99"]


# ============================================================================
# 3. GitHub Source Trigger Kinds
# ============================================================================

def test_github_source_extract_kind_close_and_merge() -> None:
    """GitHub Source gains close/merge Trigger kinds for closed and merged PRs."""
    # Open PR -> pull_request
    open_pr = {"state": "open", "merged_at": None, "merged": False}
    assert _extract_kind(SCOPE_PRS, open_pr) == "pull_request"

    # Closed and merged PR -> merge
    merged_pr1 = {"state": "closed", "merged_at": "2026-11-03T12:00:00Z", "merged": True}
    assert _extract_kind(SCOPE_PRS, merged_pr1) == "merge"

    merged_pr2 = {"state": "closed", "action": "merged"}
    assert _extract_kind(SCOPE_PRS, merged_pr2) == "merge"

    # Closed but unmerged PR -> close
    closed_unmerged = {"state": "closed", "merged_at": None, "merged": False}
    assert _extract_kind(SCOPE_PRS, closed_unmerged) == "close"

    closed_action = {"action": "closed"}
    assert _extract_kind(SCOPE_PRS, closed_action) == "close"

    # Notifications scope
    notif_merged = {"subject": {"type": "PullRequest"}, "merged": True}
    assert _extract_kind(SCOPE_NOTIFICATIONS, notif_merged) == "merge"

    notif_closed = {"subject": {"type": "PullRequest", "state": "closed"}}
    assert _extract_kind(SCOPE_NOTIFICATIONS, notif_closed) == "close"


# ============================================================================
# 4. Lifecycle & Follows-Done Tests (Primary Card Contracts)
# ============================================================================

def test_open_comment_merge_lifecycle_ends_ticket_with_outcome_merged(db_path: Path) -> None:
    """Tests: open->comment->merge lifecycle ends the Ticket with outcome merged."""
    with kbc.connect_closing(db_path) as conn:
        tid = "t_lifecycle_1"
        _create_test_ticket(conn, tid)

        # 1. OPEN: Worker reports PR #42 via summary / metadata or comment
        kb.add_comment(
            conn,
            tid,
            author="worker:sa",
            body="Created PR: https://github.com/testowner/testrepo/pull/42",
        )

        # Verify Ticket is subscribed (owning) to github:pr-42
        subs = conn.execute(
            "SELECT thread_key, context_only FROM sa_subscriptions WHERE ticket_id = ?",
            (tid,),
        ).fetchall()
        assert len(subs) == 1
        assert subs[0][0] == "github:pr-42"
        assert subs[0][1] == 0  # owning
        assert get_ticket_opened_prs(conn, tid) == ["github:pr-42"]

        # 2. COMMENT: Comment trigger arrives on github:pr-42
        comm_trg_id, disp = receive_trigger(
            conn,
            source="github",
            scope="comments",
            thread_key="github:pr-42",
            actor_source_id="reviewer_alice",
            actor_is_bot=False,
            addressed=False,
            kind="comment",
            body="LGTM! Merging now.",
            idempotency_key="comm_trg_01",
            now=1700000100.0,
        )
        assert disp == "fanned_out"

        # Verify comment delivered as Input to ticket
        inputs = conn.execute(
            "SELECT id, trigger_id, is_person, read_at FROM sa_inputs WHERE ticket_id = ?",
            (tid,),
        ).fetchall()
        assert len(inputs) == 1
        assert inputs[0][1] == comm_trg_id
        assert inputs[0][2] == 1  # person

        # Worker reads the delivered input
        rendered, delivered_ids = deliver_inputs(conn, tid, now=1700000110)
        assert delivered_ids == [inputs[0][0]]
        assert rendered is not None

        # Verify input marked as read
        unread = conn.execute(
            "SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ? AND read_at IS NULL",
            (tid,),
        ).fetchone()[0]
        assert unread == 0

        # Ticket is still active before merge
        task_before = kb.get_task(conn, tid)
        assert task_before.status != "done"

        # 3. MERGE: Merge trigger arrives on github:pr-42
        _merge_trg_id, _ = receive_trigger(
            conn,
            source="github",
            scope="prs",
            thread_key="github:pr-42",
            actor_source_id="merger_bob",
            actor_is_bot=False,
            addressed=False,
            kind="merge",
            body="Merged pull request #42",
            meta={"merged": True, "state": "closed"},
            idempotency_key="merge_trg_01",
            now=1700000200.0,
        )

        # 4. Follows-done verification: Ticket ends with outcome merged
        task_after = kb.get_task(conn, tid)
        assert task_after is not None
        assert task_after.status == "done"
        assert task_after.result == "merged"

        # Verify task_runs outcome is 'merged'
        run_row = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert run_row is not None
        assert run_row["status"] == "done"
        assert run_row["outcome"] == "merged"

        # Verify task_events recorded completion with outcome merged
        event_row = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert event_row is not None
        payload = json.loads(event_row["payload"])
        assert payload.get("outcome") == "merged"


def test_close_with_unread_person_input_does_not_end_ticket(db_path: Path) -> None:
    """Tests: close with an unread person Input does not end it."""
    with kbc.connect_closing(db_path) as conn:
        tid = "t_unread_1"
        _create_test_ticket(conn, tid)

        # Worker opens PR #42
        kb.add_comment(
            conn,
            tid,
            author="worker:sa",
            body="PR created: https://github.com/testowner/testrepo/pull/42",
        )

        # An unread person Input arrives (e.g. from alice)
        receive_trigger(
            conn,
            source="github",
            scope="comments",
            thread_key="github:pr-42",
            actor_source_id="alice",
            actor_is_bot=False,
            addressed=False,
            kind="comment",
            body="Wait! Found an edge case bug, please check.",
            idempotency_key="alice_comm_01",
            now=1700000100.0,
        )

        # Verify unread person input exists
        unread_person_count = conn.execute(
            "SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ? AND is_person = 1 AND read_at IS NULL",
            (tid,),
        ).fetchone()[0]
        assert unread_person_count == 1

        # Now close trigger arrives (PR closed)
        receive_trigger(
            conn,
            source="github",
            scope="prs",
            thread_key="github:pr-42",
            actor_source_id="admin",
            actor_is_bot=False,
            addressed=False,
            kind="close",
            body="Closed PR #42",
            meta={"merged": False, "state": "closed"},
            idempotency_key="close_trg_01",
            now=1700000200.0,
        )

        # Ticket must NOT end because resumed work is unfinished (unread person Input)
        task = kb.get_task(conn, tid)
        assert task.status != "done"
        assert task.status in ("running", "ready")

        # Now the worker reads the person Input
        _, delivered_ids = deliver_inputs(conn, tid, now=1700000250)
        assert len(delivered_ids) >= 1

        # Re-apply follows-done rule now that work is read
        ended = apply_follows_done_rule(conn, tid, now=1700000260.0)
        assert ended is True

        # Ticket is now ended
        task_ended = kb.get_task(conn, tid)
        assert task_ended.status == "done"
        assert task_ended.result == "closed"


def test_multiple_opened_prs_all_must_close(db_path: Path) -> None:
    """When a ticket opened multiple PRs, ticket ends only when ALL opened PRs close."""
    with kbc.connect_closing(db_path) as conn:
        tid = "t_multi_pr"
        _create_test_ticket(conn, tid)

        # Ticket opens PR #10 and PR #11
        subscribe_ticket_to_pr(conn, tid, 10, opened=True)
        subscribe_ticket_to_pr(conn, tid, 11, opened=True)

        assert set(get_ticket_opened_prs(conn, tid)) == {"github:pr-10", "github:pr-11"}

        # Close PR #10 first
        receive_trigger(
            conn,
            source="github",
            scope="prs",
            thread_key="github:pr-10",
            actor_source_id="merger",
            actor_is_bot=True,
            addressed=False,
            kind="merge",
            body="PR #10 merged",
            idempotency_key="m10",
            now=1700000100.0,
        )

        # Ticket must NOT end because PR #11 remains open
        task = kb.get_task(conn, tid)
        assert task.status != "done"

        # Now close PR #11
        receive_trigger(
            conn,
            source="github",
            scope="prs",
            thread_key="github:pr-11",
            actor_source_id="merger",
            actor_is_bot=True,
            addressed=False,
            kind="merge",
            body="PR #11 merged",
            idempotency_key="m11",
            now=1700000200.0,
        )

        # Both opened PRs are closed -> Ticket ends with outcome merged
        task_done = kb.get_task(conn, tid)
        assert task_done.status == "done"
        assert task_done.result == "merged"


def test_ticket_with_no_opened_prs_ends_when_all_followed_close(db_path: Path) -> None:
    """When a ticket opened no PRs (e.g. reviewing external PR), ends when all followed close."""
    with kbc.connect_closing(db_path) as conn:
        tid = "t_follow_only"
        _create_test_ticket(conn, tid)

        # Subscribed to PR #20 with opened=False
        subscribe_ticket_to_pr(conn, tid, 20, opened=False)
        assert get_ticket_opened_prs(conn, tid) == []
        assert get_ticket_followed_prs(conn, tid) == ["github:pr-20"]

        # PR #20 merges
        receive_trigger(
            conn,
            source="github",
            scope="prs",
            thread_key="github:pr-20",
            actor_source_id="merger",
            actor_is_bot=True,
            addressed=False,
            kind="merge",
            body="PR #20 merged",
            idempotency_key="m20",
            now=1700000100.0,
        )

        # Ticket ends
        task_done = kb.get_task(conn, tid)
        assert task_done.status == "done"
        assert task_done.result == "merged"

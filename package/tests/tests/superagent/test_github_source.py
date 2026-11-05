"""Tests for GitHub Source poller (Card 2.8).

Verifies:
1. First poll creates expected triggers with right thread keys, actor, and bot flags.
   (Includes '[bot]' login fixture and type-Bot fixture).
2. Second identical poll creates ZERO new triggers.
3. Simulated restart (fresh in-memory state, same DB) poll creates ZERO duplicates.
4. receive_all=false restricts to notifications only.
5. CI conclusion item maps to 'github:ci-<branch>' thread key.
6. Guard checks on superagent.enabled and repository.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.superagent_github import poll_github


FIXTURE_NOTIFICATIONS = [
    {
        "id": "notif-101",
        "unread": True,
        "reason": "mention",
        "updated_at": "2026-11-03T10:00:00Z",
        "subject": {
            "title": "Bug in worker loop",
            "url": "https://api.github.com/repos/testowner/testrepo/issues/42",
            "type": "Issue",
        },
        "repository": {
            "full_name": "testowner/testrepo",
        },
        "actor": {
            "login": "alice",
            "type": "User",
        },
    }
]

FIXTURE_ISSUES = [
    {
        "id": 1001,
        "number": 43,
        "title": "Automated security bump",
        "body": "Bumping dependency xyz",
        "state": "open",
        "created_at": "2026-11-03T10:05:00Z",
        "updated_at": "2026-11-03T10:05:00Z",
        "user": {
            "login": "dependabot[bot]",
            "type": "User",  # Note: type is User, but login ends with [bot]
        },
        "html_url": "https://github.com/testowner/testrepo/issues/43",
    }
]

FIXTURE_PRS = [
    {
        "id": 2001,
        "number": 44,
        "title": "New feature from custom bot",
        "body": "PR description",
        "state": "open",
        "created_at": "2026-11-03T10:10:00Z",
        "updated_at": "2026-11-03T10:10:00Z",
        "user": {
            "login": "code-synthesizer",
            "type": "Bot",  # Note: login doesn't end with [bot], but type is Bot
        },
        "html_url": "https://github.com/testowner/testrepo/pull/44",
    }
]

FIXTURE_COMMENTS = [
    {
        "id": 3001,
        "body": "Please review this change.",
        "created_at": "2026-11-03T10:15:00Z",
        "updated_at": "2026-11-03T10:15:00Z",
        "user": {
            "login": "charlie",
            "type": "User",
        },
        "issue_url": "https://api.github.com/repos/testowner/testrepo/issues/43",
        "html_url": "https://github.com/testowner/testrepo/issues/43#issuecomment-3001",
    }
]

FIXTURE_CI = {
    "total_count": 1,
    "workflow_runs": [
        {
            "id": 4001,
            "name": "Integration Tests",
            "head_branch": "main",
            "head_sha": "abc1234567890",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-03T10:20:00Z",
            "updated_at": "2026-11-03T10:20:00Z",
            "actor": {
                "login": "ci-runner",
                "type": "User",
            },
            "html_url": "https://github.com/testowner/testrepo/actions/runs/4001",
        }
    ],
}


def make_fake_gh_run(calls_log: list[list[str]] | None = None):
    """Factory for fake gh_run returning recorded JSON fixtures."""
    def fake_gh_run(args: list[str]) -> tuple[bool, str, str]:
        if calls_log is not None:
            calls_log.append(list(args))
        endpoint = ""
        for a in args:
            if a != "api" and not a.startswith("-"):
                endpoint = a
                break
        if "notifications" in endpoint:
            return True, json.dumps(FIXTURE_NOTIFICATIONS), ""
        if "issues/comments" in endpoint:
            return True, json.dumps(FIXTURE_COMMENTS), ""
        if "issues" in endpoint:
            return True, json.dumps(FIXTURE_ISSUES), ""
        if "pulls" in endpoint:
            return True, json.dumps(FIXTURE_PRS), ""
        if "actions/runs" in endpoint:
            return True, json.dumps(FIXTURE_CI), ""
        return False, "[]", f"unknown endpoint: {endpoint}"
    return fake_gh_run


def _base_config(receive_all: bool | dict = True, enabled: bool = True, repo: str = "testowner/testrepo") -> dict:
    return {
        "superagent": {
            "enabled": enabled,
            "github": {
                "repository": repo,
                "receive_all": receive_all,
            },
        }
    }


def test_first_poll_creates_expected_triggers(tmp_path: Path) -> None:
    """First poll creates expected triggers with correct thread keys, actors, and bot flags."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    with kbc.connect_closing(db_path) as conn:
        calls: list[list[str]] = []
        created = poll_github(conn, _base_config(receive_all=True), now=1700000000.0, gh_run=make_fake_gh_run(calls))
        assert created == 5

        # Check triggers in DB
        rows = conn.execute(
            """
            SELECT source, scope, thread_key, actor_source_id, actor_is_bot, kind, idempotency_key
            FROM sa_triggers ORDER BY id ASC
            """
        ).fetchall()
        assert len(rows) == 5

        triggers_by_scope = {r[1]: r for r in rows}

        # 1. Notifications
        n_trig = triggers_by_scope["notifications"]
        assert n_trig[0] == "github"
        assert n_trig[2] == "github:issue-42"
        assert n_trig[3] == "alice"
        assert n_trig[4] == 0  # actor_is_bot == False

        # 2. Issues: dependabot[bot] fixture (login ends with [bot])
        i_trig = triggers_by_scope["issues"]
        assert i_trig[0] == "github"
        assert i_trig[2] == "github:issue-43"
        assert i_trig[3] == "dependabot[bot]"
        assert i_trig[4] == 1  # actor_is_bot == True via [bot] login suffix

        # 3. PRs: code-synthesizer fixture (user type == 'Bot')
        p_trig = triggers_by_scope["prs"]
        assert p_trig[0] == "github"
        assert p_trig[2] == "github:pr-44"
        assert p_trig[3] == "code-synthesizer"
        assert p_trig[4] == 1  # actor_is_bot == True via type == 'Bot'

        # 4. Comments
        c_trig = triggers_by_scope["comments"]
        assert c_trig[0] == "github"
        assert c_trig[2] == "github:issue-43"
        assert c_trig[3] == "charlie"
        assert c_trig[4] == 0

        # 5. CI
        ci_trig = triggers_by_scope["ci"]
        assert ci_trig[0] == "github"
        assert ci_trig[2] == "github:ci-main"
        assert ci_trig[3] == "ci-runner"

        # Verify cursors updated for all scopes
        cursor_rows = conn.execute("SELECT scope, cursor FROM sa_source_cursors WHERE source = 'github'").fetchall()
        cursor_map = dict(cursor_rows)
        assert set(cursor_map.keys()) == {"notifications", "issues", "prs", "comments", "ci"}
        for scope, cur in cursor_map.items():
            assert cur is not None and cur != "", f"Cursor for {scope} should be set"


def test_second_identical_poll_creates_zero_new_triggers(tmp_path: Path) -> None:
    """Second identical poll creates ZERO new triggers (both cursor and idempotency dedupe)."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    with kbc.connect_closing(db_path) as conn:
        gh_run = make_fake_gh_run()
        created1 = poll_github(conn, _base_config(receive_all=True), now=1700000000.0, gh_run=gh_run)
        assert created1 == 5

        # Second poll with identical fake_gh_run responses
        created2 = poll_github(conn, _base_config(receive_all=True), now=1700000060.0, gh_run=gh_run)
        assert created2 == 0

        # sa_triggers count unchanged
        count = conn.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 5


def test_simulated_restart_creates_zero_duplicates(tmp_path: Path) -> None:
    """Simulated restart (fresh in-memory state, same DB) poll creates ZERO duplicates."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    # First session
    with kbc.connect_closing(db_path) as conn:
        poll_github(conn, _base_config(receive_all=True), now=1700000000.0, gh_run=make_fake_gh_run())

    # Simulated restart: completely new connection, fresh in-memory process state
    with kbc.connect_closing(db_path) as conn_after_restart:
        created = poll_github(conn_after_restart, _base_config(receive_all=True), now=1700000100.0, gh_run=make_fake_gh_run())
        assert created == 0

        # No duplicate rows created in sa_triggers
        count = conn_after_restart.execute("SELECT COUNT(*) FROM sa_triggers").fetchone()[0]
        assert count == 5


def test_receive_all_false_restricts_to_notifications_only(tmp_path: Path) -> None:
    """receive_all=false restricts polling to notifications only."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    with kbc.connect_closing(db_path) as conn:
        calls: list[list[str]] = []
        created = poll_github(conn, _base_config(receive_all=False), now=1700000000.0, gh_run=make_fake_gh_run(calls))
        assert created == 1

        # Only notifications were polled
        for call_args in calls:
            endpoint = next(a for a in call_args if a != "api" and not a.startswith("-"))
            assert "notifications" in endpoint

        # Only one trigger exists in sa_triggers
        rows = conn.execute("SELECT scope, thread_key FROM sa_triggers").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "notifications"

        # Only notifications cursor exists
        cursor_scopes = [r[0] for r in conn.execute("SELECT scope FROM sa_source_cursors WHERE source = 'github'").fetchall()]
        assert cursor_scopes == ["notifications"]


def test_ci_conclusion_maps_to_ci_thread_key(tmp_path: Path) -> None:
    """A CI conclusion item maps to 'github:ci-<branch>' thread key."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    custom_ci = {
        "total_count": 1,
        "workflow_runs": [
            {
                "id": 9001,
                "name": "E2E Tests",
                "head_branch": "release-v2.1",
                "head_sha": "def456",
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-11-03T12:00:00Z",
                "updated_at": "2026-11-03T12:00:00Z",
                "actor": {"login": "octocat", "type": "User"},
                "html_url": "https://github.com/testowner/testrepo/actions/runs/9001",
            }
        ],
    }

    def fake_gh_ci(args: list[str]) -> tuple[bool, str, str]:
        endpoint = next(a for a in args if a != "api" and not a.startswith("-"))
        if "actions/runs" in endpoint:
            return True, json.dumps(custom_ci), ""
        return True, "[]", ""

    config = {
        "superagent": {
            "enabled": True,
            "github": {
                "repository": "testowner/testrepo",
                "receive_all": {"ci": True},
            },
        }
    }

    with kbc.connect_closing(db_path) as conn:
        created = poll_github(conn, config, now=1700000000.0, gh_run=fake_gh_ci)
        assert created == 1

        row = conn.execute("SELECT thread_key, kind, actor_source_id FROM sa_triggers WHERE scope = 'ci'").fetchone()
        assert row is not None
        assert row[0] == "github:ci-release-v2.1"
        assert row[1] == "ci"
        assert row[2] == "octocat"


def test_guards_enabled_and_repository(tmp_path: Path) -> None:
    """Returns 0 without executing gh_run if disabled or repository is empty."""
    db_path = tmp_path / "board.db"
    kbc.init_db(db_path)

    calls: list[list[str]] = []
    gh_run = make_fake_gh_run(calls)

    with kbc.connect_closing(db_path) as conn:
        # 1. Disabled
        c1 = poll_github(conn, _base_config(enabled=False), now=1700000000.0, gh_run=gh_run)
        assert c1 == 0
        assert len(calls) == 0

        # 2. Empty repository
        c2 = poll_github(conn, _base_config(enabled=True, repo=""), now=1700000000.0, gh_run=gh_run)
        assert c2 == 0
        assert len(calls) == 0

        # 3. None repository
        c3 = poll_github(conn, {"superagent": {"enabled": True, "github": {"repository": None}}}, now=1700000000.0, gh_run=gh_run)
        assert c3 == 0
        assert len(calls) == 0

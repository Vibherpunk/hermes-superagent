"""GitHub Source poller for Super-agent (Lane B / Card 2.8).

Integration note (gateway dispatcher tick wiring):
    The kanban/superagent dispatcher tick loop in the gateway is intended to call:
        from hermes_cli.superagent_github import poll_github
        poll_github(conn, config, now=time.time())
    This runs periodically when `config['superagent']['enabled']` is True and
    `config['superagent']['github']['repository']` is set.
    Per Phase 2 rules, gateway files are NOT edited directly in this card.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import logging
import re
import sqlite3
from typing import Any

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.orchestrator import receive_trigger

logger = logging.getLogger(__name__)

# Scopes supported by the GitHub poller
SCOPE_NOTIFICATIONS = "notifications"
SCOPE_ISSUES = "issues"
SCOPE_PRS = "prs"
SCOPE_COMMENTS = "comments"
SCOPE_CI = "ci"

ALL_SCOPES = (
    SCOPE_NOTIFICATIONS,
    SCOPE_ISSUES,
    SCOPE_PRS,
    SCOPE_COMMENTS,
    SCOPE_CI,
)


def _default_gh_run(args: list[str]) -> tuple[bool, str, str]:
    """Invoke the repository's established gh subprocess helper."""
    from hermes_cli.web_git import _gh
    return _gh(".", args)


def _call_gh_api(gh_run: Callable[..., Any], args: list[str]) -> Any:
    """Invoke gh_run and parse JSON output. Returns None on failure."""
    try:
        try:
            res = gh_run(args)
        except TypeError:
            res = gh_run(".", args)
    except Exception as exc:  # health: allow BLE001 -- gh subprocess invocation failure boundary
        logger.warning("gh subprocess call failed: %s", exc)
        return None

    if isinstance(res, tuple):
        ok = bool(res[0])
        stdout = str(res[1]) if len(res) > 1 else ""
        if not ok:
            return None
    elif isinstance(res, str):
        stdout = res
    elif hasattr(res, "returncode") and hasattr(res, "stdout"):
        if res.returncode != 0:
            return None
        stdout = str(res.stdout)
    elif isinstance(res, (dict, list)):
        return res
    else:
        stdout = str(res)

    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def _get_cursor(conn: sqlite3.Connection, scope: str) -> str | None:
    """Retrieve the stored cursor for a scope from sa_source_cursors."""
    row = conn.execute(
        "SELECT cursor FROM sa_source_cursors WHERE source = 'github' AND scope = ?",
        (scope,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    return None


def _set_cursor(conn: sqlite3.Connection, scope: str, cursor: str, now: float) -> None:
    """Update stored cursor for a scope in sa_source_cursors."""
    with write_txn(conn, allow_nested=True):
        conn.execute(
            """
            INSERT INTO sa_source_cursors (source, scope, cursor, updated_at)
            VALUES ('github', ?, ?, ?)
            ON CONFLICT (source, scope) DO UPDATE SET
                cursor = excluded.cursor,
                updated_at = excluded.updated_at
            """,
            (scope, str(cursor), int(now)),
        )


def _extract_thread_key(scope: str, item: dict[str, Any]) -> str:
    """Map an item to thread_key: github:issue-<n> / github:pr-<n> / github:ci-<branch>."""
    if scope == SCOPE_CI:
        branch = item.get("head_branch") or item.get("branch") or "main"
        return f"github:ci-{branch}"

    if scope == SCOPE_PRS:
        num = item.get("number") or item.get("id") or 0
        return f"github:pr-{num}"

    if scope == SCOPE_ISSUES:
        num = item.get("number") or item.get("id") or 0
        return f"github:issue-{num}"

    if scope == SCOPE_COMMENTS:
        url = item.get("issue_url") or item.get("html_url") or ""
        match = re.search(r"/(issues|pulls|pull)/(\d+)", url)
        if match:
            kind_str, num_str = match.group(1), match.group(2)
            if kind_str in ("pulls", "pull"):
                return f"github:pr-{num_str}"
            return f"github:issue-{num_str}"
        if "issue_number" in item:
            return f"github:issue-{item['issue_number']}"
        return f"github:issue-{item.get('id', 0)}"

    if scope == SCOPE_NOTIFICATIONS:
        subject = item.get("subject") or {}
        subj_type = str(subject.get("type") or "").lower()
        subj_url = str(subject.get("url") or "")
        match = re.search(r"/(issues|pulls|pull)/(\d+)", subj_url)
        if match:
            kind_str, num_str = match.group(1), match.group(2)
            if kind_str in ("pulls", "pull") or subj_type == "pullrequest":
                return f"github:pr-{num_str}"
            return f"github:issue-{num_str}"
        if "pullrequest" in subj_type or subj_type == "pr":
            return f"github:pr-{item.get('id', 0)}"
        if any(ci_sub in subj_type for ci_sub in ("checksuite", "workflow", "ci")):
            branch = item.get("head_branch") or "main"
            return f"github:ci-{branch}"
        return f"github:issue-{item.get('id', 0)}"

    return f"github:{scope}-{item.get('id', 0)}"


def _extract_actor(item: dict[str, Any]) -> tuple[str, bool]:
    """Extract actor login and bot status (endswith '[bot]' OR user type == 'Bot')."""
    user_obj = item.get("user") or item.get("actor") or item.get("sender") or {}
    if not isinstance(user_obj, dict):
        user_obj = {}

    login = str(user_obj.get("login") or item.get("actor_login") or item.get("login") or "")
    user_type = str(user_obj.get("type") or item.get("type") or "")

    actor_is_bot = login.endswith("[bot]") or user_type.lower() == "bot"
    return login, actor_is_bot


def _extract_kind(scope: str, item: dict[str, Any]) -> str:
    """Determine event kind from scope and item."""
    if scope == SCOPE_NOTIFICATIONS:
        subj = item.get("subject") or {}
        stype = str(subj.get("type") or "notification").lower()
        if stype in ("pullrequest", "pr") or "pull" in str(subj.get("url") or ""):
            if item.get("merged") or item.get("merged_at") or subj.get("state") == "merged":
                return "merge"
            if item.get("state") == "closed" or subj.get("state") == "closed":
                return "close"
        return stype
    if scope == SCOPE_ISSUES:
        return "issue"
    if scope == SCOPE_PRS:
        if item.get("merged") or item.get("merged_at") or item.get("action") == "merged":
            return "merge"
        if item.get("state") == "closed" or item.get("action") == "closed":
            return "close"
        return "pull_request"
    if scope == SCOPE_COMMENTS:
        return "comment"
    if scope == SCOPE_CI:
        return "ci"
    return scope


def _extract_body(scope: str, item: dict[str, Any]) -> str:
    """Extract descriptive body text from item."""
    if scope == SCOPE_CI:
        conclusion = item.get("conclusion") or item.get("status") or "completed"
        name = item.get("name") or "run"
        branch = item.get("head_branch") or "main"
        return f"CI {conclusion} for {name} on {branch}"
    if scope == SCOPE_NOTIFICATIONS:
        subj = item.get("subject") or {}
        return str(subj.get("title") or item.get("title") or "")
    return str(item.get("body") or item.get("title") or "")


def _extract_idempotency_key(scope: str, item: dict[str, Any]) -> str:
    """Derive stable idempotency key from item ID or URL."""
    stable_id = item.get("id") or item.get("node_id") or item.get("url") or item.get("html_url")
    return f"github:{scope}:{stable_id}"


def _extract_timestamp(item: dict[str, Any]) -> str:
    """Extract ISO 8601 timestamp string for ordering and cursor comparison."""
    return str(item.get("updated_at") or item.get("created_at") or "")


def _resolve_active_scopes(gh_cfg: dict[str, Any]) -> list[str]:
    """Determine active scopes based on config superagent.github.receive_all."""
    receive_all = gh_cfg.get("receive_all", False)
    active = [SCOPE_NOTIFICATIONS]

    if receive_all is True:
        return list(ALL_SCOPES)

    if isinstance(receive_all, dict):
        if receive_all.get("issues"):
            active.append(SCOPE_ISSUES)
        if receive_all.get("prs") or receive_all.get("pull_requests"):
            active.append(SCOPE_PRS)
        if receive_all.get("comments"):
            active.append(SCOPE_COMMENTS)
        if receive_all.get("ci"):
            active.append(SCOPE_CI)

    return active


def _build_gh_args(scope: str, repo: str, cursor: str | None) -> list[str]:
    """Construct gh api arguments for a given scope and cursor."""
    if scope == SCOPE_NOTIFICATIONS:
        args = ["api", f"repos/{repo}/notifications"]
        if cursor:
            args.extend(["-F", f"since={cursor}"])
        return args

    if scope == SCOPE_ISSUES:
        args = ["api", f"repos/{repo}/issues", "-F", "state=all"]
        if cursor:
            args.extend(["-F", f"since={cursor}"])
        return args

    if scope == SCOPE_PRS:
        args = ["api", f"repos/{repo}/pulls", "-F", "state=all"]
        if cursor:
            args.extend(["-F", "sort=updated", "-F", "direction=desc"])
        return args

    if scope == SCOPE_COMMENTS:
        args = ["api", f"repos/{repo}/issues/comments"]
        if cursor:
            args.extend(["-F", f"since={cursor}"])
        return args

    if scope == SCOPE_CI:
        return ["api", f"repos/{repo}/actions/runs"]

    return ["api", f"repos/{repo}/{scope}"]


def _filter_scope_items(scope: str, raw_items: Any, cursor: str | None) -> list[dict[str, Any]]:
    """Unpack, filter by default branch/conclusion/cursor, and return valid items."""
    if isinstance(raw_items, dict):
        items = raw_items.get("workflow_runs") or raw_items.get("items") or []
    elif isinstance(raw_items, list):
        items = raw_items
    else:
        items = []

    valid_items: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        # In issues endpoint, skip pull requests if prs scope handles them
        if scope == SCOPE_ISSUES and "pull_request" in it:
            continue

        # In CI endpoint, only process completed runs with a conclusion
        if scope == SCOPE_CI:
            if it.get("status") != "completed" and it.get("conclusion") is None:
                continue

        # Cursor check
        ts = _extract_timestamp(it)
        if cursor and ts and ts <= cursor:
            continue

        valid_items.append(it)

    return valid_items


def poll_github(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    now: float,
    gh_run: Callable[..., Any] | None = None,
) -> int:
    """Poll GitHub source and emit triggers to orchestrator per Card 2.8.

    Reads config superagent.github.repository and superagent.github.receive_all.
    Gated on superagent.enabled and repository set.

    Returns the number of triggers created.
    """
    sa_cfg = config.get("superagent", config) if isinstance(config, dict) else {}
    if not isinstance(sa_cfg, dict) or not sa_cfg.get("enabled", False):
        return 0

    gh_cfg = sa_cfg.get("github")
    if not isinstance(gh_cfg, dict):
        return 0

    repo = gh_cfg.get("repository")
    if not repo or not isinstance(repo, str) or not repo.strip():
        return 0
    repo = repo.strip()

    runner = gh_run if gh_run is not None else _default_gh_run
    active_scopes = _resolve_active_scopes(gh_cfg)
    triggers_created = 0

    for scope in active_scopes:
        cursor = _get_cursor(conn, scope)
        gh_args = _build_gh_args(scope, repo, cursor)
        raw_result = _call_gh_api(runner, gh_args)
        if raw_result is None:
            continue

        items = _filter_scope_items(scope, raw_result, cursor)
        processed_timestamps: list[str] = []

        for item in items:
            thread_key = _extract_thread_key(scope, item)
            actor_login, actor_is_bot = _extract_actor(item)
            kind = _extract_kind(scope, item)
            body = _extract_body(scope, item)
            idempotency_key = _extract_idempotency_key(scope, item)

            _trg_id, disposition = receive_trigger(
                conn,
                source="github",
                scope=scope,
                thread_key=thread_key,
                actor_source_id=actor_login or None,
                actor_is_bot=actor_is_bot,
                addressed=False,
                kind=kind,
                body=body,
                meta=item,
                correlation=None,
                idempotency_key=idempotency_key,
                now=now,
            )

            if disposition == "queued":
                triggers_created += 1

            ts = _extract_timestamp(item)
            if ts:
                processed_timestamps.append(ts)

        # Advance cursor only after items are processed
        if processed_timestamps:
            next_cursor = max(processed_timestamps)
            _set_cursor(conn, scope, next_cursor, now)

    return triggers_created

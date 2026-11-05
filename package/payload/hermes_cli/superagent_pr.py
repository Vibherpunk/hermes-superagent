"""PR following and follows-done rule for Super-agent (Card 3.4).

Hooks the worker's report path and kanban_comment to follow PRs for the
configured GitHub repository, and applies the follows-done rule on close/merge
triggers to end tickets when their opened/followed PRs close.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from typing import Any, Optional

from hermes_cli.kanban_db_connect import write_txn

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)

_PR_TEXT_RE = re.compile(
    r"(?:PR|pull\s*request)\s*#?\s*(\d+)",
    re.IGNORECASE,
)

_PR_META_KEYS = (
    "pr",
    "pr_number",
    "pull_request",
    "published_pr",
    "pull",
    "pr_id",
    "pull_number",
    "pull_request_number",
)


def get_configured_github_repo(
    conn: Optional[sqlite3.Connection] = None,
    config: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Retrieve the configured GitHub repository string (e.g. 'owner/repo').

    Checks explicit config dict, database sa_meta, load_config(), and environment.
    """
    if isinstance(config, dict):
        sa = config.get("superagent")
        if isinstance(sa, dict):
            gh = sa.get("github")
            if isinstance(gh, dict) and gh.get("repository"):
                return str(gh["repository"]).strip()
        gh_direct = config.get("github")
        if isinstance(gh_direct, dict) and gh_direct.get("repository"):
            return str(gh_direct["repository"]).strip()

    if conn is not None:
        try:
            row = conn.execute(
                "SELECT value FROM sa_meta WHERE key = 'github_repository'"
            ).fetchone()
            if row and row[0]:
                return str(row[0]).strip()
        except Exception as exc:  # health: allow BLE001 -- db read failure fallback
            logger.debug("get_configured_github_repo sa_meta read failed: %s", exc)

    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        if isinstance(cfg, dict):
            sa_cfg = cfg.get("superagent")
            if isinstance(sa_cfg, dict):
                gh_cfg = sa_cfg.get("github")
                if isinstance(gh_cfg, dict) and gh_cfg.get("repository"):
                    return str(gh_cfg["repository"]).strip()
    except Exception as exc:  # health: allow BLE001 -- load_config fallback
        logger.debug("get_configured_github_repo load_config fallback: %s", exc)

    env_repo = os.environ.get("GITHUB_REPOSITORY")
    if env_repo:
        return env_repo.strip()

    return None


def set_configured_github_repo(conn: sqlite3.Connection, repo: str) -> None:
    """Store configured repository in sa_meta for tests and runtime caching."""
    with write_txn(conn, allow_nested=True):
        conn.execute(
            """
            INSERT INTO sa_meta (key, value) VALUES ('github_repository', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(repo).strip(),),
        )


def extract_pr_numbers_from_comment(body: str, configured_repo: str) -> list[int]:
    """Parse github.com/<owner>/<repo>/pull/<n> matching the configured repo."""
    if not body or not configured_repo:
        return []

    target_repo = configured_repo.strip().lower()
    pr_numbers: list[int] = []

    for match in _PR_URL_RE.finditer(body):
        owner, repo_name, pr_num_str = match.group(1), match.group(2), match.group(3)
        candidate_repo = f"{owner}/{repo_name}".lower()
        if candidate_repo == target_repo:
            try:
                num = int(pr_num_str)
                if num not in pr_numbers:
                    pr_numbers.append(num)
            except ValueError:
                pass

    return pr_numbers


def _extract_number_from_meta_val(val: Any, configured_repo: Optional[str]) -> Optional[int]:
    """Inspect a metadata value for an integer PR number or matching PR URL."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        if val_str.isdigit():
            return int(val_str)
        if configured_repo:
            parsed = extract_pr_numbers_from_comment(val_str, configured_repo)
            if parsed:
                return parsed[0]
    elif isinstance(val, dict):
        if "number" in val and isinstance(val["number"], int):
            return val["number"]
        if "id" in val and isinstance(val["id"], int):
            return val["id"]
        if "url" in val and isinstance(val["url"], str) and configured_repo:
            parsed = extract_pr_numbers_from_comment(val["url"], configured_repo)
            if parsed:
                return parsed[0]
    return None


def _extract_from_metadata(metadata: Any, configured_repo: str) -> list[int]:
    """Extract matching PR numbers from a run's metadata payload."""
    meta_dict: Optional[dict[str, Any]] = None
    if isinstance(metadata, dict):
        meta_dict = metadata
    elif isinstance(metadata, str):
        try:
            parsed_meta = json.loads(metadata)
            if isinstance(parsed_meta, dict):
                meta_dict = parsed_meta
        except Exception as exc:  # health: allow BLE001 -- malformed metadata json string
            logger.debug("extract_from_metadata json parse failed: %s", exc)
            meta_dict = None

    if meta_dict is None:
        return []

    meta_repo = meta_dict.get("repo") or meta_dict.get("repository")
    if meta_repo and str(meta_repo).strip().lower() != configured_repo.strip().lower():
        return []

    numbers: list[int] = []
    for k in _PR_META_KEYS:
        if k in meta_dict:
            num = _extract_number_from_meta_val(meta_dict[k], configured_repo)
            if num is not None and num not in numbers:
                numbers.append(num)

    for val in meta_dict.values():
        if isinstance(val, str):
            for num in extract_pr_numbers_from_comment(val, configured_repo):
                if num not in numbers:
                    numbers.append(num)

    return numbers


def _extract_from_summary(summary: Optional[str], configured_repo: str) -> list[int]:
    """Extract matching PR numbers from a run's summary text."""
    if not summary or not isinstance(summary, str):
        return []

    numbers: list[int] = []
    for num in extract_pr_numbers_from_comment(summary, configured_repo):
        if num not in numbers:
            numbers.append(num)

    for match in _PR_TEXT_RE.finditer(summary):
        try:
            num = int(match.group(1))
            if num not in numbers:
                numbers.append(num)
        except ValueError:
            pass

    return numbers


def extract_pr_numbers_from_report(
    summary: Optional[str] = None,
    metadata: Optional[Any] = None,
    configured_repo: Optional[str] = None,
) -> list[int]:
    """Extract PR numbers for the configured repo from a run's summary and metadata."""
    if not configured_repo:
        return []

    numbers: list[int] = []
    for num in _extract_from_metadata(metadata, configured_repo):
        if num not in numbers:
            numbers.append(num)

    for num in _extract_from_summary(summary, configured_repo):
        if num not in numbers:
            numbers.append(num)

    return numbers


def subscribe_ticket_to_pr(
    conn: sqlite3.Connection,
    ticket_id: str,
    pr_number: int,
    *,
    opened: bool = True,
    now: Optional[float] = None,
) -> bool:
    """Subscribe ticket (owning, context_only=0) to github:pr-<n> and track if opened."""
    thread_key = f"github:pr-{pr_number}"
    current_now = int(now if now is not None else time.time())

    with write_txn(conn, allow_nested=True):
        conn.execute(
            """
            INSERT INTO sa_subscriptions (thread_key, ticket_id, context_only, created_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT (thread_key, ticket_id) DO UPDATE SET context_only = 0
            """,
            (thread_key, ticket_id, current_now),
        )

        if opened:
            key = f"ticket_opened_prs:{ticket_id}"
            row = conn.execute("SELECT value FROM sa_meta WHERE key = ?", (key,)).fetchone()
            current_list: list[str] = json.loads(row[0]) if row and row[0] else []
            if thread_key not in current_list:
                current_list.append(thread_key)
                conn.execute(
                    """
                    INSERT INTO sa_meta (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, json.dumps(current_list)),
                )

    return True


def get_ticket_opened_prs(conn: sqlite3.Connection, ticket_id: str) -> list[str]:
    """Retrieve list of PR thread_keys (github:pr-<n>) that this ticket opened."""
    try:
        row = conn.execute(
            "SELECT value FROM sa_meta WHERE key = ?",
            (f"ticket_opened_prs:{ticket_id}",),
        ).fetchone()
        if row and row[0]:
            val = json.loads(row[0])
            if isinstance(val, list):
                return [str(x) for x in val]
    except Exception as exc:  # health: allow BLE001 -- sa_meta read fallback
        logger.debug("get_ticket_opened_prs sa_meta read failed: %s", exc)

    return []


def get_ticket_followed_prs(conn: sqlite3.Connection, ticket_id: str) -> list[str]:
    """Retrieve all PR thread_keys (github:pr-<n>) subscribed by this ticket."""
    try:
        rows = conn.execute(
            "SELECT thread_key FROM sa_subscriptions WHERE ticket_id = ? AND thread_key LIKE 'github:pr-%'",
            (ticket_id,),
        ).fetchall()
        return [str(r[0]) for r in rows]
    except Exception as exc:  # health: allow BLE001 -- sa_subscriptions read fallback
        logger.debug("get_ticket_followed_prs read failed: %s", exc)
        return []


def record_pr_state(
    conn: sqlite3.Connection,
    thread_key: str,
    state: str,
    outcome: Optional[str] = None,
    now: Optional[float] = None,
) -> None:
    """Record the state (open/closed) and outcome (merged/closed) of a PR in sa_meta."""
    current_now = int(now if now is not None else time.time())
    key = f"pr_state:{thread_key}"
    val = {
        "state": state,
        "outcome": outcome or state,
        "updated_at": current_now,
    }
    with write_txn(conn, allow_nested=True):
        conn.execute(
            """
            INSERT INTO sa_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, json.dumps(val)),
        )


def is_pr_open(conn: sqlite3.Connection, thread_key: str) -> bool:
    """Return True if the PR thread is currently open."""
    try:
        row = conn.execute("SELECT value FROM sa_meta WHERE key = ?", (f"pr_state:{thread_key}",)).fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            if isinstance(data, dict) and data.get("state") == "closed":
                return False
    except Exception as exc:  # health: allow BLE001 -- fallback
        logger.debug("is_pr_open sa_meta read failed: %s", exc)

    try:
        trg = conn.execute(
            """
            SELECT kind FROM sa_triggers
            WHERE thread_key = ? AND kind IN ('open', 'reopened', 'close', 'merge', 'closed', 'merged')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (thread_key,),
        ).fetchone()
        if trg and trg[0] in ("close", "merge", "closed", "merged"):
            return False
    except Exception as exc:  # health: allow BLE001 -- fallback
        logger.debug("is_pr_open triggers read failed: %s", exc)

    return True


def get_pr_outcome(conn: sqlite3.Connection, thread_key: str) -> str:
    """Determine outcome (merged | closed) for a closed PR thread."""
    try:
        row = conn.execute("SELECT value FROM sa_meta WHERE key = ?", (f"pr_state:{thread_key}",)).fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            if isinstance(data, dict) and data.get("outcome") == "merged":
                return "merged"
    except Exception as exc:  # health: allow BLE001 -- fallback
        logger.debug("get_pr_outcome sa_meta read failed: %s", exc)

    try:
        trg = conn.execute(
            """
            SELECT kind, meta_json FROM sa_triggers
            WHERE thread_key = ? AND kind IN ('merge', 'merged', 'close', 'closed')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (thread_key,),
        ).fetchone()
        if trg:
            kind_str = trg[0]
            if kind_str in ("merge", "merged"):
                return "merged"
            if trg[1]:
                meta = json.loads(trg[1])
                if isinstance(meta, dict) and (meta.get("merged") or meta.get("merged_at")):
                    return "merged"
    except Exception as exc:  # health: allow BLE001 -- fallback
        logger.debug("get_pr_outcome triggers read failed: %s", exc)

    return "closed"


def has_unfinished_work(
    conn: sqlite3.Connection,
    ticket_id: str,
    close_trigger_id: Optional[str] = None,
) -> bool:
    """Check if unfinished resumed work exists for ticket_id.

    Returns True if:
    1. Any unread person input exists on the ticket (excluding the close trigger itself).
    2. Any unread input delivered after the close trigger exists.
    """
    try:
        if close_trigger_id is not None:
            p_row = conn.execute(
                """
                SELECT 1 FROM sa_inputs
                WHERE ticket_id = ?
                  AND read_at IS NULL
                  AND is_person = 1
                  AND (trigger_id != ? OR trigger_id IS NULL)
                LIMIT 1
                """,
                (ticket_id, close_trigger_id),
            ).fetchone()
        else:
            p_row = conn.execute(
                """
                SELECT 1 FROM sa_inputs
                WHERE ticket_id = ?
                  AND read_at IS NULL
                  AND is_person = 1
                LIMIT 1
                """,
                (ticket_id,),
            ).fetchone()

        if p_row is not None:
            return True

        if close_trigger_id is not None:
            trg_row = conn.execute(
                "SELECT created_at FROM sa_triggers WHERE id = ?",
                (close_trigger_id,),
            ).fetchone()
            if trg_row is not None:
                close_time = trg_row[0]
                after_row = conn.execute(
                    """
                    SELECT 1 FROM sa_inputs
                    WHERE ticket_id = ?
                      AND read_at IS NULL
                      AND (trigger_id != ? OR trigger_id IS NULL)
                      AND created_at >= ?
                    LIMIT 1
                    """,
                    (ticket_id, close_trigger_id, close_time),
                ).fetchone()
                if after_row is not None:
                    return True
    except Exception as exc:  # health: allow BLE001 -- fallback on error
        logger.debug("has_unfinished_work query failed: %s", exc)

    return False


def end_ticket_follows_done(
    conn: sqlite3.Connection,
    ticket_id: str,
    outcome: str = "merged",
    now: Optional[float] = None,
    reason: str = "follows_done",
) -> list[str]:
    """End the ticket and its runs with the specified outcome (e.g. 'merged' or 'stopped')."""
    current_now = int(now if now is not None else time.time())
    from hermes_cli import kanban_db as kb

    with write_txn(conn, allow_nested=True):
        conn.execute(
            """
            UPDATE tasks
               SET status       = 'done',
                   result       = ?,
                   completed_at = ?,
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL,
                   block_kind   = NULL,
                   block_recurrences = 0
             WHERE id = ?
            """,
            (outcome, current_now, ticket_id),
        )

        cur = conn.execute(
            """
            UPDATE task_runs
               SET status   = 'done',
                   outcome  = ?,
                   ended_at = ?
             WHERE task_id = ? AND ended_at IS NULL
            """,
            (outcome, current_now, ticket_id),
        )

        if cur.rowcount == 0:
            run_row = conn.execute(
                "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (ticket_id,),
            ).fetchone()
            if run_row:
                conn.execute(
                    "UPDATE task_runs SET status = 'done', outcome = ? WHERE id = ?",
                    (outcome, run_row[0]),
                )
            else:
                summary = (
                    f"PR follows-done: outcome {outcome}"
                    if reason == "follows_done"
                    else f"Ticket {reason}: outcome {outcome}"
                )
                synth_id = kb._synthesize_ended_run(
                    conn,
                    ticket_id,
                    outcome=outcome,
                    summary=summary,
                )
                conn.execute(
                    "UPDATE task_runs SET status = 'done' WHERE id = ?",
                    (synth_id,),
                )

        kb._append_event(
            conn,
            ticket_id,
            "completed",
            {"outcome": outcome, "reason": reason},
        )
        try:
            from hermes_cli.orchestrator import release_on_end
            return release_on_end(conn, ticket_id, now=current_now)
        except Exception as exc:  # health: allow BLE001 -- release on end hook failure boundary
            logger.debug("release_on_end failed: %s", exc)
            return []


def _determine_follows_done_outcome(
    conn: sqlite3.Connection,
    prs: list[str],
    trigger_id: Optional[str] = None,
) -> str:
    """Determine outcome ('merged' or 'closed') for ending a ticket."""
    for p in prs:
        if get_pr_outcome(conn, p) == "merged":
            return "merged"

    if trigger_id is not None:
        trg = conn.execute("SELECT kind, meta_json FROM sa_triggers WHERE id = ?", (trigger_id,)).fetchone()
        if trg:
            if trg[0] in ("merge", "merged"):
                return "merged"
            if trg[1]:
                try:
                    meta = json.loads(trg[1])
                    if isinstance(meta, dict) and (meta.get("merged") or meta.get("merged_at")):
                        return "merged"
                except Exception as exc:  # health: allow BLE001 -- malformed meta fallback
                    logger.debug("determine_follows_done_outcome meta parse failed: %s", exc)

    return "closed"


def apply_follows_done_rule(
    conn: sqlite3.Connection,
    ticket_id: str,
    trigger_id: Optional[str] = None,
    now: Optional[float] = None,
) -> bool:
    """Apply the follows-done rule to a ticket.

    Ticket ends when:
    - No followed PR it opened remains open (or all followed closed, if it opened none)
    - Unless resumed work is unfinished (an input delivered after close is unread -> do not end).

    Returns True if ticket ended, False otherwise.
    """
    task = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (ticket_id,)).fetchone()
    if not task or task[1] == "done":
        return False

    followed_prs = get_ticket_followed_prs(conn, ticket_id)
    if not followed_prs:
        return False

    opened_prs = [p for p in get_ticket_opened_prs(conn, ticket_id) if p in followed_prs]

    if opened_prs:
        for p in opened_prs:
            if is_pr_open(conn, p):
                return False
    else:
        for p in followed_prs:
            if is_pr_open(conn, p):
                return False

    if has_unfinished_work(conn, ticket_id, close_trigger_id=trigger_id):
        return False

    outcome = _determine_follows_done_outcome(conn, opened_prs or followed_prs, trigger_id=trigger_id)
    end_ticket_follows_done(conn, ticket_id, outcome=outcome, now=now)
    return True


def hook_comment_for_pr(
    conn: sqlite3.Connection,
    ticket_id: str,
    body: str,
    config: Optional[dict[str, Any]] = None,
) -> list[int]:
    """Hook called when a comment is added to a task."""
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sa_subscriptions'"
        ).fetchone()
        if not has_table:
            return []

        repo = get_configured_github_repo(conn, config)
        if not repo:
            return []

        pr_nums = extract_pr_numbers_from_comment(body, repo)
        for num in pr_nums:
            subscribe_ticket_to_pr(conn, ticket_id, num, opened=True)
        return pr_nums
    except Exception as exc:  # health: allow BLE001 -- hook failure must not block comments
        logger.debug("hook_comment_for_pr failed for %s: %s", ticket_id, exc)
        return []


def hook_run_report_for_pr(
    conn: sqlite3.Connection,
    ticket_id: str,
    summary: Optional[str] = None,
    metadata: Optional[Any] = None,
    config: Optional[dict[str, Any]] = None,
) -> list[int]:
    """Hook called when a run finishes and records summary/metadata."""
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sa_subscriptions'"
        ).fetchone()
        if not has_table:
            return []

        repo = get_configured_github_repo(conn, config)
        if not repo:
            return []

        pr_nums = extract_pr_numbers_from_report(summary, metadata, repo)
        for num in pr_nums:
            subscribe_ticket_to_pr(conn, ticket_id, num, opened=True)
        return pr_nums
    except Exception as exc:  # health: allow BLE001 -- hook failure must not block run completion
        logger.debug("hook_run_report_for_pr failed for %s: %s", ticket_id, exc)
        return []

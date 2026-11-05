"""E-019 Super-agent Router (`orchestrator_*`) toolset handlers and gating.

Phase 1 implemented:
- `orchestrator_route`: accept-only ticket creation via `kanban_db.create_task`
  with `created_by="router"`.
- `orchestrator_read`: single-ticket (`kanban_show`) and board-list
  (`kanban_list`) reads without duplicating board query logic.
- `orchestrator_reply`: proactive conversation reply via the gateway's existing
  proactive-send path used by the kanban notifier.

Phase 2 replaces the Phase 1 stubs with live implementations:
- `orchestrator_eval`: batch evaluation via `orchestrator.run_eval`.
- `orchestrator_eval_read`: read latest Decision records from `sa_decisions`.
- `orchestrator_eval_decide`: router decision (dismiss or `orchestrator.accept`).
- `orchestrator_trigger_requeue`: reset trigger `eval_state` back to 'queued'.
- `orchestrator_thread_unsubscribe`: drop thread subscriptions from `sa_subscriptions`.
- `orchestrator_ticket_retry`: end run via existing dispatcher retry path and mark ready.
- `orchestrator_ticket_stop`: stop a ticket with person-stopped semantics via `orchestrator.stop_ticket`.
- `orchestrator_ticket_request_status`: ticket status summary.
- `orchestrator_ticket_note`: append an `sa_inputs` row kind 'note'.

All 12 tools are gated by `_check_orchestrator_mode`: visible only when
`superagent.enabled` is `True` and the active profile matches
`superagent.router_profile` (default `None` = hidden everywhere).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from typing import Any, Callable

from gateway.config import Platform
from gateway.kanban_watchers_dispatcher import superagent_settings
from hermes_cli.config import load_config
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_db_notify import list_notify_subs
from hermes_cli.orchestrator import accept, assemble_batch, run_eval
from hermes_cli.profiles import current_profile_name
from hermes_constants import get_hermes_home
from tools import kanban_tools
from tools.kanban_tools import (
    _CREATED_FIELDS,
    _Reject,
    _board,
    _check,
    _coerce_str_list,
    _existing_task,
    _fields,
    _is_delegated_child_context,
    _is_dispatcher_owned_worker,
    _maybe_auto_subscribe,
    _ok,
    _opt_int,
    _parse_bool_arg,
    _persisted_session_id,
    _reject_delegated_child_mutation,
    _require_text,
    _resolve_notify_target,
)
from tools.orchestrator_tools_schemas import (
    ORCHESTRATOR_EVAL_DECIDE_SCHEMA,
    ORCHESTRATOR_EVAL_READ_SCHEMA,
    ORCHESTRATOR_EVAL_SCHEMA,
    ORCHESTRATOR_READ_SCHEMA,
    ORCHESTRATOR_REPLY_SCHEMA,
    ORCHESTRATOR_ROUTE_SCHEMA,
    ORCHESTRATOR_THREAD_UNSUBSCRIBE_SCHEMA,
    ORCHESTRATOR_TICKET_NOTE_SCHEMA,
    ORCHESTRATOR_TICKET_REQUEST_STATUS_SCHEMA,
    ORCHESTRATOR_TICKET_RETRY_SCHEMA,
    ORCHESTRATOR_TICKET_STOP_SCHEMA,
    ORCHESTRATOR_TRIGGER_REQUEUE_SCHEMA,
)
from tools.registry import no_cache_check_fn, registry

logger = logging.getLogger(__name__)


def _active_profile_name() -> str:
    """Resolve the active profile name for router gating."""
    raw = current_profile_name("default")
    if raw and raw != "custom":
        return str(raw).strip()

    try:
        resolved = get_hermes_home().resolve()
        if resolved.parent.name == "profiles" and resolved.name:
            return resolved.name
    except Exception:  # health: allow BLE001, S110 -- fallback to default profile name on any resolution error
        pass
    return "default"


@no_cache_check_fn
def _check_orchestrator_mode() -> bool:
    """True only when `superagent.enabled` is True and active profile == `superagent.router_profile`."""
    if _is_delegated_child_context():
        return False
    if os.environ.get("HERMES_KANBAN_TASK") and _is_dispatcher_owned_worker():
        return False
    try:
        cfg = load_config()
        settings = superagent_settings(cfg if isinstance(cfg, dict) else {})
    except Exception:  # health: allow BLE001 -- fallback to disabled when config cannot be loaded
        return False

    if not settings.get("enabled"):
        return False
    router_profile = settings.get("router_profile")
    if not isinstance(router_profile, str) or not router_profile.strip():
        return False

    try:
        active_profile = _active_profile_name()
    except Exception:  # health: allow BLE001 -- fallback when profile cannot be resolved
        return False
    return active_profile == router_profile.strip()


def _orchestrator_handler(tool_name: str) -> Callable:
    """Wrap an active orchestrator handler so validation and runtime errors return JSON."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(args: dict[str, Any] | None = None, **kw: Any) -> str:
            call_args = dict(args or {})
            try:
                try:
                    cfg = load_config()
                    settings = superagent_settings(cfg if isinstance(cfg, dict) else {})
                except Exception:  # health: allow BLE001 -- fallback to empty settings
                    settings = {}
                _check(bool(settings.get("enabled")), f"{tool_name}: superagent is disabled.")
                properties = registry.get_schema(tool_name)["parameters"]["properties"]
                unknown = sorted(set(call_args) - set(properties))
                _check(
                    not unknown,
                    f"{tool_name}: unknown parameter(s): {', '.join(unknown)}. "
                    f"Valid parameters: {', '.join(sorted(properties))}. Nothing changed.",
                )
                return fn(call_args, **kw)
            except _Reject as exc:
                return exc.args[0]
            except ValueError as exc:
                return json.dumps({"ok": False, "error": f"{tool_name}: {exc}"})
            except Exception as exc:  # health: allow BLE001 -- top-level tool error boundary returns JSON
                logger.exception("Unhandled error in %s", tool_name)
                return json.dumps({"ok": False, "error": f"{tool_name} failed: {exc}"})

        return wrapper

    return deco


@_orchestrator_handler("orchestrator_route")
def _handle_route(args: dict[str, Any], **kw: Any) -> str:
    """Accept-only ticket creation via `kanban_db.create_task` with `created_by="router"`."""
    _reject_delegated_child_mutation("orchestrator_route")
    title = _require_text(args, "title")
    assignee = _require_text(
        args,
        "assignee",
        "orchestrator_route: assignee is required.",
    )
    workspace_kind, workspace_path = args.get("workspace_kind"), args.get("workspace_path")
    project_id = args["project"] if "project" in args else args.get("project_id")
    project_source_task_id = None
    triage, skills, goal_mode = (
        _parse_bool_arg(args, "triage"),
        _coerce_str_list(args.get("skills"), "skills", "skill names"),
        _parse_bool_arg(args, "goal_mode"),
    )
    model_override, provider_override = args.get("model"), args.get("provider")
    _check(model_override or not provider_override, "'provider' requires 'model' to be set as well")
    parents = _coerce_str_list(args.get("parents") or [], "parents", "task ids")
    with _board(args.get("board")) as (kb, conn):
        from gateway.session_context import get_session_env
        from tools.async_delegation import _current_origin_session_id

        self_tid = (
            os.environ.get("HERMES_KANBAN_TASK")
            if _is_dispatcher_owned_worker()
            else None
        )
        self_task = kb.get_task(conn, self_tid) if self_tid else None
        session_id = (
            _persisted_session_id(args.get("session_id"))
            or (self_task.session_id if self_task else None)
            or _persisted_session_id(_current_origin_session_id())
            or _persisted_session_id(get_session_env("HERMES_SESSION_ID", ""))
        )
        if project_id is None and workspace_kind is None and workspace_path is None:
            if self_task is not None and self_task.project_id:
                project_id, project_source_task_id = self_task.project_id, self_task.id
        new_tid = kb.create_task(
            conn,
            title=str(title).strip(),
            body=args.get("body"),
            assignee=str(assignee).strip(),
            parents=tuple(parents),
            tenant=args.get("tenant") or os.environ.get("HERMES_TENANT"),
            priority=_opt_int(args.get("priority"), 0),
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            project_id=project_id,
            board=args.get("board"),
            project_source_task_id=project_source_task_id,
            triage=triage,
            creator_task_id=self_tid,
            idempotency_key=args.get("idempotency_key"),
            max_runtime_seconds=_opt_int(args.get("max_runtime_seconds")),
            skills=skills,
            model_override=model_override,
            provider_override=provider_override,
            goal_mode=goal_mode,
            goal_max_turns=_opt_int(args.get("goal_max_turns")),
            completion_contract=args.get("completion_contract"),
            initial_status=str(args.get("initial_status") or "running"),
            created_by="router",
            session_id=session_id,
        )
        landed = _fields(kb.get_task(conn, new_tid), _CREATED_FIELDS)
        wait = [e for e in kb.list_events(conn, new_tid) if e.kind == "dependency_wait"]
        gate = {"gated": True, "gated_by": wait[-1].payload["parent"]} if wait else {"gated": False}
        return _ok(
            task_id=new_tid,
            **landed,
            **gate,
            subscribed=_maybe_auto_subscribe(conn, new_tid),
        )


@_orchestrator_handler("orchestrator_read")
def _handle_read(args: dict[str, Any], **kw: Any) -> str:
    """Delegate single-ticket reads to `_handle_show` and listings to `_handle_list`."""
    action = str(args.get("action") or "").strip().lower()
    task_id = str(args.get("task_id") or "").strip()
    _check(
        action in ("", "show", "list"),
        f"orchestrator_read: action must be 'show' or 'list', got {args.get('action')!r}.",
    )

    if action == "show" or (not action and task_id):
        _check(bool(task_id), "orchestrator_read: task_id is required when action='show'.")
        show_args: dict[str, Any] = {"task_id": task_id}
        if args.get("board") is not None:
            show_args["board"] = args["board"]
        return kanban_tools._handle_show(show_args, **kw)

    list_keys = ("assignee", "status", "tenant", "include_archived", "limit", "board")
    list_args = {k: args[k] for k in list_keys if k in args and args[k] is not None}
    return kanban_tools._handle_list(list_args, **kw)


def _resolve_reply_subscriptions(args: dict[str, Any]) -> tuple[list[dict[str, Any]], Any, str | None]:
    """Resolve target subscription row(s) from `task_id`, explicit args, or session env."""
    task_id = str(args.get("task_id") or "").strip()
    board_slug = args.get("board")
    platform_override = str(args.get("platform") or "").strip().lower()
    chat_override = str(args.get("chat_id") or "").strip()
    subs: list[dict[str, Any]] = []
    task_obj: Any = None

    if task_id:
        with _board(board_slug) as (_kb, conn):
            task_obj = _existing_task(_kb, conn, task_id)
            for row in list_notify_subs(conn, task_obj.id):
                sub = dict(row)
                if platform_override and str(sub.get("platform") or "").lower() != platform_override:
                    continue
                if chat_override and str(sub.get("chat_id") or "") != chat_override:
                    continue
                subs.append(sub)

    if subs:
        return subs, task_obj, board_slug

    target = _resolve_notify_target() or {}
    platform = platform_override or str(target.get("platform") or "").strip().lower()
    chat_id = chat_override or str(target.get("chat_id") or "").strip()
    if not platform or not chat_id:
        return [], task_obj, board_slug

    delivery_metadata = dict(target.get("delivery_metadata") or {})
    if args.get("reply_to") is not None:
        delivery_metadata["reply_to"] = args["reply_to"]

    return (
        [
            {
                "task_id": task_obj.id if task_obj is not None else task_id,
                "platform": platform,
                "chat_id": chat_id,
                "thread_id": (
                    args.get("thread_id")
                    if args.get("thread_id") is not None
                    else target.get("thread_id")
                ),
                "chat_type": target.get("chat_type"),
                "notifier_profile": target.get("notifier_profile") or _active_profile_name(),
                "delivery_metadata": delivery_metadata,
            }
        ],
        task_obj,
        board_slug,
    )


def _resolve_gateway_runner(kw: dict[str, Any]) -> Any:
    """Return the active gateway runner from kwargs or `gateway.run._gateway_runner_ref`."""
    if kw.get("runner") is not None:
        return kw["runner"]
    try:
        from gateway.run import _gateway_runner_ref

        return _gateway_runner_ref()
    except Exception:  # health: allow BLE001 -- gateway runner optional / not running
        return None


def _deliver_subscription_reply(
    runner: Any,
    sub: dict[str, Any],
    text: str,
    args: dict[str, Any],
    *,
    task_obj: Any = None,
    board_slug: str | None = None,
) -> dict[str, Any]:
    """Deliver `text` to a single subscription by queueing it to the Superagent Outbox."""
    platform_str = str(sub.get("platform") or "").strip().lower()
    try:
        Platform(platform_str)
    except ValueError:
        return {
            "platform": sub.get("platform"),
            "chat_id": sub.get("chat_id"),
            "queued": False,
            "error": f"Unknown platform {sub.get('platform')!r}",
        }

    owner_profile = str(sub.get("notifier_profile") or sub.get("owner_profile") or "").strip() or None
    delivery_metadata = sub.get("delivery_metadata")
    metadata: dict[str, Any] = dict(delivery_metadata) if isinstance(delivery_metadata, dict) else {}
    thread_id = args.get("thread_id") if args.get("thread_id") is not None else sub.get("thread_id")
    if thread_id and not metadata.get("thread_id"):
        metadata["thread_id"] = thread_id
    reply_to = args.get("reply_to") if args.get("reply_to") is not None else sub.get("reply_to")
    if reply_to and not metadata.get("reply_to"):
        metadata["reply_to"] = reply_to
    if (
        platform_str == "telegram"
        and metadata.get("reply_to")
        and "telegram_reply_to_message_id" not in metadata
    ):
        metadata["telegram_reply_to_message_id"] = metadata["reply_to"]

    # Queue reply to sa_outbox (Card 4.4 / E-028 / E-030)
    from hermes_cli import kanban_db as _kb
    from hermes_cli.superagent_outbox import queue_reply

    tid = str(sub.get("task_id") or (task_obj.id if task_obj else "") or args.get("task_id") or "") or None
    address = {
        "platform": platform_str,
        "chat_id": str(sub["chat_id"]),
        "thread_id": thread_id or None,
        "delivery_metadata": metadata,
        "notifier_profile": owner_profile or "",
        "reply_to": reply_to or None,
        "chat_type": sub.get("chat_type") or metadata.get("chat_type"),
    }
    purpose = str(args.get("purpose") or "answer")

    now = time.time()
    with _kb.pin_first_board_resolution():
        db_path = _kb.kanban_db_path(board=board_slug)
    try:
        outbox_id = queue_reply(
            db_path,
            ticket_id=tid,
            source=platform_str,
            address=address,
            purpose=purpose,
            body=text,
            now=now,
        )
    except Exception as q_exc:
        logger.error(
            "orchestrator_reply: queue_reply failed for %s/%s: %s",
            platform_str,
            sub.get("chat_id"),
            q_exc,
        )
        return {
            "platform": sub.get("platform"),
            "chat_id": str(sub["chat_id"]),
            "queued": False,
            "error": f"queue_reply failed: {q_exc}",
        }

    return {
        "platform": sub.get("platform"),
        "chat_id": str(sub["chat_id"]),
        "queued": True,
        "outbox_id": outbox_id,
    }


@_orchestrator_handler("orchestrator_reply")
def _handle_reply(args: dict[str, Any], **kw: Any) -> str:
    """Send a proactive reply by enqueueing to the Outbox queue."""
    raw_text = args.get("text") if args.get("text") is not None else args.get("message")
    _check(
        isinstance(raw_text, str) and bool(raw_text.strip()),
        "orchestrator_reply: text must be a non-empty string.",
    )
    text = raw_text.strip()

    subs, task_obj, board_slug = _resolve_reply_subscriptions(args)
    _check(
        bool(subs),
        "orchestrator_reply: no notification subscription or active gateway target found.",
    )

    runner = _resolve_gateway_runner(kw)
    _check(
        runner is not None,
        "orchestrator_reply: gateway runner is not active.",
    )

    deliveries = [
        _deliver_subscription_reply(
            runner,
            sub,
            text,
            args,
            task_obj=task_obj,
            board_slug=board_slug,
        )
        for sub in subs
    ]
    all_ok = all(d.get("queued") for d in deliveries)
    payload: dict[str, Any] = {
        "ok": all_ok,
        "queued": sum(1 for d in deliveries if d.get("queued")),
        "deliveries": deliveries,
    }
    if not all_ok:
        errors = [d["error"] for d in deliveries if d.get("error")]
        if errors:
            payload["error"] = "; ".join(errors)
    return json.dumps(payload)


@_orchestrator_handler("orchestrator_eval")
def _handle_eval(args: dict[str, Any], **kw: Any) -> str:
    """Run Eval for queued triggers for a thread or all queued triggers, returning batch summary counts."""
    thread_key = args.get("thread_key")
    if thread_key is not None:
        thread_key = str(thread_key).strip()
        if not thread_key:
            thread_key = None

    board_slug = args.get("board")
    cfg = load_config()

    with _board(board_slug) as (_kb, conn):
        counts: dict[str, int] = {}
        total_batches = 0
        total_triggers = 0

        while True:
            batch = assemble_batch(conn, thread_key=thread_key, config=cfg)
            if not batch:
                break
            dec = run_eval(batch, db_path_or_conn=conn, config=cfg)
            outcome = dec.get("outcome") or "unknown"
            counts[outcome] = counts.get(outcome, 0) + 1
            total_batches += 1
            total_triggers += len(dec.get("trigger_ids") or [])

        return _ok(
            summary=counts,
            counts=counts,
            batches=total_batches,
            triggers=total_triggers,
        )


@_orchestrator_handler("orchestrator_eval_read")
def _handle_eval_read(args: dict[str, Any], **kw: Any) -> str:
    """Read latest Decision(s) for a thread from sa_decisions, newest first."""
    thread_key = _require_text(
        args,
        "thread_key",
        "orchestrator_eval_read: thread_key is required.",
    )
    raw_limit = args.get("limit")
    limit = _opt_int(raw_limit, 20) if raw_limit is not None else 20
    if limit is None or limit < 1:
        limit = 20

    board_slug = args.get("board")
    with _board(board_slug) as (_kb, conn):
        cur = conn.execute(
            """
            SELECT id, thread_key, trigger_ids_json, outcome, strength,
                   would_accept, ignored_from_card, matched_rules_json,
                   reason_json, policy_version, ticket_id, dismissed, created_at
            FROM sa_decisions
            WHERE thread_key = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (thread_key, limit),
        )
        decisions: list[dict[str, Any]] = []
        for row in cur.fetchall():
            d = dict(row)
            try:
                d["trigger_ids"] = json.loads(d.get("trigger_ids_json") or "[]")
            except Exception:  # health: allow BLE001 -- malformed JSON in DB fallback
                d["trigger_ids"] = []
            try:
                d["matched_rules"] = json.loads(d.get("matched_rules_json") or "[]")
            except Exception:  # health: allow BLE001 -- malformed JSON in DB fallback
                d["matched_rules"] = []
            try:
                d["reason"] = json.loads(d.get("reason_json") or "{}")
            except Exception:  # health: allow BLE001 -- malformed JSON in DB fallback
                d["reason"] = {}
            decisions.append(d)

        return _ok(
            thread_key=thread_key,
            decisions=decisions,
            count=len(decisions),
        )


@_orchestrator_handler("orchestrator_eval_decide")
def _handle_eval_decide(args: dict[str, Any], **kw: Any) -> str:
    """Record a Router decision on a pending Decision (accept or dismiss)."""
    raw_id = args.get("decision_id")
    if raw_id is None:
        raw_id = args.get("id")
    _check(raw_id is not None, "orchestrator_eval_decide: decision_id is required.")
    try:
        decision_id = int(raw_id)
    except (ValueError, TypeError):
        raise _Reject("orchestrator_eval_decide: decision_id must be an integer.")

    raw_decision = args.get("decision") or args.get("action") or "accept"
    decision = str(raw_decision).strip().lower()
    _check(
        decision in ("accept", "dismiss"),
        f"orchestrator_eval_decide: decision must be 'accept' or 'dismiss', got {raw_decision!r}.",
    )

    board_slug = args.get("board")
    cfg = load_config()

    with _board(board_slug) as (_kb, conn):
        row = conn.execute(
            "SELECT * FROM sa_decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
        _check(
            row is not None,
            f"orchestrator_eval_decide: decision {decision_id} not found.",
        )
        _check(
            not bool(row["dismissed"]),
            f"orchestrator_eval_decide: decision {decision_id} is already dismissed.",
        )
        _check(
            not bool(row["ticket_id"]),
            f"orchestrator_eval_decide: decision {decision_id} already has a ticket ({row['ticket_id']}).",
        )

        if decision == "dismiss":
            with write_txn(conn):
                conn.execute(
                    "UPDATE sa_decisions SET dismissed = 1 WHERE id = ?",
                    (decision_id,),
                )
            return _ok(
                decision_id=decision_id,
                decision="dismiss",
                dismissed=True,
            )

        # decision == "accept": call orchestrator.accept
        ticket_id = accept(conn, decision_id, now=time.time(), config=cfg)
        if ticket_id is None:
            return _ok(
                decision_id=decision_id,
                decision="accept",
                accepted=False,
                would_accept=True,
                ticket_id=None,
            )
        return _ok(
            decision_id=decision_id,
            decision="accept",
            ticket_id=ticket_id,
            accepted=True,
        )


@_orchestrator_handler("orchestrator_trigger_requeue")
def _handle_trigger_requeue(args: dict[str, Any], **kw: Any) -> str:
    """Set a trigger's eval_state back to 'queued' unless it is currently evaluating."""
    trigger_id = str(args.get("trigger_id") or args.get("task_id") or "").strip()
    _check(
        bool(trigger_id),
        "orchestrator_trigger_requeue: trigger_id is required.",
    )

    board_slug = args.get("board")
    with _board(board_slug) as (_kb, conn):
        row = conn.execute(
            "SELECT id, eval_state, eval_requeues FROM sa_triggers WHERE id = ?",
            (trigger_id,),
        ).fetchone()
        _check(
            row is not None,
            f"orchestrator_trigger_requeue: trigger {trigger_id} not found.",
        )
        _check(
            row["eval_state"] != "evaluating",
            f"orchestrator_trigger_requeue: cannot requeue trigger {trigger_id} while it is evaluating.",
        )
        with write_txn(conn):
            conn.execute(
                """
                UPDATE sa_triggers
                SET eval_state = 'queued', eval_requeues = eval_requeues + 1
                WHERE id = ?
                """,
                (trigger_id,),
            )
        return _ok(
            trigger_id=trigger_id,
            eval_state="queued",
            requeued=True,
        )


@_orchestrator_handler("orchestrator_thread_unsubscribe")
def _handle_thread_unsubscribe(args: dict[str, Any], **kw: Any) -> str:
    """Drop all subscriptions for a thread_key."""
    thread_key = str(args.get("thread_key") or args.get("thread_id") or "").strip()
    _check(
        bool(thread_key),
        "orchestrator_thread_unsubscribe: thread_key is required.",
    )

    board_slug = args.get("board")
    with _board(board_slug) as (_kb, conn):
        # Integration merge point: Lane B direct DELETE against sa_subscriptions is the operative path.
        with write_txn(conn):
            cur = conn.execute(
                "DELETE FROM sa_subscriptions WHERE thread_key = ?",
                (thread_key,),
            )
            deleted_count = cur.rowcount

        return _ok(
            thread_key=thread_key,
            dropped=deleted_count,
        )


@_orchestrator_handler("orchestrator_ticket_retry")
def _handle_ticket_retry(args: dict[str, Any], **kw: Any) -> str:
    """End the ticket's current run via existing dispatcher/reclaim retry path and mark ready."""
    task_id = _require_text(
        args,
        "task_id",
        "orchestrator_ticket_retry: task_id is required.",
    )
    reason = args.get("reason")
    board_slug = args.get("board")

    with _board(board_slug) as (kb, conn):
        task = kb.get_task(conn, task_id)
        _check(
            task is not None,
            f"orchestrator_ticket_retry: ticket {task_id} not found.",
        )
        _check(
            task.status not in ("done", "archived"),
            f"orchestrator_ticket_retry: cannot retry terminal ticket {task_id} (status: {task.status}).",
        )

        # End active run via existing dispatcher reclaim/retry path if currently running
        if task.status == "running" or task.claim_lock is not None:
            kb.reclaim_task(conn, task.id, reason=reason or "orchestrator retry")
        elif task.status == "blocked":
            kb.unblock_task(conn, task.id)

        # Ensure task status is ready for a new assignment
        cur_task = kb.get_task(conn, task.id)
        if cur_task.status != "ready":
            with write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL WHERE id = ?",
                    (task.id,),
                )

        return _ok(
            task_id=task.id,
            status="ready",
            retried=True,
        )


@_orchestrator_handler("orchestrator_ticket_stop")
def _handle_ticket_stop(args: dict[str, Any], **kw: Any) -> str:
    """Stop a ticket with person-stopped semantics via orchestrator.stop_ticket."""
    task_id = _require_text(
        args,
        "task_id",
        "orchestrator_ticket_stop: task_id is required.",
    )
    reason = args.get("reason")
    board_slug = args.get("board")

    with _board(board_slug) as (kb, conn):
        task = kb.get_task(conn, task_id)
        _check(
            task is not None,
            f"orchestrator_ticket_stop: ticket {task_id} not found.",
        )
        from hermes_cli.orchestrator import stop_ticket
        res = stop_ticket(conn, task_id, reason=reason)
        if isinstance(res, str):
            _check(False, f"orchestrator_ticket_stop: {res}")
        return _ok(
            task_id=task_id,
            status=res.get("status", "stopped"),
            stopped=True,
            already_stopped=res.get("already_stopped", False),
            already_ended=res.get("already_ended", False),
            mandate_id=res.get("mandate_id"),
        )


@_orchestrator_handler("orchestrator_ticket_request_status")
def _handle_ticket_request_status(args: dict[str, Any], **kw: Any) -> str:
    """Return the ticket's status summary."""
    task_id = _require_text(
        args,
        "task_id",
        "orchestrator_ticket_request_status: task_id is required.",
    )
    board_slug = args.get("board")

    with _board(board_slug) as (kb, conn):
        task = kb.get_task(conn, task_id)
        _check(
            task is not None,
            f"orchestrator_ticket_request_status: ticket {task_id} not found.",
        )

        mandate_row = conn.execute(
            """
            SELECT id, strength, status, policy_version, scope
            FROM sa_mandates
            WHERE ticket_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task.id,),
        ).fetchone()

        unread_row = conn.execute(
            "SELECT COUNT(*) FROM sa_inputs WHERE ticket_id = ? AND read_at IS NULL",
            (task.id,),
        ).fetchone()
        unread_inputs_count = int(unread_row[0]) if unread_row else 0

        dec_row = conn.execute(
            """
            SELECT id, thread_key, outcome, strength, policy_version, created_at
            FROM sa_decisions
            WHERE ticket_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (task.id,),
        ).fetchone()
        latest_decision = dict(dec_row) if dec_row else None

        mandate_strength = mandate_row["strength"] if mandate_row else None
        mandate_status = mandate_row["status"] if mandate_row else None

        return _ok(
            task_id=task.id,
            task_status=task.status,
            status=task.status,
            mandate_strength=mandate_strength,
            mandate_status=mandate_status,
            unread_inputs_count=unread_inputs_count,
            latest_decision=latest_decision,
        )


@_orchestrator_handler("orchestrator_ticket_note")
def _handle_ticket_note(args: dict[str, Any], **kw: Any) -> str:
    """Write an sa_inputs row kind 'note' for the ticket."""
    task_id = _require_text(
        args,
        "task_id",
        "orchestrator_ticket_note: task_id is required.",
    )
    raw_note = args.get("note") if args.get("note") is not None else args.get("body")
    _check(
        isinstance(raw_note, str) and bool(raw_note.strip()),
        "orchestrator_ticket_note: note must be a non-empty string.",
    )
    note = raw_note.strip()
    board_slug = args.get("board")

    with _board(board_slug) as (kb, conn):
        task = kb.get_task(conn, task_id)
        _check(
            task is not None,
            f"orchestrator_ticket_note: ticket {task_id} not found.",
        )
        now_ts = int(time.time())
        with write_txn(conn):
            cur = conn.execute(
                """
                INSERT INTO sa_inputs (
                    ticket_id, trigger_id, kind, body, is_person, context_only, read_at, created_at
                ) VALUES (?, NULL, 'note', ?, 0, 0, NULL, ?)
                """,
                (task.id, note, now_ts),
            )
            input_id = cur.lastrowid

        return _ok(
            task_id=task.id,
            input_id=input_id,
            kind="note",
            note=note,
        )


for _schema, _handler, _emoji in (
    (ORCHESTRATOR_ROUTE_SCHEMA, _handle_route, "🧭"),
    (ORCHESTRATOR_EVAL_SCHEMA, _handle_eval, "🧪"),
    (ORCHESTRATOR_EVAL_READ_SCHEMA, _handle_eval_read, "🔎"),
    (ORCHESTRATOR_EVAL_DECIDE_SCHEMA, _handle_eval_decide, "⚖️"),
    (ORCHESTRATOR_REPLY_SCHEMA, _handle_reply, "💬"),
    (ORCHESTRATOR_READ_SCHEMA, _handle_read, "📋"),
    (ORCHESTRATOR_TICKET_RETRY_SCHEMA, _handle_ticket_retry, "🔁"),
    (ORCHESTRATOR_TICKET_STOP_SCHEMA, _handle_ticket_stop, "🛑"),
    (ORCHESTRATOR_TICKET_REQUEST_STATUS_SCHEMA, _handle_ticket_request_status, "📡"),
    (ORCHESTRATOR_TICKET_NOTE_SCHEMA, _handle_ticket_note, "📝"),
    (ORCHESTRATOR_TRIGGER_REQUEUE_SCHEMA, _handle_trigger_requeue, "♻️"),
    (ORCHESTRATOR_THREAD_UNSUBSCRIBE_SCHEMA, _handle_thread_unsubscribe, "🔕"),
):
    registry.register(
        name=_schema["name"],
        toolset="orchestrator",
        schema=_schema,
        handler=_handler,
        check_fn=_check_orchestrator_mode,
        emoji=_emoji,
    )

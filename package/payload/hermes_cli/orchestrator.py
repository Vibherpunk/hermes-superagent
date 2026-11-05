"""Superagent Orchestrator runtime and persistence layer (Lane B single-writer).

Implements trigger event ingress, idempotent persistence into ``sa_triggers``,
and policy snapshot/versioning per E-007, E-013, and Phase 2 architecture.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid

from hermes_cli.kanban_db_connect import connect_closing, write_txn

logger = logging.getLogger(__name__)

__all__ = [
    "accept",
    "amend_mandate",
    "assemble_batch",
    "expire_evals",
    "handback",
    "policy_changed",
    "policy_snapshot",
    "policy_version",
    "receive_trigger",
    "release_on_end",
    "reopen_ended_ticket",
    "reopen_ticket",
    "reset_tick_throttle",
    "revalidate",
    "run_eval",
    "run_superagent_tick",
    "stop_ticket",
    "trigger_handback",
]


def _normalize_json_friendly(val: Any) -> Any:
    """Recursively convert sets/tuples into sorted lists/lists for deterministic JSON serialization."""
    if isinstance(val, (set, frozenset)):
        return sorted(val)
    if isinstance(val, tuple):
        return [_normalize_json_friendly(x) for x in val]
    if isinstance(val, list):
        return [_normalize_json_friendly(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _normalize_json_friendly(v) for k, v in val.items()}
    return val


def _extract_allowlists(config: dict[str, Any], sa_block: dict[str, Any]) -> Any:
    """Extract allowlists from superagent block, sources, or root config."""
    direct = None
    for d in (sa_block, config):
        if "allowlists" in d and d["allowlists"] is not None:
            direct = d["allowlists"]
            break
        if "allowlist" in d and d["allowlist"] is not None:
            direct = d["allowlist"]
            break

    sources = sa_block.get("sources")
    if not isinstance(sources, dict):
        sources = config.get("sources")
    source_allowlists: dict[str, Any] = {}
    if isinstance(sources, dict):
        for sname, sdata in sorted(sources.items()):
            if isinstance(sdata, dict) and "allowlist" in sdata and sdata["allowlist"] is not None:
                source_allowlists[sname] = sdata["allowlist"]

    if direct is not None and source_allowlists:
        if isinstance(direct, dict):
            merged = dict(direct)
            merged.setdefault("sources", source_allowlists)
            return _normalize_json_friendly(merged)
        return _normalize_json_friendly({"global": direct, "sources": source_allowlists})
    if direct is not None:
        return _normalize_json_friendly(direct)
    if source_allowlists:
        return _normalize_json_friendly(source_allowlists)
    return {}


def _extract_owner_identities(config: dict[str, Any], sa_block: dict[str, Any]) -> Any:
    """Extract owner identities if present in superagent block or root config."""
    for d in (sa_block, config):
        for k in ("owner_identities", "owner_ids", "owners", "owner_id", "owner"):
            if k in d and d[k] is not None:
                return _normalize_json_friendly(d[k])
    return None


# Owner's ruling (2026-11-04): a mandate's default scope includes restore-to-intended
# repairs. Part of the versioned policy snapshot, so every Decision/Mandate records it.
RESTORE_TO_INTENDED_DEFAULT: dict[str, Any] = {
    "name": "restore_to_intended",
    "definition": (
        "Diagnosing a malfunction and applying the smallest patch that returns the system "
        "to its documented or last-working intended behavior, with verification against "
        "user-visible signals and a report after the repair."
    ),
    "applies_to": "all systems under management, including tenant containers",
    "includes": [
        "diagnosis of a malfunction",
        "the minimum patch that restores intended behavior",
        "verification against user-visible signals",
        "a report after the repair",
    ],
    "excludes": [
        "behavior changes or new features",
        "schema or data changes",
        "deletions of real data",
        "external sends",
        "money movement",
        "anything irreversible",
    ],
    "ambiguity_rule": (
        "If the intended state is ambiguous, it is not a repair: escalate instead of patching."
    ),
}

DEFAULT_MANDATE_SCOPE_DEFAULTS: list[dict[str, Any]] = [RESTORE_TO_INTENDED_DEFAULT]


def policy_snapshot(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Capture the superagent policy inputs per E-013.

    Captures accept_mode, rules list, allowlists, and owner identities (if present in config).
    Snapshots are built per Eval batch, never cached across batches.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config()
        except Exception:  # health: allow BLE001 -- fallback to empty dict if load_config fails
            config = {}
    elif not isinstance(config, dict):
        config = {}

    sa_block = config.get("superagent") if isinstance(config.get("superagent"), dict) else config

    # 1. accept_mode (observe by default)
    accept_mode = sa_block.get("mode") or sa_block.get("accept_mode") or "observe"

    # 2. rules list
    raw_rules = sa_block.get("rules", [])
    if isinstance(raw_rules, list):
        rules = _normalize_json_friendly(list(raw_rules))
    else:
        rules = []

    # 3. allowlists
    allowlists = _extract_allowlists(config, sa_block)

    raw_defaults = sa_block.get("mandate_scope_defaults")
    if isinstance(raw_defaults, list) and raw_defaults:
        scope_defaults = _normalize_json_friendly(list(raw_defaults))
    else:
        scope_defaults = _normalize_json_friendly(list(DEFAULT_MANDATE_SCOPE_DEFAULTS))

    snapshot: dict[str, Any] = {
        "accept_mode": accept_mode,
        "allowlists": allowlists,
        "rules": rules,
        "mandate_scope_defaults": scope_defaults,
    }

    sources = sa_block.get("sources")
    if not isinstance(sources, dict):
        sources = config.get("sources")
    if isinstance(sources, dict) and sources:
        snapshot["sources"] = _normalize_json_friendly(sources)

    for k in ("first_party", "first_party_sources"):
        for d in (sa_block, config):
            if k in d and d[k] is not None:
                snapshot[k] = _normalize_json_friendly(d[k])
                break

    # 4. owner identities (only included if present in config)
    owner = _extract_owner_identities(config, sa_block)
    if owner is not None:
        snapshot["owner_identities"] = owner

    return snapshot


def policy_version(snapshot: dict[str, Any]) -> str:
    """Compute the 12-char hex sha256 digest of canonical snapshot JSON per E-013.

    policy_version = first 12 hex chars of sha256 of the canonical JSON
    (json.dumps(policy, sort_keys=True)).
    """
    canonical = json.dumps(snapshot, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _parse_correlation_keys(correlation: Any) -> list[str]:
    """Extract string correlation keys from correlation parameter or JSON."""
    if correlation is None:
        return []
    if isinstance(correlation, (list, tuple, set)):
        return [str(k) for k in correlation if k is not None and str(k).strip()]
    if isinstance(correlation, str):
        s = correlation.strip()
        if not s or s == "[]":
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(k) for k in parsed if k is not None and str(k).strip()]
            if isinstance(parsed, str) and parsed.strip():
                return [parsed.strip()]
        except (ValueError, TypeError):
            return [s]
    return []


def _receive_trigger_on_conn(
    conn: sqlite3.Connection,
    *,
    source: str,
    scope: str,
    thread_key: str,
    actor_source_id: str | None,
    actor_is_bot: bool | int,
    addressed: bool | int,
    kind: str,
    body: str | None,
    meta: Any | None,
    correlation: Any | None,
    idempotency_key: str,
    now: float,
    config: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Execute idempotent trigger insertion on an open database connection."""
    if meta is None:
        meta_json = "{}"
    elif isinstance(meta, str):
        meta_json = meta
    else:
        meta_json = json.dumps(meta)

    if correlation is None:
        correlation_json = "[]"
    elif isinstance(correlation, str):
        correlation_json = correlation
    else:
        correlation_json = json.dumps(correlation)

    new_id = f"trg_{uuid.uuid4().hex[:12]}"
    is_person = 1 if (not bool(actor_is_bot) and actor_source_id is not None and str(actor_source_id).strip() != "") else 0

    with write_txn(conn, allow_nested=True):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO sa_triggers (
                id, source, scope, thread_key,
                actor_source_id, actor_is_bot, addressed,
                kind, body, meta_json, correlation_json,
                idempotency_key, eval_state, eval_requeues,
                disposition, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, NULL, ?)
            """,
            (
                new_id,
                str(source),
                str(scope),
                str(thread_key),
                str(actor_source_id) if actor_source_id is not None else None,
                1 if actor_is_bot else 0,
                1 if addressed else 0,
                str(kind),
                str(body) if body is not None else None,
                meta_json,
                correlation_json,
                str(idempotency_key),
                int(now),
            ),
        )
        if cur.rowcount == 0:
            # Duplicate insert: find existing row id (silent no-op, never raise)
            row = conn.execute(
                "SELECT id FROM sa_triggers WHERE idempotency_key = ?",
                (str(idempotency_key),),
            ).fetchone()
            if row is not None:
                return (row[0], "duplicate")
            return ("", "duplicate")

        # Check if passthrough (fan-out) is enabled per Card 3.8.
        # If superagent.passthrough is False, fan-out is disabled.
        # Default is True per Phase 3 spec.
        passthrough_enabled = True
        if config is not None:
            sa_cfg = config.get("superagent") if isinstance(config.get("superagent"), dict) else config
            if isinstance(sa_cfg, dict) and "passthrough" in sa_cfg:
                passthrough_enabled = bool(sa_cfg.get("passthrough"))
        elif policy is not None and "passthrough" in policy:
            passthrough_enabled = bool(policy.get("passthrough"))
        else:
            try:
                from hermes_cli.config import load_config_readonly
                cfg = load_config_readonly()
                sa_cfg = cfg.get("superagent") if isinstance(cfg.get("superagent"), dict) else cfg
                if isinstance(sa_cfg, dict) and "passthrough" in sa_cfg:
                    passthrough_enabled = bool(sa_cfg.get("passthrough"))
            except Exception:
                passthrough_enabled = True

        if not passthrough_enabled:
            return (new_id, "queued")

        # Fan-out: look up subscriptions for the trigger's thread_key.
        # Integration merge point: direct SQL against sa_subscriptions.
        # At integration, these queries delegate to hermes_cli.superagent_subscriptions.
        direct_rows = conn.execute(
            "SELECT ticket_id, context_only FROM sa_subscriptions WHERE thread_key = ?",
            (str(thread_key),),
        ).fetchall()

        has_owning_subscription = False
        deliveries: dict[str, tuple[int, str]] = {}

        for row in direct_rows:
            tid = str(row[0] if isinstance(row, tuple) else row["ticket_id"])
            ctx = int(row[1] if isinstance(row, tuple) else row["context_only"])

            # Check if ticket is person-stopped:
            # "Person-stopped Tickets (mandate status withdrawn) receive nothing, ever."
            stopped_row = conn.execute(
                """
                SELECT 1 FROM sa_mandates m
                WHERE (m.ticket_id = ? OR m.id = (SELECT mandate_id FROM tasks WHERE id = ?))
                  AND m.status = 'withdrawn'
                LIMIT 1
                """,
                (tid, tid),
            ).fetchone()
            if stopped_row is not None:
                continue

            # Check if ticket is ended:
            trow = conn.execute(
                "SELECT id, status, mandate_id FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            is_ended = (
                trow is not None
                and trow["status"] in ("done", "archived", "failed", "cancelled")
            )

            if is_ended:
                # Ended-Ticket resume (Card 3.6):
                # in fan-out, if the owning Ticket is ended and not person-stopped
                # and the trigger is a person's message: call revalidate()
                if ctx == 0 and is_person == 1:
                    mid = None
                    if trow and trow["mandate_id"]:
                        mid = trow["mandate_id"]
                    else:
                        mrow = conn.execute(
                            "SELECT id FROM sa_mandates WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
                            (tid,),
                        ).fetchone()
                        if mrow is not None:
                            mid = mrow[0] if isinstance(mrow, (tuple, list)) else mrow["id"]

                    reval_ok = False
                    if mid is not None:
                        try:
                            reval_ok = _revalidate_on_conn(
                                conn, mid, now=now, config=config, policy=policy
                            )
                        except Exception as exc:  # health: allow BLE001 -- revalidate error boundary
                            logger.debug("revalidate failed for mandate %s: %s", mid, exc)
                            reval_ok = False

                    if reval_ok:
                        # pass -> reopen (new task_runs row via the dispatcher path,
                        # resume Input written, disposition fanned_out)
                        _reopen_ended_ticket_on_conn(conn, tid, now=now)
                        deliveries[tid] = (0, "resume")
                        has_owning_subscription = True
                    else:
                        # fail -> no resume, message treated as unowned (Eval path),
                        # and NO notice to the Router that it reached a worker (v2 rule).
                        try:
                            if mid is not None:
                                conn.execute(
                                    "UPDATE sa_mandates SET status = 'revoked' WHERE id = ? AND status = 'active'",
                                    (mid,),
                                )
                        except Exception as exc:  # health: allow BLE001 -- mandate revoke error boundary
                            logger.debug("mandate revocation failed for %s: %s", mid, exc)
                else:
                    # Context-only subscription or bot/CI trigger to ended ticket:
                    # Never resumes, receives no inputs.
                    pass
            else:
                # Live ticket (or non-task subscription in pure fan-out tests)
                deliveries[tid] = (ctx, "trigger")
                if ctx == 0:
                    has_owning_subscription = True

        # Correlation keys in correlation_json add CONTEXT-ONLY delivery to tickets
        # subscribed to the primary thread only (never creates ownership on correlated thread).
        corr_keys = _parse_correlation_keys(correlation if correlation is not None else correlation_json)
        for ckey in corr_keys:
            if ckey == str(thread_key):
                continue
            corr_rows = conn.execute(
                "SELECT ticket_id, context_only FROM sa_subscriptions WHERE thread_key = ?",
                (ckey,),
            ).fetchall()
            for row in corr_rows:
                tid = str(row[0] if isinstance(row, tuple) else row["ticket_id"])
                if tid in deliveries:
                    continue

                # Check if person-stopped
                stopped_row = conn.execute(
                    """
                    SELECT 1 FROM sa_mandates m
                    WHERE (m.ticket_id = ? OR m.id = (SELECT mandate_id FROM tasks WHERE id = ?))
                      AND m.status = 'withdrawn'
                    LIMIT 1
                    """,
                    (tid, tid),
                ).fetchone()
                if stopped_row is not None:
                    continue

                # Correlation is always context-only; ended tickets never resume from context
                trow = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
                if trow is not None and trow["status"] in ("done", "archived", "failed", "cancelled"):
                    continue

                deliveries[tid] = (1, "trigger")  # Always context-only delivery via correlation

        # Write an sa_inputs row for each subscription delivery
        for tid in sorted(deliveries.keys()):
            ctx_flag, inp_kind = deliveries[tid]
            conn.execute(
                """
                INSERT INTO sa_inputs (
                    ticket_id, trigger_id, kind, body,
                    is_person, context_only, read_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    tid,
                    new_id,
                    inp_kind,
                    str(body) if body is not None else None,
                    is_person,
                    ctx_flag,
                    int(now),
                ),
            )

        # Follows-done rule on close/merge trigger (Card 3.4)
        if str(kind) in ("close", "merge", "closed", "merged"):
            try:
                from hermes_cli.superagent_pr import apply_follows_done_rule, record_pr_state
                pr_outcome = "merged" if str(kind) in ("merge", "merged") else "closed"
                record_pr_state(conn, str(thread_key), state="closed", outcome=pr_outcome, now=now)
                for tid in deliveries:
                    apply_follows_done_rule(conn, tid, trigger_id=new_id, now=now)
            except Exception as exc:  # health: allow BLE001 -- follows-done failure must not block trigger receipt
                logger.debug("follows-done failed for trigger %s: %s", new_id, exc)

        # Disposition rule:
        # If ANY owning (context_only=0) subscription exists AND the trigger is a person's message:
        # disposition 'fanned_out' and the trigger does NOT enter Eval (set eval_state to 'done').
        # Bot/CI triggers, context-only subscriptions, or no subscriptions queue for Eval normally.
        if has_owning_subscription and is_person == 1:
            # Terminal marking: trigger fanned out to owning subscription(s) skips Eval
            # and is marked terminal with eval_state='done' and disposition='fanned_out'.
            conn.execute(
                "UPDATE sa_triggers SET eval_state = 'done', disposition = 'fanned_out' WHERE id = ?",
                (new_id,),
            )
            return (new_id, "fanned_out")

        return (new_id, "queued")


def receive_trigger(
    db_path_or_conn: Path | str | sqlite3.Connection,
    *,
    source: str,
    scope: str,
    thread_key: str,
    actor_source_id: str | None,
    actor_is_bot: bool | int,
    addressed: bool | int,
    kind: str,
    body: str | None,
    meta: Any | None = None,
    correlation: Any | None = None,
    idempotency_key: str,
    now: float,
    config: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Idempotently insert an event trigger into ``sa_triggers`` per E-007.

    Returns (trigger_id, disposition):
      - Fresh insert: (new id 'trg_<uuid4hex12>', 'queued')
      - Duplicate idempotency_key: (existing_id, 'duplicate') with NO new row.
    """
    if isinstance(db_path_or_conn, (str, Path)):
        with connect_closing(Path(db_path_or_conn)) as conn:
            return _receive_trigger_on_conn(
                conn,
                source=source,
                scope=scope,
                thread_key=thread_key,
                actor_source_id=actor_source_id,
                actor_is_bot=actor_is_bot,
                addressed=addressed,
                kind=kind,
                body=body,
                meta=meta,
                correlation=correlation,
                idempotency_key=idempotency_key,
                now=now,
                config=config,
                policy=policy,
            )
    return _receive_trigger_on_conn(
        db_path_or_conn,
        source=source,
        scope=scope,
        thread_key=thread_key,
        actor_source_id=actor_source_id,
        actor_is_bot=actor_is_bot,
        addressed=addressed,
        kind=kind,
        body=body,
        meta=meta,
        correlation=correlation,
        idempotency_key=idempotency_key,
        now=now,
        config=config,
        policy=policy,
    )


# --- Handback Engine (Card 3.5, E-027) ---

def _trigger_handback_on_conn(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    trigger_id: str,
    reason: str,
    now: float,
) -> dict[str, Any]:
    """Record trigger handback by a ticket on connection per E-027."""
    with write_txn(conn, allow_nested=True):
        trg = conn.execute(
            "SELECT * FROM sa_triggers WHERE id = ?",
            (str(trigger_id),),
        ).fetchone()
        if trg is None:
            return {"ok": False, "error": f"trigger {trigger_id} not found"}

        meta = json.loads(trg["meta_json"]) if trg["meta_json"] else {}
        if not isinstance(meta, dict):
            meta = {}
        handbacks = meta.get("handbacks", [])
        if not isinstance(handbacks, list):
            handbacks = []

        # Second handback of the same Trigger by the same Ticket -> error string
        for hb in handbacks:
            if isinstance(hb, dict) and hb.get("ticket_id") == str(ticket_id):
                return {
                    "ok": False,
                    "error": f"trigger {trigger_id} already handed back by ticket {ticket_id}",
                }

        # Record this handback on the Trigger
        handbacks.append({
            "ticket_id": str(ticket_id),
            "reason": str(reason),
            "created_at": int(now),
        })
        meta["handbacks"] = handbacks
        reasons_list = meta.get("handback_reasons", [])
        if not isinstance(reasons_list, list):
            reasons_list = []
        if str(reason) not in reasons_list:
            reasons_list.append(str(reason))
        meta["handback_reasons"] = reasons_list

        new_meta_json = json.dumps(meta)

        # Check ownership:
        # A ticket owns a trigger if it has an owning subscription (context_only = 0)
        # on the trigger's thread_key, or an owning input delivery.
        thread_key = str(trg["thread_key"])
        owning_sub_rows = conn.execute(
            "SELECT ticket_id FROM sa_subscriptions WHERE thread_key = ? AND context_only = 0",
            (thread_key,),
        ).fetchall()
        owning_ticket_ids = {
            str(r[0] if isinstance(r, tuple) else r["ticket_id"])
            for r in owning_sub_rows
        }

        input_rows = conn.execute(
            "SELECT ticket_id FROM sa_inputs WHERE trigger_id = ? AND context_only = 0",
            (str(trigger_id),),
        ).fetchall()
        for r in input_rows:
            owning_ticket_ids.add(str(r[0] if isinstance(r, tuple) else r["ticket_id"]))

        handed_back_ticket_ids = {
            hb.get("ticket_id")
            for hb in handbacks
            if isinstance(hb, dict) and "ticket_id" in hb
        }

        remaining_owners = owning_ticket_ids - handed_back_ticket_ids

        # Orchestrator records the handback on the Trigger's disposition,
        # and when no other Ticket owns it, queues it for fresh Eval with the reason attached to the batch context.
        queued_for_eval = False
        if not remaining_owners:
            conn.execute(
                """
                UPDATE sa_triggers
                SET eval_state = 'queued',
                    disposition = 'handed_back',
                    meta_json = ?
                WHERE id = ?
                """,
                (new_meta_json, str(trigger_id)),
            )
            queued_for_eval = True
        else:
            conn.execute(
                """
                UPDATE sa_triggers
                SET disposition = 'handed_back',
                    meta_json = ?
                WHERE id = ?
                """,
                (new_meta_json, str(trigger_id)),
            )

        return {
            "ok": True,
            "trigger_id": str(trigger_id),
            "ticket_id": str(ticket_id),
            "disposition": "handed_back",
            "queued_for_eval": queued_for_eval,
            "remaining_owners": sorted(remaining_owners),
        }


def trigger_handback(
    db_path_or_conn: Path | str | sqlite3.Connection,
    *,
    ticket_id: str,
    trigger_id: str,
    reason: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Record trigger handback by a ticket per E-027.

    Orchestrator records the handback on the Trigger's disposition, and when no
    other Ticket owns it, queues it for fresh Eval with the reason attached to the
    batch context. Second handback of the same Trigger by the same Ticket returns
    an error dict with ok=False and error string.
    """
    current_now = time.time() if now is None else float(now)
    if isinstance(db_path_or_conn, (str, Path)):
        with connect_closing(Path(db_path_or_conn)) as conn:
            return _trigger_handback_on_conn(
                conn,
                ticket_id=ticket_id,
                trigger_id=trigger_id,
                reason=reason,
                now=current_now,
            )
    return _trigger_handback_on_conn(
        db_path_or_conn,
        ticket_id=ticket_id,
        trigger_id=trigger_id,
        reason=reason,
        now=current_now,
    )


handback = trigger_handback


# --- Eval Engine: Batch Assembly, Evaluation, and Deadlines (E-016, E-017) ---

def _extract_llm_text(resp: Any) -> str:
    """Extract string content from diverse LLM response shapes."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if hasattr(resp, "choices") and resp.choices:
        choice = resp.choices[0]
        if hasattr(choice, "message") and hasattr(choice.message, "content"):
            return choice.message.content or ""
        if isinstance(choice, dict):
            msg = choice.get("message", {})
            if isinstance(msg, dict):
                return msg.get("content", "") or ""
            if hasattr(msg, "content"):
                return msg.content or ""
    if isinstance(resp, dict):
        choices = resp.get("choices")
        if choices:
            c = choices[0]
            if isinstance(c, dict):
                return c.get("message", {}).get("content", "") or ""
        for k in ("content", "text", "response"):
            val = resp.get(k)
            if isinstance(val, str):
                return val
    if hasattr(resp, "content"):
        return str(resp.content or "")
    if hasattr(resp, "text"):
        return str(resp.text or "")
    return str(resp)


_VERDICT_OUTCOME_MAP: dict[str, str] = {
    "accept": "accept",
    "approve": "accept",
    "yes": "accept",
    "accepted": "accept",
    "stop": "stop",
    "halt": "stop",
    "cancel": "stop",
    "terminate": "stop",
    "ignore": "ignore",
    "reject": "ignore",
    "deny": "ignore",
    "declined": "ignore",
    "ignored": "ignore",
    "unavailable": "unavailable",
}


def _parse_model_verdict(raw_text: str) -> tuple[str, list[Any], dict[str, Any]]:
    """Parse model verdict JSON into (outcome, matched_rules, reason) per E-016.

    Raises ValueError on empty or unparseable output.
    """
    content = raw_text.strip()
    if not content:
        raise ValueError("Empty model response")
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Model JSON must be a dict")

    verdict = data.get("outcome") or data.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        raise ValueError("Model JSON missing outcome/verdict")

    norm = verdict.strip().lower()
    outcome = _VERDICT_OUTCOME_MAP.get(norm, "ignore")

    raw_rules = data.get("matched_rules", [])
    if isinstance(raw_rules, list):
        matched_rules = raw_rules
    elif raw_rules is not None:
        matched_rules = [raw_rules]
    else:
        matched_rules = []

    reason = data.get("reason", {})
    if isinstance(reason, str):
        reason_dict: dict[str, Any] = {"text": reason}
    elif isinstance(reason, dict):
        reason_dict = reason
    elif reason is not None:
        reason_dict = {"text": str(reason)}
    else:
        reason_dict = {}

    return outcome, matched_rules, reason_dict


def _assemble_batch_on_conn(
    conn: sqlite3.Connection,
    *,
    thread_key: str | None = None,
    now: float,
    timeout_seconds: float = 60.0,
    max_batch_size: int = 20,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Assemble up to 20 queued triggers for a thread into an active pending batch (E-017)."""
    with write_txn(conn, allow_nested=True):
        target_thread = thread_key
        if target_thread is None:
            row = conn.execute(
                """
                SELECT thread_key, MIN(created_at) AS oldest
                FROM sa_triggers
                WHERE eval_state = 'queued'
                  AND thread_key NOT IN (
                      SELECT DISTINCT thread_key FROM sa_triggers WHERE eval_state = 'pending'
                  )
                GROUP BY thread_key
                ORDER BY oldest ASC, thread_key ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            target_thread = row["thread_key"]
        else:
            pending = conn.execute(
                "SELECT COUNT(*) FROM sa_triggers WHERE thread_key = ? AND eval_state = 'pending'",
                (target_thread,),
            ).fetchone()
            if pending is not None and pending[0] > 0:
                return None

        rows = conn.execute(
            """
            SELECT * FROM sa_triggers
            WHERE thread_key = ? AND eval_state = 'queued'
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (target_thread, max_batch_size),
        ).fetchall()
        if not rows:
            return None

        deadline = int(now + timeout_seconds)
        trigger_ids: list[str] = []
        batch_triggers: list[dict[str, Any]] = []

        for r in rows:
            tid = r["id"]
            trigger_ids.append(tid)
            try:
                meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:  # health: allow BLE001 -- malformed meta fallback
                meta = {}
            meta["eval_deadline"] = deadline
            meta["deadline"] = deadline
            new_meta_json = json.dumps(meta)

            conn.execute(
                "UPDATE sa_triggers SET eval_state = 'pending', meta_json = ? WHERE id = ?",
                (new_meta_json, tid),
            )
            tdict = dict(r)
            tdict["eval_state"] = "pending"
            tdict["meta_json"] = new_meta_json
            batch_triggers.append(tdict)

        handback_reasons: list[str] = []
        for tdict in batch_triggers:
            try:
                m = json.loads(tdict.get("meta_json") or "{}")
                if isinstance(m, dict):
                    t_reasons: list[str] = []
                    for hb in m.get("handbacks", []):
                        if isinstance(hb, dict) and hb.get("reason"):
                            r_str = str(hb["reason"])
                            if r_str not in handback_reasons:
                                handback_reasons.append(r_str)
                            if r_str not in t_reasons:
                                t_reasons.append(r_str)
                    for r_item in m.get("handback_reasons", []):
                        r_str = str(r_item)
                        if r_str not in handback_reasons:
                            handback_reasons.append(r_str)
                        if r_str not in t_reasons:
                            t_reasons.append(r_str)
                    if t_reasons:
                        tdict["handback_reasons"] = t_reasons
            except Exception:  # health: allow BLE001 -- malformed meta fallback
                pass

        batch_result: dict[str, Any] = {
            "thread_key": target_thread,
            "trigger_ids": trigger_ids,
            "triggers": batch_triggers,
            "eval_deadline": deadline,
            "db_path": str(db_path) if db_path is not None else None,
            "_conn": conn,
        }
        if handback_reasons:
            ctx: dict[str, Any] = {
                "handback_reasons": handback_reasons,
                "reasons": handback_reasons,
                "reason": handback_reasons[0] if len(handback_reasons) == 1 else handback_reasons,
            }
            for r_str in handback_reasons:
                ctx[r_str] = True
            batch_result["context"] = ctx
            batch_result["handback_reasons"] = handback_reasons

        return batch_result


def assemble_batch(
    db_path_or_conn: Path | str | sqlite3.Connection | None = None,
    thread_key: str | None = None,
    *,
    now: float | None = None,
    timeout_seconds: float | None = None,
    max_batch_size: int = 20,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Assemble up to 20 queued triggers into a pending batch for a thread per E-017.

    Triggers arriving while an Eval runs join the NEXT batch, never the running one.
    """
    current_now = time.time() if now is None else float(now)
    if timeout_seconds is None:
        if isinstance(config, dict) and isinstance(config.get("superagent"), dict):
            timeout_seconds = float(config["superagent"].get("eval_timeout_seconds", 60.0))
        elif isinstance(config, dict) and "eval_timeout_seconds" in config:
            timeout_seconds = float(config["eval_timeout_seconds"])
        else:
            timeout_seconds = 60.0

    if isinstance(db_path_or_conn, (str, Path)) or db_path_or_conn is None:
        target_path = Path(db_path_or_conn) if db_path_or_conn is not None else None
        with connect_closing(target_path) as conn:
            return _assemble_batch_on_conn(
                conn,
                thread_key=thread_key,
                now=current_now,
                timeout_seconds=timeout_seconds,
                max_batch_size=max_batch_size,
                db_path=target_path,
            )

    return _assemble_batch_on_conn(
        db_path_or_conn,
        thread_key=thread_key,
        now=current_now,
        timeout_seconds=timeout_seconds,
        max_batch_size=max_batch_size,
        db_path=None,
    )


def _unpack_batch_triggers(
    conn: sqlite3.Connection, batch: Any
) -> tuple[str, list[dict[str, Any]]]:
    """Extract thread_key and hydrated triggers list from input batch shape."""
    if isinstance(batch, dict):
        thread_key = str(batch.get("thread_key") or "")
        raw_triggers = batch.get("triggers", [])
        raw_ids = batch.get("trigger_ids", [])
    elif isinstance(batch, list):
        raw_triggers = batch
        thread_key = (
            str(raw_triggers[0].get("thread_key") or "")
            if raw_triggers and isinstance(raw_triggers[0], dict)
            else ""
        )
        raw_ids = []
    else:
        raw_triggers = [batch]
        thread_key = str(getattr(batch, "thread_key", "") or "")
        raw_ids = []

    if raw_triggers and isinstance(raw_triggers[0], dict):
        triggers = [dict(t) for t in raw_triggers]
    elif raw_ids or (raw_triggers and isinstance(raw_triggers[0], str)):
        id_list = raw_ids if raw_ids else raw_triggers
        placeholders = ",".join("?" for _ in id_list)
        rows = conn.execute(
            f"SELECT * FROM sa_triggers WHERE id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
            id_list,
        ).fetchall()
        triggers = [dict(r) for r in rows]
    else:
        triggers = []

    if not thread_key and triggers:
        thread_key = str(triggers[0].get("thread_key") or "")

    return thread_key, triggers


def _is_observe_mode(config: dict[str, Any] | None, policy: dict[str, Any]) -> bool:
    """Return True if observe mode is configured."""
    if isinstance(config, dict):
        sa_block = config.get("superagent")
        if isinstance(sa_block, dict):
            if "mode" in sa_block:
                return sa_block["mode"] == "observe"
            if "accept_mode" in sa_block:
                return sa_block["accept_mode"] == "observe"
        if "mode" in config:
            return config["mode"] == "observe"
        if "accept_mode" in config:
            return config["accept_mode"] == "observe"
    return policy.get("accept_mode") == "observe"


def _evaluate_inferred_with_llm(
    thread_key: str,
    triggers: list[dict[str, Any]],
    rules: list[Any],
    scope_defaults: list[Any] | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    """Assess inferred trigger batch via call_llm('superagent_eval') per E-016."""
    from agent.auxiliary_client import call_llm

    system_prompt = (
        "You are the Hermes Superagent evaluation engine. Assess incoming events and determine "
        "whether they should be accepted, ignored, or stopped. Output JSON with fields: "
        "outcome (one of 'accept', 'ignore', 'stop'), reason, matched_rules. "
        "A mandate's default scope includes restore-to-intended repairs as defined in "
        "mandate_scope_defaults; behavior changes, schema/data changes, deletions, external "
        "sends, money movement, and irreversible actions are never in default scope."
    )
    payload: dict[str, Any] = {
        "thread_key": thread_key,
        "triggers": [
            {
                "id": t.get("id"),
                "source": t.get("source"),
                "kind": t.get("kind"),
                "body": t.get("body"),
                "actor_source_id": t.get("actor_source_id"),
                **(
                    {"handback_reasons": t["handback_reasons"]}
                    if "handback_reasons" in t
                    else {}
                ),
            }
            for t in triggers
        ],
        "rules": rules,
    }
    if scope_defaults:
        payload["mandate_scope_defaults"] = scope_defaults
    user_prompt = json.dumps(payload)

    try:
        resp = call_llm(
            task="superagent_eval",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )
        raw_text = _extract_llm_text(resp)
        return _parse_model_verdict(raw_text)
    except Exception:  # health: allow BLE001 -- unparseable JSON or failed LLM maps to unavailable per E-016
        return "unavailable", [], {"error": "unparseable_model_output_or_call_failed"}


def _run_eval_on_conn(
    conn: sqlite3.Connection,
    batch: Any,
    *,
    config: dict[str, Any] | None = None,
    now: float,
) -> dict[str, Any]:
    """Execute evaluation for an assembled trigger batch and record decision row."""
    from hermes_cli.superagent_gate import admit

    thread_key, triggers = _unpack_batch_triggers(conn, batch)
    if not triggers:
        return {}

    trigger_ids = [t["id"] for t in triggers if "id" in t]
    policy = policy_snapshot(config)
    p_ver = policy_version(policy)
    is_observe = _is_observe_mode(config, policy)

    has_explicit = False
    has_inferred = False
    first_explicit_basis: dict[str, Any] = {}

    for t in triggers:
        gate_strength, basis = admit(t, policy)
        if gate_strength == "explicit":
            has_explicit = True
            if not first_explicit_basis:
                first_explicit_basis = basis
        elif gate_strength == "inferred":
            has_inferred = True

    if has_explicit:
        strength: str | None = "explicit"
        outcome = "accept"
        matched_rules: list[Any] = []
        reason = first_explicit_basis
    elif has_inferred:
        strength = "inferred"
        outcome, matched_rules, reason = _evaluate_inferred_with_llm(
            thread_key,
            triggers,
            policy.get("rules", []),
            scope_defaults=policy.get("mandate_scope_defaults"),
        )
    else:
        strength = "not_admitted"
        outcome = "ignore"
        matched_rules = []
        reason = {"via": "admission_gate", "detail": "not_admitted"}

    would_accept = 1 if (is_observe and outcome == "accept") else 0

    with write_txn(conn, allow_nested=True):
        cur = conn.execute(
            """
            INSERT INTO sa_decisions (
                thread_key, trigger_ids_json, outcome, strength,
                would_accept, ignored_from_card, matched_rules_json,
                reason_json, policy_version, ticket_id, dismissed, created_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, 0, ?)
            """,
            (
                thread_key,
                json.dumps(trigger_ids),
                outcome,
                strength,
                would_accept,
                json.dumps(matched_rules),
                json.dumps(reason),
                p_ver,
                int(now),
            ),
        )
        decision_id = cur.lastrowid

        for tid in trigger_ids:
            conn.execute(
                "UPDATE sa_triggers SET eval_state = 'done', disposition = ? WHERE id = ?",
                (outcome, tid),
            )

    return {
        "id": decision_id,
        "thread_key": thread_key,
        "trigger_ids": trigger_ids,
        "outcome": outcome,
        "strength": strength,
        "would_accept": would_accept,
        "matched_rules": matched_rules,
        "reason": reason,
        "policy_version": p_ver,
        "created_at": int(now),
    }


def run_eval(
    batch: Any,
    db_path_or_conn: Path | str | sqlite3.Connection | None = None,
    *,
    config: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Execute evaluation for an assembled trigger batch per E-016 and E-017.

    Evaluates pure admission gate; for explicit triggers records outcome 'accept',
    for inferred triggers queries call_llm('superagent_eval'), and records a Decision
    row in sa_decisions. Never creates tasks.
    """
    current_now = time.time() if now is None else float(now)

    target_db = db_path_or_conn
    if target_db is None and isinstance(batch, dict):
        target_db = batch.get("db_path") or batch.get("_conn") or batch.get("conn")

    if isinstance(target_db, (str, Path)) or target_db is None:
        target_path = Path(target_db) if target_db is not None else None
        with connect_closing(target_path) as conn:
            return _run_eval_on_conn(conn, batch, config=config, now=current_now)

    return _run_eval_on_conn(target_db, batch, config=config, now=current_now)


def _expire_evals_on_conn(
    conn: sqlite3.Connection,
    now: float,
    *,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Inspect pending triggers, requeue up to 2 times, and record unavailable on third expiry."""
    rows = conn.execute(
        "SELECT * FROM sa_triggers WHERE eval_state = 'pending' ORDER BY thread_key, created_at ASC, id ASC"
    ).fetchall()
    if not rows:
        return []

    expired_by_thread: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        try:
            meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception:  # health: allow BLE001 -- malformed meta fallback
            meta = {}
        deadline = meta.get("eval_deadline") or meta.get("deadline")
        if deadline is not None:
            if float(deadline) <= now:
                expired_by_thread.setdefault(r["thread_key"], []).append(r)
        elif float(r["created_at"]) + 60.0 <= now:
            expired_by_thread.setdefault(r["thread_key"], []).append(r)

    if not expired_by_thread:
        return []

    results: list[dict[str, Any]] = []
    policy = policy_snapshot(config)
    p_ver = policy_version(policy)

    with write_txn(conn, allow_nested=True):
        for thread_key, trg_list in expired_by_thread.items():
            tids = [t["id"] for t in trg_list]
            current_requeues = max(int(t["eval_requeues"] or 0) for t in trg_list)
            next_requeues = current_requeues + 1

            if next_requeues <= 2:
                for t in trg_list:
                    try:
                        meta = json.loads(t["meta_json"]) if t["meta_json"] else {}
                        if not isinstance(meta, dict):
                            meta = {}
                    except Exception:  # health: allow BLE001 -- meta fallback
                        meta = {}
                    meta.pop("eval_deadline", None)
                    meta.pop("deadline", None)
                    conn.execute(
                        """
                        UPDATE sa_triggers
                        SET eval_state = 'queued', eval_requeues = ?, meta_json = ?
                        WHERE id = ?
                        """,
                        (next_requeues, json.dumps(meta), t["id"]),
                    )
                results.append({
                    "thread_key": thread_key,
                    "action": "requeued",
                    "eval_requeues": next_requeues,
                    "trigger_ids": tids,
                })
            else:
                reason = {"reason": "eval_timeout_expired", "requeues": next_requeues}
                cur = conn.execute(
                    """
                    INSERT INTO sa_decisions (
                        thread_key, trigger_ids_json, outcome, strength,
                        would_accept, ignored_from_card, matched_rules_json,
                        reason_json, policy_version, ticket_id, dismissed, created_at
                    ) VALUES (?, ?, 'unavailable', NULL, 0, 0, '[]', ?, ?, NULL, 0, ?)
                    """,
                    (
                        thread_key,
                        json.dumps(tids),
                        json.dumps(reason),
                        p_ver,
                        int(now),
                    ),
                )
                dec_id = cur.lastrowid
                for t in trg_list:
                    conn.execute(
                        """
                        UPDATE sa_triggers
                        SET eval_state = 'done', disposition = 'unavailable', eval_requeues = ?
                        WHERE id = ?
                        """,
                        (next_requeues, t["id"]),
                    )
                results.append({
                    "thread_key": thread_key,
                    "action": "unavailable",
                    "decision_id": dec_id,
                    "eval_requeues": next_requeues,
                    "trigger_ids": tids,
                })

    return results


def expire_evals(
    now: float,
    db_path_or_conn: Path | str | sqlite3.Connection | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expire overdue in-flight trigger evaluations, requeuing or recording unavailable per E-017."""
    if isinstance(db_path_or_conn, (str, Path)) or db_path_or_conn is None:
        target_path = Path(db_path_or_conn) if db_path_or_conn is not None else None
        with connect_closing(target_path) as conn:
            return _expire_evals_on_conn(conn, float(now), config=config)

    return _expire_evals_on_conn(db_path_or_conn, float(now), config=config)


_last_superagent_tick_at: float = 0.0

# Backlog drains over successive ticks.
TICK_DRAIN_MAX_BATCHES = 3


def reset_tick_throttle() -> None:
    """Reset the 60s throttle state for testing."""
    global _last_superagent_tick_at
    _last_superagent_tick_at = 0.0


def run_superagent_tick(
    db_path: Path | str | sqlite3.Connection | None,
    cfg: dict[str, Any] | None,
    now: float | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Execute periodic Superagent maintenance steps (INT-2).

    Only runs when ``superagent.enabled`` is True in configuration.
    Throttled to at most once per 60s using ``now`` (unless ``force=True``).

    Steps:
      (a) calls expire_evals to reclaim overdue trigger evaluations.
      (b) polls GitHub source (poll_github) when enabled in config.
      (c) polls VibeHard source (poll_vibehard) when enabled in config (import-guarded).
      (d) drains the eval queue: assembles and evaluates up to
          TICK_DRAIN_MAX_BATCHES queued batches and accepts explicit/inferred
          accept decisions (skipped while paused or in observe mode).
    """
    if not isinstance(cfg, dict):
        return {"status": "disabled"}
    sa_cfg = cfg.get("superagent")
    if not isinstance(sa_cfg, dict) or not sa_cfg.get("enabled", False):
        return {"status": "disabled"}

    global _last_superagent_tick_at
    if now is None:
        now = time.time()
    now_f = float(now)

    if not force and _last_superagent_tick_at > 0 and (now_f - _last_superagent_tick_at) < 60.0:
        return {"status": "throttled"}

    _last_superagent_tick_at = now_f

    results: dict[str, Any] = {
        "status": "ok",
        "expired_evals": [],
        "github_triggers": 0,
        "vibehard_triggers": [],
        "eval_drain": {"status": "skipped", "batches": 0, "accepted": 0},
    }

    if isinstance(db_path, sqlite3.Connection):
        _run_superagent_tick_on_conn(db_path, cfg, sa_cfg, now_f, results)
    else:
        target_path = Path(db_path) if db_path is not None else None
        with connect_closing(target_path) as conn:
            _run_superagent_tick_on_conn(conn, cfg, sa_cfg, now_f, results)

    return results


def _run_superagent_tick_on_conn(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    sa_cfg: dict[str, Any],
    now: float,
    results: dict[str, Any],
) -> None:
    # (a) Expire evaluations
    try:
        results["expired_evals"] = expire_evals(now, db_path_or_conn=conn, config=cfg)
    except Exception as exc:  # health: allow BLE001 -- periodic tick failure isolation
        logger.warning("run_superagent_tick: expire_evals failed: %s", exc)

    # (b) Poll GitHub when its source config enables it
    gh_cfg = sa_cfg.get("github")
    if isinstance(gh_cfg, dict) and gh_cfg.get("enabled", True) and gh_cfg.get("repository"):
        try:
            from hermes_cli import superagent_github as _sa_gh
            results["github_triggers"] = _sa_gh.poll_github(conn, cfg, now)
        except Exception as exc:  # health: allow BLE001 -- periodic tick failure isolation
            logger.warning("run_superagent_tick: poll_github failed: %s", exc)

    # (c) Poll VibeHard when superagent.vibehard.enabled (import-guarded)
    vh_cfg = sa_cfg.get("vibehard")
    if isinstance(vh_cfg, dict) and vh_cfg.get("enabled", False):
        try:
            from hermes_cli.superagent_vibehard import poll_vibehard
        except ImportError:
            poll_vibehard = None
            logger.info("superagent_vibehard module not present; skipping poll_vibehard")

        if poll_vibehard is not None:
            try:
                results["vibehard_triggers"] = poll_vibehard(conn, cfg, now)
            except Exception as exc:  # health: allow BLE001 -- periodic tick failure isolation
                logger.warning("run_superagent_tick: poll_vibehard failed: %s", exc)

    # (d) Drain the eval queue (assemble -> eval -> accept) without a Router turn
    results["eval_drain"] = _drain_eval_queue_on_conn(conn, cfg, now)


def _drain_eval_queue_on_conn(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    """Evaluate (and, outside observe mode, accept) queued triggers for one tick."""
    try:
        from hermes_cli.superagent_pause import is_paused

        paused = is_paused(conn)
    except Exception as exc:  # health: allow BLE001 -- fail closed: never auto-accept if pause state is unknown
        logger.warning("run_superagent_tick: pause check failed, skipping eval drain: %s", exc)
        return {"status": "pause_check_failed", "batches": 0, "accepted": 0}
    if paused:
        return {"status": "paused", "batches": 0, "accepted": 0}

    batches = 0
    accepted = 0
    errors = 0
    decisions: dict[str, int] = {}
    for _ in range(TICK_DRAIN_MAX_BATCHES):
        try:
            batch = assemble_batch(conn, config=cfg, now=now)
            if not batch:
                break
            batches += 1
            dec = run_eval(batch, db_path_or_conn=conn, config=cfg, now=now)
            if not dec:
                break
            outcome = str(dec.get("outcome"))
            decisions[outcome] = decisions.get(outcome, 0) + 1
            if outcome == "accept" and not _is_observe_mode(cfg, policy_snapshot(cfg)):
                if accept(conn, dec["id"], now=now, config=cfg):
                    accepted += 1
        except Exception as exc:  # health: allow BLE001 -- periodic tick failure isolation
            logger.warning("run_superagent_tick: eval drain failed: %s", exc)
            errors += 1
            break

    return {
        "status": "ok",
        "batches": batches,
        "decisions": decisions,
        "accepted": accepted,
        "errors": errors,
    }



# --- Accept Path & Mandate Mutation (Card 2.6, E-013, D-1) ---


def _collect_identities(val: Any, out: set[str]) -> None:
    """Collect non-empty string identities into the target set."""
    if isinstance(val, (list, set, tuple)):
        for x in val:
            if x is not None and str(x).strip():
                out.add(str(x).strip())
    elif isinstance(val, str) and val.strip():
        out.add(val.strip())


def _resolve_source_entry_owners(src_entry: Any, out: set[str]) -> None:
    """Extract owner identities from a specific source dictionary."""
    if not isinstance(src_entry, dict):
        return
    for k in ("owner", "owner_id", "owner_identities", "owners"):
        _collect_identities(src_entry.get(k), out)


def _resolve_owner_identities(config: dict[str, Any] | None, source: str | None = None) -> set[str]:
    """Resolve configured owner identities for a source per superagent.owner_identities."""
    if not config or not isinstance(config, dict):
        return set()

    sa_block = config.get("superagent") if isinstance(config.get("superagent"), dict) else config
    owners: set[str] = set()

    # 1. Check sources block for this source
    if source:
        sources = sa_block.get("sources")
        if not isinstance(sources, dict) and isinstance(config.get("sources"), dict):
            sources = config.get("sources")
        if isinstance(sources, dict):
            _resolve_source_entry_owners(sources.get(source), owners)

    # 2. Check top-level superagent.owner_identities (or root owner_identities)
    raw_owners = sa_block.get("owner_identities")
    if raw_owners is None and "owner_identities" in config:
        raw_owners = config.get("owner_identities")

    if raw_owners is None:
        return owners

    if isinstance(raw_owners, dict):
        if source and source in raw_owners:
            _collect_identities(raw_owners[source], owners)
        elif source and isinstance(raw_owners.get("sources"), dict):
            _collect_identities(raw_owners["sources"].get(source), owners)
        elif not source:
            for val in raw_owners.values():
                _collect_identities(val, owners)
    else:
        _collect_identities(raw_owners, owners)

    return owners


_SA_WORKER_CONTRACT = (
    "WORK ORDER — READ FIRST\n\n"
    "You are executing this request as a background work order. You are not "
    "replying in a chat, and nobody sees your messages except through the board.\n\n"
    "- Work until the request is fully done. Then call kanban_complete with a "
    "summary of the result — that summary is what gets reported back to the "
    "requester.\n"
    "- If you are genuinely blocked and cannot proceed, call kanban_block with "
    "the reason.\n"
    "- Never end your turn with a progress or status message. A run that ends "
    "without a terminal kanban call (kanban_complete, kanban_block, or "
    "kanban_request_review) is recorded as a failure, no matter what work it "
    "did. If you have finished part of the request, keep going — do not "
    "narrate a next step and stop."
)


def _accept_on_conn(
    conn: sqlite3.Connection,
    decision_id: int | str,
    now: float,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Execute accept transition on an open database connection."""
    from hermes_cli.kanban_db import create_task

    # A31-2 entry guard (surfaces Rev 3.1): the in-transaction ticket_id
    # re-check below serializes concurrent accepts ONLY when this
    # function's write_txn is the OUTERMOST transaction on the caller's
    # connection. An already-open caller transaction degrades
    # write_txn(allow_nested=True) to a savepoint on a stale snapshot and
    # voids the guarantee, so fail closed with no writes (the tick's
    # existing retry contract retries on a clean connection).
    if getattr(conn, "in_transaction", False):
        raise RuntimeError(
            "_accept_on_conn requires a connection with no open transaction: "
            "the in-transaction ticket_id re-check only serializes concurrent "
            "accepts when its write_txn is the outermost transaction"
        )

    dec_id = int(decision_id)
    row = conn.execute("SELECT * FROM sa_decisions WHERE id = ?", (dec_id,)).fetchone()
    if row is None:
        raise ValueError(f"Decision {decision_id} not found")

    if row["outcome"] != "accept":
        raise ValueError(
            f"Cannot accept Decision {decision_id}: outcome is '{row['outcome']}', expected 'accept'"
        )

    # Idempotent return if already accepted
    if row["ticket_id"]:
        return str(row["ticket_id"])

    policy = policy_snapshot(config)
    is_observe = _is_observe_mode(config, policy)

    # In observe mode: record would_accept on Decision, create NOTHING, return None
    if is_observe:
        with write_txn(conn, allow_nested=True):
            conn.execute(
                "UPDATE sa_decisions SET would_accept = 1 WHERE id = ?",
                (dec_id,),
            )
        return None

    # Retrieve associated triggers
    trigger_ids = json.loads(row["trigger_ids_json"] or "[]")
    triggers = []
    if trigger_ids:
        placeholders = ",".join("?" for _ in trigger_ids)
        triggers = conn.execute(
            f"SELECT * FROM sa_triggers WHERE id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
            trigger_ids,
        ).fetchall()

    first_trg = triggers[0] if triggers else None
    source = (first_trg["source"] if first_trg and first_trg["source"] else "").strip()
    scope = (first_trg["scope"] if first_trg and first_trg["scope"] else "default").strip()

    # v1 rule: ticket_kind is 'coding' if the trigger's Source is github, else 'personal'.
    # Recorded per card instructions; do not generalize.
    ticket_kind = "coding" if source.lower() == "github" else "personal"

    # DB-layer creation function hermes_cli.kanban_db.create_task is used matching
    # kanban_create's underlying implementation path.
    task_title = (
        (first_trg["body"] or "").strip().splitlines()[0]
        if first_trg and first_trg["body"] and first_trg["body"].strip()
        else f"Superagent ticket for decision {dec_id}"
    )
    task_body = first_trg["body"] if first_trg and first_trg["body"] else None
    if task_body:
        # Worker sign-off contract (live evidence 2026-11-04): SA cards
        # carried the bare user prompt and nothing else — no worker context
        # anywhere states the terminal-call requirement up front, and
        # workers (sa-worker profile) repeatedly did the work, narrated a
        # next step, and exited rc=0 without kanban_complete. The contract
        # rides the card body, where the worker actually reads it.
        task_body = task_body + "\n\n" + _SA_WORKER_CONTRACT

    # RECONCILED (gates x surfaces U4): Super Agent step 2 (DESIGN rev 4,
    # §3.1/§3.3): classification + assignment are PRECOMPUTED here, before
    # the U4 accept transaction opens (B-9) — no config reads inside the
    # txn.  The hook performs raw in-txn writes only, inside create_task's
    # own allow_nested txn nested in the U4 unit.  When the board
    # config disables assignment (or its section is invalid), no hook is
    # passed and creation is exactly the pre-feature behavior.  Step 1's SA
    # gate-spec seam (§2.1): a spec is passed only when the Decision basis
    # carries a valid gate_spec object, or a default template applies.
    _sa_work_class: str | None = None
    _sa_assignment_hook = None
    _sa_gate_spec = None
    # FIX-18: precompute errors surface visibly (like the CLI path) — there
    # is NO blanket swallow here, and the computed work_class is never
    # silently discarded.  (The gate-spec seam's local ValueError handling
    # below is deliberate per §2.1: an invalid basis/template spec attaches
    # no spec; classification and assignment errors are NOT in that class.)
    from hermes_cli import kanban_assignment as _ka_sa
    from hermes_cli import kanban_board_config as _kbcfg_sa

    _sa_cfg, _sa_errs = _kbcfg_sa.load_board_config()
    _sa_assign_cfg = _kbcfg_sa.section_config(_sa_cfg, "assignment")
    _sa_work_class = _ka_sa.classify_sa_work_class(row, triggers, _sa_assign_cfg)
    if _kbcfg_sa.feature_enabled(_sa_cfg, _sa_errs, "assignment"):
        _sa_plan = _ka_sa.resolve_assignment(
            _sa_work_class, board_config=_sa_cfg,
            explicit=_ka_sa.ExplicitOverrides(), assignee=None, sa_path=True,
        )

        def _sa_assignment_hook(_conn, _tid, _plan=_sa_plan):
            _ka_sa.apply_creation_assignment(_conn, _tid, _plan)

        _sa_assignment_hook = _sa_assignment_hook
    # Gate-spec seam: basis gate_spec first, then the default template.
    _sa_basis: dict = {}
    if row["reason_json"]:
        try:
            _parsed = json.loads(row["reason_json"])
            if isinstance(_parsed, dict):
                _sa_basis = _parsed
        except Exception:
            _sa_basis = {}
    from hermes_cli import kanban_gates as _kg_sa

    _sa_gates_cfg = _kbcfg_sa.section_config(_sa_cfg, "gates")
    # FIX-M1 (merge verification, DESIGN rev 4 section 5.1 / B-6): the gates
    # default timeout is resolved ONCE here, from the loader's validation
    # errors -- never by int()-coercing the raw config value.
    # load_board_config() has already validated
    # kanban.gates.default_timeout_seconds (positive int, <= 1800) and
    # logged any error at ERROR (B-6); a section error for that key means
    # the gates feature is disabled for this call. A spec-less accept must
    # not evaluate the value at all (it passes
    # gate_spec_default_timeout_seconds=None to create_task below), and a
    # spec-carrying accept must not silently drop or mis-time its gate: it
    # fails closed with a typed gates-config error BEFORE the accept txn
    # opens, so no task/mandate is created. (board_config_invalid event
    # surfacing is task-scoped -- surface_errors needs a ticket -- so a
    # refused pre-creation accept surfaces through the loader's ERROR log
    # plus this error, not a task event; see BUILD-LOG FIX-M1.)
    _sa_gates_timeout_errs = [
        e for e in _kbcfg_sa.section_errors(_sa_errs, "gates")
        if e.get("key") == "default_timeout_seconds"
    ]
    _sa_default_timeout = None
    if not _sa_gates_timeout_errs:
        _raw_timeout = _sa_gates_cfg.get("default_timeout_seconds", 300)
        _sa_default_timeout = (
            _raw_timeout
            if isinstance(_raw_timeout, int)
            and not isinstance(_raw_timeout, bool)
            else 300
        )

    def _raise_gates_timeout_invalid():
        _e = _sa_gates_timeout_errs[0]
        raise RuntimeError(
            "board_config_invalid: [gates] "
            + str(_e.get("key")) + ": " + str(_e.get("message"))
            + " (SA accept refused: the decision carries a gate_spec but"
            " the gates default timeout is invalid; no ticket was created)"
        )

    if isinstance(_sa_basis.get("gate_spec"), dict):
        if _sa_gates_timeout_errs:
            _raise_gates_timeout_invalid()
        try:
            _sa_gate_spec = _kg_sa.validate_gate_spec(
                _sa_basis["gate_spec"],
                default_timeout_seconds=_sa_default_timeout,
            )
        except ValueError:
            _sa_gate_spec = None
    if _sa_gate_spec is None and _sa_gates_cfg.get("apply_default_spec"):
        _tmpl = (_sa_gates_cfg.get("default_specs") or {}).get(_sa_work_class)
        if isinstance(_tmpl, dict) and _tmpl:
            if _sa_gates_timeout_errs:
                _raise_gates_timeout_invalid()
            try:
                _sa_gate_spec = _kg_sa.validate_gate_spec(
                    _tmpl,
                    default_timeout_seconds=_sa_default_timeout,
                )
            except ValueError:
                _sa_gate_spec = None

    # All pure computation moves ABOVE the transaction (surfaces Rev 3
    # §3/U4.1): title/body (above), the gates precompute (above) and
    # basis_json here. The in-txn boundary (section 5.1 / B-9) is: nothing
    # inside the block computes or reads kanban: feature config. One
    # pre-existing exception (L-1): create_task reads board metadata
    # (board.json, via _board_meta_for) inside the block -- that is
    # board-DB metadata, not feature config; it swallows OSError/JSON
    # errors into defaults and cannot fail this transaction.
    # Build mandate basis_json per E-013/Decision basis
    basis: dict[str, Any] = {}
    if row["reason_json"]:
        try:
            parsed_reason = json.loads(row["reason_json"])
            if isinstance(parsed_reason, dict):
                basis = parsed_reason
            else:
                basis = {"reason": parsed_reason}
        except Exception:  # health: allow BLE001 -- malformed reason JSON fallback
            basis = {}

    if first_trg:
        if "sender" not in basis and first_trg["actor_source_id"]:
            basis["sender"] = first_trg["actor_source_id"]
        if "source" not in basis and first_trg["source"]:
            basis["source"] = first_trg["source"]
        if "scope" not in basis and first_trg["scope"]:
            basis["scope"] = first_trg["scope"]

    basis["scope_defaults"] = policy.get("mandate_scope_defaults", [])

    basis_json = json.dumps(basis, sort_keys=True)

    # U4 atomic accept unit (surfaces Rev 3 §3/U4.1): ONE write_txn —
    # create_task (first write), mandate insert, task stamp, decision
    # link, outbox ack, and the owning subscribe() commit or roll back
    # together. NO idempotency_key kwarg and NO unique index (A31-3:
    # permanently rejected — mandate amendment writes further mandate
    # rows per ticket by design). The single exactly-once mechanism is
    # the in-transaction ticket_id re-check under BEGIN IMMEDIATE
    # (preconditioned by the A31-2 entry guard above); the
    # pre-transaction early-return above is a fast path only.
    already_ticket: str | None = None
    ticket_id: str | None = None
    with write_txn(conn, allow_nested=True):
        # Statement #1 inside the txn: re-read sa_decisions.ticket_id.
        existing = conn.execute(
            "SELECT ticket_id FROM sa_decisions WHERE id = ?", (dec_id,)
        ).fetchone()
        if existing is not None and existing["ticket_id"]:
            already_ticket = str(existing["ticket_id"])
        else:
            # FIX-M1: the precomputed timeout is passed ONLY when a
            # gate spec was resolved; a spec-less accept passes None
            # (create_task's not-supplied sentinel) and never evaluates
            # the gates timeout value.
            ticket_id = create_task(
                conn,
                title=task_title,
                body=task_body,
                created_by="superagent",
                gate_spec=_sa_gate_spec,
                work_class=_sa_work_class,
                assignment_hook=_sa_assignment_hook,
                gate_spec_default_timeout_seconds=(
                    _sa_default_timeout if _sa_gate_spec is not None else None
                ),
            )

            # Create sa_mandates row
            cur = conn.execute(
                """
                INSERT INTO sa_mandates (
                    ticket_id, policy_version, scope, strength,
                    basis_json, status, amended_from, created_at
                ) VALUES (?, ?, ?, ?, ?, 'active', NULL, ?)
                """,
                (
                    ticket_id,
                    row["policy_version"],
                    scope,
                    row["strength"] or "explicit",
                    basis_json,
                    int(now),
                ),
            )
            mandate_id = cur.lastrowid

            # Stamp tasks.mandate_id and tasks.ticket_kind (+ work_class,
            # gates §3.1: stamped in the same U4 txn; ticket_kind is never
            # touched by assignment — the two axes are independent, and the
            # work_class value came from the precompute, not an in-txn read).
            conn.execute(
                "UPDATE tasks SET mandate_id = ?, ticket_kind = ?, work_class = ? WHERE id = ?",
                (mandate_id, ticket_kind, _sa_work_class, ticket_id),
            )

            # Link ticket_id on the Decision row
            conn.execute(
                "UPDATE sa_decisions SET ticket_id = ? WHERE id = ?",
                (ticket_id, dec_id),
            )

            # Write acknowledgement request via superagent_outbox.queue_reply
            # in the same transaction as ticket/mandate state changes (E-010).
            address_dict = {
                "platform": source,
                "chat_id": scope,
                "thread_key": row["thread_key"],
                "actor_source_id": first_trg["actor_source_id"] if first_trg else None,
                "scope": scope,
            }
            from hermes_cli.superagent_outbox import queue_reply

            queue_reply(
                conn,
                ticket_id=ticket_id,
                source=source,
                address=address_dict,
                purpose="acknowledgement",
                body=f"Acknowledged ticket {ticket_id} for decision {dec_id}",
                now=now,
            )

            # Subscribe-at-accept (U4): the owning subscription in the
            # SAME transaction, after queue_reply. Not swallowed: a
            # ticket without its subscription is the bug being fixed —
            # any throw rolls the whole unit back and the next tick
            # retries cleanly.
            from hermes_cli.superagent_subscriptions import subscribe

            subscribe(
                conn,
                ticket_id=ticket_id,
                thread_key=row["thread_key"],
                context_only=False,
                now=int(now),
            )

    if already_ticket is not None:
        return already_ticket
    # FIX-4 (post-commit): the assignment consumer surfaces ONLY its own
    # section's errors. surface_errors opens its own write txn, so this
    # runs after the U4 accept unit has committed, never inside it.
    _kbcfg_sa.surface_if_exercised(conn, ticket_id, _sa_cfg, _sa_errs, "assignment")
    return ticket_id


def accept(
    db_path_or_conn: Path | str | sqlite3.Connection,
    decision_id: int | str,
    now: float,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Accept an admitted Decision and instantiate its kanban task and mandate per E-013.

    For a Decision with outcome 'accept':
      1. In observe mode (superagent.mode == 'observe' / accept_mode == 'observe'):
         records would_accept = 1 on the Decision and creates NOTHING (returns None).
      2. In active mode:
         - Creates the kanban task via hermes_cli.kanban_db.create_task (the SAME DB-layer
           creation function that kanban_create uses; recorded per card instructions).
         - Inserts an sa_mandates row with ticket_id, policy_version, scope, strength,
           basis_json (recording sender, source, scope), and status 'active'.
         - Stamps tasks.mandate_id = mandate id and tasks.ticket_kind: 'coding' if
           the trigger's source is github, else 'personal' (v1 rule — do not generalize).
         - Stamps sa_decisions.ticket_id = ticket_id.
         - Writes an acknowledgement request into sa_outbox via direct INSERT
           (interim path until Lane C superagent_outbox integration).
         - Returns ticket_id.
    """
    if isinstance(db_path_or_conn, (str, Path)):
        with connect_closing(Path(db_path_or_conn)) as conn:
            return _accept_on_conn(conn, decision_id=decision_id, now=now, config=config)

    return _accept_on_conn(db_path_or_conn, decision_id=decision_id, now=now, config=config)


def _amend_mandate_on_conn(
    conn: sqlite3.Connection,
    ticket_id: str,
    added_scope: str,
    approver: str,
    now: float,
    config: dict[str, Any] | None = None,
) -> int:
    """Execute mandate amendment on an open database connection."""
    task = conn.execute(
        "SELECT id, ticket_kind, mandate_id FROM tasks WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if task is None:
        raise ValueError(f"Ticket {ticket_id} not found")

    if task["ticket_kind"] != "personal":
        raise ValueError(
            f"Cannot amend mandate: ticket {ticket_id} has ticket_kind '{task['ticket_kind']}', "
            f"only 'personal' tickets may be amended (D-1)"
        )

    mandate = conn.execute(
        "SELECT * FROM sa_mandates WHERE ticket_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    if mandate is None:
        raise ValueError(f"No active mandate found for ticket {ticket_id}")

    try:
        basis = json.loads(mandate["basis_json"] or "{}")
        if not isinstance(basis, dict):
            basis = {}
    except Exception:  # health: allow BLE001 -- malformed basis fallback
        basis = {}

    sender = basis.get("sender")
    if not sender:
        raise ValueError(f"Cannot amend mandate: basis has no sender (mandate {mandate['id']})")

    source = basis.get("source")

    # Resolve owner identities from config
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config()
        except Exception:  # health: allow BLE001 -- fallback to empty dict if load_config fails
            config = {}
    elif not isinstance(config, dict):
        config = {}

    owner_identities = _resolve_owner_identities(config, source)
    if not owner_identities:
        raise ValueError(
            "Cannot amend mandate: superagent.owner_identities is empty or not configured (D-1)"
        )

    if sender not in owner_identities:
        raise ValueError(
            f"Cannot amend mandate: basis sender '{sender}' is not a configured owner identity (D-1)"
        )

    if approver not in owner_identities or approver != sender:
        raise ValueError(
            f"Cannot amend mandate: approver '{approver}' does not match owner identity '{sender}' (D-1)"
        )

    old_mandate_id = mandate["id"]
    old_scope = mandate["scope"]
    new_scope = old_scope + added_scope

    new_basis = dict(basis)
    new_basis["old_scope"] = old_scope
    new_basis["added_scope"] = added_scope
    new_basis["scope"] = new_scope
    new_basis["approver"] = approver

    with write_txn(conn, allow_nested=True):
        conn.execute(
            "UPDATE sa_mandates SET status = 'superseded' WHERE id = ?",
            (old_mandate_id,),
        )
        cur = conn.execute(
            """
            INSERT INTO sa_mandates (
                ticket_id, policy_version, scope, strength,
                basis_json, status, amended_from, created_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                ticket_id,
                mandate["policy_version"],
                new_scope,
                mandate["strength"],
                json.dumps(new_basis, sort_keys=True),
                old_mandate_id,
                int(now),
            ),
        )
        new_mandate_id = cur.lastrowid

        conn.execute(
            "UPDATE tasks SET mandate_id = ? WHERE id = ?",
            (new_mandate_id, ticket_id),
        )

    return new_mandate_id


def amend_mandate(
    db_path_or_conn: Path | str | sqlite3.Connection,
    ticket_id: str,
    added_scope: str,
    approver: str,
    now: float,
    config: dict[str, Any] | None = None,
) -> int:
    """Amend an existing personal ticket's active mandate per D-1.

    REFUSES (raises ValueError) unless ALL hold:
      1. The ticket's ticket_kind == 'personal'
      2. The mandate's basis sender is the owner identity for that source
         per config superagent.owner_identities (may be empty -> refuse)
      3. approver matches that owner identity.

    On success: inserts a NEW mandate row with amended_from = old mandate id,
    scope = old scope + added_scope (recording both in basis_json), marks
    the old row status 'superseded', and updates tasks.mandate_id.
    Returns new_mandate_id.
    """
    if isinstance(db_path_or_conn, (str, Path)):
        with connect_closing(Path(db_path_or_conn)) as conn:
            return _amend_mandate_on_conn(
                conn,
                ticket_id=ticket_id,
                added_scope=added_scope,
                approver=approver,
                now=now,
                config=config,
            )

    return _amend_mandate_on_conn(
        db_path_or_conn,
        ticket_id=ticket_id,
        added_scope=added_scope,
        approver=approver,
        now=now,
        config=config,
    )


def _rule_matches_policy(req: Any, policy_rules: list[Any]) -> bool:
    """Check if a required rule is still present in policy rules."""
    if not policy_rules:
        return False
    if req in policy_rules:
        return True
    req_id = req.get("id") if isinstance(req, dict) else str(req)
    req_name = req.get("name") if isinstance(req, dict) else None
    for pr in policy_rules:
        if isinstance(pr, dict):
            if req_id and pr.get("id") == req_id:
                return True
            if req_name and pr.get("name") == req_name:
                return True
        elif str(pr) == req_id:
            return True
    return False


def _check_inferred_rules(
    conn: sqlite3.Connection,
    ticket_id: str | None,
    basis: dict[str, Any],
    policy_rules: list[Any],
) -> tuple[bool, str]:
    """Check if rules required by mandate basis or decision are satisfied in policy_rules."""
    req_rules: list[Any] = []
    for k in ("matched_rules", "rules"):
        val = basis.get(k)
        if isinstance(val, list):
            req_rules.extend(val)
        elif isinstance(val, (str, dict)):
            req_rules.append(val)
    for k in ("rule", "scope_rule"):
        val = basis.get(k)
        if val:
            req_rules.append(val)

    if not req_rules and ticket_id:
        dec_row = conn.execute(
            "SELECT matched_rules_json FROM sa_decisions WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if dec_row and dec_row["matched_rules_json"]:
            try:
                parsed_rules = json.loads(dec_row["matched_rules_json"])
                if isinstance(parsed_rules, list):
                    req_rules.extend(parsed_rules)
            except Exception:  # health: allow BLE001 -- malformed decision rules fallback
                req_rules = []

    if req_rules:
        for req in req_rules:
            if not _rule_matches_policy(req, policy_rules):
                rule_desc = req.get("id") if isinstance(req, dict) else str(req)
                return True, f"scope rule removed: '{rule_desc}'"

    return False, ""


def _check_admission_basis(
    source: str,
    scope: str,
    sender: str | None,
    orig_strength: str,
    basis: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[bool, str]:
    """Check if admission-based mandate holds today's computed strength."""
    from hermes_cli.superagent_gate import admit

    synth_trigger = {
        "source": source,
        "scope": scope,
        "actor_source_id": sender,
        "actor_is_bot": False,
        "addressed": bool(basis.get("addressed", False)),
        "kind": basis.get("kind", "message"),
    }
    today_strength, _ = admit(synth_trigger, policy)
    strength_rank = {"explicit": 2, "inferred": 1, "not_admitted": 0}
    today_rank = strength_rank.get(today_strength, 0)
    orig_rank = strength_rank.get(orig_strength, 1)
    if today_rank < orig_rank:
        if today_strength == "not_admitted":
            return True, f"sender '{sender}' no longer listed in allowlist"
        return True, f"strength downgrade from {orig_strength} to {today_strength}"
    return False, ""


def _check_inferred_basis(
    conn: sqlite3.Connection,
    ticket_id: str | None,
    source: str,
    scope: str,
    sender: str | None,
    orig_strength: str,
    basis: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[bool, str]:
    """Check if inferred mandate remains admitted under auto mode with matching rules."""
    mode = str(policy.get("accept_mode") or policy.get("mode") or "observe").strip().lower()
    if mode != "auto":
        return True, f"policy mode changed to '{mode}', inferred mandate requires 'auto'"

    from hermes_cli.superagent_gate import admit

    synth_trigger = {
        "source": source,
        "scope": scope,
        "actor_source_id": sender,
        "actor_is_bot": bool(basis.get("actor_is_bot", False)),
        "addressed": bool(basis.get("addressed", False)),
        "kind": basis.get("kind", "message"),
    }
    today_strength, _ = admit(synth_trigger, policy)
    if today_strength == "not_admitted":
        return True, f"sender '{sender}' no longer listed in allowlist"

    strength_rank = {"explicit": 2, "inferred": 1, "not_admitted": 0}
    today_rank = strength_rank.get(today_strength, 0)
    orig_rank = strength_rank.get(orig_strength, 1)
    if today_rank < orig_rank:
        return True, f"strength downgrade from {orig_strength} to {today_strength}"

    return _check_inferred_rules(conn, ticket_id, basis, policy.get("rules", []))


def _check_mandate_affected(
    conn: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    policy: dict[str, Any],
) -> tuple[bool, str]:
    """Determine whether a mandate's basis evaluates differently under new_snapshot.

    Per SPEC-V2 §2.5, a live ticket (task not in terminal status) is AFFECTED if its
    mandate's basis would evaluate differently under the new snapshot:
    1. Strength downgrade: today's computed strength for the basis is lower than the
       mandate's original strength (e.g. rank dropped from explicit/inferred to not_admitted).
    2. Sender no longer listed: the sender in basis is no longer listed in the source
       allowlist or bot allowlist.
    3. Scope rule removed: any scope rule or matched rule associated with the mandate
       (in basis['matched_rules'], basis['rules'], basis['rule'], basis['scope_rule'],
       or decision['matched_rules_json']) is no longer present in new_snapshot['rules'].
    4. Mode not auto: an inferred mandate requires policy accept_mode == 'auto'.

    Explicit and followup mandates are unaffected.
    Affected tickets receive an sa_inputs row kind 'policy_change' with body summarizing
    what changed. Unaffected tickets receive no input. Live tasks are never cancelled.

    Returns:
        (is_affected, reason_summary)
    """
    orig_strength = str(row["strength"] or "").strip().lower()
    try:
        basis = json.loads(row["basis_json"] or "{}")
        if not isinstance(basis, dict):
            basis = {}
    except Exception:  # health: allow BLE001 -- malformed basis fallback
        basis = {}

    scope = str(basis.get("scope") or row["scope"] or "").strip()
    source = str(basis.get("source") or "").strip()
    sender = basis.get("sender")
    if sender is not None:
        sender = str(sender).strip()
    via = str(basis.get("via") or "").strip().lower()

    # 1. Bot basis -> True only if the sender is still listed
    if via == "bot" or basis.get("is_bot") is True or bool(basis.get("actor_is_bot", False)):
        from hermes_cli.superagent_gate import _extract_source_bots

        bot_list = _extract_source_bots(policy, source)
        if not sender or sender not in bot_list:
            return True, f"sender '{sender}' no longer listed in bot allowlist"
        return False, ""

    # 2. Admission-based (via 'admission') -> True only if today's computed strength >= original strength
    if via == "admission":
        return _check_admission_basis(source, scope, sender, orig_strength, basis, policy)

    # 3. Explicit + followup basis -> True (unaffected)
    if (
        orig_strength == "explicit"
        or via in ("explicit", "followup", "follow_up")
        or bool(basis.get("followup", False))
        or basis.get("type") in ("explicit", "followup")
        or basis.get("kind") in ("explicit", "followup")
    ):
        return False, ""

    # 4. Inferred -> True only if current policy still admits basis sender under mode 'auto' with matching rules
    ticket_id = row["ticket_id"] if ("ticket_id" in row.keys() and row["ticket_id"]) else None
    return _check_inferred_basis(
        conn, ticket_id, source, scope, sender, orig_strength, basis, policy
    )


def _revalidate_on_conn(
    conn: sqlite3.Connection,
    mandate_id: int | str,
    now: float | None = None,
    *,
    config: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> bool:
    """Execute mandate revalidation on an open database connection."""
    row = conn.execute(
        "SELECT * FROM sa_mandates WHERE id = ?",
        (int(mandate_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"Mandate {mandate_id} not found")

    if str(row["status"] or "").strip().lower() != "active":
        return False

    if policy is None:
        policy = policy_snapshot(config)

    is_affected, _ = _check_mandate_affected(conn, row, policy)
    return not is_affected


def _reopen_ended_ticket_on_conn(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Reopen an ended ticket to ready status so the dispatcher path claims it with a new task_runs row."""
    cur = conn.execute(
        """
        UPDATE tasks
        SET status = 'ready',
            current_run_id = NULL,
            claim_lock = NULL,
            claim_expires = NULL,
            worker_pid = NULL,
            worker_started_at = NULL,
            completed_at = NULL,
            consecutive_failures = 0,
            last_failure_error = NULL
        WHERE id = ?
        """,
        (ticket_id,),
    )
    if cur.rowcount > 0:
        try:
            from hermes_cli import kanban_db as _kb
            _kb._append_event(
                conn, ticket_id, "promoted",
                {"reason": "resume", "status": "ready"}
            )
        except Exception as exc:  # health: allow BLE001 -- event append failure boundary
            logger.debug("reopen_ended_ticket append_event failed: %s", exc)
        return True
    return False


def reopen_ended_ticket(
    db_path_or_conn: Path | str | sqlite3.Connection,
    ticket_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Reopen an ended ticket to ready status so the dispatcher path claims it with a new task_runs row."""
    if isinstance(db_path_or_conn, (str, Path)):
        with connect_closing(Path(db_path_or_conn)) as conn:
            with write_txn(conn):
                return _reopen_ended_ticket_on_conn(conn, ticket_id, now=now)
    return _reopen_ended_ticket_on_conn(db_path_or_conn, ticket_id, now=now)


reopen_ticket = reopen_ended_ticket


def revalidate(
    db_path_or_conn: Path | str | sqlite3.Connection,
    mandate_id: int | str,
    now: float | None = None,
    *,
    config: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> bool:
    """Revalidate an active mandate against current policy per SPEC-V2 §2.5 matrix.

    The v2 matrix:
      - explicit + followup basis -> True
      - inferred -> True only if current policy still admits basis sender under mode 'auto'
        with matching rules
      - admission-based (via 'admission') -> True only if today's computed strength
        for the basis >= the mandate's original strength
      - bot basis -> True only if sender is still listed in source bots

    Implements against current policy snapshot (policy_snapshot()) and pure gate
    (superagent_gate.admit). Reuses existing primitives, does not re-derive.
    """
    if isinstance(db_path_or_conn, (str, Path)):
        with connect_closing(Path(db_path_or_conn)) as conn:
            return _revalidate_on_conn(
                conn, mandate_id=mandate_id, now=now, config=config, policy=policy
            )

    return _revalidate_on_conn(
        db_path_or_conn, mandate_id=mandate_id, now=now, config=config, policy=policy
    )


def _policy_changed_on_conn(
    conn: sqlite3.Connection,
    old_version: str | None,
    new_snapshot: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    """Execute policy change processing on an open database connection."""
    new_version = policy_version(new_snapshot)
    int_now = int(now)

    with write_txn(conn, allow_nested=True):
        # (a) Running Evals requeue: triggers back to 'queued', eval_requeues unchanged
        cur = conn.execute(
            "UPDATE sa_triggers SET eval_state = 'queued' WHERE eval_state IN ('pending', 'running')"
        )
        requeued_evals = cur.rowcount

        # (b) For each LIVE ticket (task not in terminal status) whose mandate's policy_version != new_version:
        rows = conn.execute(
            """
            SELECT t.id AS ticket_id, t.status AS task_status,
                   m.id AS mandate_id, m.policy_version, m.scope, m.strength, m.basis_json, m.status AS mandate_status
            FROM tasks t
            JOIN sa_mandates m ON m.id = COALESCE(
                t.mandate_id,
                (SELECT id FROM sa_mandates WHERE ticket_id = t.id AND status = 'active' ORDER BY id DESC LIMIT 1)
            )
            WHERE t.status NOT IN ('done', 'archived', 'failed', 'cancelled')
              AND m.status = 'active'
              AND m.policy_version != ?
            ORDER BY t.id ASC
            """,
            (new_version,),
        ).fetchall()

        affected_tickets: list[str] = []
        unaffected_tickets: list[str] = []

        for r in rows:
            tid = str(r["ticket_id"])
            mid = r["mandate_id"]
            is_affected, reason = _check_mandate_affected(conn, r, new_snapshot)

            if is_affected:
                affected_tickets.append(tid)
                body_text = f"Policy change ({new_version}): {reason}" if reason else f"Policy change ({new_version})"
                conn.execute(
                    """
                    INSERT INTO sa_inputs (
                        ticket_id, trigger_id, kind, body,
                        is_person, context_only, read_at, created_at
                    ) VALUES (?, NULL, 'policy_change', ?, 0, 0, NULL, ?)
                    """,
                    (tid, body_text, int_now),
                )
            else:
                unaffected_tickets.append(tid)

            conn.execute(
                "UPDATE sa_mandates SET policy_version = ? WHERE id = ?",
                (new_version, mid),
            )

    return {
        "old_version": str(old_version or ""),
        "new_version": new_version,
        "requeued_evals": requeued_evals,
        "affected_tickets": affected_tickets,
        "unaffected_tickets": unaffected_tickets,
    }


def policy_changed(
    db_path_or_conn: Path | str | sqlite3.Connection,
    old_version: str | None,
    new_snapshot: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    """Process policy update across running Evals and live tickets per SPEC-V2 §2.5.

    (a) Running Evals requeue: their triggers' eval_state is set back to 'queued'
        with eval_requeues unchanged.
    (b) For each LIVE ticket (task not in terminal status) whose mandate's policy_version != new_version:
        determines affected per the v2 rules:
        - A ticket is AFFECTED if its mandate's basis would evaluate differently under the new
          snapshot (strength downgrade, sender no longer listed, scope rule removed, or mode
          no longer 'auto' for inferred mandates).
        - Affected tickets get an sa_inputs row with kind 'policy_change' and body summarizing
          what changed.
        - Unaffected tickets get NOTHING.
        - A policy change NEVER cancels a live ticket (no task status writes).

    Returns:
        dict summary with keys:
          - old_version: str
          - new_version: str
          - requeued_evals: int
          - affected_tickets: list[str]
          - unaffected_tickets: list[str]
    """
    current_now = time.time() if now is None else float(now)

    if isinstance(db_path_or_conn, (str, Path)):
        with connect_closing(Path(db_path_or_conn)) as conn:
            return _policy_changed_on_conn(
                conn, old_version=old_version, new_snapshot=new_snapshot, now=current_now
            )

    return _policy_changed_on_conn(
        db_path_or_conn, old_version=old_version, new_snapshot=new_snapshot, now=current_now
    )


# --- Release-on-End Engine (Card 3.7) ---

def _is_input_acknowledged(
    conn: sqlite3.Connection,
    ticket_id: str,
    read_at: int | float | None,
) -> bool:
    """Return True if the run acknowledged the input (commented or reported after delivery).

    Acknowledged = the run commented or reported after delivery (read_at):
    - Comment: a comment exists on the ticket with created_at >= read_at
      and author != 'dispatcher:inbound'.
    - Report: a completion/report exists on the ticket with created_at >= read_at
      (e.g. task_events kind IN ('completed', 'review_requested', 'report', 'pr_reported'),
      tasks completed_at >= read_at, or task_runs with ended_at >= read_at and summary/outcome).
    """
    if read_at is None:
        return False

    read_ts = int(read_at)

    # 1. Check for run comment after delivery
    try:
        has_task_comments = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_comments'"
        ).fetchone()
        if has_task_comments:
            comment_row = conn.execute(
                """
                SELECT 1 FROM task_comments
                WHERE task_id = ?
                  AND created_at >= ?
                  AND author != 'dispatcher:inbound'
                LIMIT 1
                """,
                (str(ticket_id), read_ts),
            ).fetchone()
            if comment_row is not None:
                return True
    except Exception as exc:  # health: allow BLE001 -- table check boundary
        logger.debug("_is_input_acknowledged task_comments check: %s", exc)

    # Check task_events for 'commented' by non-dispatcher
    try:
        events = conn.execute(
            """
            SELECT payload FROM task_events
            WHERE task_id = ?
              AND kind = 'commented'
              AND created_at >= ?
            """,
            (str(ticket_id), read_ts),
        ).fetchall()
        for ev in events:
            payload_str = ev[0] if isinstance(ev, (tuple, list)) else ev["payload"]
            if payload_str:
                try:
                    payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
                    if isinstance(payload, dict) and payload.get("author") != "dispatcher:inbound":
                        return True
                except Exception:
                    pass
    except Exception as exc:  # health: allow BLE001 -- event check boundary
        logger.debug("_is_input_acknowledged task_events commented check: %s", exc)

    # 2. Check for run report / completion after delivery
    try:
        report_ev = conn.execute(
            """
            SELECT 1 FROM task_events
            WHERE task_id = ?
              AND kind IN ('completed', 'review_requested', 'report', 'pr_reported')
              AND created_at >= ?
            LIMIT 1
            """,
            (str(ticket_id), read_ts),
        ).fetchone()
        if report_ev is not None:
            return True
    except Exception as exc:  # health: allow BLE001 -- event check boundary
        logger.debug("_is_input_acknowledged task_events report check: %s", exc)

    try:
        run_row = conn.execute(
            """
            SELECT 1 FROM task_runs
            WHERE task_id = ?
              AND ended_at >= ?
              AND (summary IS NOT NULL OR outcome IS NOT NULL)
            LIMIT 1
            """,
            (str(ticket_id), read_ts),
        ).fetchone()
        if run_row is not None:
            return True
    except Exception as exc:  # health: allow BLE001 -- task_runs check boundary
        logger.debug("_is_input_acknowledged task_runs check: %s", exc)

    try:
        task_row = conn.execute(
            "SELECT completed_at, result FROM tasks WHERE id = ?",
            (str(ticket_id),),
        ).fetchone()
        if task_row is not None:
            completed_at = task_row[0] if isinstance(task_row, (tuple, list)) else task_row["completed_at"]
            if completed_at is not None and int(completed_at) >= read_ts:
                return True
    except Exception as exc:  # health: allow BLE001 -- tasks check boundary
        logger.debug("_is_input_acknowledged tasks check: %s", exc)

    return False


def _release_on_end_on_conn(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    now: float | None = None,
) -> list[str]:
    """Release unread, unacknowledged person inputs when a ticket ends per Card 3.7.

    When a Ticket ends, its unread, unacknowledged person Inputs (not handed back,
    not answered -- acknowledged = the run commented/reported after delivery;
    use read_at + a run-activity check mirroring the phase SCOPE rule) are re-queued
    for Eval unless another Ticket owns them.
    """
    current_now = time.time() if now is None else float(now)

    has_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('sa_inputs', 'sa_triggers')"
    ).fetchone()[0]
    if has_tables < 2:
        return []

    with write_txn(conn, allow_nested=True):
        input_rows = conn.execute(
            """
            SELECT id, trigger_id, is_person, read_at
            FROM sa_inputs
            WHERE ticket_id = ?
              AND is_person = 1
              AND trigger_id IS NOT NULL
            ORDER BY created_at ASC, id ASC
            """,
            (str(ticket_id),),
        ).fetchall()

        requeued_triggers: list[str] = []

        for inp in input_rows:
            trg_id = str(inp["trigger_id"] if isinstance(inp, sqlite3.Row) else inp[1])
            read_at = inp["read_at"] if isinstance(inp, sqlite3.Row) else inp[3]

            trg = conn.execute(
                "SELECT id, thread_key, eval_state, disposition, meta_json FROM sa_triggers WHERE id = ?",
                (trg_id,),
            ).fetchone()
            if trg is None:
                continue

            # If already queued for Eval, nothing to re-queue
            if str(trg["eval_state"]) == "queued":
                continue

            meta = json.loads(trg["meta_json"]) if trg["meta_json"] else {}
            if not isinstance(meta, dict):
                meta = {}

            handbacks = meta.get("handbacks", [])
            if not isinstance(handbacks, list):
                handbacks = []

            # 1. Not handed back: if this ticket already handed it back, skip
            handed_back_by_this = any(
                isinstance(hb, dict) and str(hb.get("ticket_id")) == str(ticket_id)
                for hb in handbacks
            )
            if handed_back_by_this:
                continue

            # 2. Not answered: acknowledged = the run commented/reported after delivery
            if read_at is not None:
                if _is_input_acknowledged(conn, str(ticket_id), read_at):
                    continue

            # 3. Re-queued for Eval unless another Ticket owns them
            thread_key = str(trg["thread_key"])
            owning_sub_rows = conn.execute(
                "SELECT ticket_id FROM sa_subscriptions WHERE thread_key = ? AND context_only = 0",
                (thread_key,),
            ).fetchall()
            owning_ticket_ids = {
                str(r[0] if isinstance(r, tuple) else r["ticket_id"])
                for r in owning_sub_rows
            }

            input_rows_for_trg = conn.execute(
                "SELECT ticket_id FROM sa_inputs WHERE trigger_id = ? AND context_only = 0",
                (trg_id,),
            ).fetchall()
            for r in input_rows_for_trg:
                owning_ticket_ids.add(str(r[0] if isinstance(r, tuple) else r["ticket_id"]))

            handed_back_ticket_ids = {
                str(hb.get("ticket_id"))
                for hb in handbacks
                if isinstance(hb, dict) and "ticket_id" in hb
            }

            candidate_owners = (owning_ticket_ids - {str(ticket_id)}) - handed_back_ticket_ids

            has_other_owner = False
            for other_tid in candidate_owners:
                trow = conn.execute(
                    "SELECT status, mandate_id FROM tasks WHERE id = ?",
                    (other_tid,),
                ).fetchone()
                if trow is None:
                    continue
                other_status = trow["status"] if isinstance(trow, sqlite3.Row) else trow[0]
                if other_status in ("done", "archived", "failed", "cancelled"):
                    continue

                # Also check if person-stopped (mandate withdrawn)
                stopped = conn.execute(
                    """
                    SELECT 1 FROM sa_mandates m
                    WHERE (m.ticket_id = ? OR m.id = (SELECT mandate_id FROM tasks WHERE id = ?))
                      AND m.status = 'withdrawn'
                    LIMIT 1
                    """,
                    (other_tid, other_tid),
                ).fetchone()
                if stopped is not None:
                    continue

                has_other_owner = True
                break

            if has_other_owner:
                continue

            # Re-queue for Eval
            if trg_id not in requeued_triggers:
                releases = meta.get("releases", [])
                if not isinstance(releases, list):
                    releases = []
                releases.append({
                    "ticket_id": str(ticket_id),
                    "reason": "release_on_end",
                    "released_at": int(current_now),
                })
                meta["releases"] = releases
                new_meta_json = json.dumps(meta)

                conn.execute(
                    """
                    UPDATE sa_triggers
                    SET eval_state = 'queued',
                        disposition = NULL,
                        meta_json = ?
                    WHERE id = ?
                    """,
                    (new_meta_json, trg_id),
                )
                requeued_triggers.append(trg_id)

        return requeued_triggers


def release_on_end(
    db_path_or_conn: Path | str | sqlite3.Connection,
    ticket_id: str,
    *,
    now: float | None = None,
) -> list[str]:
    """Release unread, unacknowledged person inputs when a ticket ends per Card 3.7.

    Re-queues their triggers for Eval unless another Ticket owns them.
    Returns list of re-queued trigger IDs.
    """
    if isinstance(db_path_or_conn, (str, Path)):
        with connect_closing(Path(db_path_or_conn)) as conn:
            return _release_on_end_on_conn(conn, ticket_id, now=now)
    return _release_on_end_on_conn(db_path_or_conn, ticket_id, now=now)


def _stop_ticket_on_conn(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    reason: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | str:
    """Stop a ticket with person-stopped semantics per Card INT-3.

    Transitions active mandate to 'withdrawn', ends the ticket via the
    follows-done/release_on_end end path, and releases unread person inputs.
    Idempotent: stopping an already-stopped or already-ended ticket returns
    a clear result with no duplicate side effects.
    Unknown ticket returns an error string.
    """
    task = conn.execute(
        "SELECT id, status, mandate_id FROM tasks WHERE id = ?",
        (str(ticket_id),),
    ).fetchone()
    if task is None:
        return f"Ticket '{ticket_id}' not found"

    current_now = int(time.time() if now is None else now)
    task_status = task["status"] if isinstance(task, sqlite3.Row) else task[1]
    is_ended = task_status in ("done", "archived", "failed", "cancelled")

    # Check if mandate is already withdrawn (person-stopped)
    mandate_stopped = conn.execute(
        """
        SELECT id, status FROM sa_mandates
        WHERE (ticket_id = ? OR id = (SELECT mandate_id FROM tasks WHERE id = ?))
          AND status = 'withdrawn'
        LIMIT 1
        """,
        (str(ticket_id), str(ticket_id)),
    ).fetchone()

    if mandate_stopped is not None:
        mid = mandate_stopped["id"] if isinstance(mandate_stopped, sqlite3.Row) else mandate_stopped[0]
        return {
            "ok": True,
            "ticket_id": str(ticket_id),
            "status": "already_stopped",
            "already_stopped": True,
            "already_ended": is_ended,
            "mandate_id": mid,
            "released_inputs": [],
        }

    # Find active mandate
    active_mandate = conn.execute(
        """
        SELECT id, basis_json FROM sa_mandates
        WHERE (ticket_id = ? OR id = (SELECT mandate_id FROM tasks WHERE id = ?))
          AND status = 'active'
        ORDER BY id DESC LIMIT 1
        """,
        (str(ticket_id), str(ticket_id)),
    ).fetchone()
    mid = (active_mandate["id"] if isinstance(active_mandate, sqlite3.Row) else active_mandate[0]) if active_mandate else None

    # If ticket was already ended and has no active mandate to withdraw
    if is_ended and active_mandate is None:
        return {
            "ok": True,
            "ticket_id": str(ticket_id),
            "status": "already_ended",
            "already_stopped": False,
            "already_ended": True,
            "mandate_id": None,
            "released_inputs": [],
        }

    with write_txn(conn, allow_nested=True):
        # 1. Withdraw active mandate(s)
        if active_mandate is not None:
            try:
                basis = json.loads(active_mandate["basis_json"] or "{}")
                if not isinstance(basis, dict):
                    basis = {}
            except Exception:  # health: allow BLE001 -- malformed basis fallback
                basis = {}
            basis["stopped_reason"] = reason or "person_stopped"
            basis["stopped_at"] = current_now
            new_basis_json = json.dumps(basis, sort_keys=True)
            conn.execute(
                """
                UPDATE sa_mandates
                SET status = 'withdrawn',
                    basis_json = ?
                WHERE id = ?
                """,
                (new_basis_json, mid),
            )
        conn.execute(
            """
            UPDATE sa_mandates
            SET status = 'withdrawn'
            WHERE (ticket_id = ? OR id = (SELECT mandate_id FROM tasks WHERE id = ?))
              AND status = 'active'
            """,
            (str(ticket_id), str(ticket_id)),
        )

        # 2. Record stop event in task_events
        try:
            from hermes_cli import kanban_db as _kb
            _kb._append_event(
                conn,
                str(ticket_id),
                "stopped",
                {
                    "reason": reason or "person_stopped",
                    "status": "withdrawn",
                    "mandate_id": mid,
                },
            )
        except Exception as exc:  # health: allow BLE001 -- event append failure boundary
            logger.debug("stop_ticket append_event failed: %s", exc)

        # 3. If ticket was already ended, do NOT run end-path again (prevent duplicate side effects)
        if is_ended:
            return {
                "ok": True,
                "ticket_id": str(ticket_id),
                "status": "stopped",
                "already_stopped": False,
                "already_ended": True,
                "mandate_id": mid,
                "released_inputs": [],
            }

        # 4. End the ticket through the follows-done / release_on_end end path
        from hermes_cli.superagent_pr import end_ticket_follows_done
        requeued = end_ticket_follows_done(
            conn,
            str(ticket_id),
            outcome="stopped",
            now=current_now,
            reason=reason or "person_stopped",
        )

        return {
            "ok": True,
            "ticket_id": str(ticket_id),
            "status": "stopped",
            "already_stopped": False,
            "already_ended": False,
            "mandate_id": mid,
            "released_inputs": requeued or [],
        }


def stop_ticket(
    db_path_or_conn_or_ticket_id: Path | str | sqlite3.Connection | None = None,
    ticket_id_or_reason: str | None = None,
    reason: str | None = None,
    *,
    ticket_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any] | str:
    """Stop a ticket with person-stopped semantics per Card INT-3.

    Transitions active mandate to 'withdrawn', ends the ticket via the
    follows-done/release_on_end end path, and releases unread person inputs.
    Idempotent: stopping an already-stopped or already-ended ticket returns
    a clear result with no duplicate side effects.
    Unknown ticket returns an error string.
    """
    if ticket_id is not None:
        actual_tid = ticket_id
        actual_reason = reason if reason is not None else ticket_id_or_reason
        conn_or_path = db_path_or_conn_or_ticket_id
    elif isinstance(db_path_or_conn_or_ticket_id, sqlite3.Connection):
        return _stop_ticket_on_conn(
            db_path_or_conn_or_ticket_id,
            str(ticket_id_or_reason),
            reason=reason,
            now=now,
        )
    elif isinstance(db_path_or_conn_or_ticket_id, Path):
        with connect_closing(db_path_or_conn_or_ticket_id) as conn:
            return _stop_ticket_on_conn(
                conn,
                str(ticket_id_or_reason),
                reason=reason,
                now=now,
            )
    elif isinstance(db_path_or_conn_or_ticket_id, str):
        if ticket_id_or_reason is not None and (
            db_path_or_conn_or_ticket_id.endswith(".db")
            or "/" in db_path_or_conn_or_ticket_id
            or "\\" in db_path_or_conn_or_ticket_id
        ):
            with connect_closing(Path(db_path_or_conn_or_ticket_id)) as conn:
                return _stop_ticket_on_conn(
                    conn,
                    str(ticket_id_or_reason),
                    reason=reason,
                    now=now,
                )
        else:
            actual_tid = db_path_or_conn_or_ticket_id
            actual_reason = reason if reason is not None else ticket_id_or_reason
            conn_or_path = None
    else:
        raise ValueError("stop_ticket requires a ticket_id")

    if conn_or_path is None:
        with connect_closing() as conn:
            return _stop_ticket_on_conn(conn, actual_tid, reason=actual_reason, now=now)
    elif isinstance(conn_or_path, sqlite3.Connection):
        return _stop_ticket_on_conn(conn_or_path, actual_tid, reason=actual_reason, now=now)
    else:
        with connect_closing(Path(conn_or_path)) as conn:
            return _stop_ticket_on_conn(conn, actual_tid, reason=actual_reason, now=now)


# --- Re-exports from superagent_delivery (Card 3.3) ---
from hermes_cli.superagent_delivery import (
    BODY_TRUNCATE_LIMIT,
    deliver_inputs,
    fetch_unread_inputs,
    get_ticket_wake_kind,
    has_input_read_tool,
    mark_inputs_read,
    render_inputs_message,
    render_trigger_card,
    request_launch_for_unread_inputs,
)

# --- Re-exports from superagent_pr (Card 3.4) ---
from hermes_cli.superagent_pr import (
    apply_follows_done_rule,
    end_ticket_follows_done,
    extract_pr_numbers_from_comment,
    extract_pr_numbers_from_report,
    get_configured_github_repo,
    get_pr_outcome,
    get_ticket_followed_prs,
    get_ticket_opened_prs,
    has_unfinished_work,
    hook_comment_for_pr,
    hook_run_report_for_pr,
    is_pr_open,
    record_pr_state,
    set_configured_github_repo,
    subscribe_ticket_to_pr,
)

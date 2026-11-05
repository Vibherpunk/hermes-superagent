"""Coded escalation trigger (DESIGN rev 4, §4).

Counter authority: ``tasks.structured_failures``.  Audit authority:
``task_events``.  A structured failure is (a) a gate exhaustion (kind A,
counted by §2.4/§4.1 — outside the state-anchored dedup) or (b) a filed,
sha-bound reviewer verdict (kind B, counted only under the State-Anchored
Episode Rule, Rule D, §4.2).  At the 1->2 transition the card's overrides are
rewritten to the counter-frontier seat inside the increment's own txn.

Import strategy (§4.0): this module is imported function-locally / late-bound
by ``kanban_db`` and the dispatcher, and itself reaches ``kanban_db`` helpers
only through function-local imports — no top-level cycle.

Spend (ruling 8): tokens + runs only, accumulated lazily and exactly-once
into the additive ``task_run_usage`` table (rev 4, N-3: row presence IS the
stamp; no metadata merge anywhere).  Accumulation race contract (T4-CLOSEOUT
F-2): each run's accumulation unit commits in its own txn; a
``task_run_usage`` PK conflict means another accumulator won — the loser
catches the integrity error, rolls back only its own unit, and treats the
run as accounted.  No exception escapes ``accumulate_ticket_usage`` for a
lost race.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

WORKING_STATUSES = ("running", "ready", "blocked", "review")

VERDICT_KEYS = {
    "version", "task_id", "commit_sha", "verdict", "reviewer",
    "builder_model", "builder_provider", "filed_at", "summary",
}

_UNACCOUNTED_FINAL_SECONDS = 86_400


# --------------------------------------------------------------------------
# Small event/payload helpers
# --------------------------------------------------------------------------

def _payload(row: Any) -> dict:
    if row is None:
        return {}
    try:
        raw = row["payload"]
    except Exception:
        raw = None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _task_row(conn: Any, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def _epoch_start_event_id(conn: Any, task_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(id) AS mid FROM task_events WHERE task_id = ? AND kind = 'completed'",
        (task_id,),
    ).fetchone()
    return int(row["mid"] or 0) if row else 0


def _spend_snapshot(conn: Any, task_id: str) -> dict:
    row = conn.execute(
        "SELECT spend_tokens_total, spend_runs_total FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return {"tokens": 0, "runs": 0}
    return {"tokens": int(row["spend_tokens_total"] or 0), "runs": int(row["spend_runs_total"] or 0)}


# --------------------------------------------------------------------------
# Builder-seat provenance (§4.3 — ONE writer, ONE read location)
# --------------------------------------------------------------------------

def builder_seat(conn: Any, task_id: str, board_config: Optional[dict] = None) -> tuple[Optional[str], Optional[str], str]:
    """(model, provider, seat_class) for the task's builder, read ONLY from:
    (1) the latest closed run's ``owner_admitted`` event payload
    (``effective_model``/``effective_provider``); (2) the task's override
    columns; (3) the assignee profile's configured default; (4) unknown.
    Run metadata is NEVER a provenance source."""
    from hermes_cli import kanban_assignment as _ka

    row = _task_row(conn, task_id)
    if row is None:
        return None, None, "other"
    run = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if run is not None:
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'owner_admitted' "
            "ORDER BY id DESC LIMIT 1", (task_id, int(run["id"])),
        ).fetchone()
        payload = _payload(ev)
        model = payload.get("effective_model")
        if model:
            provider = payload.get("effective_provider")
            return str(model), (str(provider) if provider else None), _ka.seat_class(str(model), provider, board_config)
    try:
        model_override = row["model_override"]
        provider_override = row["provider_override"]
    except Exception:
        model_override = provider_override = None
    if model_override:
        return str(model_override), (str(provider_override) if provider_override else None), \
            _ka.seat_class(str(model_override), provider_override, board_config)
    # (3) assignee profile default
    try:
        assignee = row["assignee"]
    except Exception:
        assignee = None
    cfg = _ka._profile_config_for(assignee)
    if isinstance(cfg, dict):
        model_block = cfg.get("model")
        if isinstance(model_block, dict):
            model = str(model_block.get("default") or "").strip()
            provider = str(model_block.get("provider") or "").strip()
            if provider.lower() == "auto":
                provider = ""
            if model:
                return model, (provider or None), _ka.seat_class(model, provider or None, board_config)
        elif isinstance(model_block, str) and model_block.strip():
            return model_block.strip(), None, _ka.seat_class(model_block.strip(), None, board_config)
    return None, None, "other"


# --------------------------------------------------------------------------
# Escalated flag (§4.3 step 4, F-8)
# --------------------------------------------------------------------------

def escalated_flag_set(conn: Any, task_id: str) -> bool:
    """SET iff an ``escalation_triggered`` event exists in the current epoch
    AND at least one run started after it in the same epoch."""
    epoch = _epoch_start_event_id(conn, task_id)
    ev = conn.execute(
        "SELECT id, created_at FROM task_events WHERE task_id = ? AND kind = 'escalation_triggered' AND id > ? "
        "ORDER BY id DESC LIMIT 1", (task_id, epoch),
    ).fetchone()
    if ev is None:
        return False
    run = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND started_at > ? LIMIT 1",
        (task_id, int(ev["created_at"])),
    ).fetchone()
    if run is not None:
        return True
    admitted = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'owner_admitted' AND id > ? LIMIT 1",
        (task_id, int(ev["id"])),
    ).fetchone()
    return admitted is not None


# --------------------------------------------------------------------------
# The increment + trigger (§4.3)
# --------------------------------------------------------------------------

def _maybe_escalate_in_txn(conn: Any, task_id: str, *, trigger_kind: str,
                           threshold: int = 2, board_config: Optional[dict] = None) -> dict:
    """Increment ``structured_failures`` inside the CALLER's open write_txn and
    run the trigger.  Returns an outcome dict:
    ``{count, action: none|escalated|already_blocked|unresolvable, seat_changed,
    target_seat, from_seat_class}``.  Opens no txn of its own (§4.0)."""
    from hermes_cli import kanban_assignment as _ka
    from hermes_cli import kanban_db as _kb

    conn.execute(
        "UPDATE tasks SET structured_failures = structured_failures + 1 WHERE id = ?", (task_id,))
    row = conn.execute(
        "SELECT structured_failures, status, work_class, assignee FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    count = int(row["structured_failures"] or 0)
    status = row["status"]
    outcome = {"count": count, "action": "none", "seat_changed": False,
               "target_seat": None, "from_seat_class": None}

    if count == threshold:
        _model, _provider, from_class = builder_seat(conn, task_id, board_config)
        outcome["from_seat_class"] = from_class
        if from_class == "argon":
            target_class = "opus"
        elif from_class == "opus":
            target_class = "argon"
        else:
            target_class = _ka.frontier_primary(board_config)
        work_class = None
        try:
            work_class = row["work_class"]
        except Exception:
            work_class = None
        rule_reasoning = _ka.RULE_TABLE.get(work_class or "general", (None, None))[1]
        seat = _ka.seat_for_class(target_class, board_config,
                                  reasoning_effort=rule_reasoning or "high")
        outcome["target_seat"] = {"model": seat.model, "provider": seat.provider,
                                  "reasoning_effort": seat.reasoning_effort}
        assignee = row["assignee"]
        if not _ka.seat_resolvable(seat, assignee):
            _kb._append_event(conn, task_id, "escalation_unresolvable",
                              {"target_seat": outcome["target_seat"], "trigger_kind": trigger_kind})
            outcome["action"] = "unresolvable"
            return outcome
        conn.execute(
            "UPDATE tasks SET model_override = ?, provider_override = ?, reasoning_effort = ? WHERE id = ?",
            (seat.model, seat.provider, seat.reasoning_effort, task_id),
        )
        _kb._append_event(conn, task_id, "escalation_triggered", {
            "from_seat_class": from_class,
            "to_seat": outcome["target_seat"],
            "structured_failures": count,
            "trigger_kind": trigger_kind,
            "spend_snapshot": _spend_snapshot(conn, task_id),
        })
        _kb._append_event(conn, task_id, "assignment_applied", {
            "work_class": work_class, "source": "escalation",
            "model": seat.model, "provider": seat.provider,
            "reasoning_effort": seat.reasoning_effort,
        })
        outcome["action"] = "escalated"
        outcome["seat_changed"] = True
        return outcome

    if count > threshold:
        # Already-escalated rule: only a frontier run that actually ran earns
        # the attention block; done cards never arm it (F-8).
        if status in WORKING_STATUSES and escalated_flag_set(conn, task_id):
            outcome["action"] = "already_blocked"
        return outcome
    return outcome


def record_gate_exhaustion_in_txn(conn: Any, task_id: str, *, run_id: Optional[int],
                                  evidence: Optional[dict] = None, enabled: bool = True,
                                  threshold: int = 2, board_config: Optional[dict] = None) -> dict:
    """Kind-A structured failure (§4.1): called from INSIDE ``complete_task``'s
    atomic exhaustion txn.  Kind A counting is outside the state-anchored
    dedup — exhaustion is reachable only through counted fix attempts, so the
    episode evidence is the run's own trajectory."""
    if not enabled:
        row = conn.execute("SELECT structured_failures FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return {"count": int(row["structured_failures"] or 0) if row else 0,
                "action": "disabled", "seat_changed": False, "target_seat": None,
                "from_seat_class": None}
    return _maybe_escalate_in_txn(conn, task_id, trigger_kind="gate",
                                  threshold=threshold, board_config=board_config)


# --------------------------------------------------------------------------
# Kind B — filed, sha-bound verdicts (§4.2, Rule D)
# --------------------------------------------------------------------------

def gate_attempt_decisions(conn: Any, task_id: str) -> dict:
    """THE counted-attempt pairing (FIX-19): map of
    ``gate_failed`` event id -> its ``gate_attempt_counted`` decision payload,
    built in ONE pass.  This is the single shared implementation used by both
    ``kanban_db._counted_gate_attempts`` and :func:`_current_anchor` — the
    §2.4/§4.2 pairing rule has exactly one home so the two readers can never
    diverge."""
    decisions: dict[int, dict] = {}
    for d in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'gate_attempt_counted'",
        (task_id,),
    ).fetchall():
        dp = _payload(d)
        if dp.get("gate_failed_event_id") is not None:
            try:
                decisions[int(dp["gate_failed_event_id"])] = dp
            except (TypeError, ValueError):
                continue
    return decisions


def _current_anchor(conn: Any, task_id: str) -> Optional[dict]:
    """The counted failure with the greatest event id in the current epoch."""
    epoch = _epoch_start_event_id(conn, task_id)
    decisions = gate_attempt_decisions(conn, task_id)
    rows = conn.execute(
        "SELECT id, run_id, kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        "AND kind IN ('gate_failed', 'verdict_filed', 'gate_cross_run_exhaustion') ORDER BY id DESC", (task_id, epoch),
    ).fetchall()
    for row in rows:
        payload = _payload(row)
        if row["kind"] == "verdict_filed":
            if payload.get("counted") is True:
                # Anchor state: the step-6a reviewed fingerprint, or — for a
                # filing counted via the weak-anchor/P3 state proxy (FIX-16)
                # — the proxy state it was actually judged against.
                state = payload.get("reviewed_fingerprint")
                if not isinstance(state, dict):
                    state = payload.get("state_proxy_fingerprint")
                return {"event_id": int(row["id"]), "kind": "verdict",
                        "sha": payload.get("commit_sha"),
                        "state": state,
                        "run_id": payload.get("produced_by_run_id")}
        elif row["kind"] == "gate_cross_run_exhaustion":
            # Cross-run kind-A mint (AMENDMENT-DESIGN-REV2 §A6.2/W-4): the
            # mint event exists only for a counted increment (the marker is
            # appended after the increment, in the same txn), so it anchors
            # unconditionally.  event_id is the MINT event's id (recorded
            # semantics change); the evidence event rides in the payload.
            return {"event_id": int(row["id"]), "kind": "gate",
                    "sha": payload.get("anchor_head_sha"),
                    "state": payload.get("anchor_fingerprint"),
                    "run_id": payload.get("anchor_run_id")}
        else:  # gate_failed counts only when its paired decision says exhausted
            dp = decisions.get(int(row["id"]))
            if dp is not None and dp.get("exhausted") is True and dp.get("counted") is True:
                fp = payload.get("fingerprint")
                return {"event_id": int(row["id"]), "kind": "gate",
                        "sha": (fp or {}).get("head_sha") if isinstance(fp, dict) else None,
                        "state": fp if isinstance(fp, dict) else None,
                        "run_id": int(row["run_id"]) if row["run_id"] is not None else None}
    return None


def _produced_by_run(conn: Any, task_id: str, commit_sha: str):
    """R_B resolution (first hit wins): the run_id of the most recent
    ``pr_acceptance`` event in the current epoch whose payload head_sha equals
    ``commit_sha``.  No other source is consulted (ruling 5)."""
    epoch = _epoch_start_event_id(conn, task_id)
    rows = conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id = ? AND kind = 'pr_acceptance' "
        "AND id > ? ORDER BY id DESC", (task_id, epoch),
    ).fetchall()
    for row in rows:
        payload = _payload(row)
        if payload.get("head_sha") == commit_sha and row["run_id"] is not None:
            run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (int(row["run_id"]),)).fetchone()
            if run is not None:
                return run
    return None


def _run_state_proxy(conn: Any, task_id: str, run_id: int) -> Optional[dict]:
    """A run's PRE-GATE state-proxy fingerprint from its most recent
    fingerprint-bearing event (§4.2), or None when the run has none."""
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind IN ('gate_passed', 'gate_failed', 'gate_env_failed', 'completed') ORDER BY id DESC",
        (task_id, int(run_id)),
    ).fetchall()
    for row in rows:
        payload = _payload(row)
        if row["kind"] == "gate_passed":
            fp = payload.get("pre_gate_fingerprint")
        elif row["kind"] in ("gate_failed", "gate_env_failed"):
            fp = payload.get("fingerprint")
        else:  # completed
            gate = payload.get("gate") or {}
            fp = gate.get("pre_gate_fingerprint") if isinstance(gate, dict) else None
        if isinstance(fp, dict):
            return fp
    return None


def _states_same(a: Optional[dict], b: Optional[dict]) -> Optional[bool]:
    """State identity (§4.2): True/False when decidable, None when not.
    ``head_sha`` is NOT part of identity; weak fingerprints are undecidable."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return None
    if a.get("weak") or b.get("weak"):
        return None
    return a.get("manifest_hash") == b.get("manifest_hash")


def _git(args: list, cwd: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _workspace_is_git(workspace: Optional[str]) -> bool:
    if not workspace:
        return False
    try:
        return _git(["rev-parse", "--git-dir"], workspace).returncode == 0
    except Exception:
        return False


def _sha_binds_git(workspace: str, commit_sha: str) -> bool:
    """Binding leg (iii): commit equals workspace HEAD or is an ancestor of it."""
    try:
        head = _git(["rev-parse", "HEAD"], workspace)
        if head.returncode == 0 and (head.stdout or "").strip() == commit_sha:
            return True
        anc = _git(["merge-base", "--is-ancestor", commit_sha, "HEAD"], workspace)
        return anc.returncode == 0
    except Exception:
        return False


def _reviewed_fingerprint(workspace: str, commit_sha: str, attachments_root: Optional[Path],
                          globs: list) -> Optional[dict]:
    """Step 6a: materialize the reviewed commit via ``git archive`` into a temp
    dir OUTSIDE the workspace and fingerprint it with the §2.2 function.
    Uncomputable (None) on any failure — never estimated."""
    from hermes_cli import kanban_gates as _kg

    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "archive", "--format=tar", commit_sha],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        with tempfile.TemporaryDirectory(prefix="verdict-review-") as td:
            tmp = Path(td)
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
                tf.extractall(tmp)  # reviewed tree is the ticket's own commit
            # Hard sanity limits (§4.2 step 6a).
            n_files = 0
            n_bytes = 0
            import os as _os
            for dirpath, _dirnames, filenames in _os.walk(tmp):
                for name in filenames:
                    n_files += 1
                    try:
                        n_bytes += (Path(dirpath) / name).stat().st_size
                    except OSError:
                        pass
                    if n_files > 100_000 or n_bytes > 20 * 1024 * 1024 * 1024:
                        return None
            exclude_roots: list = []
            if attachments_root is not None:
                try:
                    rel = Path(attachments_root).resolve().relative_to(Path(workspace).resolve())
                    cand = tmp / rel
                    if cand.exists():
                        exclude_roots.append(cand)
                except Exception:
                    pass
            fp, _entries = _kg._fingerprint_and_entries(
                str(tmp), exclude_roots=exclude_roots, fingerprint_exclude=globs)
            if fp.get("weak"):
                # Caps exceeded: recompute ONCE with caps lifted (A-8).
                fp, _entries = _kg._fingerprint_and_entries(
                    str(tmp), exclude_roots=exclude_roots, fingerprint_exclude=globs,
                    force_strong=True)
            return fp
    except Exception:
        return None


def _evaluate_kind_b(conn: Any, task: Any, anchor: Optional[dict], commit_sha: str,
                     workspace: Optional[str], produced_run: Any, leg_i: bool) -> tuple:
    """Evaluate Rule D's predicates OUTSIDE any txn.
    Returns (counted, reason, reviewed_fingerprint, produced_by_run_id,
    state_proxy_fingerprint).  FIX-16: the produced run's state-proxy is
    returned SEPARATELY — ``reviewed_fingerprint`` is only ever the step-6a
    fingerprint of the reviewed state; on the weak-anchor/P3 paths (where
    step 6a does not decide) it is None and the proxy travels in its own
    slot with its provenance (the produced-by run id)."""
    from hermes_cli import kanban_gates as _kg

    produced_id = int(produced_run["id"]) if produced_run is not None else None
    globs = _kg.spec_exclude_globs(task["gate_spec"] if task is not None else None)
    attach_root = None
    try:
        from hermes_cli import kanban_db as _kb
        attach_root = _kb.task_attachments_dir(task["id"]) if task is not None else None
    except Exception:
        attach_root = None

    if anchor is None:
        # P0 — no anchor: counts; still record the reviewed state when computable.
        reviewed = None
        if workspace and _workspace_is_git(workspace):
            reviewed = _reviewed_fingerprint(workspace, commit_sha, attach_root, globs)
        return True, None, reviewed, produced_id, None

    anchor_sha = anchor.get("sha")
    anchor_state = anchor.get("state")
    is_git = _workspace_is_git(workspace)

    if is_git and anchor_sha:
        # P1 — directional predicate.
        try:
            proc = _git(["merge-base", "--is-ancestor", str(anchor_sha), commit_sha], workspace)  # type: ignore[arg-type]
        except Exception:
            return False, "dedup_unavailable", None, produced_id, None
        if proc.returncode == 1:
            return False, "not_descendant_of_anchor", None, produced_id, None
        if proc.returncode != 0:
            return False, "dedup_unavailable", None, produced_id, None
        if commit_sha == anchor_sha:
            return False, "dedup_unavailable", None, produced_id, None
        # Anchor state availability (F5).
        if not isinstance(anchor_state, dict):
            return False, "anchor_state_unavailable", None, produced_id, None
        if anchor_state.get("weak"):
            # Weak-anchor fallback: P3(b)-(d) with the produced-by run's proxy.
            if produced_run is None or anchor.get("run_id") is None \
                    or produced_id <= int(anchor["run_id"]):  # type: ignore[arg-type]
                return False, "weak_anchor_unresolved", None, produced_id, None
            if produced_run["ended_at"] is None:
                return False, "weak_anchor_unresolved", None, produced_id, None
            proxy = _run_state_proxy(conn, task["id"], produced_id)
            if proxy is None:
                return False, "weak_anchor_unresolved", None, produced_id, None
            same = _states_same(anchor_state, proxy)
            if same is True:
                return False, "same_episode_state", None, produced_id, proxy
            if same is None:
                # Both weak-form entries compare on (path, size, mtime) tuples
                # via manifest hash — decidable when both are weak.
                if anchor_state.get("manifest_hash") == proxy.get("manifest_hash"):
                    return False, "same_episode_state", None, produced_id, proxy
                return True, None, None, produced_id, proxy
            return True, None, None, produced_id, proxy
        # P2 — state predicate.
        reviewed = _reviewed_fingerprint(workspace, commit_sha, attach_root, globs)  # type: ignore[arg-type]
        if reviewed is None or reviewed.get("weak"):
            return False, "reviewed_state_unavailable", reviewed, produced_id, None
        if reviewed.get("manifest_hash") == anchor_state.get("manifest_hash"):
            return False, "same_episode_state", reviewed, produced_id, None
        return True, None, reviewed, produced_id, None

    # P3 — non-git / NULL-anchor-sha case.
    if not leg_i:
        return False, "not_after_last_kind_a", None, produced_id, None
    if produced_run is None or anchor.get("run_id") is None or produced_id <= int(anchor["run_id"]):  # type: ignore[arg-type]
        return False, "not_after_last_kind_a", None, produced_id, None
    if produced_run["ended_at"] is None:
        return False, "not_after_last_kind_a", None, produced_id, None
    if not isinstance(anchor_state, dict):
        return False, "anchor_state_unavailable", None, produced_id, None
    proxy = _run_state_proxy(conn, task["id"], produced_id)
    if proxy is None:
        return False, "reviewed_state_unavailable", None, produced_id, None
    same = _states_same(anchor_state, proxy)
    if same is True:
        return False, "same_episode_state", None, produced_id, proxy
    if same is None:
        if anchor_state.get("weak"):
            return False, "anchor_state_unavailable", None, produced_id, proxy
        return False, "reviewed_state_unavailable", None, produced_id, proxy
    return True, None, None, produced_id, proxy


def _append_simple_event(conn: Any, task_id: str, kind: str, payload: dict, run_id: Optional[int] = None) -> None:
    from hermes_cli import kanban_db as _kb
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, kind, payload, run_id=run_id)


def file_verdict(conn: Any, task_id: str, *, source_path_or_payload: Any,
                 filed_by: str, board_config: Optional[dict] = None,
                 escalation_enabled: bool = True, threshold: int = 2) -> dict:
    """Validate and file a reviewer verdict (§4.2).  Returns a result dict:
    ``{filed, counted, reason|rejected, sha256, escalation}``."""
    from hermes_cli import kanban_db as _kb

    result: dict[str, Any] = {"filed": False, "counted": False, "reason": None,
                              "rejected": None, "sha256": None, "escalation": None}

    # ---- Load the payload -------------------------------------------------
    raw_bytes: Optional[bytes] = None
    payload: Any = None
    if isinstance(source_path_or_payload, dict):
        payload = source_path_or_payload
        raw_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    elif isinstance(source_path_or_payload, (str, Path)):
        p = Path(source_path_or_payload)
        if p.is_file():
            raw_bytes = p.read_bytes()
            try:
                payload = json.loads(raw_bytes.decode("utf-8"))
            except Exception:
                payload = None
        else:
            try:
                payload = json.loads(str(source_path_or_payload))
                raw_bytes = str(source_path_or_payload).encode("utf-8")
            except Exception:
                payload = None
    if not isinstance(payload, dict) or raw_bytes is None:
        _append_simple_event(conn, task_id, "verdict_rejected", {"reason": "unparseable"})
        result["rejected"] = "unparseable"
        return result

    def _reject(reason: str) -> dict:
        _append_simple_event(conn, task_id, "verdict_rejected", {"reason": reason})
        result["rejected"] = reason
        return result

    # ---- Step 1-2: schema + reviewer binding ------------------------------
    unknown = set(payload) - VERDICT_KEYS
    if unknown:
        return _reject(f"unknown_keys:{sorted(unknown)}")
    if payload.get("version") != 1:
        return _reject("bad_version")
    if payload.get("task_id") != task_id:
        return _reject("task_mismatch")
    verdict = payload.get("verdict")
    if verdict not in ("pass", "fail"):
        return _reject("bad_verdict")
    reviewer = payload.get("reviewer")
    if not reviewer or reviewer != filed_by:
        return _reject("reviewer_mismatch")
    commit_sha = payload.get("commit_sha") or ""
    if not isinstance(commit_sha, str) or len(commit_sha) != 40 or \
            any(c not in "0123456789abcdef" for c in commit_sha.lower()):
        return _reject("bad_commit_sha")
    commit_sha = commit_sha.lower()
    summary = payload.get("summary")
    if summary is not None and (not isinstance(summary, str) or len(summary) > 4000):
        return _reject("bad_summary")

    task = _task_row(conn, task_id)
    if task is None:
        return _reject("unknown_task")
    status_at_filing = task["status"]
    assignee = task["assignee"]
    workspace = task["workspace_path"]

    produced_run = _produced_by_run(conn, task_id, commit_sha)
    if produced_run is not None:
        builder_profile = produced_run["profile"]
        if builder_profile and reviewer == builder_profile:
            return _reject("self_review")
        if assignee and builder_profile and assignee == builder_profile and reviewer == assignee:
            return _reject("self_review")

    # ---- Step 3: sha binding (legs i / iii only; metadata never binds) ----
    leg_i = produced_run is not None
    leg_iii = bool(workspace) and _workspace_is_git(workspace) and _sha_binds_git(workspace, commit_sha)
    if not leg_i and not leg_iii:
        return _reject("unbindable_sha")

    # ---- Step 4: artifact integrity ---------------------------------------
    verdicts_dir = _kb.task_attachments_dir(task_id) / "verdicts"
    verdicts_dir.mkdir(parents=True, exist_ok=True)
    dest = verdicts_dir / f"{commit_sha}.{reviewer}.json"
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    result["sha256"] = file_hash
    result["path"] = str(dest)
    if dest.exists():
        existing = dest.read_bytes()
        if existing != raw_bytes:
            _append_simple_event(conn, task_id, "verdict_invalidated",
                                 {"commit_sha": commit_sha, "reviewer": reviewer,
                                  "reason": "artifact_mutated"})
            result["rejected"] = "artifact_mutated"
            return result
    else:
        dest.write_bytes(raw_bytes)
    result["filed"] = True

    base_event = {"sha256": file_hash, "path": str(dest), "commit_sha": commit_sha,
                  "reviewer": reviewer, "verdict": verdict}

    # ---- Pass verdicts: event only, never count, never anchor -------------
    if verdict == "pass":
        _append_simple_event(conn, task_id, "verdict_filed", {**base_event, "counted": False,
                                                             "reviewed_fingerprint": None,
                                                             "anchor_event_id": None,
                                                             "anchor_kind": None,
                                                             "produced_by_run_id": int(produced_run["id"]) if produced_run is not None else None})
        return result

    # ---- Escalation disabled (FIX-5): validation + artifact + event still
    # happen (§5), but the filing records counted: false and NEVER increments
    # — a disabled-era filing must not become a phantom anchor that dedups a
    # later genuine failure after enablement. -------------------------------
    if not escalation_enabled:
        _append_simple_event(conn, task_id, "verdict_filed", {**base_event, "counted": False,
                                                             "reviewed_fingerprint": None,
                                                             "anchor_event_id": None,
                                                             "anchor_kind": None,
                                                             "produced_by_run_id": int(produced_run["id"]) if produced_run is not None else None})
        return result

    # ---- Step 5+6: dedup (Rule D) + counting, anchor re-scanned in-txn ----
    for _round in range(3):
        anchor = _current_anchor(conn, task_id)
        counted, reason, reviewed_fp, produced_id, proxy_fp = _evaluate_kind_b(
            conn, task, anchor, commit_sha, workspace, produced_run, leg_i)
        event_payload = {**base_event,
                         "reviewed_fingerprint": reviewed_fp,
                         "state_proxy_fingerprint": proxy_fp,
                         "state_proxy_run_id": produced_id if proxy_fp is not None else None,
                         "anchor_event_id": anchor["event_id"] if anchor else None,
                         "anchor_kind": anchor["kind"] if anchor else None,
                         "produced_by_run_id": produced_id}
        if not counted:
            with _kb.write_txn(conn):
                _kb._append_event(conn, task_id, "verdict_filed", {**event_payload, "counted": False})
                _kb._append_event(conn, task_id, "verdict_not_counted",
                                  {"commit_sha": commit_sha, "reviewer": reviewer, "reason": reason})
            result["reason"] = reason
            return result
        # Counting txn: re-scan anchor + counted duplicates inside the txn.
        anchor_changed = False
        duplicate = False
        invalidated = False
        outcome: dict = {"action": "none", "seat_changed": False}
        with _kb.write_txn(conn):
            anchor2 = _current_anchor(conn, task_id)
            if (anchor2 or {}).get("event_id") != (anchor or {}).get("event_id"):
                anchor_changed = True
            else:
                dup_rows = conn.execute(
                    "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'verdict_filed'",
                    (task_id,),
                ).fetchall()
                for drow in dup_rows:
                    dp = _payload(drow)
                    if dp.get("counted") is True and dp.get("commit_sha") == commit_sha \
                            and dp.get("reviewer") == reviewer:
                        duplicate = True
                        break
                if not duplicate:
                    # Hash re-verification at count time (filed-record rule).
                    try:
                        disk_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
                    except OSError:
                        disk_hash = ""
                    if disk_hash != file_hash:
                        invalidated = True
                        _kb._append_event(conn, task_id, "verdict_invalidated",
                                          {"commit_sha": commit_sha, "reviewer": reviewer,
                                           "reason": "hash_mismatch_at_count"})
                    else:
                        _kb._append_event(conn, task_id, "verdict_filed",
                                          {**event_payload, "counted": True})
                        if escalation_enabled:
                            outcome = _maybe_escalate_in_txn(
                                conn, task_id, trigger_kind="verdict",
                                threshold=threshold, board_config=board_config)
        if anchor_changed:
            continue  # re-evaluate against the new anchor (max 3 rounds)
        if duplicate:
            result["reason"] = "duplicate_counted"
            return result
        if invalidated:
            result["rejected"] = "hash_mismatch_at_count"
            return result
        result["counted"] = True
        result["escalation"] = outcome
        # Post-commit consequences of the escalation outcome (§4.3).
        if outcome.get("seat_changed"):
            _kb.notify_task_updated(conn, task_id, ("model_override", "provider_override", "reasoning_effort"))
        if outcome.get("action") == "already_blocked" and status_at_filing in WORKING_STATUSES:
            _kb.block_task_if_state(conn, task_id, kind="gate_worker",
                                    reason="structured failures continue after frontier escalation",
                                    expected_status=status_at_filing)
        elif outcome.get("action") == "unresolvable":
            _kb.block_task_if_state(conn, task_id, kind="capability",
                                    reason="escalation target seat unresolvable for assignee profile",
                                    expected_status=status_at_filing)
        return result

    # Anchor changed 3 times (F9).
    anchor = _current_anchor(conn, task_id)
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, "verdict_filed",
                          {**base_event, "counted": False, "reviewed_fingerprint": None,
                           "state_proxy_fingerprint": None, "state_proxy_run_id": None,
                           "anchor_event_id": anchor["event_id"] if anchor else None,
                           "anchor_kind": anchor["kind"] if anchor else None,
                           "produced_by_run_id": int(produced_run["id"]) if produced_run is not None else None})
        _kb._append_event(conn, task_id, "verdict_not_counted",
                          {"commit_sha": commit_sha, "reviewer": reviewer, "reason": "dedup_unavailable"})
    result["reason"] = "dedup_unavailable"
    return result


# --------------------------------------------------------------------------
# Spend: lazy exactly-once accumulation (§4.4) + guard
# --------------------------------------------------------------------------

def _read_session_usage(db_path: str, session_id: str) -> Optional[dict]:
    """Usage totals for one session from its recorded state.db (read-only).
    None when the DB/table/rows are unavailable (the pending path)."""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        sconn = sqlite3.connect(uri, uri=True)
        try:
            rows = sconn.execute(
                "SELECT input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens "
                "FROM session_model_usage WHERE session_id = ?", (session_id,),
            ).fetchall()
        finally:
            sconn.close()
    except Exception:
        return None
    if not rows:
        return None
    totals = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    for inp, out, reas, cr, cw in rows:
        i_, o_, r_ = int(inp or 0), int(out or 0), int(reas or 0)
        totals["input_tokens"] += i_
        totals["output_tokens"] += o_
        totals["reasoning_tokens"] += r_
        totals["total_tokens"] += i_ + o_ + r_ + int(cr or 0) + int(cw or 0)
    return totals


def accumulate_ticket_usage(conn: Any, task_id: str) -> None:
    """Lazy, exactly-once accumulation (§4.4).  Called with NO write txn open;
    each run's unit commits in its own txn (F-2 race contract)."""
    from hermes_cli import kanban_db as _kb

    if getattr(conn, "in_transaction", False):
        raise RuntimeError("accumulate_ticket_usage must be called outside any write txn (B-11)")
    try:
        accounted = {int(r["run_id"]) for r in conn.execute(
            "SELECT run_id FROM task_run_usage WHERE task_id = ?", (task_id,))}
    except sqlite3.Error:
        accounted = set()
    runs = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    now = int(time.time())
    for run in runs:
        run_id = int(run["id"])
        if run_id in accounted:
            continue
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'owner_admitted' "
            "ORDER BY id DESC LIMIT 1", (task_id, run_id),
        ).fetchone()
        payload = _payload(ev)
        db_path = payload.get("db")
        session_id = payload.get("session_id")
        usage = None
        if db_path and session_id:
            usage = _read_session_usage(str(db_path), str(session_id))
        if usage is None:
            ended_at = int(run["ended_at"] or now)
            if now - ended_at > _UNACCOUNTED_FINAL_SECONDS:
                try:
                    with _kb.write_txn(conn):
                        conn.execute(
                            "INSERT INTO task_run_usage (run_id, task_id, input_tokens, output_tokens, "
                            "reasoning_tokens, total_tokens, unaccounted_final, recorded_at) "
                            "VALUES (?, ?, NULL, NULL, NULL, NULL, 1, ?)", (run_id, task_id, now))
                        conn.execute(
                            "UPDATE tasks SET spend_runs_total = spend_runs_total + 1 WHERE id = ?",
                            (task_id,))
                        _kb._append_event(conn, task_id, "spend_run_unaccounted",
                                          {"run_id": run_id, "final": True}, run_id=run_id)
                except sqlite3.IntegrityError:
                    pass  # another accumulator won (F-2)
            else:
                already = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'spend_run_unaccounted' "
                    "AND run_id = ? LIMIT 1", (task_id, run_id),
                ).fetchone()
                if not already:
                    with _kb.write_txn(conn):
                        _kb._append_event(conn, task_id, "spend_run_unaccounted",
                                          {"run_id": run_id}, run_id=run_id)
            continue
        try:
            with _kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_run_usage (run_id, task_id, input_tokens, output_tokens, "
                    "reasoning_tokens, total_tokens, unaccounted_final, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (run_id, task_id, usage["input_tokens"], usage["output_tokens"],
                     usage["reasoning_tokens"], usage["total_tokens"], now))
                conn.execute(
                    "UPDATE tasks SET spend_tokens_total = spend_tokens_total + ?, "
                    "spend_runs_total = spend_runs_total + 1 WHERE id = ?",
                    (usage["total_tokens"], task_id))
        except sqlite3.IntegrityError:
            pass  # PK conflict: another accumulator won; treat as accounted (F-2)


def check_spend_guard(conn: Any, task_id: str, board_config=None) -> Optional[str]:
    """Spend guard (§4.4): escalated/frontier tickets only.  Accumulates in its
    own txn BEFORE comparison; on breach (``>=``) blocks via Shape B and
    returns the reason, else None.

    ``board_config`` (FIX-12): an optional ``(config, errors)`` snapshot loaded
    ONCE by the caller (one per dispatcher tick); when omitted the guard
    loads its own, preserving standalone call-site behavior."""
    from hermes_cli import kanban_board_config as _kbcfg
    from hermes_cli import kanban_db as _kb

    if board_config is not None:
        config, errors = board_config
    else:
        config, errors = _kbcfg.load_board_config()
    # FIX-4: this consumer surfaces ONLY the escalation section's errors.
    _kbcfg.surface_if_exercised(conn, task_id, config, errors, "escalation")
    if not _kbcfg.feature_enabled(config, errors, "escalation"):
        return None
    task = _task_row(conn, task_id)
    if task is None:
        return None
    in_scope = escalated_flag_set(conn, task_id) or _epoch_has_escalation(conn, task_id)
    if not in_scope:
        _m, _p, cls = builder_seat(conn, task_id, config)
        in_scope = cls in ("argon", "opus")
    if not in_scope:
        return None
    accumulate_ticket_usage(conn, task_id)
    task = _task_row(conn, task_id)
    totals = {"tokens": int(task["spend_tokens_total"] or 0), "runs": int(task["spend_runs_total"] or 0)}
    esc_cfg = _kbcfg.section_config(config, "escalation")
    ceilings = esc_cfg.get("ceilings") or {}
    work_class = None
    try:
        work_class = task["work_class"]
    except Exception:
        work_class = None
    row_cfg = ceilings.get(work_class) if work_class else None
    if not isinstance(row_cfg, dict):
        row_cfg = ceilings.get("default") if isinstance(ceilings.get("default"), dict) else {}
    max_tokens_default = int(row_cfg.get("max_total_tokens", 2_000_000))
    max_runs = int(row_cfg.get("max_runs", 12))
    ceiling_tokens = task["spend_ceiling_tokens"] if task["spend_ceiling_tokens"] is not None else max_tokens_default
    ceiling_tokens = int(ceiling_tokens)
    reason = None
    if totals["tokens"] >= ceiling_tokens:
        reason = f"spend ceiling breached: tokens {totals['tokens']} >= {ceiling_tokens}"
    elif totals["runs"] >= max_runs:
        reason = f"spend ceiling breached: runs {totals['runs']} >= {max_runs}"
    if reason is None:
        return None
    status = task["status"]
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, "spend_ceiling_breached",
                          {"tokens": totals["tokens"], "runs": totals["runs"],
                           "ceilings": {"max_total_tokens": ceiling_tokens, "max_runs": max_runs},
                           "reason": reason})
    _kb.block_task_if_state(conn, task_id, kind="spend_ceiling", reason=reason,
                            expected_status=status)
    return reason


def _epoch_has_escalation(conn: Any, task_id: str) -> bool:
    epoch = _epoch_start_event_id(conn, task_id)
    row = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'escalation_triggered' AND id > ? LIMIT 1",
        (task_id, epoch),
    ).fetchone()
    return row is not None

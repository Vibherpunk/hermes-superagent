"""Tests for structured-failure escalation (DESIGN rev 4 §4, E series) plus
the gate/escalation interaction cases G-4b, G-4c, G-5b, G-5c.

Covers E-1, E-2, E-3, E-4, E-4b (rev-4 replacement), E-4c, E-4d, E-4e,
E-4f (with T4 F-4), E-4g, E-4h, E-4i, E-5, E-6, E-6a, E-6b, E-6c, E-7, E-8.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_board_config as kbcfg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_escalation as ke
from hermes_cli import kanban_gates as kg


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


FULL_CFG = """kanban:
  gates:
    enabled: true
    max_attempts_per_run: 3
  escalation:
    enabled: true
    threshold: 2
custom_providers:
  - name: antigravity-bridge
  - name: bonsai-local
"""

FAIL_ARGV = [sys.executable, "-c", "import sys; sys.exit(1)"]
PASS_ARGV = [sys.executable, "-c", "pass"]


def _cfg(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _board_cfg():
    cfg, _errs = kbcfg.load_board_config()
    return cfg


def _spec(argv=FAIL_ARGV):
    return {"version": 1, "checks": [{"id": "c", "argv": argv}]}


def _events(conn, tid, kind=None):
    evs = kb.list_events(conn, tid)
    return [e for e in evs if kind is None or e.kind == kind]


def _git(ws: Path, *args, check=True):
    return subprocess.run(["git", "-C", str(ws), *args],
                          check=check, capture_output=True, text=True)


def _repo(tmp_path, name="repo"):
    ws = tmp_path / name
    ws.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(ws)], check=True, capture_output=True)
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "T")
    return ws


def _commit(ws: Path, content: str, msg="c", fname="f.txt") -> str:
    (ws / fname).write_text(content, encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", msg)
    return _git(ws, "rev-parse", "HEAD").stdout.strip()


def _mk_gated(conn, ws, argv=FAIL_ARGV, assignee="worker"):
    tid = kb.create_task(conn, title="gated", assignee=assignee,
                         workspace_kind="dir", workspace_path=str(ws),
                         gate_spec=_spec(argv))
    assert kb.claim_task(conn, tid, claimer="host:worker") is not None
    return tid


def _attempt(conn, tid, ws, content):
    """One gate attempt after editing the tracked file to ``content``."""
    (ws / "f.txt").write_text(content, encoding="utf-8")
    task = kb.get_task(conn, tid)
    with pytest.raises(kb.GateFailedError) as ei:
        kb.complete_task(conn, tid, summary="try",
                         expected_run_id=task.current_run_id)
    return ei.value


def _exhaust(conn, tid, ws, states=("s1", "s2", "s3")):
    """Drive one run to gate exhaustion with three counted attempts."""
    for i, state in enumerate(states):
        err = _attempt(conn, tid, ws, state)
        assert err.exhausted is (i == len(states) - 1)


def _admit(conn, tid, model, provider, session_id="s1", db=""):
    task = kb.get_task(conn, tid)
    kb._append_event(conn, task.id, "owner_admitted",
                     {"db": db, "session_id": session_id,
                      "effective_model": model, "effective_provider": provider},
                     run_id=task.current_run_id)


def _verdict(tid, sha, verdict="fail", reviewer="rev-a", **kw):
    payload = {"version": 1, "task_id": tid, "commit_sha": sha,
               "verdict": verdict, "reviewer": reviewer,
               "filed_at": 1_700_000_000, "summary": "review"}
    payload.update(kw)
    return payload


def _filev(conn, tid, sha, verdict="fail", reviewer="rev-a", **kw):
    return ke.file_verdict(
        conn, tid, source_path_or_payload=_verdict(tid, sha, verdict, reviewer, **kw),
        filed_by=reviewer, board_config=_board_cfg(),
        escalation_enabled=True, threshold=2)


def _failures(conn, tid):
    return kb.get_task(conn, tid).structured_failures


# --------------------------------------------------------------------------
# E-1 / E-2: kind-A escalation, both directions; provenance discipline
# --------------------------------------------------------------------------

def test_e1_argon_builder_escalates_to_opus(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _admit(conn, tid, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid, ws)
        assert _failures(conn, tid) == 1
        assert not _events(conn, tid, "escalation_triggered")
        # Second run, second exhaustion -> threshold reached.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _admit(conn, tid, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid, ws, states=("t1", "t2", "t3"))
        assert _failures(conn, tid) == 2
        task = kb.get_task(conn, tid)
        assert task.model_override == "anthropic/claude-opus-5.5"
        assert task.provider_override == "openrouter"
        trig = _events(conn, tid, "escalation_triggered")
        assert len(trig) == 1
        assert trig[0].payload["from_seat_class"] == "argon"
        assert trig[0].payload["trigger_kind"] == "gate"
        applied = [e for e in _events(conn, tid, "assignment_applied")
                   if e.payload.get("source") == "escalation"]
        assert len(applied) == 1


def test_e2_opus_builder_escalates_to_argon(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    # The argon seat is a custom: provider — the assignee profile must
    # register it for the escalation pin to be resolvable.
    pdir = kanban_home / "profiles" / "worker"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(
        "custom_providers:\n  - name: antigravity-bridge\n  - name: bonsai-local\n",
        encoding="utf-8")
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _admit(conn, tid, "anthropic/claude-opus-5.5", "openrouter")
        _exhaust(conn, tid, ws)
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _admit(conn, tid, "anthropic/claude-opus-5.5", "openrouter")
        _exhaust(conn, tid, ws, states=("t1", "t2", "t3"))
        task = kb.get_task(conn, tid)
        assert task.model_override == "gemini-4-argon"
        assert task.provider_override == "custom:antigravity-bridge"


def test_e2_metadata_only_provenance_is_not_provenance(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        # Builder identity ONLY in run metadata (worker-writable): the seat
        # resolver must ignore it — no model is attributed and the class is
        # neither frontier seat (such work escalates to the frontier primary).
        model, provider, seat_cls = ke.builder_seat(conn, tid, _board_cfg())
        assert model is None
        assert seat_cls not in ("argon", "opus", "cheap")


# --------------------------------------------------------------------------
# E-3: epoch flag semantics; done cards are never blocked
# --------------------------------------------------------------------------

def test_e3_post_escalation_failure_blocks_and_done_card_never_blocked(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _admit(conn, tid, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid, ws)
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _admit(conn, tid, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid, ws, states=("t1", "t2", "t3"))
        assert _events(conn, tid, "escalation_triggered")
        # The second exhaustion also trips the unified failure budget
        # (gave_up -> blocked); revive the card for the third run without
        # touching the epoch or the structured-failure counter.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', block_kind = NULL, "
                "block_recurrences = 0, current_run_id = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ?", (tid,))
        # Third exhaustion in the same epoch — the BOTH-TRIP case (FIX-6):
        # this failure is BOTH a post-escalation structured failure AND the
        # breaker trip (consecutive_failures is already at the limit and is
        # NOT reset by the manual revive above).  DESIGN §2.4(d)/(f) orders
        # the BREAKER outcome first: the card stays blocked as a gave_up
        # park, the preserved block snapshot (NULL / 0 after the revive) is
        # restored exactly, NO gate_worker Shape A block is written, no
        # recurrence is consumed, and no blocked-family event is appended
        # by the exhaustion sequence.
        n_blocked_events = len(_events(conn, tid, "blocked")) + \
            len(_events(conn, tid, "block_loop_detected"))
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _admit(conn, tid, "anthropic/claude-opus-5.5", "openrouter")
        _exhaust(conn, tid, ws, states=("u1", "u2", "u3"))
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind is None
        assert task.block_recurrences == 0
        assert _events(conn, tid, "gave_up")
        assert len(_events(conn, tid, "blocked")) + \
            len(_events(conn, tid, "block_loop_detected")) == n_blocked_events
        assert _failures(conn, tid) == 3
        # A verdict filed against the DONE card counts zero blocks.  (Revive
        # the card first: a repeated block may sit in triage, which
        # unblock_task deliberately refuses — reset it directly.)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', block_kind = NULL, "
                "block_recurrences = 0, current_run_id = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _cfg(kanban_home, "kanban:\n  gates:\n    enabled: false\n  escalation:\n    enabled: true\n")
        task = kb.get_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done",
                                expected_run_id=task.current_run_id) is True
        head = _git(ws, "rev-parse", "HEAD").stdout.strip()
        res = _filev(conn, tid, head)
        assert kb.get_task(conn, tid).status == "done"   # never blocked


# --------------------------------------------------------------------------
# E-4: basic verdict counting discipline
# --------------------------------------------------------------------------

def test_e4_pass_never_counts_and_self_review_rejected(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    sha = _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reviewed", assignee="builder-prof",
                             workspace_kind="dir", workspace_path=str(ws))
        res = _filev(conn, tid, sha, verdict="pass")
        assert res["filed"] is True and res["counted"] is False
        assert _failures(conn, tid) == 0
        # Fail counts (no anchor yet).
        res = _filev(conn, tid, sha, reviewer="rev-b")
        assert res["counted"] is True
        assert _failures(conn, tid) == 1
        # Self-review: reviewer == producing profile (assignee) via receipt run.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha},
                         run_id=task.current_run_id)
        res = _filev(conn, tid, sha, reviewer="builder-prof")
        assert res["counted"] is False
        assert res.get("rejected") == "self_review"


def test_e4_metadata_only_sha_is_unbindable(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    ghost = "ab" * 20
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reviewed", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws))
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        task = kb.get_task(conn, tid)
        # The sha exists ONLY in worker-writable run metadata.
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                         (json.dumps({"commit_sha": ghost}), task.current_run_id))
        res = _filev(conn, tid, ghost)
        assert res["counted"] is False
        assert res.get("rejected") == "unbindable_sha"


def test_e4_ancestor_sha_binds_and_duplicate_counted_is_idempotent(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    sha1 = _commit(ws, "one", "c1")
    sha2 = _commit(ws, "two", "c2")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reviewed", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws))
        res = _filev(conn, tid, sha1)      # ancestor of HEAD binds (leg iii)
        assert res["counted"] is True
        assert _failures(conn, tid) == 1
        # Exact duplicate counted filing: idempotent, one increment total.
        res2 = _filev(conn, tid, sha1)
        assert res2["counted"] is False
        assert _failures(conn, tid) == 1
        # Concurrent duplicates of a FRESH (sha, reviewer): exactly one
        # increment from the pair (F-7/F-14).
        results = {}

        def _file(i):
            with kbc.connect() as c2:
                results[i] = ke.file_verdict(
                    c2, tid, source_path_or_payload=_verdict(tid, sha2, reviewer="rev-c"),
                    filed_by="rev-c", board_config=_board_cfg(),
                    escalation_enabled=True, threshold=2)

        threads = [threading.Thread(target=_file, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert _failures(conn, tid) == 2   # one increment from the pair


def test_e4_mutated_artifact_is_invalidated(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    sha = _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reviewed", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws))
        # Pre-place a DIFFERENT artifact at the deterministic verdict path.
        vdir = kb.task_attachments_dir(tid) / "verdicts"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / f"{sha}.rev-a.json").write_text('{"tampered": true}', encoding="utf-8")
        res = _filev(conn, tid, sha)
        assert res["counted"] is False
        assert res.get("rejected") == "artifact_mutated"
        assert _events(conn, tid, "verdict_invalidated")
        assert _failures(conn, tid) == 0


# --------------------------------------------------------------------------
# E-4b: state-anchored episode rule (rev-4 replacement)
# --------------------------------------------------------------------------

def test_e4b_state_anchored_sequence(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    head_h = _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        assert _failures(conn, tid) == 1
        # (i) verdict on H itself: not counted.
        res = _filev(conn, tid, head_h)
        assert res["counted"] is False
        assert _failures(conn, tid) == 1
        # (ii) N-1: the identical uncommitted tree committed as descendant C.
        sha_c = _commit(ws, "final-tree", "commit the exhausted tree")
        res = _filev(conn, tid, sha_c)
        assert res["counted"] is False
        assert res.get("reason") == "same_episode_state"
        assert _failures(conn, tid) == 1
        not_counted = [e for e in _events(conn, tid, "verdict_not_counted")
                       if e.payload.get("reason") == "same_episode_state"]
        assert not_counted
        # (iv) cherry-picked copy of the exhausted tree onto a descendant.
        # Filed while HEAD is the cherry-pick (binding leg iii needs the sha
        # reachable from the workspace HEAD).
        _git(ws, "checkout", "-b", "cp", head_h)
        # A distinct committer date guarantees a NEW sha for the same tree
        # (a same-second cherry-pick onto the same parent can be identical).
        subprocess.run(
            ["git", "-C", str(ws), "cherry-pick", sha_c], check=True,
            capture_output=True, text=True,
            env={**os.environ, "GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z"})
        sha_cp = _git(ws, "rev-parse", "HEAD").stdout.strip()
        assert sha_cp != sha_c
        res = _filev(conn, tid, sha_cp)
        assert res["counted"] is False
        _git(ws, "checkout", "main")
        # (iii) child commit with a DIFFERENT tree counts -> 2 + escalation.
        sha_c2 = _commit(ws, "changed-tree", "differing child")
        res = _filev(conn, tid, sha_c2)
        assert res["counted"] is True
        assert _failures(conn, tid) == 2
        assert _events(conn, tid, "escalation_triggered") or \
            _events(conn, tid, "escalation_unresolvable")


def test_e4b_ancestor_of_anchor_not_counted(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    sha_a0 = _commit(ws, "base", "root")
    (ws / "g.txt").write_text("g\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "second")
    head_h = _git(ws, "rev-parse", "HEAD").stdout.strip()
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        res = _filev(conn, tid, sha_a0)     # ancestor of anchor head H
        assert res["counted"] is False
        assert res.get("reason") == "not_descendant_of_anchor"
        # rev-3 cherry-pick case, kept: the exhausted tree cherry-picked onto
        # a NON-descendant base (A0) is not counted either.
        sha_c = _commit(ws, "final-tree", "exhausted tree committed")
        _git(ws, "checkout", "-b", "rebased", sha_a0)
        _git(ws, "cherry-pick", sha_c)
        sha_rb = _git(ws, "rev-parse", "HEAD").stdout.strip()
        res = _filev(conn, tid, sha_rb)
        assert res["counted"] is False
        assert _failures(conn, tid) == 1
        _git(ws, "checkout", "main")


# --------------------------------------------------------------------------
# E-4c: B+B pairs
# --------------------------------------------------------------------------

def test_e4c_bb_pairs_and_lag(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    sha_base = _commit(ws, "base", "init")
    sha_c1 = _commit(ws, "state-1", "c1")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reviewed", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws))
        res = _filev(conn, tid, sha_c1)
        assert res["counted"] is True
        # (i) different reviewer, empty child commit (same tree): not counted.
        _git(ws, "commit", "--allow-empty", "-m", "empty")
        sha_empty = _git(ws, "rev-parse", "HEAD").stdout.strip()
        res = _filev(conn, tid, sha_empty, reviewer="rev-b")
        assert res["counted"] is False
        assert res.get("reason") == "same_episode_state"
        # (ii) divergent branch (based on C1's PARENT, so not a descendant
        # of the anchor C1) with a different tree: not counted.
        _git(ws, "checkout", "-b", "div", sha_base)
        sha_div = _commit(ws, "divergent", "div")
        res = _filev(conn, tid, sha_div, reviewer="rev-b")
        assert res["counted"] is False
        assert res.get("reason") == "not_descendant_of_anchor"
        _git(ws, "checkout", "main")
        # (iii) descendant with a different tree counts.
        sha_c2 = _commit(ws, "state-2", "c2")
        res = _filev(conn, tid, sha_c2, reviewer="rev-b")
        assert res["counted"] is True
        assert _failures(conn, tid) == 2


def test_e4c_lag_case_both_count(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reviewed", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws))
        # One run produces both commits, then closes; verdicts filed after.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        sha_c1 = _commit(ws, "lag-1", "c1")
        sha_c2 = _commit(ws, "lag-2", "c2")
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_c1},
                         run_id=task.current_run_id)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_c2},
                         run_id=task.current_run_id)
        assert kb.block_task(conn, tid, reason="run closed") is True
        res1 = _filev(conn, tid, sha_c1)
        res2 = _filev(conn, tid, sha_c2, reviewer="rev-b")
        assert res1["counted"] is True
        assert res2["counted"] is True
        assert _failures(conn, tid) == 2


# --------------------------------------------------------------------------
# E-4d: A+A cycle-back still counts (kind A is never deduped)
# --------------------------------------------------------------------------

def test_e4d_aa_cycleback_counts(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("x1", "x2", "anchor-state"))
        assert _failures(conn, tid) == 1
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        # Trajectory S -> S1 -> S2 -> S: final state equals R1's anchor state.
        _exhaust(conn, tid, ws, states=("y1", "y2", "anchor-state"))
        assert _failures(conn, tid) == 2
        assert _events(conn, tid, "escalation_triggered") or \
            _events(conn, tid, "escalation_unresolvable")


# --------------------------------------------------------------------------
# E-4e: no retro-count
# --------------------------------------------------------------------------

def test_e4e_no_retro_count(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        sha_c = _commit(ws, "final-tree", "same tree committed")
        res = _filev(conn, tid, sha_c)
        assert res["counted"] is False
        # R2 produces a differing state and closes; the early verdict must
        # NOT retro-count.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        sha_c2 = _commit(ws, "differing", "c2")
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_c2},
                         run_id=task.current_run_id)
        assert kb.block_task(conn, tid, reason="closed") is True
        assert _failures(conn, tid) == 1
        assert not _events(conn, tid, "escalation_triggered")
        # Re-filed on C2: counts now.
        res = _filev(conn, tid, sha_c2)
        assert res["counted"] is True
        assert _failures(conn, tid) == 2


# --------------------------------------------------------------------------
# E-4f: unavailability classes (T4 F-4: P1-first outcome is dedup_unavailable)
# --------------------------------------------------------------------------

def test_e4f_missing_object_then_available(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    head_h = _commit(ws, "base", "init")
    # Build the reviewed commit in a CLONE so its object is absent from ws.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(ws), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "T")
    sha_x = _commit(clone, "reviewed-elsewhere", "x")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_x},
                         run_id=task.current_run_id)
        assert kb.block_task(conn, tid, reason="closed") is True
        res = _filev(conn, tid, sha_x)
        assert res["counted"] is False
        assert res.get("reason") == "dedup_unavailable"   # T4 F-4 (P1-first)
        assert _failures(conn, tid) == 1
        # Object becomes available; the SAME (sha, reviewer) re-files and counts.
        _git(ws, "fetch", "-q", str(clone), "main")
        res = _filev(conn, tid, sha_x)
        assert res["counted"] is True
        assert _failures(conn, tid) == 2


def test_e4f_git_error_is_dedup_unavailable(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    sha_c2 = _commit(ws, "differing", "c2")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        real_git = ke._git

        def stub_git(args, cwd, timeout=10):
            if "merge-base" in args:
                return subprocess.CompletedProcess(args, 2, "", "boom")
            return real_git(args, cwd, timeout=timeout)

        monkeypatch.setattr(ke, "_git", stub_git)
        res = _filev(conn, tid, sha_c2)
        assert res["counted"] is False
        assert res.get("reason") == "dedup_unavailable"
        assert _failures(conn, tid) == 1


def test_e4f_anchor_state_removed(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "f.txt").write_text("base", encoding="utf-8")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        # Simulate pre-rev-4 data: strip the fingerprint from the anchor's
        # gate_failed event payload.
        rows = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'gate_failed'",
            (tid,)).fetchall()
        with kb.write_txn(conn):
            for row in rows:
                payload = json.loads(row["payload"])
                payload.pop("fingerprint", None)
                conn.execute("UPDATE task_events SET payload = ? WHERE id = ?",
                             (json.dumps(payload), row["id"]))
        # Receipt-bound verdict from a later closed run.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        task = kb.get_task(conn, tid)
        sha_q = "cd" * 20
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_q},
                         run_id=task.current_run_id)
        assert kb.block_task(conn, tid, reason="closed") is True
        res = _filev(conn, tid, sha_q)
        assert res["counted"] is False
        assert res.get("reason") == "anchor_state_unavailable"


# --------------------------------------------------------------------------
# E-4g: weak anchor
# --------------------------------------------------------------------------

def test_e4g_weak_anchor(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        # Force weak fingerprints during the kind-A exhaustion (cap stub).
        monkeypatch.setattr(kg, "_MAX_FILES_STRONG", 0)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        monkeypatch.setattr(kg, "_MAX_FILES_STRONG", 10_000)
        # (i) leg-(iii)-only verdict on the identical-tree commit: unresolved.
        sha_c = _commit(ws, "final-tree", "same tree")
        res = _filev(conn, tid, sha_c)
        assert res["counted"] is False
        assert res.get("reason") == "weak_anchor_unresolved"
        # (ii) receipt-bound verdict from a later closed run whose recorded
        # state proxy differs from the anchor: counts.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        sha_z = _commit(ws, "differing-tree", "z")
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_z},
                         run_id=task.current_run_id)
        _attempt(conn, tid, ws, "differing-tree")   # records R2's proxy state
        assert kb.block_task(conn, tid, reason="closed") is True
        res = _filev(conn, tid, sha_z)
        assert res["counted"] is True
        assert _failures(conn, tid) == 2


# --------------------------------------------------------------------------
# E-4h: non-git workspaces (state-proxy path)
# --------------------------------------------------------------------------

def test_e4h_non_git_sequence(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "f.txt").write_text("base", encoding="utf-8")
    sha_q = "aa" * 20
    sha_q2 = "bb" * 20
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        # Receipt for a sha produced by R1 itself, appended during R1.
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_q},
                         run_id=task.current_run_id)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        assert _failures(conn, tid) == 1
        # (i) verdict produced by the SAME run: not after the last kind A.
        res = _filev(conn, tid, sha_q)
        assert res["counted"] is False
        assert res.get("reason") == "not_after_last_kind_a"
        # (ii) later closed run, state unchanged (proxy == anchor): not counted.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_q2},
                         run_id=task.current_run_id)
        _attempt(conn, tid, ws, "final-tree")       # proxy recorded, == anchor
        assert kb.block_task(conn, tid, reason="closed") is True
        res = _filev(conn, tid, sha_q2)
        assert res["counted"] is False
        assert res.get("reason") == "same_episode_state"


def test_e4h_non_git_differing_and_missing_proxy(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "f.txt").write_text("base", encoding="utf-8")
    sha_q3 = "cc" * 20
    sha_q4 = "dd" * 20
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "anchor-state"))
        # Later closed run with a DIFFERING recorded state: counts.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_q3},
                         run_id=task.current_run_id)
        _attempt(conn, tid, ws, "other-state")
        assert kb.block_task(conn, tid, reason="closed") is True
        res = _filev(conn, tid, sha_q3)
        assert res["counted"] is True
        assert _failures(conn, tid) == 2
        # Later closed run with NO fingerprint-bearing event: unavailable.
        assert kb.unblock_task(conn, tid) is True
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_q4},
                         run_id=task.current_run_id)
        assert kb.block_task(conn, tid, reason="closed") is True
        res = _filev(conn, tid, sha_q4)
        assert res["counted"] is False
        assert res.get("reason") == "reviewed_state_unavailable"


# --------------------------------------------------------------------------
# E-4i: anchor race — re-evaluation against the new anchor (F9)
# --------------------------------------------------------------------------

def _insert_anchor(conn, tid, state, head_sha, run_id=None):
    fp = {"manifest_hash": state, "head_sha": head_sha, "weak": False,
          "file_count": 1, "total_bytes": 1}
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "gate_failed",
                         {"spec_hash": "x", "classification": "worker",
                          "checks": [], "fingerprint": fp,
                          "check_written_paths": []}, run_id=run_id)
        ev_id = conn.execute("SELECT MAX(id) AS m FROM task_events").fetchone()["m"]
        kb._append_event(conn, tid, "gate_attempt_counted",
                         {"gate_failed_event_id": int(ev_id), "counted": True,
                          "exhausted": True, "attempt": 3}, run_id=run_id)


def test_e4i_anchor_race_reevaluates(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    head_h = _commit(ws, "base", "init")
    sha_c2 = _commit(ws, "differing", "c2")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        assert _failures(conn, tid) == 1
        real_eval = ke._evaluate_kind_b
        fired = {"n": 0}

        def trap_eval(c, task, anchor, commit_sha, workspace, produced_run, leg_i):
            # FIX-16: _evaluate_kind_b now returns a 5-tuple (the state
            # proxy travels separately from reviewed_fingerprint).
            counted, reason, reviewed_fp, pid, proxy_fp = real_eval(
                c, task, anchor, commit_sha, workspace, produced_run, leg_i)
            if fired["n"] == 0:
                fired["n"] += 1
                # A new kind-A anchor lands between evaluation and counting:
                # its state IS the reviewed state, so the new anchor's
                # predicates say do-not-count.
                _insert_anchor(c, tid, (reviewed_fp or {}).get("manifest_hash"),
                               commit_sha)
            return counted, reason, reviewed_fp, pid, proxy_fp

        monkeypatch.setattr(ke, "_evaluate_kind_b", trap_eval)
        res = _filev(conn, tid, sha_c2)
        assert res["counted"] is False


def test_e4i_three_anchor_changes_dedup_unavailable(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    head_h = _commit(ws, "base", "init")
    sha_c2 = _commit(ws, "differing", "c2")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "final-tree"))
        real_eval = ke._evaluate_kind_b
        counter = {"n": 0}

        def trap_eval(c, task, anchor, commit_sha, workspace, produced_run, leg_i):
            counter["n"] += 1
            _insert_anchor(c, tid, f"changing-{counter['n']}", head_h)
            return real_eval(c, task, anchor, commit_sha, workspace,
                             produced_run, leg_i)

        monkeypatch.setattr(ke, "_evaluate_kind_b", trap_eval)
        res = _filev(conn, tid, sha_c2)
        assert res["counted"] is False
        assert res.get("reason") == "dedup_unavailable"
        assert _failures(conn, tid) == 1


# --------------------------------------------------------------------------
# E-5: the counter resets only on completion
# --------------------------------------------------------------------------

def test_e5_reset_only_on_completion(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws)
        assert _failures(conn, tid) == 1
        # Block/unblock/manual edits do not reset.
        assert kb.block_task(conn, tid, reason="pause") is True
        assert _failures(conn, tid) == 1
        assert kb.unblock_task(conn, tid) is True
        assert _failures(conn, tid) == 1
        # Completion does.
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _cfg(kanban_home, "kanban:\n  gates:\n    enabled: false\n")
        task = kb.get_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done",
                                expected_run_id=task.current_run_id) is True
        assert _failures(conn, tid) == 0


# --------------------------------------------------------------------------
# E-6: spend accumulation + guard
# --------------------------------------------------------------------------

def _state_db(tmp_path, totals, session_id="sess-1"):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, "
        "billing_provider TEXT, input_tokens INTEGER, output_tokens INTEGER, "
        "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
        "reasoning_tokens INTEGER, task TEXT)")
    if totals is not None:
        conn.execute(
            "INSERT INTO session_model_usage VALUES (?, 'm', 'p', ?, ?, 0, 0, ?, '')",
            (session_id, totals, 0, 0))
    conn.commit()
    conn.close()
    return str(path)


SPEND_CFG = """kanban:
  escalation:
    enabled: true
    threshold: 2
    ceilings:
      default:
        max_total_tokens: 1000
        max_runs: 12
custom_providers:
  - name: antigravity-bridge
"""


def _closed_run_with_usage(conn, tid, db_path, session_id="sess-1",
                           model="gemini-4-argon", provider="custom:antigravity-bridge"):
    assert kb.claim_task(conn, tid, claimer="host:worker") is not None
    task = kb.get_task(conn, tid)
    kb._append_event(conn, tid, "owner_admitted",
                     {"db": db_path, "session_id": session_id,
                      "effective_model": model, "effective_provider": provider},
                     run_id=task.current_run_id)
    assert kb.block_task(conn, tid, reason="closed") is True
    # Revive for the next run: repeated blocks route to triage (block-loop
    # breaker), which unblock_task refuses — reset the card directly.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready', block_kind = NULL, "
            "block_recurrences = 0 WHERE id = ?", (tid,))
    return task.current_run_id


def test_e6_breach_at_exact_ceiling_and_exactly_once(kanban_home, tmp_path):
    _cfg(kanban_home, SPEND_CFG)
    db_path = _state_db(tmp_path, 1000)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spendy", assignee="w")
        _closed_run_with_usage(conn, tid, db_path)
        reason = ke.check_spend_guard(conn, tid)
        assert reason is not None and "1000 >= 1000" in reason
        task = kb.get_task(conn, tid)
        assert task.spend_tokens_total == 1000
        assert task.spend_runs_total == 1
        assert task.status in ("blocked", "triage")
        assert task.block_kind == "spend_ceiling"
        # Exactly-once: repeated accumulation changes nothing.
        ke.accumulate_ticket_usage(conn, tid)
        ke.accumulate_ticket_usage(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.spend_tokens_total == 1000 and task.spend_runs_total == 1
        rows = conn.execute("SELECT COUNT(*) AS n FROM task_run_usage WHERE task_id = ?",
                            (tid,)).fetchone()["n"]
        assert rows == 1


def test_e6_runs_ceiling_and_cheap_seat_not_guarded(kanban_home, tmp_path):
    _cfg(kanban_home, SPEND_CFG.replace("max_runs: 12", "max_runs: 2"))
    db_path = _state_db(tmp_path, 10)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="runsy", assignee="w")
        _closed_run_with_usage(conn, tid, db_path)
        _closed_run_with_usage(conn, tid, db_path)
        reason = ke.check_spend_guard(conn, tid)
        assert reason is not None and "runs" in reason
        # Cheap seat: never guarded, never accumulated by the guard.
        tid2 = kb.create_task(conn, title="cheap", assignee="w")
        _closed_run_with_usage(conn, tid2, db_path,
                               model="mtplx-bonsai-2-27b-optimized-speed",
                               provider="custom:bonsai-local")
        assert ke.check_spend_guard(conn, tid2) is None
        assert kb.get_task(conn, tid2).spend_tokens_total == 0


def test_e6_set_spend_ceiling_override(kanban_home, tmp_path):
    _cfg(kanban_home, SPEND_CFG)
    db_path = _state_db(tmp_path, 600)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spendy", assignee="w")
        _closed_run_with_usage(conn, tid, db_path)
        assert ke.check_spend_guard(conn, tid) is None     # 600 < 1000 default
        assert kb.set_spend_ceiling(conn, tid, tokens=500) is True
        assert _events(conn, tid, "spend_ceiling_set")
        reason = ke.check_spend_guard(conn, tid)
        assert reason is not None and "600 >= 500" in reason
        assert kb.set_spend_ceiling(conn, tid, tokens=None) is True
        assert kb.get_task(conn, tid).spend_ceiling_tokens is None
        assert kb.set_spend_ceiling(conn, "t_missing", tokens=1) is False


def test_e6c_concurrent_accumulation_exactly_once(kanban_home, tmp_path):
    _cfg(kanban_home, SPEND_CFG)
    db_path = _state_db(tmp_path, 700)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spendy", assignee="w")
        _closed_run_with_usage(conn, tid, db_path)
        errors = {}

        def _acc(i):
            with kbc.connect() as c2:
                try:
                    ke.accumulate_ticket_usage(c2, tid)
                except Exception as exc:  # noqa: BLE001
                    errors[i] = exc

        threads = [threading.Thread(target=_acc, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors
        task = kb.get_task(conn, tid)
        assert task.spend_tokens_total == 700 and task.spend_runs_total == 1


def test_e6a_pending_then_late_session_row_and_final(kanban_home, tmp_path):
    _cfg(kanban_home, SPEND_CFG)
    db_path = _state_db(tmp_path, None)       # DB exists, session row absent
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spendy", assignee="w")
        _closed_run_with_usage(conn, tid, db_path)
        ke.accumulate_ticket_usage(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.spend_tokens_total == 0 and task.spend_runs_total == 0
        assert _events(conn, tid, "spend_run_unaccounted")
        # The session row lands late: the next accumulation counts it fully.
        sconn = sqlite3.connect(db_path)
        sconn.execute(
            "INSERT INTO session_model_usage VALUES ('sess-1', 'm', 'p', 300, 0, 0, 0, 0, '')")
        sconn.commit()
        sconn.close()
        ke.accumulate_ticket_usage(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.spend_tokens_total == 300 and task.spend_runs_total == 1
        # A run closed > 24h ago with no usage path finalizes as unaccounted.
        rid = _closed_run_with_usage(conn, tid, "", session_id="")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET ended_at = ? WHERE id = ?",
                         (int(time.time()) - 90_000, rid))
        ke.accumulate_ticket_usage(conn, tid)
        row = conn.execute(
            "SELECT * FROM task_run_usage WHERE run_id = ?", (rid,)).fetchone()
        assert row is not None and row["unaccounted_final"] == 1
        assert kb.get_task(conn, tid).spend_runs_total == 2


def test_e6b_accumulate_inside_txn_raises(kanban_home, tmp_path):
    _cfg(kanban_home, SPEND_CFG)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="w")
        with kb.write_txn(conn):
            with pytest.raises(RuntimeError):
                ke.accumulate_ticket_usage(conn, tid)


def test_e7_features_work_without_sa_accept_mode(kanban_home, tmp_path):
    # No superagent config anywhere (accept_mode absent): verdict counting
    # and spend accumulation behave identically — the mechanisms never read it.
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    sha = _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reviewed", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws))
        res = _filev(conn, tid, sha)
        assert res["counted"] is True


def test_e8_no_new_block_task_call_sites(kanban_home):
    import hermes_cli.kanban_escalation as ke_mod
    src = Path(ke_mod.__file__).read_text(encoding="utf-8")
    assert "block_task(" not in src.replace("block_task_if_state(", "")
    import hermes_cli.kanban_gates as kg_mod
    src2 = Path(kg_mod.__file__).read_text(encoding="utf-8")
    assert "block_task(" not in src2


# --------------------------------------------------------------------------
# G-4b / G-4c: block-state snapshot + restoration around exhaustion
# --------------------------------------------------------------------------

def test_g4b_snapshot_carried_and_restored(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        # Give the card a pre-existing block history: recurrences = 1.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_recurrences = 1 WHERE id = ?", (tid,))
        _exhaust(conn, tid, ws)
        pending = _events(conn, tid, "gate_exhaustion_pending")
        assert pending
        assert pending[-1].payload["prev_block_recurrences"] == 1
        task = kb.get_task(conn, tid)
        assert task.block_recurrences == 1     # restored exactly, not incremented


def test_g4c_gave_up_restores_snapshot(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_recurrences = 1 WHERE id = ?", (tid,))
        _exhaust(conn, tid, ws)
        # Second exhaustion trips the unified failure budget (limit 2).
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _exhaust(conn, tid, ws, states=("t1", "t2", "t3"))
        assert _events(conn, tid, "gave_up")
        task = kb.get_task(conn, tid)
        assert task.block_recurrences == 1     # snapshot wins over _route_block


# --------------------------------------------------------------------------
# G-5b / G-5c: sticky gate blocks + hook parity
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# FIX round (T7): FIX-5, FIX-6 both-trip, FIX-7 parity, FIX-19 cross-check
# --------------------------------------------------------------------------

def test_fix6_both_trip_restores_snapshot_and_consumes_no_recurrence(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _admit(conn, tid, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid, ws)
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _admit(conn, tid, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid, ws, states=("t1", "t2", "t3"))
        assert _events(conn, tid, "escalation_triggered")
        # Revive WITHOUT resetting consecutive_failures (still at the limit,
        # so the next exhaustion trips the breaker again) and give the card
        # a non-trivial preserved snapshot: gate_env / 1 recurrence.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', block_kind = 'gate_env', "
                "block_recurrences = 1, current_run_id = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ?", (tid,))
        n_blocked = len(_events(conn, tid, "blocked")) + \
            len(_events(conn, tid, "block_loop_detected"))
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        _admit(conn, tid, "anthropic/claude-opus-5.5", "openrouter")
        _exhaust(conn, tid, ws, states=("u1", "u2", "u3"))
        task = kb.get_task(conn, tid)
        # Breaker park won: snapshot restored EXACTLY (kind + recurrences),
        # no recurrence consumed, no new blocked-family event, gave_up held.
        assert task.status == "blocked"
        assert task.block_kind == "gate_env"
        assert task.block_recurrences == 1
        assert len(_events(conn, tid, "blocked")) + \
            len(_events(conn, tid, "block_loop_detected")) == n_blocked
        assert _events(conn, tid, "gave_up")
        assert _failures(conn, tid) == 3


def test_fix5_disabled_verdict_records_uncounted_and_never_anchors(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    sha = _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reviewed", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws))
        # Escalation DISABLED at filing time: validation + artifact + event
        # still happen, but the event records counted: false (§5/FIX-5).
        res = ke.file_verdict(
            conn, tid, source_path_or_payload=_verdict(tid, sha),
            filed_by="rev-a", board_config=_board_cfg(),
            escalation_enabled=False, threshold=2)
        assert res["filed"] is True and res["counted"] is False
        assert _failures(conn, tid) == 0
        filed = _events(conn, tid, "verdict_filed")
        assert len(filed) == 1 and filed[0].payload.get("counted") is False
        # After enablement, the SAME (sha, reviewer) re-files and counts —
        # the disabled-era filing is not an anchor and does not dedup it.
        res2 = _filev(conn, tid, sha)
        assert res2["counted"] is True
        assert _failures(conn, tid) == 1
        filed = _events(conn, tid, "verdict_filed")
        assert filed[-1].payload.get("counted") is True


def test_fix7_failure_accounting_parity(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    with kbc.connect() as conn:
        def _mk_pair(max_retries=None, consec=0):
            a = kb.create_task(conn, title="orig", assignee="w", max_retries=max_retries)
            b = kb.create_task(conn, title="intxn", assignee="w", max_retries=max_retries)
            if consec:
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET consecutive_failures = ? WHERE id IN (?, ?)",
                                 (consec, a, b))
            return a, b

        def _gave_up(tid):
            evs = _events(conn, tid, "gave_up")
            return evs[-1].payload if evs else None

        # Trip case: identical rows + identical gave_up payloads (incl.
        # retry_status and the event run_id) through both paths.
        a, b = _mk_pair(consec=1)
        assert kbd._record_task_failure(conn, a, "boom", outcome="gate_failed") is True
        with kb.write_txn(conn):
            res = kbd._record_task_failure_in_txn(
                conn, b, "boom", outcome="gate_failed",
                retry_status="ready", run_id=None)
            # The in-txn caller owns the status write (gate transient shape).
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (b,))
        assert res["tripped"] is True
        ta, tb = kb.get_task(conn, a), kb.get_task(conn, b)
        assert ta.consecutive_failures == tb.consecutive_failures == 2
        assert ta.last_failure_error == tb.last_failure_error == "boom"
        assert _gave_up(a) == _gave_up(b)
        assert _gave_up(a)["retry_status"] == "ready"
        ev_a = _events(conn, a, "gave_up")[-1]
        ev_b = _events(conn, b, "gave_up")[-1]
        assert ev_a.run_id == ev_b.run_id

        # Non-trip case: identical counter movement, no gave_up either side.
        c, d = _mk_pair(max_retries=99)
        assert kbd._record_task_failure(conn, c, "meh", outcome="timeout") is False
        with kb.write_txn(conn):
            res = kbd._record_task_failure_in_txn(
                conn, d, "meh", outcome="timeout", retry_status="ready", run_id=None)
        assert res["tripped"] is False
        assert kb.get_task(conn, c).consecutive_failures == \
            kb.get_task(conn, d).consecutive_failures == 1
        assert not _events(conn, c, "gave_up") and not _events(conn, d, "gave_up")

        # Infrastructure: neither path increments or trips.
        e, f = _mk_pair()
        assert kbd._record_task_failure(conn, e, "infra", outcome="spawn_failed",
                                        infrastructure=True) is False
        with kb.write_txn(conn):
            res = kbd._record_task_failure_in_txn(
                conn, f, "infra", outcome="spawn_failed", infrastructure=True)
        assert res["tripped"] is False
        assert kb.get_task(conn, e).consecutive_failures == \
            kb.get_task(conn, f).consecutive_failures == 0

        # None error: identical (error or "") handling — neither path raises.
        g, h = _mk_pair(max_retries=99)
        assert kbd._record_task_failure(conn, g, None, outcome="timeout") is False
        with kb.write_txn(conn):
            kbd._record_task_failure_in_txn(conn, h, None, outcome="timeout")


def test_fix19_shared_pairing_agrees_with_anchor(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        run_id = kb.get_task(conn, tid).current_run_id
        _exhaust(conn, tid, ws)
        # kanban_db's counted-attempt reader and the shared pairing agree.
        counted, _last = kb._counted_gate_attempts(conn, tid, run_id)
        decisions = ke.gate_attempt_decisions(conn, tid)
        failed = _events(conn, tid, "gate_failed")
        shared_counted = sum(
            1 for e in failed
            if (decisions.get(e.id) or {}).get("counted") is True)
        assert counted == shared_counted == 3
        # The anchor is the exhausting gate_failed event, via the same map.
        anchor = ke._current_anchor(conn, tid)
        assert anchor is not None and anchor["kind"] == "gate"
        assert anchor["event_id"] == failed[-1].id


# --------------------------------------------------------------------------
# FIX-10: G-5b / G-5c made real (production Shape A/B paths, all hook sites)
# --------------------------------------------------------------------------

def test_g5b_gate_blocks_survive_dispatch_ticks(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        # (1) gate_env limit block via the production env sequence.
        tid_env = _mk_gated(conn, ws, argv=["/nonexistent/no-binary"])
        for cycle in range(3):
            task = kb.get_task(conn, tid_env)
            with pytest.raises(kb.GateEnvError):
                kb.complete_task(conn, tid_env, summary="x",
                                 expected_run_id=task.current_run_id)
            if cycle < 2:
                assert kb.claim_task(conn, tid_env, claimer="host:worker") is not None
        t = kb.get_task(conn, tid_env)
        assert t.status == "blocked" and t.block_kind == "gate_env"
        assert _events(conn, tid_env, "blocked")
        # (2) post-escalation gate_worker block via the gate Shape A path
        # (breaker disarmed with a high per-task limit so the already-
        # escalated outcome — not the breaker — produces the block).
        tid_gw = kb.create_task(conn, title="gw", assignee="worker",
                                workspace_kind="dir", workspace_path=str(ws),
                                gate_spec=_spec(), max_retries=99)
        assert kb.claim_task(conn, tid_gw, claimer="host:worker") is not None
        _admit(conn, tid_gw, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid_gw, ws)
        assert kb.claim_task(conn, tid_gw, claimer="host:worker") is not None
        _admit(conn, tid_gw, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid_gw, ws, states=("t1", "t2", "t3"))
        assert _events(conn, tid_gw, "escalation_triggered")
        assert kb.claim_task(conn, tid_gw, claimer="host:worker") is not None
        _admit(conn, tid_gw, "anthropic/claude-opus-5.5", "openrouter")
        _exhaust(conn, tid_gw, ws, states=("u1", "u2", "u3"))
        t = kb.get_task(conn, tid_gw)
        assert t.status in ("blocked", "triage") and t.block_kind == "gate_worker"
        assert _events(conn, tid_gw, "blocked") or _events(conn, tid_gw, "block_loop_detected")
        # (3) spend_ceiling block via the guard's Shape B path (E-6 setup).
        _cfg(kanban_home, SPEND_CFG)
        db_path = _state_db(tmp_path, 1000)
        tid_sp = kb.create_task(conn, title="spendy", assignee="w")
        _closed_run_with_usage(conn, tid_sp, db_path)
        assert ke.check_spend_guard(conn, tid_sp) is not None
        t = kb.get_task(conn, tid_sp)
        assert t.status in ("blocked", "triage") and t.block_kind == "spend_ceiling"
        # All three survive 3 dispatcher ticks with kind unchanged.
        for tid, kind in ((tid_env, "gate_env"), (tid_gw, "gate_worker"),
                          (tid_sp, "spend_ceiling")):
            for _ in range(3):
                kbd.dispatch_once(conn)
            t = kb.get_task(conn, tid)
            assert t.status in ("blocked", "triage"), kind
            assert t.block_kind == kind, kind


def test_g5c_hook_parity(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    fired = []

    def recorder(event, task, task_id, run_id, **fields):
        if event == "kanban_task_blocked":
            fired.append(task_id)

    monkeypatch.setattr(kb, "_fire_task_hook", recorder)
    with kbc.connect() as conn:
        # Site 1: Shape A gate_env (env path).
        tid1 = _mk_gated(conn, ws, argv=["/nonexistent/no-binary"])
        for cycle in range(3):
            task = kb.get_task(conn, tid1)
            with pytest.raises(kb.GateEnvError):
                kb.complete_task(conn, tid1, summary="x",
                                 expected_run_id=task.current_run_id)
            if cycle < 2:
                assert kb.claim_task(conn, tid1, claimer="host:worker") is not None
        assert fired.count(tid1) == 1
        # Site 2: Shape A post-escalation gate_worker (gate path).
        tid2 = kb.create_task(conn, title="gw", assignee="worker",
                              workspace_kind="dir", workspace_path=str(ws),
                              gate_spec=_spec(), max_retries=99)
        assert kb.claim_task(conn, tid2, claimer="host:worker") is not None
        _admit(conn, tid2, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid2, ws)
        assert kb.claim_task(conn, tid2, claimer="host:worker") is not None
        _admit(conn, tid2, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid2, ws, states=("t1", "t2", "t3"))
        assert kb.claim_task(conn, tid2, claimer="host:worker") is not None
        _admit(conn, tid2, "anthropic/claude-opus-5.5", "openrouter")
        _exhaust(conn, tid2, ws, states=("u1", "u2", "u3"))
        assert kb.get_task(conn, tid2).block_kind == "gate_worker"
        assert fired.count(tid2) == 1
        # Site 3: Shape B spend_ceiling (guard breach path).
        _cfg(kanban_home, SPEND_CFG)
        db_path = _state_db(tmp_path, 1000, session_id="sess-g5c")
        tid3 = kb.create_task(conn, title="spendy", assignee="w")
        _closed_run_with_usage(conn, tid3, db_path, session_id="sess-g5c")
        pre3 = fired.count(tid3)   # the setup block_task firing is not ours
        assert ke.check_spend_guard(conn, tid3) is not None
        assert kb.get_task(conn, tid3).block_kind == "spend_ceiling"
        assert fired.count(tid3) == pre3 + 1
        # Site 4: Shape B post-escalation gate_worker (verdict path).
        _cfg(kanban_home, FULL_CFG)
        tid4 = kb.create_task(conn, title="gw2", assignee="worker",
                              workspace_kind="dir", workspace_path=str(ws),
                              gate_spec=_spec(), max_retries=99)
        assert kb.claim_task(conn, tid4, claimer="host:worker") is not None
        _admit(conn, tid4, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid4, ws, states=("v1", "v2", "v3"))
        assert kb.claim_task(conn, tid4, claimer="host:worker") is not None
        _admit(conn, tid4, "gemini-4-argon", "custom:antigravity-bridge")
        _exhaust(conn, tid4, ws, states=("w1", "w2", "w3"))
        assert _events(conn, tid4, "escalation_triggered")
        # A frontier run starts after the escalation (arms the flag), then
        # closes; a counted verdict on its differing descendant tree is the
        # post-escalation structured failure -> Shape B gate_worker block.
        assert kb.claim_task(conn, tid4, claimer="host:worker") is not None
        _admit(conn, tid4, "anthropic/claude-opus-5.5", "openrouter")
        sha2 = _commit(ws, "frontier-fix", "c-frontier")
        task4 = kb.get_task(conn, tid4)
        kb._append_event(conn, tid4, "pr_acceptance", {"head_sha": sha2},
                         run_id=task4.current_run_id)
        assert kb.block_task(conn, tid4, reason="pause") is True
        pre4 = fired.count(tid4)   # the pause block's own firing is not ours
        res = _filev(conn, tid4, sha2, reviewer="rev-z")
        assert res["counted"] is True
        t4 = kb.get_task(conn, tid4)
        assert t4.block_kind == "gate_worker" and t4.status in ("blocked", "triage")
        assert fired.count(tid4) == pre4 + 1


def test_fix16_p3_event_records_proxy_with_provenance(kanban_home, tmp_path):
    _cfg(kanban_home, FULL_CFG)
    ws = tmp_path / "plain16"
    ws.mkdir()
    (ws / "f.txt").write_text("base", encoding="utf-8")
    sha_q = "ee" * 20
    with kbc.connect() as conn:
        tid = _mk_gated(conn, ws)
        _exhaust(conn, tid, ws, states=("s1", "s2", "anchor-state"))
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        task = kb.get_task(conn, tid)
        run2 = task.current_run_id
        kb._append_event(conn, tid, "pr_acceptance", {"head_sha": sha_q},
                         run_id=run2)
        _attempt(conn, tid, ws, "other-state")
        assert kb.block_task(conn, tid, reason="closed") is True
        res = _filev(conn, tid, sha_q)
        assert res["counted"] is True
        filed = [e for e in _events(conn, tid, "verdict_filed")
                 if e.payload.get("counted") is True]
        assert len(filed) == 1
        payload = filed[0].payload
        # FIX-16: reviewed_fingerprint is ONLY the step-6a fingerprint of
        # the reviewed state — on the P3 path it is null, and the state
        # proxy that actually decided the count travels in its own fields
        # with its provenance (the produced-by run).
        assert payload.get("reviewed_fingerprint") is None
        assert isinstance(payload.get("state_proxy_fingerprint"), dict)
        assert payload.get("state_proxy_run_id") == run2
        assert payload.get("produced_by_run_id") == run2

def test_fix4_escalation_consumer_surfaces_only_its_section(kanban_home, tmp_path):
    # Escalation raw-enabled but its section is invalid: the spend-guard
    # consumer surfaces exactly its own section's error (once) and the
    # feature is disabled by the error (guard returns None, no block).
    _cfg(kanban_home, """kanban:
  escalation:
    enabled: true
    bogus_fix4_esc: 1
  assignment:
    bogus_fix4_other: 1
""")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="guarded", assignee="w")
        assert ke.check_spend_guard(conn, tid) is None
        invalid = _events(conn, tid, "board_config_invalid")
        assert len(invalid) == 1
        assert invalid[0].payload.get("section") == "escalation"
        assert invalid[0].payload.get("key") == "bogus_fix4_esc"
        assert kb.get_task(conn, tid).block_kind is None


# ===========================================================================
# X-11 / X-14 — cross-run mint as a Rule D anchor; spend-guard subject
# (AMENDMENT-DESIGN-REV2 §A6.2 / §A6.4).  Same production-binding fixture
# rule as the X series in test_kanban_gates.py: create_task -> claim_task ->
# one complete_task per run -> detect_crashed_workers.
# ===========================================================================

from hermes_cli import kanban_db_dispatch as _kbd_x


@pytest.fixture
def x_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _x_refused_run(conn, tid):
    assert kb.claim_task(conn, tid) is not None
    run_id = kb.get_task(conn, tid).current_run_id
    with pytest.raises(kb.GateFailedError) as ei:
        kb.complete_task(conn, tid, summary="try", expected_run_id=run_id)
    assert ei.value.exhausted is False
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _kbd_x._record_worker_exit(proc.pid, 0)
    _kbd_x._set_worker_pid(conn, tid, proc.pid)
    crashed = _kbd_x.detect_crashed_workers(conn)
    assert tid in crashed
    return run_id


def test_x11_cross_run_mint_is_rule_d_anchor(x_home, tmp_path):
    _cfg(x_home, FULL_CFG)
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gated", assignee="worker",
                             workspace_kind="dir", workspace_path=str(ws),
                             gate_spec=_spec())
        for _ in range(3):
            _x_refused_run(conn, tid)
        assert _failures(conn, tid) == 1
        mint = _events(conn, tid, "gate_cross_run_exhaustion")
        assert len(mint) == 1
        mint_id = mint[0].id
        # Verdict on the identical tree committed as a descendant: NOT
        # counted — the cross-run mint anchors the episode (N-1 stays
        # closed through the new mint path).
        _git(ws, "commit", "--allow-empty", "-m", "same tree, descendant commit")
        sha_c = _git(ws, "rev-parse", "HEAD").stdout.strip()
        res = _filev(conn, tid, sha_c)
        assert res["counted"] is False
        assert res.get("reason") == "same_episode_state"
        assert _failures(conn, tid) == 1
        filed = _events(conn, tid, "verdict_filed")
        assert filed and filed[-1].payload["anchor_event_id"] == mint_id
        not_counted = [e for e in _events(conn, tid, "verdict_not_counted")
                       if e.payload.get("reason") == "same_episode_state"]
        assert not_counted
        # A differing descendant counts and escalates.
        sha_c2 = _commit(ws, "changed-tree", "differing child")
        res = _filev(conn, tid, sha_c2)
        assert res["counted"] is True
        assert _failures(conn, tid) == 2
        assert _events(conn, tid, "escalation_triggered") or \
            _events(conn, tid, "escalation_unresolvable")


def test_x14_spend_guard_subject_after_cross_run_escalation(x_home, tmp_path):
    _cfg(x_home, """kanban:
  gates:
    enabled: true
    max_attempts_per_run: 3
  assignment:
    enabled: true
    frontier_primary: argon
    seats:
      argon:
        model: "x-argon-model"
        provider: "openrouter"
      opus:
        model: "x-opus-model"
        provider: "anthropic"
  escalation:
    enabled: true
    threshold: 2
    ceilings:
      default:
        max_total_tokens: 2000000
        max_runs: 12
""")
    ws = _repo(tmp_path)
    _commit(ws, "base", "init")
    db_path = _state_db(tmp_path, 500)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gated", assignee="worker",
                             workspace_kind="dir", workspace_path=str(ws),
                             gate_spec=_spec())
        for _ in range(6):
            _x_refused_run(conn, tid)
        assert _failures(conn, tid) == 2
        assert _events(conn, tid, "escalation_triggered")
        # One frontier run to closure, with its session usage recorded.
        assert kb.claim_task(conn, tid) is not None
        frontier_run = kb.get_task(conn, tid).current_run_id
        _admit(conn, tid, "x-argon-model", "openrouter",
               session_id="sess-1", db=db_path)
        assert kb.block_task(conn, tid, reason="frontier run closed") is True
        # In scope (escalation leg): the guard accumulates the closed
        # frontier run into task_run_usage.
        reason = ke.check_spend_guard(conn, tid)
        assert reason is None  # 500 tokens / few runs: no breach asserted
        rows = conn.execute(
            "SELECT run_id FROM task_run_usage WHERE task_id = ?", (tid,)
        ).fetchall()
        assert [int(r["run_id"]) for r in rows] == [frontier_run]
        assert kb.get_task(conn, tid).spend_tokens_total == 500

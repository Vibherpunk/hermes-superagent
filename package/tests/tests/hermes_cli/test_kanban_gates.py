"""Tests for enforced completion gates (DESIGN rev 4 §2, test plan G series).

Covers G-1, G-1b, G-2, G-3, G-3b, G-3c, G-3d (+ T4 F-1 variant), G-4, G-4b,
G-4c, G-5, G-5b, G-5c, G-6, G-7, G-7b, G-7c, G-8, G-8b, G-8c, G-9, G-10,
G-11, G-12, G-13.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_gates as kg


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _cfg(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


GATES_ON = "kanban:\n  gates:\n    enabled: true\n    max_attempts_per_run: 3\n"
GATES_ON_5 = "kanban:\n  gates:\n    enabled: true\n    max_attempts_per_run: 5\n"

PASS = [sys.executable, "-c", "pass"]
FAIL = [sys.executable, "-c", "import sys; sys.exit(3)"]


def _spec(checks, **kw):
    spec = {"version": 1, "checks": checks}
    spec.update(kw)
    return spec


def _check(cid, argv, **kw):
    c = {"id": cid, "argv": argv}
    c.update(kw)
    return c


def _ws(tmp_path, name="ws"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "code.py").write_text("x = 1\n", encoding="utf-8")
    return d


def _mk(conn, ws, spec=None, assignee="worker", claim=True):
    tid = kb.create_task(
        conn, title="gated", assignee=assignee,
        workspace_kind="dir", workspace_path=str(ws), gate_spec=spec,
    )
    if claim:
        claimed = kb.claim_task(conn, tid, claimer="host:worker")
        assert claimed is not None
    return tid


def _run_id(conn, tid):
    return kb.get_task(conn, tid).current_run_id


def _events(conn, tid, kind=None):
    evs = kb.list_events(conn, tid)
    return [e for e in evs if kind is None or e.kind == kind]


def _complete(conn, tid, **kw):
    kw.setdefault("summary", "done")
    return kb.complete_task(conn, tid, expected_run_id=_run_id(conn, tid), **kw)


# --------------------------------------------------------------------------
# G-1 / G-1b: spec validation + storage
# --------------------------------------------------------------------------

def test_g1_spec_validation_rejects_bad_specs(kanban_home, tmp_path):
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        bad_specs = [
            "echo hi",                                            # not an object
            {"version": 1, "checks": []},                         # zero checks
            {"version": 1, "checks": [_check(f"c{i}", PASS) for i in range(9)]},  # nine
            {"version": 1, "checks": [_check("Bad Id!", PASS)]},  # bad id
            {"version": 1, "checks": [_check("a", PASS, timeout_seconds=0)]},
            {"version": 1, "checks": [_check("a", PASS, timeout_seconds=1801)]},
            {"version": 1, "checks": [_check("a", "echo hi")]},             # shell string argv
            {"version": 1, "checks": [_check("a", PASS)], "bogus": 1},      # unknown top key
            {"version": 1, "checks": [_check("a", PASS, shell=True)]},      # unknown check key
            {"version": 2, "checks": [_check("a", PASS)]},                  # bad version
            {"version": 1, "checks": [_check("a", PASS, timeout_seconds=1800),
                                      _check("b", PASS, timeout_seconds=1800),
                                      _check("c", PASS, timeout_seconds=1)]},  # sum > 3600
            "null", "", "   ", [], {},
        ]
        for bad in bad_specs:
            with pytest.raises(ValueError):
                kb.create_task(conn, title="x", assignee="w", gate_spec=bad,
                               workspace_kind="dir", workspace_path=str(ws))
        # Degenerate forms all raise (never silently stored as no-spec).
        for degenerate in ("null", "", [], {}):
            with pytest.raises(ValueError):
                kg.validate_gate_spec(degenerate)


def test_g1_canonical_storage_materializes_defaults(kanban_home, tmp_path):
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws),
                             gate_spec=_spec([_check("a", PASS)]))
        stored = json.loads(kb.get_task(conn, tid).gate_spec)
        assert stored["checks"][0]["timeout_seconds"] == 300
        assert stored["fingerprint_exclude"] == []
        assert kg.spec_hash(kb.get_task(conn, tid).gate_spec)


def test_g1b_edit_task_gate_spec_and_no_sentinel(kanban_home, tmp_path):
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="w",
                             workspace_kind="dir", workspace_path=str(ws))
        with pytest.raises(ValueError):
            kb.edit_task(conn, tid, gate_spec="{not json")
        with pytest.raises(ValueError):
            kb.edit_task(conn, tid, gate_spec=_spec([_check("a", ["rm -rf /"])]))
        assert kb.edit_task(conn, tid, gate_spec=_spec([_check("a", PASS)])) is True
        assert json.loads(kb.get_task(conn, tid).gate_spec)["checks"][0]["id"] == "a"
        # FIX-9: the production sentinel is GONE — edit_task must validate
        # the old sentinel string like any other invalid spec.
        with pytest.raises(ValueError):
            kb.edit_task(conn, tid, gate_spec="not-a-gate-spec")
        assert not hasattr(kb, "GATE_SPEC_CORRUPT_SENTINEL")
        # A corrupt stored spec (for the corrupt-at-completion path) is
        # written via direct SQL in the fixture, never through edit_task.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET gate_spec = ? WHERE id = ?",
                         ("not-a-gate-spec", tid))
        assert kb.get_task(conn, tid).gate_spec == "not-a-gate-spec"
        with pytest.raises(ValueError):
            kb.edit_task(conn, tid, work_class="bogus-class")


# --------------------------------------------------------------------------
# G-2: pass path + verification chain (§2.5)
# --------------------------------------------------------------------------

def test_g2_pass_completes_with_verification_chain(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    marker = ws / "ran.txt"
    writer = [sys.executable, "-c", "open('ran.txt','w').write('ok')"]
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("w", writer), _check("p", PASS)]))
        assert _complete(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        passed = _events(conn, tid, "gate_passed")
        assert len(passed) == 1
        payload = passed[0].payload
        assert payload["checks"][0]["exit_code"] == 0
        assert payload["checks"][1]["exit_code"] == 0
        # The recorded fingerprint verifies against a fresh recomputation.
        fresh = kg.workspace_fingerprint(
            ws, exclude_roots=[kb.task_attachments_dir(tid)],
            fingerprint_exclude=[])
        assert kg.fingerprints_equal(payload["fingerprint"], fresh)
        # Main-txn stage-2 verified the same fingerprint into the completed event.
        completed = _events(conn, tid, "completed")
        assert completed[-1].payload["gate"]["verdict"] == "pass"
        assert kg.fingerprints_equal(completed[-1].payload["gate"]["fingerprint"], fresh)
        run = kb.list_runs(conn, tid)[-1]
        assert run.metadata["gate"]["verdict"] == "pass"
        assert marker.exists()


# --------------------------------------------------------------------------
# G-3 / G-3b / G-3c: counting, no-change, content edits
# --------------------------------------------------------------------------

def test_g3_exhaustion_after_max_counted_attempts(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        for attempt in (1, 2):
            with pytest.raises(kb.GateFailedError) as ei:
                _complete(conn, tid)
            assert ei.value.exhausted is False
            # Worker changes state between attempts so each counts.
            (ws / "code.py").write_text(f"x = {attempt + 1}\n", encoding="utf-8")
        with pytest.raises(kb.GateFailedError) as ei:
            _complete(conn, tid)
        assert ei.value.exhausted is True
        counted = [e.payload for e in _events(conn, tid, "gate_attempt_counted")]
        assert [c["counted"] for c in counted] == [True, True, True]
        assert counted[-1]["exhausted"] is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"           # re-queued (no failure trip yet)
        assert task.current_run_id is None      # run closed by exhaustion
        assert task.consecutive_failures == 1   # one structured failure recorded
        run = kb.list_runs(conn, tid)[-1]
        assert run.outcome == "gate_failed"


def test_g3b_no_change_repeats_never_exhaust(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        for _ in range(5):
            with pytest.raises(kb.GateFailedError) as ei:
                _complete(conn, tid)
            assert ei.value.exhausted is False
        counted = [e.payload for e in _events(conn, tid, "gate_attempt_counted")]
        assert counted[0]["counted"] is True
        assert all(c["counted"] is False and c["no_change_retry"] is True for c in counted[1:])
        task = kb.get_task(conn, tid)
        assert task.status == "running"         # run stays open for fix-and-retry
        assert task.consecutive_failures == 0
        assert not _events(conn, tid, "gate_exhaustion_pending")


def test_g3c_untracked_and_ignored_edits_count_as_change(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON_5)
    ws = _ws(tmp_path)
    subprocess.run(["git", "init", "-b", "main", str(ws)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "T"], check=True, capture_output=True)
    (ws / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-m", "init"], check=True, capture_output=True)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)
        (ws / "untracked.txt").write_text("new\n", encoding="utf-8")   # untracked edit
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)
        (ws / "ignored.log").write_text("log\n", encoding="utf-8")     # ignored edit
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)
        counted = [e.payload for e in _events(conn, tid, "gate_attempt_counted")]
        assert [c["counted"] for c in counted] == [True, True, True]


# --------------------------------------------------------------------------
# G-3d + F-1: check-written outputs (T4-CLOSEOUT F-1)
# --------------------------------------------------------------------------

_VARYING_WRITER = (
    "import sys, time;"
    "open('junit.xml','w').write('<x>%s</x>' % time.time_ns());"
    "sys.exit(1)"
)


def test_g3d_f1_check_written_output_no_exclude_never_exhausts(kanban_home, tmp_path):
    """F-1 variant: run-varying check output, NO fingerprint_exclude — three
    verbatim repeats must NOT exhaust (check-written paths are excluded from
    the fix-attempt comparison)."""
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    argv = [sys.executable, "-c", _VARYING_WRITER]
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("tests", argv)]))
        for _ in range(3):
            with pytest.raises(kb.GateFailedError) as ei:
                _complete(conn, tid)
            assert ei.value.exhausted is False
        counted = [e.payload for e in _events(conn, tid, "gate_attempt_counted")]
        assert counted[0]["counted"] is True
        assert all(c["counted"] is False for c in counted[1:])
        failed = _events(conn, tid, "gate_failed")
        assert "junit.xml" in (failed[0].payload.get("check_written_paths") or [])


def test_g3d_fingerprint_exclude_glob_also_no_change(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    argv = [sys.executable, "-c", _VARYING_WRITER]
    spec = _spec([_check("tests", argv)], fingerprint_exclude=["junit.xml"])
    with kbc.connect() as conn:
        tid = _mk(conn, ws, spec)
        for _ in range(3):
            with pytest.raises(kb.GateFailedError) as ei:
                _complete(conn, tid)
            assert ei.value.exhausted is False


def test_g3d_real_edit_still_counts_despite_check_output(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON_5)
    ws = _ws(tmp_path)
    argv = [sys.executable, "-c", _VARYING_WRITER]
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("tests", argv)]))
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)
        (ws / "code.py").write_text("x = 2\n", encoding="utf-8")   # real fix attempt
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)
        counted = [e.payload for e in _events(conn, tid, "gate_attempt_counted")]
        assert [c["counted"] for c in counted] == [True, True]


def test_g3d_fingerprint_exclude_validation(kanban_home):
    base = [_check("a", PASS)]
    for bad_globs in (["/abs/path"], ["../up"], [""], ["ok"] * 33):
        with pytest.raises(ValueError):
            kg.validate_gate_spec(_spec(base, fingerprint_exclude=bad_globs))
    with pytest.raises(ValueError):
        kg.validate_gate_spec(_spec([_check("a", PASS, fingerprint_exclude=["x"])]))
    ok = kg.validate_gate_spec(_spec(base, fingerprint_exclude=["build/**", "*.xml"]))
    assert json.loads(ok)["fingerprint_exclude"] == ["build/**", "*.xml"]


# --------------------------------------------------------------------------
# G-5: environment failures
# --------------------------------------------------------------------------

def test_g5_command_missing_is_env_and_never_counts(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("x", ["/nonexistent/hermes-no-such-binary"])]))
        with pytest.raises(kb.GateEnvError) as ei:
            _complete(conn, tid)
        assert ei.value.env_reason == "command_missing"
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0
        assert task.structured_failures == 0
        assert not _events(conn, tid, "gate_attempt_counted")
        env = _events(conn, tid, "gate_env_failed")
        assert env and env[-1].payload["env_reason"] == "command_missing"


def test_g5_three_consecutive_env_failures_block_gate_env(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("x", ["/nonexistent/hermes-no-such-binary"])]))
        for cycle in range(3):
            with pytest.raises(kb.GateEnvError):
                _complete(conn, tid)
            task = kb.get_task(conn, tid)
            if cycle < 2:
                assert task.status == "ready"
                assert kb.claim_task(conn, tid, claimer="host:worker") is not None
            else:
                assert task.status in ("blocked", "triage")
                assert task.block_kind == "gate_env"


def test_g5_mid_gate_workspace_deletion(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    deleter = [sys.executable, "-c",
               "import os, shutil; shutil.rmtree(os.getcwd())"]
    marker = tmp_path / "second-ran.txt"
    second = [sys.executable, "-c", f"open({str(marker)!r},'w').write('x')"]
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("del", deleter), _check("second", second)]))
        with pytest.raises(kb.GateEnvError) as ei:
            _complete(conn, tid)
        assert ei.value.env_reason == "workspace_missing"
        assert not marker.exists()      # second check never spawned


# --------------------------------------------------------------------------
# G-6: timeout is a worker failure
# --------------------------------------------------------------------------

def test_g6_timeout_is_worker_failure(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("slow", slow, timeout_seconds=1)]))
        with pytest.raises(kb.GateFailedError) as ei:
            _complete(conn, tid)
        assert ei.value.exhausted is False
        failed = _events(conn, tid, "gate_failed")
        assert failed and failed[-1].payload["classification"] == "worker"
        rec = failed[-1].payload["checks"][0]
        assert rec.get("timed_out") is True or rec.get("exit_code") is None


# --------------------------------------------------------------------------
# G-7 / G-7b / G-7c: lock discipline + lease
# --------------------------------------------------------------------------

def test_g7_check_may_write_board_db_and_concurrent_txn_ok(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    db_path = str(kb.kanban_home() / "kanban.db")
    script = (
        "import sqlite3, sys;"
        "c = sqlite3.connect(sys.argv[1], timeout=30);"
        "c.execute('CREATE TABLE IF NOT EXISTS gate_probe (x INTEGER)');"
        "c.execute('INSERT INTO gate_probe VALUES (1)'); c.commit(); c.close()"
    )
    slow = [sys.executable, "-c", "import time; time.sleep(2)"]
    writer = [sys.executable, "-c", script, db_path]
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("w", writer), _check("s", slow)]))
        rid = _run_id(conn, tid)
        outcome: dict = {}

        def _run():
            with kbc.connect() as c2:
                try:
                    outcome["ok"] = kb.complete_task(
                        c2, tid, summary="done", expected_run_id=rid)
                except Exception as exc:  # noqa: BLE001
                    outcome["err"] = exc

        th = threading.Thread(target=_run)
        th.start()
        time.sleep(0.7)
        # A concurrent write txn during the slow check must not deadlock.
        with kbc.connect() as c3:
            with kb.write_txn(c3):
                kb._append_event(c3, tid, "comment", {"body": "concurrent"})
        th.join(timeout=30)
        assert outcome.get("ok") is True, outcome
        probe = conn.execute("SELECT COUNT(*) AS n FROM gate_probe").fetchone()["n"]
        assert probe == 1


def test_g7b_concurrent_completion_single_evaluation(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    slow_fail = [sys.executable, "-c", "import time, sys; time.sleep(2); sys.exit(1)"]
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", slow_fail)]))
        rid = _run_id(conn, tid)
        results: dict = {}

        def _run(name):
            with kbc.connect() as c2:
                try:
                    results[name] = ("ok", kb.complete_task(
                        c2, tid, summary="done", expected_run_id=rid))
                except Exception as exc:  # noqa: BLE001
                    results[name] = ("err", type(exc).__name__, str(exc))

        threads = [threading.Thread(target=_run, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        counted = [e.payload for e in _events(conn, tid, "gate_attempt_counted")]
        assert len([c for c in counted if c["counted"]]) <= 1
        assert len(_events(conn, tid, "gate_failed")) <= 2


def test_g7c_lease_ttl_and_lease_lost(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    slow = [sys.executable, "-c", "import time; time.sleep(3)"]
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("s1", slow, timeout_seconds=30),
                                   _check("s2", PASS, timeout_seconds=30)]))
        rid = _run_id(conn, tid)
        outcome: dict = {}

        def _run():
            with kbc.connect() as c2:
                try:
                    kb.complete_task(c2, tid, summary="done",
                                     expected_run_id=rid)
                    outcome["ok"] = True
                except Exception as exc:  # noqa: BLE001
                    outcome["err"] = exc

        th = threading.Thread(target=_run)
        th.start()
        # Wait for the lease row, verify TTL == max(1200, 60 + 900) == 1200.
        lease = None
        for _ in range(50):
            lease = conn.execute(
                "SELECT * FROM gate_leases WHERE task_id = ?", (tid,)).fetchone()
            if lease is not None:
                break
            time.sleep(0.1)
        assert lease is not None
        assert abs((lease["expires_at"] - lease["acquired_at"]) - 1200) <= 2
        # Steal the lease mid-evaluation: the next renewal must fail.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE gate_leases SET holder = 'thief', expires_at = ? WHERE task_id = ?",
                (int(time.time()) + 3600, tid))
        th.join(timeout=30)
        assert isinstance(outcome.get("err"), kb.GateEnvError)
        assert outcome["err"].env_reason == "lease_lost"
        assert not _events(conn, tid, "gate_attempt_counted")


def test_g7c_expired_lease_allows_later_evaluation(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("p", PASS)]))
        now = int(time.time())
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO gate_leases (task_id, holder, acquired_at, expires_at) "
                "VALUES (?, 'dead', ?, ?)", (tid, now - 5000, now - 4000))
        assert _complete(conn, tid) is True


# --------------------------------------------------------------------------
# G-8 / G-8b / G-8c: review exemption, bypass, fence, freshness
# --------------------------------------------------------------------------

def test_g8_review_exempt_force_gated_bypass(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        # Review-status cards are exempt.
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]), claim=False)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        assert kb.complete_task(conn, tid, summary="reviewed") is True
        completed = _events(conn, tid, "completed")
        assert completed[-1].payload["gate"] == {"result": "exempt_review"}
        # Bypass: audited, >= 8 chars, completes despite failing checks.
        tid2 = _mk(conn, ws, _spec([_check("f", FAIL)]))
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid2)
        assert kb.complete_task(
            conn, tid2, summary="done", expected_run_id=_run_id(conn, tid2),
            gate_bypass_reason="operator override for drill",
            gate_bypass_actor="cli:tester") is True
        bypassed = _events(conn, tid2, "gate_bypassed")
        assert bypassed and bypassed[-1].payload["actor"] == "cli:tester"
        # Short reason does NOT bypass.
        tid3 = _mk(conn, ws, _spec([_check("f", FAIL)]))
        with pytest.raises(kb.GateFailedError):
            kb.complete_task(conn, tid3, summary="done",
                             expected_run_id=_run_id(conn, tid3),
                             gate_bypass_reason="short", gate_bypass_actor="cli:tester")


def test_g8_tool_schema_has_no_bypass(kanban_home):
    from tools.kanban_tools_schemas import KANBAN_COMPLETE_SCHEMA
    assert "bypass" not in json.dumps(KANBAN_COMPLETE_SCHEMA)


def test_g8b_foreign_claim_refused_before_any_subprocess(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    marker = ws / "gate-ran.txt"
    writer = [sys.executable, "-c", "open('gate-ran.txt','w').write('x')"]
    # A claim is only "live" (fence-protected) when a real worker process
    # holds it: spawn a sleeper and stamp its pid + the production start
    # fingerprint (_process_fingerprint, the format _set_worker_pid writes).
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = _process_fingerprint(sleeper.pid)
        with kbc.connect() as conn:
            tid = _mk(conn, ws, _spec([_check("w", writer)]))
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                    (sleeper.pid, started, tid))
            with pytest.raises(kb.LiveClaimError):
                kb.complete_task(conn, tid, summary="done")   # no expected_run_id, no force
            assert not marker.exists()
            assert not _events(conn, tid, "gate_failed")
            assert not _events(conn, tid, "gate_passed")
            assert kb.get_task(conn, tid).consecutive_failures == 0
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


def test_g8c_freshness_mismatch_returns_false(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("p", PASS)]))
        real_fp = kg._fingerprint_and_entries
        calls = {"n": 0}

        def _wrapped(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                # Calls 1-2 are the gate's own pre/post fingerprints; call 3
                # is the stage-1 freshness recompute.  Mutate the workspace
                # first so the recomputed fingerprint cannot match the token.
                (ws / "code.py").write_text("x = 999\n", encoding="utf-8")
            return real_fp(*a, **kw)

        monkeypatch.setattr(kg, "_fingerprint_and_entries", _wrapped)
        assert _complete(conn, tid) is False
        assert kb.get_task(conn, tid).status == "running"
        assert calls["n"] >= 3


# --------------------------------------------------------------------------
# G-9 / G-10 / G-11 / G-12 / G-13
# --------------------------------------------------------------------------

def test_g9_flags_off_no_gate_behavior(kanban_home, tmp_path):
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        assert _complete(conn, tid) is True     # failing spec never evaluated
        kinds = [e.kind for e in _events(conn, tid)]
        assert not any(k.startswith("gate_") for k in kinds)
        tid2 = kb.create_task(conn, title="plain", assignee="w")
        kb.claim_task(conn, tid2, claimer="host:worker")
        assert _complete(conn, tid2) is True


def test_g10_runner_exception_takes_env_path(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("p", PASS)]))

        def _boom(*a, **kw):
            raise RuntimeError("runner exploded")

        monkeypatch.setattr(kg, "run_gate_spec", _boom)
        with pytest.raises(kb.GateEnvError):
            _complete(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status != "done"
        assert task.consecutive_failures == 0


def test_g11_runless_completion_paths(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]), claim=False)
        with pytest.raises(kb.GateFailedError) as ei:
            kb.complete_task(conn, tid, summary="done")
        assert ei.value.exhausted is False
        failed = _events(conn, tid, "gate_failed")
        assert failed and failed[-1].payload.get("runless") is True
        assert failed[-1].run_id is None
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0 and task.structured_failures == 0
        # Run-less pass completes.
        tid2 = _mk(conn, ws, _spec([_check("p", PASS)]), claim=False)
        assert kb.complete_task(conn, tid2, summary="done") is True
        # Run-less env failure: no env-guard counting either.
        tid3 = _mk(conn, ws, _spec([_check("x", ["/nonexistent/nope"])]), claim=False)
        with pytest.raises(kb.GateEnvError):
            kb.complete_task(conn, tid3, summary="done")
        assert kb.get_task(conn, tid3).consecutive_failures == 0


def test_g12_board_config_is_authority_not_worker_profile(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    profile_dir = kanban_home / "profiles" / "worker"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "kanban:\n  gates:\n    enabled: false\n", encoding="utf-8")
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)    # board says ON: gate runs despite profile
    # Converse: board OFF, profile ON -> no evaluation.
    _cfg(kanban_home, "kanban:\n  gates:\n    enabled: false\n")
    (profile_dir / "config.yaml").write_text(GATES_ON, encoding="utf-8")
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        assert _complete(conn, tid) is True
        assert not _events(conn, tid, "gate_failed")


def test_g13_bad_assignment_key_disables_only_assignment(kanban_home, tmp_path):
    _cfg(kanban_home,
         "kanban:\n  gates:\n    enabled: true\n  assignment:\n    bogus_g13: 1\n")
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        # Gates still run.
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)
        # Unspec'd completion is unaffected.  FIX-4 (§5.1 per-section
        # isolation): the gates consumer surfaces ONLY the gates section's
        # errors — an assignment-section error is NOT surfaced by a
        # completion, and the assignment feature is not enabled in this
        # config, so no board_config_invalid event appears anywhere here.
        # (Assignment-consumer surfacing is pinned in the FIX-4 tests.)
        tid2 = kb.create_task(conn, title="plain", assignee="w")
        kb.claim_task(conn, tid2, claimer="host:worker")
        assert _complete(conn, tid2) is True
        assert _complete(conn, tid2) is False  # already done; no extra surfacing
        invalid = _events(conn, tid, "board_config_invalid") + \
            _events(conn, tid2, "board_config_invalid")
        assert invalid == []


# --------------------------------------------------------------------------
# FIX round (T7): FIX-1, FIX-2, FIX-3, FIX-4, FIX-13, FIX-15, G-8c placement
# --------------------------------------------------------------------------

def _lease_holder(conn, tid):
    row = conn.execute("SELECT holder FROM gate_leases WHERE task_id = ?",
                       (tid,)).fetchone()
    return row["holder"] if row else None


def test_fix2_lease_released_when_prepare_acceptance_returns_false(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    import hermes_cli.kanban_pr_acceptance_store as pas
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("p", PASS)]))
        monkeypatch.setattr(pas, "prepare_acceptance", lambda *a, **kw: False)
        assert _complete(conn, tid) is False
        # The lease is free IMMEDIATELY — not stranded until TTL expiry.
        assert _lease_holder(conn, tid) is None
        monkeypatch.undo()
        # And a retry completes instead of raising "already in progress".
        assert _complete(conn, tid) is True


def test_fix2_lease_released_when_prepare_acceptance_raises(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    import hermes_cli.kanban_pr_acceptance_store as pas

    def _boom(*a, **kw):
        raise RuntimeError("acceptance exploded")

    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("p", PASS)]))
        monkeypatch.setattr(pas, "prepare_acceptance", _boom)
        with pytest.raises(RuntimeError):
            _complete(conn, tid)
        assert _lease_holder(conn, tid) is None
        monkeypatch.undo()
        assert _complete(conn, tid) is True


def test_fix3_flags_off_step_f_is_inert(kanban_home, tmp_path):
    # No config file at all: every feature is OFF.  A spec'd card with a
    # live foreign claim and unsatisfied parents must see PRE-FEATURE
    # semantics — complete_task returns False from the parents pre-check,
    # it does NOT raise LiveClaimError from the new step F (§2.6 restore).
    ws = _ws(tmp_path)
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = _process_fingerprint(sleeper.pid)
        with kbc.connect() as conn:
            parent = kb.create_task(conn, title="parent", assignee="w")
            tid = kb.create_task(conn, title="child", assignee="w",
                                 parents=(parent,),
                                 workspace_kind="dir", workspace_path=str(ws),
                                 gate_spec=_spec([_check("f", FAIL)]))
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'running', claim_lock = 'foreign', "
                    "worker_pid = ?, worker_started_at = ? WHERE id = ?",
                    (sleeper.pid, started, tid))
            assert kb.complete_task(conn, tid, summary="done") is False
            assert not _events(conn, tid, "gate_bypassed")
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


def test_fix4_disabled_paths_emit_no_board_config_invalid(kanban_home, tmp_path):
    # Skeptic M-2: invalid keys in the config + ALL features disabled +
    # a spec'd completion => zero board_config_invalid events, byte-for-byte
    # pre-feature completion.
    _cfg(kanban_home, "kanban:\n  assignment:\n    bogus_fix4: 1\n")
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        assert _complete(conn, tid) is True     # gates off: spec never runs
        assert _events(conn, tid, "board_config_invalid") == []


def test_fix4_gates_consumer_surfaces_only_gates_errors(kanban_home, tmp_path):
    # Gates enabled in config but the gates SECTION is invalid: the gates
    # consumer surfaces exactly its own section's error (once), the feature
    # is disabled by the error, and other sections' errors stay unsurfaced.
    _cfg(kanban_home, """kanban:
  gates:
    enabled: true
    bogus_fix4_gates: 1
  assignment:
    bogus_fix4_asg: 1
""")
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        assert _complete(conn, tid) is True     # invalid section -> disabled
        invalid = _events(conn, tid, "board_config_invalid")
        assert len(invalid) == 1
        assert invalid[0].payload.get("section") == "gates"
        assert invalid[0].payload.get("key") == "bogus_fix4_gates"


def test_fix1_exhaustion_reentry_is_idempotent(kanban_home, tmp_path):
    _cfg(kanban_home, "kanban:\n  gates:\n    enabled: true\n"
         "    max_attempts_per_run: 3\n  escalation:\n    enabled: true\n")
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        run_id = _run_id(conn, tid)
        # Three counted attempts (workspace changes each time) -> exhaustion.
        for i in range(3):
            (ws / "code.py").write_text(f"x = {i + 10}\n", encoding="utf-8")
            with pytest.raises(kb.GateFailedError) as ei:
                _complete(conn, tid)
            assert ei.value.exhausted is (i == 2)
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1 and task.structured_failures == 1
        n_decisions = len(_events(conn, tid, "gate_attempt_counted"))
        n_failed = len(_events(conn, tid, "gate_failed"))
        # A revived evaluator re-enters the exhaustion sequence for the SAME
        # (now closed) run: evidence only, NO second accounting.
        from hermes_cli import kanban_board_config as _kbcfg_f1
        _snap_cfg, _snap_errs = _kbcfg_f1.load_board_config()
        outcome = kb._gate_exhaustion_sequence(
            conn, tid, run_id=run_id, result=None,
            evidence_payload={"spec_hash": "x", "classification": "worker",
                              "checks": [], "failed_check": "f",
                              "fingerprint": None, "after_fingerprint": None},
            gates_cfg={"max_attempts_per_run": 3},
            board_config=_snap_cfg, board_errors=_snap_errs)
        assert outcome == "already_exhausted"
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1 and task.structured_failures == 1
        assert len(_events(conn, tid, "gate_attempt_counted")) == n_decisions
        assert len(_events(conn, tid, "gate_failed")) == n_failed + 1  # evidence only
        assert not _events(conn, tid, "escalation_triggered")


def test_fix1_decision_pairs_to_captured_evidence_id(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        for i in range(2):
            (ws / "code.py").write_text(f"x = {i + 20}\n", encoding="utf-8")
            with pytest.raises(kb.GateFailedError):
                _complete(conn, tid)
        failed = _events(conn, tid, "gate_failed")
        decisions = _events(conn, tid, "gate_attempt_counted")
        counted = [d for d in decisions if d.payload.get("counted") is True]
        assert len(counted) == 2
        # Each decision pairs to ITS OWN evidence event, in order.
        assert [d.payload["gate_failed_event_id"] for d in counted] == \
            [failed[0].id, failed[1].id]


def test_fix13_fingerprint_skips_fifo(kanban_home, tmp_path):
    ws = _ws(tmp_path)
    os.mkfifo(ws / "pipe.fifo")
    done = {}

    def _run():
        done["fp"] = kg.workspace_fingerprint(str(ws))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "fingerprint hung on a FIFO"
    assert done["fp"]["manifest_hash"]


def test_fix15_commit_only_change_counts_as_fix_attempt(kanban_home, tmp_path):
    _cfg(kanban_home, GATES_ON)
    ws = tmp_path / "repo"
    ws.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(ws)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "T"], check=True)
    (ws / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-m", "base"], check=True, capture_output=True)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("f", FAIL)]))
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)
        # Commit-only change: HEAD moves, the tree (manifest) is identical.
        subprocess.run(["git", "-C", str(ws), "commit", "--allow-empty", "-m", "empty"],
                       check=True, capture_output=True)
        with pytest.raises(kb.GateFailedError):
            _complete(conn, tid)
        decisions = _events(conn, tid, "gate_attempt_counted")
        counted = [d for d in decisions if d.payload.get("counted") is True]
        assert len(counted) == 2, decisions
        assert counted[-1].payload.get("attempt") == 2
        assert not any(d.payload.get("no_change_retry") for d in decisions)


def test_g8c_head_move_freshness_variant(kanban_home, tmp_path, monkeypatch):
    _cfg(kanban_home, GATES_ON)
    ws = tmp_path / "repo2"
    ws.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(ws)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "T"], check=True)
    (ws / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-m", "base"], check=True, capture_output=True)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("p", PASS)]))
        real_fp = kg._fingerprint_and_entries
        calls = {"n": 0}

        def _wrapped(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                # Stage-1 recompute moment: move HEAD only.
                subprocess.run(["git", "-C", str(ws), "commit", "--allow-empty", "-m", "mv"],
                               check=True, capture_output=True)
            return real_fp(*a, **kw)

        monkeypatch.setattr(kg, "_fingerprint_and_entries", _wrapped)
        assert _complete(conn, tid) is False
        assert kb.get_task(conn, tid).status == "running"


def test_g8c_placement_traps(kanban_home, tmp_path, monkeypatch):
    # N-4 placement: the full recompute runs OUTSIDE any txn and AFTER
    # prepare_acceptance; no fingerprint walk ever runs inside a txn.
    _cfg(kanban_home, GATES_ON)
    ws = _ws(tmp_path)
    import hermes_cli.kanban_pr_acceptance_store as pas
    order = []
    real_pa = pas.prepare_acceptance

    def _pa(*a, **kw):
        order.append("prepare_acceptance")
        return real_pa(*a, **kw)

    monkeypatch.setattr(pas, "prepare_acceptance", _pa)
    with kbc.connect() as conn:
        tid = _mk(conn, ws, _spec([_check("p", PASS)]))
        real_fp = kg._fingerprint_and_entries
        seen = []

        def _wrapped(*a, **kw):
            seen.append((conn.in_transaction, "prepare_acceptance" in order))
            return real_fp(*a, **kw)

        monkeypatch.setattr(kg, "_fingerprint_and_entries", _wrapped)
        assert _complete(conn, tid) is True
        assert seen, "fingerprint never ran"
        # No walk inside a write txn, ever.
        assert all(in_txn is False for in_txn, _ in seen)
        # The stage-1 recompute (the LAST fingerprint call) ran after
        # prepare_acceptance.
        assert seen[-1][1] is True


# ===========================================================================
# X series — cross-run gate exhaustion (AMENDMENT-DESIGN-REV2, Option D).
# Fixture rule (§A7): every transition is driven through production functions
# — create_task -> claim_task -> ONE complete_task per run (the non-retrying
# worker of the smokes) -> detect_crashed_workers (production reclaim +
# accounting), with the dead worker faked exactly as the existing reclaim
# fixtures fake it (an already-exited child + _record_worker_exit + the
# precedented _set_worker_pid plumbing).  Board config is a real board-home
# config.yaml, read through load_board_config().
# ===========================================================================

import shutil

from hermes_cli import kanban_board_config as kbcfg
from hermes_cli import kanban_db_dispatch as kbd


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


def _x_cfg(home, *, gates=True, escalation=True, threshold=2, max_refused=None,
           frontier_primary=None, seats=None, extra_gates="", extra_esc=""):
    lines = ["kanban:", "  gates:"]
    lines.append(f"    enabled: {'true' if gates else 'false'}")
    lines.append("    max_attempts_per_run: 3")
    if max_refused is not None:
        lines.append(f"    max_refused_runs: {max_refused}")
    if extra_gates:
        lines.append(extra_gates)
    lines.append("  assignment:")
    lines.append("    enabled: true")
    if frontier_primary:
        lines.append(f"    frontier_primary: {frontier_primary}")
    if seats:
        lines.append("    seats:")
        for name, (model, provider) in seats.items():
            lines.append(f"      {name}:")
            lines.append(f"        model: {json.dumps(model)}")
            lines.append(f"        provider: {json.dumps(provider)}")
    lines.append("  escalation:")
    lines.append(f"    enabled: {'true' if escalation else 'false'}")
    lines.append(f"    threshold: {threshold}")
    if extra_esc:
        lines.append(extra_esc)
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _x_ws(tmp_path, name="ws"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "code.py").write_text("x = 1\n", encoding="utf-8")
    return d


def _x_mk(conn, ws, spec, assignee="worker"):
    return kb.create_task(conn, title="x", assignee=assignee,
                          workspace_kind="dir", workspace_path=str(ws),
                          gate_spec=spec)


def _dead_close(conn, tid):
    """Close the task's live run via the production crash reclaim: fake the
    dead worker (already-exited child, clean-exit reap record, worker pid
    published through the precedented plumbing), then detect."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    kbd._record_worker_exit(proc.pid, 0)
    kbd._set_worker_pid(conn, tid, proc.pid)
    crashed = kbd.detect_crashed_workers(conn)
    assert tid in crashed


def _refused_run(conn, tid):
    """One non-retrying-worker run: exactly one complete_task, refused with
    exhausted=False, then the worker ends its session (PV closure)."""
    assert kb.claim_task(conn, tid) is not None
    run_id = kb.get_task(conn, tid).current_run_id
    with pytest.raises(kb.GateFailedError) as ei:
        kb.complete_task(conn, tid, summary="try", expected_run_id=run_id)
    assert ei.value.exhausted is False
    _dead_close(conn, tid)
    return run_id


def _plain_pv_run(conn, tid):
    """One plain protocol-violation run: claim, exit, NO completion attempt."""
    assert kb.claim_task(conn, tid) is not None
    run_id = kb.get_task(conn, tid).current_run_id
    _dead_close(conn, tid)
    return run_id


def _auto_blocked():
    return list(getattr(kbd.detect_crashed_workers, "_last_auto_blocked", []))


def _marker_argv(marker):
    return [sys.executable, "-c",
            "import pathlib, sys; "
            "sys.exit(0 if pathlib.Path(sys.argv[1]).read_text().strip() == 'good' else 3)",
            str(marker)]


FAILC = [sys.executable, "-c", "import sys; sys.exit(3)"]


def _rewind_escalation_trigger(conn, tid):
    """Fixture plumbing: whole-second timestamps make a same-second frontier
    run invisible to escalated_flag_set (started_at > trigger created_at);
    rewind the trigger event so the frontier runs that follow are 'after'
    it, exactly as the reclaim fixtures rewind started_at for the crash
    grace window."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 10 "
            "WHERE task_id = ? AND kind = 'escalation_triggered'", (tid,))



# ---------------------------------------------------------------------------
# X-1: cross-run mint (unfixable)
# ---------------------------------------------------------------------------

def test_x1_cross_run_mint_unfixable(x_home, tmp_path):
    _x_cfg(x_home)  # defaults: threshold 2, max_refused_runs absent -> 3
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        for n in (1, 2):
            _refused_run(conn, tid)
            task = kb.get_task(conn, tid)
            assert task.structured_failures == 0
            assert task.status == "ready"
            assert task.block_kind is None
            assert task.consecutive_failures == 0
            assert not _events(conn, tid, "gave_up")
            refused = _events(conn, tid, "gate_run_refused")
            assert len(refused) == n
            assert refused[-1].payload["window_count"] == n
            assert tid not in _auto_blocked()
        _refused_run(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.structured_failures == 1
        assert task.status == "ready"
        assert _events(conn, tid, "gate_cross_run_exhaustion")
        assert not _events(conn, tid, "gave_up")


# ---------------------------------------------------------------------------
# X-2: escalation arc + typed terminal (non-default seats, Q-1 + Q-6)
# ---------------------------------------------------------------------------

def test_x2_escalation_arc_and_typed_terminal(x_home, tmp_path, monkeypatch):
    _x_cfg(x_home, threshold=2, frontier_primary="opus",
           seats={"argon": ("x-argon-model", "openrouter"),
                  "opus": ("x-opus-model", "anthropic")})
    ws = _x_ws(tmp_path)
    hook_calls = []
    real_hook = kb._fire_task_hook

    def _spy(name, task, task_id, run_id, **kw):
        hook_calls.append((name, task_id))
        return real_hook(name, task, task_id, run_id, **kw)

    monkeypatch.setattr(kb, "_fire_task_hook", _spy)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        for _ in range(3):
            _refused_run(conn, tid)
        assert kb.get_task(conn, tid).structured_failures == 1
        for _ in range(3):
            _refused_run(conn, tid)
            assert tid not in _auto_blocked()
        task = kb.get_task(conn, tid)
        assert task.structured_failures == 2
        trig = _events(conn, tid, "escalation_triggered")
        assert len(trig) == 1 and trig[0].payload["trigger_kind"] == "gate"
        # Q-1: overrides are the CONFIGURED counter-frontier seat strings,
        # not the shipped defaults (a tuple-passed-as-dict build fails here).
        assert task.model_override == "x-opus-model"
        assert task.provider_override == "anthropic"
        assert task.status == "ready"
        _rewind_escalation_trigger(conn, tid)
        for _ in range(2):
            _refused_run(conn, tid)
            assert tid not in _auto_blocked()
        _refused_run(conn, tid)  # run 9: the typed terminal block
        task = kb.get_task(conn, tid)
        assert task.structured_failures == 3
        assert task.status in ("blocked", "triage")
        assert task.block_kind == "gate_worker"
        assert _events(conn, tid, "blocked") or _events(conn, tid, "block_loop_detected")
        assert not _events(conn, tid, "gave_up")
        assert tid in _auto_blocked()
        blocked_hooks = [c for c in hook_calls
                         if c[0] == "kanban_task_blocked" and c[1] == tid]
        assert len(blocked_hooks) == 1
        for _ in range(3):
            kbd.dispatch_once(conn, dry_run=True)
            assert kb.get_task(conn, tid).status in ("blocked", "triage")


# ---------------------------------------------------------------------------
# X-3 / X-3b: fixable success; pass-then-abort is not refused
# ---------------------------------------------------------------------------

def test_x3_fixable_spec_success(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    marker = tmp_path / "marker.txt"
    marker.write_text("bad\n", encoding="utf-8")
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("m", _marker_argv(marker))]))
        _refused_run(conn, tid)
        assert len(_events(conn, tid, "gate_run_refused")) == 1
        marker.write_text("good\n", encoding="utf-8")
        assert kb.claim_task(conn, tid) is not None
        run2 = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(conn, tid, summary="done", expected_run_id=run2) is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.structured_failures == 0
        assert len(_events(conn, tid, "gate_run_refused")) == 1
        assert not _events(conn, tid, "gate_cross_run_exhaustion")


def test_x3b_pass_then_abort_is_not_refused(x_home, tmp_path, monkeypatch):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    marker = tmp_path / "marker.txt"
    marker.write_text("bad\n", encoding="utf-8")
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("m", _marker_argv(marker))]))
        assert kb.claim_task(conn, tid) is not None
        run_id = kb.get_task(conn, tid).current_run_id
        with pytest.raises(kb.GateFailedError):
            kb.complete_task(conn, tid, summary="try", expected_run_id=run_id)
        # Same run: fix the marker so the gate PASSES, then mutate the tree
        # at the freshness recompute (G-8c pattern) so completion aborts.
        marker.write_text("good\n", encoding="utf-8")
        real_fp = kg._fingerprint_and_entries
        calls = {"n": 0}

        def _wrapped(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                (ws / "code.py").write_text("x = 999\n", encoding="utf-8")
            return real_fp(*a, **kw)

        monkeypatch.setattr(kg, "_fingerprint_and_entries", _wrapped)
        assert kb.complete_task(conn, tid, summary="try", expected_run_id=run_id) is False
        assert _events(conn, tid, "gate_passed")
        _dead_close(conn, tid)
        task = kb.get_task(conn, tid)
        assert not _events(conn, tid, "gate_run_refused")
        assert task.structured_failures == 0
        assert task.status == "ready"  # legacy PV accounting, below limit


# ---------------------------------------------------------------------------
# X-4: no fingerprint gate across runs
# ---------------------------------------------------------------------------

def test_x4_no_fingerprint_gate_across_runs(x_home, tmp_path):
    _x_cfg(x_home)
    with kbc.connect() as conn:
        ws1 = _x_ws(tmp_path, "ws1")
        t1 = _x_mk(conn, ws1, _spec([_check("f", FAILC)]))
        for _ in range(3):
            _refused_run(conn, t1)
        mint = _events(conn, t1, "gate_cross_run_exhaustion")
        assert len(mint) == 1
        assert mint[0].payload["no_tree_change_across_runs"] is True
        ws2 = _x_ws(tmp_path, "ws2")
        t2 = _x_mk(conn, ws2, _spec([_check("f", FAILC)]))
        for i in range(3):
            _refused_run(conn, t2)
            (ws2 / "code.py").write_text(f"x = {100 + i}\n", encoding="utf-8")
        mint2 = _events(conn, t2, "gate_cross_run_exhaustion")
        assert len(mint2) == 1
        assert mint2[0].payload["no_tree_change_across_runs"] is False


# ---------------------------------------------------------------------------
# X-5 / X-5b: spec change resets the window; A->B->A restarts at 0
# ---------------------------------------------------------------------------

def test_x5_spec_change_resets_window(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    spec_h1 = _spec([_check("a", FAILC)])
    spec_h2 = _spec([_check("b", FAILC)])
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, spec_h1)
        _refused_run(conn, tid)
        _refused_run(conn, tid)
        assert kb.edit_task(conn, tid, gate_spec=spec_h2) is True
        h2 = kg.spec_hash(kb.get_task(conn, tid).gate_spec)
        _refused_run(conn, tid)
        _refused_run(conn, tid)
        assert kb.get_task(conn, tid).structured_failures == 0
        assert not _events(conn, tid, "gate_cross_run_exhaustion")
        _refused_run(conn, tid)
        mint = _events(conn, tid, "gate_cross_run_exhaustion")
        assert len(mint) == 1 and mint[0].payload["spec_hash"] == h2
        assert kb.get_task(conn, tid).structured_failures == 1


def test_x5b_a_b_a_restarts_at_zero(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    spec_h1 = _spec([_check("a", FAILC)])
    spec_h2 = _spec([_check("b", FAILC)])
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, spec_h1)
        _refused_run(conn, tid)
        _refused_run(conn, tid)
        assert kb.edit_task(conn, tid, gate_spec=spec_h2) is True
        assert kb.edit_task(conn, tid, gate_spec=spec_h1) is True
        _refused_run(conn, tid)
        _refused_run(conn, tid)
        assert kb.get_task(conn, tid).structured_failures == 0
        assert not _events(conn, tid, "gate_cross_run_exhaustion")
        _refused_run(conn, tid)
        assert len(_events(conn, tid, "gate_cross_run_exhaustion")) == 1
        assert kb.get_task(conn, tid).structured_failures == 1


# ---------------------------------------------------------------------------
# X-6: unblock does not reset the window
# ---------------------------------------------------------------------------

def test_x6_unblock_does_not_reset_window(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        _refused_run(conn, tid)
        _refused_run(conn, tid)
        assert kb.block_task(conn, tid, kind="needs_input", reason="wait") is True
        assert kb.unblock_task(conn, tid) is True
        _refused_run(conn, tid)
        assert kb.get_task(conn, tid).structured_failures == 1
        assert _events(conn, tid, "gate_cross_run_exhaustion")


# ---------------------------------------------------------------------------
# X-7 / X-8: breaker jurisdiction, both directions
# ---------------------------------------------------------------------------

def test_x7_mixed_card_breaker_jurisdiction(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        _refused_run(conn, tid)          # run 1: refused (neutral in streak)
        _plain_pv_run(conn, tid)         # run 2: plain PV (streak 1)
        _plain_pv_run(conn, tid)         # run 3: plain PV (streak 2)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert not _events(conn, tid, "gave_up")
        _plain_pv_run(conn, tid)         # run 4: plain PV (streak 3) -> park
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind is None
        gave = _events(conn, tid, "gave_up")
        assert len(gave) == 1
        assert gave[0].payload["protocol_violations"] == 3
        assert task.structured_failures == 0
        assert len(_events(conn, tid, "gate_run_refused")) == 1


def test_x8_non_specd_regression(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, None)
        _plain_pv_run(conn, tid)
        _plain_pv_run(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"
        _plain_pv_run(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        gave = _events(conn, tid, "gave_up")
        assert len(gave) == 1
        assert gave[0].payload["protocol_violations"] == 3
        assert not _events(conn, tid, "gate_run_refused")
        assert not _events(conn, tid, "gate_cross_run_exhaustion")


# ---------------------------------------------------------------------------
# X-9: env + run-less exclusions
# ---------------------------------------------------------------------------

def test_x9_env_and_runless_exclusions(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("x", ["/nonexistent/hermes-no-such-binary"])]))
        assert kb.claim_task(conn, tid) is not None
        run_id = kb.get_task(conn, tid).current_run_id
        with pytest.raises(kb.GateEnvError):
            kb.complete_task(conn, tid, summary="try", expected_run_id=run_id)
        # The env sequence closed the run itself: no PV closure exists.
        assert tid not in kbd.detect_crashed_workers(conn)
        assert _events(conn, tid, "gate_env_failed")
        assert not _events(conn, tid, "gate_run_refused")
        assert kb.get_task(conn, tid).structured_failures == 0
        # Run-less failing completions never become refused runs.
        tid2 = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        for _ in range(3):
            with pytest.raises(kb.GateFailedError):
                kb.complete_task(conn, tid2, summary="done")
        assert not _events(conn, tid2, "gate_run_refused")
        assert not _events(conn, tid2, "gate_cross_run_exhaustion")
        assert kb.get_task(conn, tid2).structured_failures == 0


# ---------------------------------------------------------------------------
# X-10: idempotency + state-moved
# ---------------------------------------------------------------------------

def test_x10_idempotency_and_state_moved(x_home, tmp_path):
    _x_cfg(x_home, max_refused=1)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        # (a) double _account_crashes invocation on the same details.
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        assert kb.claim_task(conn, tid) is not None
        run_id = kb.get_task(conn, tid).current_run_id
        with pytest.raises(kb.GateFailedError):
            kb.complete_task(conn, tid, summary="try", expected_run_id=run_id)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        kbd._record_worker_exit(proc.pid, 0)
        kbd._set_worker_pid(conn, tid, proc.pid)
        sweep = kbd._reclaim_dead_workers(conn)
        assert len(sweep.crash_details) == 1
        kbd._account_crashes(conn, sweep.crash_details)
        assert kb.get_task(conn, tid).structured_failures == 1
        kbd._account_crashes(conn, sweep.crash_details)
        assert len(_events(conn, tid, "gate_run_refused")) == 1
        assert len(_events(conn, tid, "gate_cross_run_exhaustion")) == 1
        assert kb.get_task(conn, tid).structured_failures == 1
        assert not _events(conn, tid, "gave_up")
        # (b) the card moves (operator completion) between reclaim and
        # accounting: state_moved, nothing written, breaker suppressed.
        tid2 = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        assert kb.claim_task(conn, tid2) is not None
        run2 = kb.get_task(conn, tid2).current_run_id
        with pytest.raises(kb.GateFailedError):
            kb.complete_task(conn, tid2, summary="try", expected_run_id=run2)
        proc2 = subprocess.Popen([sys.executable, "-c", "pass"])
        proc2.wait()
        kbd._record_worker_exit(proc2.pid, 0)
        kbd._set_worker_pid(conn, tid2, proc2.pid)
        sweep2 = kbd._reclaim_dead_workers(conn)
        _x_cfg(x_home, gates=False, max_refused=1)  # ungated completion window
        assert kb.complete_task(conn, tid2, summary="done") is True
        _x_cfg(x_home, max_refused=1)
        out = kb._gate_refused_run_accounting(
            conn, tid2, run_id=run2, board_config=kbcfg.load_board_config())
        assert out["outcome"] == "state_moved"
        assert not _events(conn, tid2, "gate_run_refused")
        kbd._account_crashes(conn, sweep2.crash_details)
        assert kb.get_task(conn, tid2).status == "done"
        assert not _events(conn, tid2, "gave_up")


# ---------------------------------------------------------------------------
# X-12: in-run exhaustion untouched + mutually exclusive
# ---------------------------------------------------------------------------

def test_x12_in_run_exhaustion_untouched(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        assert kb.claim_task(conn, tid) is not None
        run_id = kb.get_task(conn, tid).current_run_id
        for i, content in enumerate(("s1", "s2", "s3")):
            (ws / "code.py").write_text(f"# {content}\n", encoding="utf-8")
            with pytest.raises(kb.GateFailedError) as ei:
                kb.complete_task(conn, tid, summary="try", expected_run_id=run_id)
            assert ei.value.exhausted is (i == 2)
        assert kb.get_task(conn, tid).structured_failures == 1
        assert not _events(conn, tid, "gate_run_refused")
        assert tid not in kbd.detect_crashed_workers(conn)


# ---------------------------------------------------------------------------
# X-13 / X-13b: config validation, defaults, B-6 split
# ---------------------------------------------------------------------------

def test_x13_config_validation_and_defaults(kanban_home, tmp_path):
    _cfg(kanban_home, "kanban:\n  gates:\n    enabled: true\n")
    cfg, errors = kbcfg.load_board_config()
    assert kbcfg.section_config(cfg, "gates")["max_refused_runs"] == 3
    assert not kbcfg.section_errors(errors, "gates")
    for bad in ("0", '"3"'):
        _cfg(kanban_home,
             f"kanban:\n  gates:\n    enabled: true\n    max_refused_runs: {bad}\n")
        cfg, errors = kbcfg.load_board_config()
        assert any(e["key"] == "max_refused_runs"
                   for e in kbcfg.section_errors(errors, "gates"))
        assert kbcfg.feature_enabled(cfg, errors, "gates") is False


def test_x13bi_errored_gates_section_completes_ungated(x_home, tmp_path):
    ws = _x_ws(tmp_path)
    for bad in ("0", '"3"'):
        _x_cfg(x_home, extra_gates=f"    max_refused_runs: {bad}")
        cfg, errors = kbcfg.load_board_config()
        assert any(e["key"] == "max_refused_runs"
                   for e in kbcfg.section_errors(errors, "gates"))
        assert kbcfg.feature_enabled(cfg, errors, "gates") is False
        with kbc.connect() as conn:
            tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
            assert kb.claim_task(conn, tid) is not None
            run_id = kb.get_task(conn, tid).current_run_id
            assert kb.complete_task(conn, tid, summary="done",
                                    expected_run_id=run_id) is True
            task = kb.get_task(conn, tid)
            assert task.status == "done"
            assert not _events(conn, tid, "gate_failed")
            assert not _events(conn, tid, "gate_run_refused")


def test_x13bii_errored_escalation_section_legacy_park(x_home, tmp_path):
    _x_cfg(x_home, extra_esc="    ceilings:\n      default:\n        max_total_tokens: \"lots\"")
    cfg, errors = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errors, "escalation") is False
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        for _ in range(3):
            _refused_run(conn, tid)
        task = kb.get_task(conn, tid)
        assert _events(conn, tid, "gate_failed")  # refusals still form
        assert task.status == "blocked"
        assert _events(conn, tid, "gave_up")
        assert task.structured_failures == 0
        assert not _events(conn, tid, "gate_run_refused")


# ---------------------------------------------------------------------------
# X-15: gates ON / escalation OFF -> legacy park, pinned
# ---------------------------------------------------------------------------

def test_x15_gates_on_escalation_off_legacy_park(x_home, tmp_path):
    _x_cfg(x_home, escalation=False)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        for _ in range(3):
            _refused_run(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert _events(conn, tid, "gave_up")
        assert task.structured_failures == 0
        assert task.model_override is None
        assert not _events(conn, tid, "gate_run_refused")
        assert not _events(conn, tid, "gate_cross_run_exhaustion")


# ---------------------------------------------------------------------------
# X-16: spec_changed trap (edit between reclaim and accounting)
# ---------------------------------------------------------------------------

def test_x16_spec_changed_trap(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("a", FAILC)]))
        assert kb.claim_task(conn, tid) is not None
        run_id = kb.get_task(conn, tid).current_run_id
        with pytest.raises(kb.GateFailedError):
            kb.complete_task(conn, tid, summary="try", expected_run_id=run_id)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        kbd._record_worker_exit(proc.pid, 0)
        kbd._set_worker_pid(conn, tid, proc.pid)
        sweep = kbd._reclaim_dead_workers(conn)
        assert kb.edit_task(conn, tid, gate_spec=_spec([_check("b", FAILC)])) is True
        kbd._account_crashes(conn, sweep.crash_details)
        assert not _events(conn, tid, "gate_run_refused")
        assert kb.get_task(conn, tid).structured_failures == 0
        # The trap run counts in the legacy streak: two more plain PVs park.
        _plain_pv_run(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"
        _plain_pv_run(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert _events(conn, tid, "gave_up")


# ---------------------------------------------------------------------------
# X-17 (CORRECTED shape, per reviews/amendment-rev2-verification.md M-1 and
# the coordinator's binding ruling): threshold 1 / max_refused_runs 3 — the
# unresolvable mint takes 3 refused runs; a 4th BELOW-BUDGET pass then lands
# the pending capability block via the Zone-2 re-drive with NO new mint.
# (The rev-2 text's threshold 1 / max_refused_runs 1 shape mints a second
# time in Zone 1 before the re-drive is exercised, defeating its own
# assertion.)
# ---------------------------------------------------------------------------

def test_x17_unresolvable_pending_marker_redrive(x_home, tmp_path, monkeypatch):
    _x_cfg(x_home, threshold=1, max_refused=3, frontier_primary="argon",
           seats={"argon": ("x-argon-model", "custom:missing-a"),
                  "opus": ("x-opus-model", "custom:missing-b")})
    ws = _x_ws(tmp_path)
    real_bts = kb.block_task_if_state
    calls = {"n": 0}

    def _flaky(conn_, tid_, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected Shape B failure")
        return real_bts(conn_, tid_, **kw)

    monkeypatch.setattr(kb, "block_task_if_state", _flaky)
    hook_calls = []
    real_hook = kb._fire_task_hook

    def _spy(name, task, task_id, run_id, **kw):
        hook_calls.append((name, task_id))
        return real_hook(name, task, task_id, run_id, **kw)

    monkeypatch.setattr(kb, "_fire_task_hook", _spy)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        for _ in range(3):
            _refused_run(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.structured_failures == 1
        assert _events(conn, tid, "escalation_unresolvable")
        assert not _events(conn, tid, "escalation_triggered")
        assert len(_events(conn, tid, "gate_cross_run_exhaustion")) == 1
        assert task.status == "ready"      # Shape B raised; marker pending
        assert not _events(conn, tid, "gave_up")
        assert calls["n"] == 1
        _refused_run(conn, tid)            # 4th run: below-budget pass
        task = kb.get_task(conn, tid)
        assert task.status in ("blocked", "triage")
        assert task.block_kind == "capability"
        assert _events(conn, tid, "blocked") or _events(conn, tid, "block_loop_detected")
        # The re-drive alone landed the block: no second mint, no double
        # increment.
        assert task.structured_failures == 1
        assert len(_events(conn, tid, "gate_cross_run_exhaustion")) == 1
        blocked_hooks = [c for c in hook_calls
                         if c[0] == "kanban_task_blocked" and c[1] == tid]
        assert len(blocked_hooks) == 1     # fired once, by the wrapper


# ---------------------------------------------------------------------------
# X-18: exception fallback, both zones
# ---------------------------------------------------------------------------

def test_x18a_zone1_exception_falls_back_per_detail(x_home, tmp_path, monkeypatch):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    with kbc.connect() as conn:
        t1 = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        t2 = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        for tid in (t1, t2):
            assert kb.claim_task(conn, tid) is not None
            run_id = kb.get_task(conn, tid).current_run_id
            with pytest.raises(kb.GateFailedError):
                kb.complete_task(conn, tid, summary="try", expected_run_id=run_id)
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            proc.wait()
            kbd._record_worker_exit(proc.pid, 0)
            kbd._set_worker_pid(conn, tid, proc.pid)
        real_acct = kb._gate_refused_run_accounting

        def _stub(conn_, tid_, **kw):
            if tid_ == t1:
                raise RuntimeError("injected Zone-1 failure")
            return real_acct(conn_, tid_, **kw)

        monkeypatch.setattr(kb, "_gate_refused_run_accounting", _stub)
        crashed = kbd.detect_crashed_workers(conn)  # ONE call, both details
        assert t1 in crashed and t2 in crashed
        # The SAME call still accounted the unrelated detail (per-detail
        # guard), while the injected detail fell back to legacy.
        assert len(_events(conn, t2, "gate_run_refused")) == 1
        assert not _events(conn, t1, "gate_run_refused")
        monkeypatch.setattr(kb, "_gate_refused_run_accounting", real_acct)
        # t1's trap run counts in the PV streak: two more plain PVs park it.
        _plain_pv_run(conn, t1)
        _plain_pv_run(conn, t1)
        task = kb.get_task(conn, t1)
        assert task.status == "blocked"
        assert _events(conn, t1, "gave_up")


def test_x18b_zone2_exception_never_falls_back(x_home, tmp_path, monkeypatch):
    _x_cfg(x_home, threshold=2, max_refused=1, frontier_primary="opus")
    ws = _x_ws(tmp_path)

    def _boom(conn_, tid_, fields):
        raise RuntimeError("injected notify failure")

    monkeypatch.setattr(kb, "notify_task_updated", _boom)
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, _spec([_check("f", FAILC)]))
        _refused_run(conn, tid)   # mint 1 (count 1, action none)
        assert kb.get_task(conn, tid).structured_failures == 1
        _refused_run(conn, tid)   # mint 2 escalates; notify raises in Zone 2
        task = kb.get_task(conn, tid)
        assert task.structured_failures == 2
        assert task.model_override == "anthropic/claude-opus-5.5"
        _rewind_escalation_trigger(conn, tid)
        assert len(_events(conn, tid, "gate_cross_run_exhaustion")) == 2
        assert not _events(conn, tid, "gave_up")
        # The mint STANDS and later accounting is unaffected: a third
        # refused run mints again and lands the typed block.
        _refused_run(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.structured_failures == 3
        assert task.status in ("blocked", "triage")
        assert task.block_kind == "gate_worker"
        assert not _events(conn, tid, "gave_up")


# ---------------------------------------------------------------------------
# X-19: mixed worker->env run does NOT count (Q-9)
# ---------------------------------------------------------------------------

def test_x19_mixed_run_does_not_count(x_home, tmp_path):
    _x_cfg(x_home)
    ws = _x_ws(tmp_path)
    marker = tmp_path / "marker.txt"
    marker.write_text("bad\n", encoding="utf-8")
    runner = ws / "runner"

    def _write_runner():
        runner.write_text("#!/bin/sh\ngrep -q good \"$1\"\n", encoding="utf-8")
        runner.chmod(0o755)

    _write_runner()
    spec = _spec([_check("m", [str(runner), str(marker)])])
    with kbc.connect() as conn:
        tid = _x_mk(conn, ws, spec)
        # Run 1 (mixed): call 1 is a counted worker refusal; deleting the
        # check's interpreter target makes call 2 an env outcome, and the
        # env sequence closes the run itself.
        assert kb.claim_task(conn, tid) is not None
        run1 = kb.get_task(conn, tid).current_run_id
        with pytest.raises(kb.GateFailedError):
            kb.complete_task(conn, tid, summary="try", expected_run_id=run1)
        runner.unlink()
        with pytest.raises(kb.GateEnvError):
            kb.complete_task(conn, tid, summary="try", expected_run_id=run1)
        assert _events(conn, tid, "gate_env_failed")
        assert not _events(conn, tid, "gate_run_refused")
        _write_runner()
        _refused_run(conn, tid)
        _refused_run(conn, tid)
        assert len(_events(conn, tid, "gate_run_refused")) == 2
        assert kb.get_task(conn, tid).structured_failures == 0
        _refused_run(conn, tid)   # 4th run overall, 3rd refused -> mint
        assert kb.get_task(conn, tid).structured_failures == 1
        assert len(_events(conn, tid, "gate_cross_run_exhaustion")) == 1
        # The env counter counted run 1 under its own rules: two further
        # env failures reach env_failure_limit (3) and block gate_env.
        runner.unlink()
        for cycle in range(2):
            assert kb.claim_task(conn, tid) is not None
            rid = kb.get_task(conn, tid).current_run_id
            with pytest.raises(kb.GateEnvError):
                kb.complete_task(conn, tid, summary="try", expected_run_id=rid)
        task = kb.get_task(conn, tid)
        assert task.status in ("blocked", "triage")
        assert task.block_kind == "gate_env"


# ---------------------------------------------------------------------------
# W-6: structural guard (static)
# ---------------------------------------------------------------------------

def test_x_w6_structural_guard():
    import re

    kb_src = Path(kb.__file__).read_text(encoding="utf-8")
    fn_start = kb_src.index("def _gate_refused_run_accounting")
    zone2_at = kb_src.index("ZONE 2", fn_start)
    zone1 = kb_src[fn_start:zone2_at]
    # (a) Zone 1 contains no direct block, no unified failure accounting,
    # no subprocess.
    assert "block_task(" not in zone1
    assert "_record_task_failure" not in zone1
    assert "subprocess" not in zone1
    # (c) Q-1: the tuple unpack precedes the first increment call, and the
    # increment receives the dict local, never the tuple parameter.
    m = re.search(r"(\w+), (\w+) = board_config", zone1)
    assert m is not None
    assert zone1.index(m.group(0)) < zone1.index("record_gate_exhaustion_in_txn")
    assert f"board_config={m.group(1)}" in zone1
    fn_end = kb_src.index("\ndef ", fn_start + 10)
    assert "board_config=board_config" not in kb_src[fn_start:fn_end]
    # W-5/Q-11 (dispatch side): exactly one accounting call site; inside
    # _account_crashes the predicate call sits in the same try block as the
    # accounting call, textually before the streak computation, and there
    # is no predicate call site outside that guard.
    disp_src = Path(kbd.__file__).read_text(encoding="utf-8")
    assert disp_src.count("_gate_refused_run_accounting") == 1
    assert disp_src.count("_gate_refused_for_run") == 1
    acct_start = disp_src.index("def _account_crashes")
    acct_end = disp_src.index("\ndef detect_crashed_workers", acct_start)
    acct = disp_src[acct_start:acct_end]
    pred_at = acct.index("_gate_refused_for_run")
    call_at = acct.index("_gate_refused_run_accounting")
    assert pred_at < acct.index("_protocol_violation_streak")
    try_at = acct.rindex("try:", 0, pred_at)
    except_at = acct.index("except Exception:", call_at)
    assert try_at < pred_at < call_at < except_at
    # (d) Q-7: the anchor's single DESC scan, kind list extended in place.
    from hermes_cli import kanban_escalation as ke
    ke_src = Path(ke.__file__).read_text(encoding="utf-8")
    a_start = ke_src.index("def _current_anchor")
    a_end = ke_src.index("\ndef _produced_by_run", a_start)
    anchor_body = ke_src[a_start:a_end]
    assert anchor_body.count("ORDER BY id DESC") == 1
    assert "'gate_cross_run_exhaustion'" in anchor_body

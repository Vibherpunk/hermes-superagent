"""Tests for the board-config direct read (FIX-DESIGN-REV2, Option D).

Covers:
- G-12R-1 (subprocess, production binding): a managed worker bound through
  ``bind_worker_policy(frame)`` + ``bind_worker_context(frame)`` reads the
  BOARD home's ``kanban:`` block — spec-fail completions are gated and
  counted (escalation accounting engages), spec-pass completions carry the
  gate evidence shape, and an operator kill-switch edit takes effect at
  the next completion call in the SAME process.  Plus the standalone-root
  leg (leg S) pinning the L-5 deployment-shape fork.
- G-12R-2 (in-process snapshot seam): the snapshot no longer shadows the
  board read, while plain session config reads still answer from the
  snapshot; G-13 per-section isolation under snapshot; the section
  non-dict clobber rule (P-2c); the top-level non-dict triple.
- G-12R-3 (memo / per-call merge): signature memo, failures never cached,
  deleted-file eviction, managed-overlay and ${VAR} legs proving merge +
  expansion run per call after memo retrieval.
- The §10.3 structural guard: no production module outside
  ``hermes_cli/kanban_board_config.py`` obtains the gates/assignment/
  escalation sections through the snapshot-intercepted loaders.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_board_config as kbcfg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("kanban_snapshot_worker_harness.py")

FAIL_ARGV = [sys.executable, "-c", "import sys; sys.exit(3)"]
PASS_ARGV = [sys.executable, "-c", "pass"]
FAIL_SPEC = {"version": 1, "checks": [{"id": "f", "argv": FAIL_ARGV}]}

BOARD_ON = (
    "kanban:\n"
    "  gates:\n"
    "    enabled: true\n"
    "    max_attempts_per_run: 1\n"
    "  escalation:\n"
    "    enabled: true\n"
    "    threshold: 2\n"
)
KILL_SWITCH = (
    "kanban:\n"
    "  gates:\n"
    "    enabled: false\n"
    "  escalation:\n"
    "    enabled: true\n"
    "    threshold: 2\n"
)
# The worker profile's own file carries a third, conflicting value (gates
# OFF) so the fixture proves neither the profile file nor the snapshot wins.
PROFILE_CFG = (
    "model:\n"
    "  default: fixture-model\n"
    "agent:\n"
    "  disabled_toolsets: []\n"
    "kanban:\n"
    "  gates:\n"
    "    enabled: false\n"
)


@pytest.fixture
def board_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    return home


def _cfg(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# G-12R-1 — subprocess, production binding chain
# ---------------------------------------------------------------------------

def _producer_frame(*, task, db_path: Path, workspace: Path,
                    workspaces_root: Path, profile: str,
                    profile_home: Path) -> dict:
    """A producer-shaped frame per §9.2 step 1 + verification nit L-2.

    ``policy.source`` is named explicitly as ``'kanban'`` (bind_worker_context
    requires it).  ``config_json`` / ``kanban_json`` are JSON strings.
    ``kanban_json`` carries every key bind_worker_context indexes directly:
    board, task_id, run_id, claim_lock, db, workspace, branch, tenant,
    goal_mode, workspaces_root, profile, max_runtime_seconds — plus the
    remaining producer keys (skills, goal_max_turns, goal_text, context,
    accept_hooks).  The snapshot content is the live sa-worker shape:
    present profile sections + NO ``kanban`` key.
    """
    snapshot = {
        "model": {"default": "fixture-model"},
        "agent": {"disabled_toolsets": []},
    }
    kanban_ctx = {
        "board": "default",
        "task_id": task.id,
        "run_id": task.current_run_id,
        "claim_lock": task.claim_lock,
        "db": str(db_path),
        "workspace": str(workspace),
        "branch": task.branch_name,
        "tenant": task.tenant,
        "profile": profile,
        "workspaces_root": str(workspaces_root),
        "skills": [],
        "goal_mode": task.goal_mode,
        "goal_max_turns": task.goal_max_turns,
        "goal_text": task.title,
        "context": "fixture worker context",
        "max_runtime_seconds": task.max_runtime_seconds,
        "accept_hooks": True,
    }
    policy = {
        "source": "kanban",
        "platform": "cli",
        "cwd": str(workspace),
        "model": None,
        "toolsets": ["kanban"],
        "config_json": json.dumps(snapshot, sort_keys=True),
        "request_json": json.dumps(
            {"source": "kanban", "board": "default", "task_id": task.id,
             "run_id": task.current_run_id}, sort_keys=True),
        "terminal_json": "{}",
        "credential_ref": None,
        "config_secret_ref": None,
        "editor_mcp_json": None,
        "kanban_json": json.dumps(kanban_ctx, sort_keys=True),
        "safe_mode": False,
        "ignore_user_config": False,
        "skills_prompt": None,
        "coding_posture": False,
        "client_env_json": None,
        "client_env_remote": False,
        "client_env_rules_json": None,
        "edit_approval_policy": "ask",
    }
    return {
        "version": 1,
        "home": str(profile_home),
        "policy": policy,
        "safe_mode": False,
        "ignore_user_config": False,
    }


def _run_harness(scenario: dict, scenario_path: Path, *, home_parent: Path,
                 hermes_home: Path) -> dict:
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home_parent),
        "HERMES_HOME": str(hermes_home),
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONUTF8": "1",
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(scenario_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO_ROOT), timeout=600)
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("G12R_RESULT ")]
    assert lines, proc.stdout + "\n" + proc.stderr
    return json.loads(lines[-1][len("G12R_RESULT "):])


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """Fixture parent shaped like the live deployment (§9.2): native root
    ``<parent>/.hermes`` (the board home), worker profile home under its
    ``profiles/`` dir, ``HERMES_KANBAN_HOME`` unset everywhere."""
    parent = tmp_path / "fx"
    board = parent / ".hermes"
    profile_home = board / "profiles" / "sa-worker"
    profile_home.mkdir(parents=True)
    workspace = parent / "workspace"
    workspace.mkdir()
    (workspace / "code.py").write_text("x = 1\n", encoding="utf-8")
    _cfg(board, BOARD_ON)
    (profile_home / "config.yaml").write_text(PROFILE_CFG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(board))
    monkeypatch.setattr(Path, "home", lambda: parent)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    return {
        "parent": parent, "board": board, "profile_home": profile_home,
        "workspace": workspace, "db_path": board / "kanban.db",
        "workspaces_root": board / "kanban" / "workspaces",
    }


def _claimed_task(conn, title, workspace, spec=None):
    tid = kb.create_task(conn, title=title, assignee="sa-worker",
                         workspace_kind="dir", workspace_path=str(workspace),
                         gate_spec=spec)
    claimed = kb.claim_task(conn, tid, claimer="host:sa-worker")
    assert claimed is not None
    return claimed


def test_g12r1_snapshot_bound_worker_reads_board_config(fx):
    with kbc.connect() as conn:
        first = _claimed_task(conn, "g12r-fail", fx["workspace"], spec=FAIL_SPEC)
    frame = _producer_frame(task=first, db_path=fx["db_path"],
                            workspace=fx["workspace"],
                            workspaces_root=fx["workspaces_root"],
                            profile="sa-worker",
                            profile_home=fx["profile_home"])
    scenario = {
        "mode": "main",
        "frame": frame,
        "first_task_id": first.id,
        "first_run_id": first.current_run_id,
        "workspace": str(fx["workspace"]),
        "profile": "sa-worker",
        "board_config_path": str(fx["board"] / "config.yaml"),
        "kill_switch_config": KILL_SWITCH,
        "fail_argv": FAIL_ARGV,
        "pass_argv": PASS_ARGV,
    }
    result = _run_harness(scenario, fx["parent"] / "scenario-main.json",
                          home_parent=fx["parent"],
                          hermes_home=fx["profile_home"])

    probe = result["probe"]
    assert probe["gates_enabled"] is True
    assert probe["escalation_enabled"] is True
    assert probe["errors"] == []
    # The snapshot is bound, kanban-free, and still governs session config.
    assert probe["snapshot_bound"] is True
    assert probe["snapshot_has_kanban"] is False
    assert probe["session_gates_enabled"] is False

    leg_fail = result["spec_fail"]
    assert leg_fail["raised"] == "GateFailedError"
    assert leg_fail["exhausted"] is True
    assert leg_fail["gate_failed_events"] >= 1
    assert leg_fail["counted_true"] == 1
    # Completion-path escalation accounting engaged under the snapshot.
    assert leg_fail["structured_failures"] == 1
    assert leg_fail["status"] != "done"

    leg_pass = result["spec_pass"]
    assert leg_pass["completed"] is True
    assert leg_pass["status"] == "done"
    assert leg_pass["gate_passed_events"] == 1
    assert leg_pass["completed_gate_verdict"] == "pass"

    leg_kill = result["kill_switch"]
    assert leg_kill["completed"] is True
    assert leg_kill["status"] == "done"
    assert leg_kill["gate_events"] == 0

    # Parent-side readback of the board DB state.
    with kbc.connect() as conn:
        task = kb.get_task(conn, first.id)
        assert task.structured_failures == 1
        assert task.status != "done"


def test_g12r1_standalone_root_leg_documents_the_fork(fx):
    """Leg S (L-5): a standalone-root profile home is its OWN board home —
    the fixed loader reads that home's config (no kanban: block), returns
    defaults with no errors.  Executable documentation of the fork shape."""
    standalone = fx["parent"] / "standalone-profile"
    standalone.mkdir()
    (standalone / "config.yaml").write_text(
        "model:\n  default: solo-model\n", encoding="utf-8")
    with kbc.connect() as conn:
        task = _claimed_task(conn, "g12r-standalone", fx["workspace"])
    frame = _producer_frame(task=task, db_path=fx["db_path"],
                            workspace=fx["workspace"],
                            workspaces_root=fx["workspaces_root"],
                            profile="standalone-profile",
                            profile_home=standalone)
    scenario = {"mode": "standalone", "frame": frame}
    result = _run_harness(scenario, fx["parent"] / "scenario-standalone.json",
                          home_parent=fx["parent"], hermes_home=standalone)
    assert result["kanban_home"] == str(standalone)
    assert result["gates_enabled"] is False
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# G-12R-2 — in-process snapshot seam (supporting, not a substitute)
# ---------------------------------------------------------------------------

def _bind_fake_snapshot(monkeypatch, snapshot):
    import agent.safe_worker_policy as swp

    monkeypatch.setattr(swp, "worker_config_snapshot", lambda: snapshot)


def test_g12r2_snapshot_does_not_shadow_board_read(board_home, monkeypatch):
    _cfg(board_home, "kanban:\n  gates:\n    enabled: true\n")
    _bind_fake_snapshot(monkeypatch, {"model": {"default": "snap-model"}})
    cfg, errs = kbcfg.load_board_config()
    assert errs == []
    assert kbcfg.feature_enabled(cfg, errs, "gates") is True
    # The snapshot mechanism is untouched: plain session reads still answer
    # from the snapshot (its model wins; kanban falls back to defaults).
    from hermes_cli.config import load_config_readonly

    session_cfg = load_config_readonly()
    assert session_cfg["model"]["default"] == "snap-model"
    assert session_cfg["kanban"]["gates"]["enabled"] is False


def test_g12r2_g13_isolation_under_snapshot(board_home, tmp_path, monkeypatch):
    _cfg(board_home,
         "kanban:\n  gates:\n    enabled: true\n  assignment:\n    bogus_g12r: 1\n")
    _bind_fake_snapshot(monkeypatch, {"model": {"default": "snap-model"}})
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "code.py").write_text("x = 1\n", encoding="utf-8")
    cfg, errs = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errs, "gates") is True
    assert kbcfg.feature_enabled(cfg, errs, "assignment") is False
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gated", assignee="worker",
                             workspace_kind="dir", workspace_path=str(ws),
                             gate_spec=FAIL_SPEC)
        assert kb.claim_task(conn, tid, claimer="host:worker") is not None
        run_id = kb.get_task(conn, tid).current_run_id
        with pytest.raises(kb.GateFailedError):
            kb.complete_task(conn, tid, summary="done", expected_run_id=run_id)
        invalid = [e for e in kb.list_events(conn, tid)
                   if e.kind == "board_config_invalid"]
        assert invalid == []


def test_g12r2_section_non_dict_clobbers_with_error(board_home, monkeypatch):
    # P-2c: a non-dict SECTION clobbers via _deep_merge and the unchanged
    # validators surface "must be a mapping" — never a silent disable.
    _cfg(board_home, 'kanban:\n  gates: "yes"\n')
    _bind_fake_snapshot(monkeypatch, {"model": {"default": "snap-model"}})
    cfg, errs = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errs, "gates") is False
    gates_errs = kbcfg.section_errors(errs, "gates")
    assert any("must be a mapping" in e["message"] for e in gates_errs)


def test_g12r2_top_level_non_dict_triple(board_home):
    _cfg(board_home, 'kanban: "nope"\n')
    cfg, errs = kbcfg.load_board_config()
    assert len(errs) == 3
    assert {e["section"] for e in errs} == {"gates", "assignment", "escalation"}
    assert all(e["message"] == "top-level kanban: block is not a mapping"
               for e in errs)
    assert kbcfg.feature_enabled(cfg, errs, "gates") is False


# ---------------------------------------------------------------------------
# G-12R-3 — memo coherence + per-call merge/expansion
# ---------------------------------------------------------------------------

def test_g12r3_signature_memo_and_reload(board_home, monkeypatch):
    import hermes_cli.config_backend as cb

    _cfg(board_home, "kanban:\n  gates:\n    enabled: true\n")
    real_read = cb.read_config_doc
    calls = {"n": 0}

    def counting_read(path):
        calls["n"] += 1
        return real_read(path)

    monkeypatch.setattr(cb, "read_config_doc", counting_read)
    cfg1, _ = kbcfg.load_board_config()
    cfg2, _ = kbcfg.load_board_config()
    assert calls["n"] == 1
    assert cfg1 == cfg2
    assert kbcfg.feature_enabled(cfg1, [], "gates") is True
    _cfg(board_home, "kanban:\n  gates:\n    enabled: false\n    max_attempts_per_run: 9\n")
    cfg3, errs3 = kbcfg.load_board_config()
    assert calls["n"] == 2
    assert kbcfg.feature_enabled(cfg3, errs3, "gates") is False
    assert cfg3["kanban"]["gates"]["max_attempts_per_run"] == 9


def test_g12r3_failures_are_never_cached(board_home):
    good = "kanban:\n  gates:\n    enabled: true\n"
    _cfg(board_home, good)
    cfg, errs = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errs, "gates") is True
    # Malformed -> breakage branch (defaults + one error per section).
    _cfg(board_home, "kanban:\n  gates: [1, 2\n")
    cfg, errs = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errs, "gates") is False
    assert len(errs) == 3 and all(e["key"] == "config" for e in errs)
    # Fixed in place (new signature) -> next call returns the fixed values.
    _cfg(board_home, "kanban:\n  gates:\n    enabled: true\n    max_attempts_per_run: 7\n")
    cfg, errs = kbcfg.load_board_config()
    assert errs == []
    assert cfg["kanban"]["gates"]["max_attempts_per_run"] == 7
    # Corrupt again, then restore the ORIGINAL bytes under a NEW inode:
    # no failure may be memoized under any signature this process has seen.
    _cfg(board_home, "kanban:\n  gates: [1, 2\n")
    cfg, errs = kbcfg.load_board_config()
    assert len(errs) == 3
    tmp = board_home / "config.yaml.tmp"
    tmp.write_text(good, encoding="utf-8")
    os.replace(tmp, board_home / "config.yaml")
    cfg, errs = kbcfg.load_board_config()
    assert errs == []
    assert kbcfg.feature_enabled(cfg, errs, "gates") is True


def test_g12r3_deleted_file_evicts_memo(board_home):
    _cfg(board_home, "kanban:\n  gates:\n    enabled: true\n")
    cfg, errs = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errs, "gates") is True
    (board_home / "config.yaml").unlink()
    cfg, errs = kbcfg.load_board_config()
    assert errs == []
    assert kbcfg.feature_enabled(cfg, errs, "gates") is False


def test_g12r3_managed_overlay_merges_per_call(board_home, tmp_path, monkeypatch):
    # Board file signature held constant; the managed overlay alone changes.
    _cfg(board_home, "kanban:\n  gates:\n    enabled: false\n")
    cfg, errs = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errs, "gates") is False
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    (managed_dir / "config.yaml").write_text(
        "kanban:\n  gates:\n    enabled: true\n    max_attempts_per_run: 7\n",
        encoding="utf-8")
    cfg, errs = kbcfg.load_board_config()
    assert kbcfg.feature_enabled(cfg, errs, "gates") is True
    assert cfg["kanban"]["gates"]["max_attempts_per_run"] == 7
    (managed_dir / "config.yaml").write_text(
        "kanban:\n  gates:\n    enabled: true\n    max_attempts_per_run: 8\n",
        encoding="utf-8")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    cfg, errs = kbcfg.load_board_config()
    assert cfg["kanban"]["gates"]["max_attempts_per_run"] == 8


def test_g12r3_env_expansion_runs_per_call(board_home, monkeypatch):
    _cfg(board_home, (
        "kanban:\n"
        "  assignment:\n"
        "    enabled: true\n"
        "    seats:\n"
        "      argon:\n"
        '        model: "${BOARD_TEST_MODEL}"\n'
        '        provider: "custom:antigravity-bridge"\n'))
    monkeypatch.setenv("BOARD_TEST_MODEL", "model-a")
    cfg, errs = kbcfg.load_board_config()
    assert errs == []
    assert cfg["kanban"]["assignment"]["seats"]["argon"]["model"] == "model-a"
    monkeypatch.setenv("BOARD_TEST_MODEL", "model-b")
    cfg, errs = kbcfg.load_board_config()
    assert cfg["kanban"]["assignment"]["seats"]["argon"]["model"] == "model-b"


# ---------------------------------------------------------------------------
# §10.3 structural guard — the authority boundary gets a test, not a
# convention.  No production module outside hermes_cli/kanban_board_config.py
# may obtain the gates/assignment/escalation sections by reading kanban
# config through load_config()/load_config_readonly().
# ---------------------------------------------------------------------------

_FEATURE_SECTIONS = ("gates", "assignment", "escalation")
# The ONE allow-listed exception, named by file + key: _maybe_auto_subscribe
# reads kanban.auto_subscribe_on_create (not a §5.1 feature section).
_GUARD_ALLOWLIST = {("tools/kanban_tools.py", "auto_subscribe_on_create")}
_PROD_TREES = ("hermes_cli", "tools", "gateway", "agent")
_INTERCEPTED_LOADERS = {"load_config", "load_config_readonly"}
_CFG_PATH_RE = re.compile(
    r"""["']kanban["']\s*,\s*["'](gates|assignment|escalation)["']"""
    r"""|["']kanban["']\s*\]\s*\[\s*["'](gates|assignment|escalation)["']"""
    r"""|\.get\(\s*["']kanban["']\s*\)[^\n]{0,60}?(?:\.get\(|\[)\s*"""
    r"""["'](gates|assignment|escalation)["']""")


def test_g12r_structural_guard_no_intercepted_board_section_reads():
    violations = []
    auto_subscribe_reads = []
    for tree_name in _PROD_TREES:
        for path in sorted((REPO_ROOT / tree_name).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel == "hermes_cli/kanban_board_config.py":
                continue
            src = path.read_text(encoding="utf-8-sig")
            # (a) cfg_get-style kanban -> feature-section paths, any loader.
            for m in re.finditer(r"cfg_get\(", src):
                window = src[m.start():m.start() + 200]
                km = re.search(r"""["']kanban["']\s*,\s*["'](\w+)["']""", window)
                if km is None:
                    continue
                key = km.group(1)
                if key in _FEATURE_SECTIONS:
                    violations.append(f"{rel}: cfg_get kanban->{key}")
                if (rel, key) in _GUARD_ALLOWLIST:
                    auto_subscribe_reads.append((rel, key))
            # (b) an intercepted loader call in the same function as a
            # kanban -> feature-section index/get path.
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                seg = ast.get_source_segment(src, node) or ""
                has_loader = any(
                    isinstance(n, ast.Call)
                    and ((isinstance(n.func, ast.Name)
                          and n.func.id in _INTERCEPTED_LOADERS)
                         or (isinstance(n.func, ast.Attribute)
                             and n.func.attr in _INTERCEPTED_LOADERS))
                    for n in ast.walk(node))
                if has_loader and _CFG_PATH_RE.search(seg):
                    violations.append(f"{rel}:{node.name}: intercepted loader + "
                                      "kanban feature-section path")
    assert violations == [], violations
    # The allow-listed exception exists exactly as named (file + key).
    assert auto_subscribe_reads == [("tools/kanban_tools.py",
                                     "auto_subscribe_on_create")]
    # And the board loader itself no longer routes through the intercepted
    # loaders at all.
    board_src = (REPO_ROOT / "hermes_cli" / "kanban_board_config.py").read_text(
        encoding="utf-8-sig")
    assert "load_config_readonly" not in board_src
    assert "load_config(" not in board_src

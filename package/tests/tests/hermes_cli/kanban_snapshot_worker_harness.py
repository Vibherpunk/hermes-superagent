"""G-12R subprocess harness (FIX-DESIGN-REV2 §9.2).

Executed as a FRESH subprocess by tests/hermes_cli/test_kanban_board_config.py.
The worker snapshot bind is one-shot, pre-runtime-import state, so faithful
reproduction of a managed worker requires a fresh interpreter that binds
BEFORE importing the runtime — exactly as production does.

The harness imports only ``json``/``sys`` (and, for the bind itself,
``agent.managed_worker``) before calling, in production order:

1. ``agent.managed_worker.bind_worker_policy(frame)`` — the production
   binding entry (NOT a direct ``_bind_worker_config`` call);
2. ``gateway.session_kanban.bind_worker_context(frame)`` — pins the board
   DB env from the frame's ``kanban_json``, validates the claim, chdirs to
   the workspace.

The frame is producer-shaped per §9.2 step 1 (and verification nit L-2):
``policy.source == 'kanban'`` is named explicitly, ``policy.config_json``
and ``policy.kanban_json`` are JSON STRINGS (the round-trip through
``json.loads`` inside the binders is part of the production path), and
``kanban_json`` carries every key ``bind_worker_context`` indexes directly:
``board``, ``task_id``, ``run_id``, ``claim_lock``, ``db``, ``workspace``,
``branch``, ``tenant``, ``goal_mode``, ``workspaces_root``, ``profile``,
``max_runtime_seconds`` (plus the remaining producer keys ``skills``,
``goal_max_turns``, ``goal_text``, ``context``, ``accept_hooks``).

Usage: ``python kanban_snapshot_worker_harness.py <scenario.json>``.
Prints one ``G12R_RESULT <json>`` line with per-leg results.
"""

import json
import sys


def _emit(results):
    print("G12R_RESULT " + json.dumps(results), flush=True)


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        scenario = json.load(f)
    frame = scenario["frame"]

    # --- Production binding, in production order, before runtime imports.
    from agent.managed_worker import bind_worker_policy

    bind_worker_policy(frame)

    from gateway.session_kanban import bind_worker_context

    bind_worker_context(frame)

    # --- Runtime imports are legal only from here on.
    results = {"mode": scenario["mode"]}

    from hermes_cli import kanban_board_config as kbcfg
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    if scenario["mode"] == "standalone":
        results["kanban_home"] = str(kb.kanban_home())
        cfg, errs = kbcfg.load_board_config()
        results["gates_enabled"] = kbcfg.feature_enabled(cfg, errs, "gates")
        results["errors"] = errs
        _emit(results)
        return

    # Leg 4a (unit probe): the direct probe of the defect, under the
    # production-bound snapshot.
    cfg, errs = kbcfg.load_board_config()
    probe = {
        "gates_enabled": kbcfg.feature_enabled(cfg, errs, "gates"),
        "escalation_enabled": kbcfg.feature_enabled(cfg, errs, "escalation"),
        "errors": errs,
    }
    # Boundary guard: the snapshot mechanism itself is untouched — the
    # frozen snapshot is bound, carries no kanban key, and plain session
    # config reads still answer from it (gates OFF there).
    from agent.safe_worker_policy import worker_config_snapshot

    snapshot = worker_config_snapshot()
    probe["snapshot_bound"] = snapshot is not None
    probe["snapshot_has_kanban"] = isinstance(snapshot, dict) and "kanban" in snapshot
    from hermes_cli.config import load_config_readonly

    session_cfg = load_config_readonly()
    probe["session_gates_enabled"] = (
        session_cfg.get("kanban", {}).get("gates", {}).get("enabled") is True
        if isinstance(session_cfg.get("kanban"), dict)
        and isinstance(session_cfg.get("kanban", {}).get("gates"), dict)
        else False
    )
    results["probe"] = probe

    fail_spec = {"version": 1, "checks": [{"id": "f", "argv": scenario["fail_argv"]}]}
    pass_spec = {"version": 1, "checks": [{"id": "p", "argv": scenario["pass_argv"]}]}
    workspace = scenario["workspace"]
    profile = scenario["profile"]

    def _events(conn, tid, kind=None):
        return [e for e in kb.list_events(conn, tid) if kind is None or e.kind == kind]

    def _mk(conn, spec):
        tid = kb.create_task(conn, title="g12r", assignee=profile,
                             workspace_kind="dir", workspace_path=workspace,
                             gate_spec=spec)
        claimed = kb.claim_task(conn, tid, claimer="host:worker")
        assert claimed is not None
        return tid, claimed.current_run_id

    with kbc.connect() as conn:
        # Leg 4b (spec-fail): the frame's own claimed card, max_attempts=1.
        tid1 = scenario["first_task_id"]
        run1 = scenario["first_run_id"]
        leg_fail = {}
        try:
            kb.complete_task(conn, tid1, summary="done", expected_run_id=run1)
            leg_fail["raised"] = None
        except kb.GateFailedError as exc:
            leg_fail["raised"] = "GateFailedError"
            leg_fail["exhausted"] = bool(exc.exhausted)
        task1 = kb.get_task(conn, tid1)
        leg_fail["status"] = task1.status
        leg_fail["structured_failures"] = task1.structured_failures
        leg_fail["gate_failed_events"] = len(_events(conn, tid1, "gate_failed"))
        counted = [e.payload for e in _events(conn, tid1, "gate_attempt_counted")]
        leg_fail["counted_true"] = sum(1 for p in counted if p.get("counted") is True)
        results["spec_fail"] = leg_fail

        # Leg 5 (spec-pass): completes with the §2.5 gate evidence shape.
        tid2, run2 = _mk(conn, pass_spec)
        leg_pass = {"completed": kb.complete_task(conn, tid2, summary="done",
                                                  expected_run_id=run2)}
        task2 = kb.get_task(conn, tid2)
        leg_pass["status"] = task2.status
        leg_pass["gate_passed_events"] = len(_events(conn, tid2, "gate_passed"))
        completed = _events(conn, tid2, "completed")
        gate_payload = completed[-1].payload.get("gate") if completed else None
        leg_pass["completed_gate_verdict"] = (
            gate_payload.get("verdict") if isinstance(gate_payload, dict) else None)
        results["spec_pass"] = leg_pass

    # Leg 6 (kill switch, same live process): an operator edit to the board
    # home config takes effect at the NEXT completion call in this process.
    with open(scenario["board_config_path"], "w", encoding="utf-8") as f:
        f.write(scenario["kill_switch_config"])
    with kbc.connect() as conn:
        tid3, run3 = _mk(conn, fail_spec)
        leg_kill = {"completed": kb.complete_task(conn, tid3, summary="done",
                                                  expected_run_id=run3)}
        task3 = kb.get_task(conn, tid3)
        leg_kill["status"] = task3.status
        leg_kill["gate_events"] = sum(
            1 for e in _events(conn, tid3) if e.kind.startswith("gate_"))
        results["kill_switch"] = leg_kill

    _emit(results)


if __name__ == "__main__":
    main()

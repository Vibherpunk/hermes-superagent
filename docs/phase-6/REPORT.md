# Phase 6 Report — VibeHard Source Poller, Stuck Detection & Periodic Tick Wiring

**Date**: 2026-11-03  
**Branch**: `superagent-int`  
**Worktree**: `superagent-int`  
**Base Context**: The final stacked tree (`superagent-int`) incorporating Phases 0 through 5, with Phase 6 landed via cherry-picks (6.2 as `933deeddf9` / `e69f9e75d9`, 6.3 as `620f1176d9` / `f2da424522`), and tick wiring completed by INT-2 (`96577422dc`).  
**Invariant Posture**: `superagent.enabled` remains `false` in production configs; live daemon configurations remain untouched; observe-only admission intact.

---

## 1. Executive Summary

Phase 6 implements the **VibeHard Source Poller** for the Hermes Super-agent orchestrator. The subsystem continuously inspects the local VibeHard ledger (read-only SQLite database), detects newly submitted jobs, tracks status transitions, diagnoses stalled/stuck worker jobs in non-terminal execution states, and emits canonical triggers into `sa_triggers` via `hermes_cli.orchestrator.receive_trigger`.

All phase cards (6.1, 6.2, 6.3, and 6.4) are complete, with deterministic test coverage verifying zero-regression stability across the full 30-suite Superagent test battery.

---

## 2. Card Execution & Review Table

| Card | Scope & Title | Deliverables | Review Seat & Status | Reference / Commit |
|---|---|---|---|---|
| **6.1** | Recon: VibeHard source analysis | Architectural reconnaissance of VibeHard SQLite ledger schema (`jobs`, `sessions`), transition semantics, cursor state models, and polling strategy. | Supervisor — **Complete** | `executor-pack/phase-6/SOURCE-RECON.md` |
| **6.2** | VibeHard poller module + config | Poller module `hermes_cli/superagent_vibehard.py`, config defaults and validation (`superagent.vibehard.*`), schema cursor tracking (`sa_source_cursors`), and unit tests. | Independent Reviewer — **PASS** (findings: none) | `REVIEW-p6-6.2.md` (`933deeddf9` / `e69f9e75d9`) |
| **6.3** | Stuck detection + gate posture | Heartbeat and updated_at staleness analysis (`WORKING_STATUSES`), stuck trigger emission, observe-mode admission posture in gate, and edge-case tests. | Independent Reviewer — **PASS** (findings: none) | `REVIEW-p6-6.3.md` (`620f1176d9` / `f2da424522`) |
| **6.4** | Phase 6 acceptance + REPORT | Full test suite verification across all Superagent subsystems, integration documentation, and formal acceptance reporting. | Current Builder — **PASS** | *(This card / commit)* |

---

## 3. Tick Wiring Note (INT-2)

The periodic driver wiring for Phase 6 was implemented by **INT-2** (commit `96577422dc: superagent int-2: Periodic driver wiring for eval expiry + source polls`):

- **Integration Point**: `run_superagent_tick` in `hermes_cli/orchestrator.py` was wired into `gateway/kanban_watchers.py` inside the `_kanban_notifier_watcher` loop (running on a 5-second sleep interval).
- **Throttling**: `run_superagent_tick` enforces a strict 60-second throttle using monotonic clock comparisons (`now - _last_superagent_tick_at >= 60.0`), preventing high-frequency polling overhead while ensuring timely sweep execution.
- **Enabled Gate**: Gated completely by `superagent.enabled: true` in configuration. If disabled, the tick immediately short-circuits returning `{"status": "disabled"}`.
- **Source Orchestration**:
  1. Calls `expire_evals(now, ...)` to transition overdue trigger evaluations from `evaluating` / `queued` to terminal expiry.
  2. Calls `poll_github(conn, cfg, now)` when `superagent.github.enabled` is true.
  3. Calls `poll_vibehard(conn, cfg, now)` when `superagent.vibehard.enabled` is true.
- **Import Guarding**: The import of `poll_vibehard` is isolated inside a `try / except ImportError` block within `_run_superagent_tick_on_conn`. If the module is absent or unavailable, the tick logs an informational notice and proceeds without raising exceptions. All poller invocations are wrapped in exception catch guards (`logger.warning`) to preserve daemon loop resilience.

---

## 4. Live-Ledger Smoke Fact & Verification

As documented in the supervisory reconnaissance (`executor-pack/phase-6/SOURCE-RECON.md`) and verified against the live environment:

- **Database Location**: the ledger in the dogfood home on the build machine (the shipped default, `~/.vibehard-agent/ledger.sqlite`; accessed read-only via SQLite URI `file:...mode=ro`). Its ledger is live.
- **Live Inventory Breakdown**: over a hundred real jobs, roughly half failed, with the rest spread across `complete`, `awaiting_human`, and a small number of `pending` jobs.
- **Smoke Candidates**: The pending jobs (`j_<id>` and `j_<id>`), originally submitted earlier and not recently updated, represent genuine stuck jobs (inactivity exceeding the default 1800s threshold). When `superagent.vibehard.enabled` is toggled on in observe mode, these jobs serve as the primary live validation candidates that the VibeHard poller will diagnose and emit as `kind='stuck'` triggers.

---

## 5. Architectural Deviations & Scope Boundaries

1. **Parallel Worktree Stacking**: Per Adam's stacking order, the Phase 6 module was authored and verified in a parallel feature worktree (`superagent-p6`), then cherry-picked into the final integration tree (`superagent-int`) following the completion of Phase 4 and Phase 5 PRs.
2. **Observe-Only Admission**: In v1, all VibeHard triggers are admitted in observe mode. The orchestrator receives and tracks VibeHard job lifecycle events, but does not autonomously transition tickets into execution loops without operator instruction.
3. **No Worker Execution on VibeHard in v1**: Hermes Superagent workers never directly mutate, cancel, retry, or claim VibeHard jobs in v1. VibeHard acts strictly as an external monitored source.
4. **Direct Read-Only SQLite Polling**: The architecture deliberately selects direct SQLite polling via `mode=ro` over connecting via MCP or the VibeHard Daemon Protocol (VDP). As justified during recon (Card 6.1), the read-only SQLite path guarantees zero lock contention with active VibeHard worker runners, eliminates process lifecycle coupling, requires zero socket plumbing, and provides atomic reads of jobs joined to session metadata.

---

## 6. Acceptance Test Evidence

The full Superagent test suite was executed in the `superagent-int` worktree:

```bash
./scripts/run_tests.sh tests/superagent/
```

### Verbatim Summary Line

```
=== Summary: 30 files, 295 tests passed, 0 failed (100% complete) in 14.1s (14 workers) ===
```

### Subsystem Verification Highlights
- **VibeHard Poller & Stuck Detection**: `tests/superagent/test_vibehard_source.py` (13/13 passed in 2.85s)
  - First-sight job discovery (`submitted` trigger emission)
  - State progression tracking (`state_change` trigger emission)
  - Terminal failure alerts (`failed` trigger emission with error text)
  - Inactivity and staleness evaluation (`stuck` trigger emission for stalled `pending`/`claimed`/`running` jobs)
  - Status progression stuck-flag reset (clearing `stuck_flag` upon activity or stage change)
  - Read-only URI enforcement and connection resilience
- **Periodic Tick Driver**: `tests/superagent/test_periodic_driver.py` (7/7 passed in 1.95s)
  - 60s throttle timing and force override
  - Gated dispatch for disabled state
  - Independent source poll routing for GitHub and VibeHard
- **Gate & Admission Invariants**: `tests/superagent/test_gate.py` (13/13 passed in 2.9s)
  - First-party actor matching and observe mode admission policies
- **Configuration & Defaults**: `tests/superagent/test_config.py` (23/23 passed in 3.8s)
  - Complete validation for `superagent.vibehard.enabled`, `ledger_path`, and `stuck_after_seconds`

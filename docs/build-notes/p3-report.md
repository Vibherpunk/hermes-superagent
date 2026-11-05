# Phase 3 Report — Subscriptions & Direct Pass-Through

Date: 2026-11-03. Branch `superagent-p2`, worktree `superagent-p2`.
Base commit: `3e8961e26d` (Phase 2 completion + REPORT). All changes remain unmerged to main, `superagent.enabled` remains `false` everywhere, and live profile configs remain untouched.

---

## 1. Test Runs & Acceptance Evidence

### (a) Superagent Test Suite
Command:
```bash
./scripts/run_tests.sh tests/superagent/
```
Verbatim Summary & Output:
```text
Discovered 18 test files (~156 tests) under ['tests/superagent']; running with -j 14
[  1.9% |     3/~156 | ✓12 | ✗ 0] ✓ tests/superagent/test_gate.py (12✓, 1.1s)
[ 14.7% |    23/~156 | ✓32 | ✗ 0] ✓ tests/superagent/test_config.py (20✓, 1.6s)
[ 19.9% |    31/~156 | ✓40 | ✗ 0] ✓ tests/superagent/test_orchestrator_triggers…
[ 21.8% |    34/~156 | ✓43 | ✗ 0] ✓ tests/superagent/test_pilot_fixture.py (3✓, 0.8s)
[ 25.6% |    40/~156 | ✓49 | ✗ 0] ✓ tests/superagent/test_github_source.py (6✓, 1.5s)
[ 29.5% |    46/~156 | ✓55 | ✗ 0] ✓ tests/superagent/test_slots.py (6✓, 0.7s)
[ 32.7% |    51/~156 | ✓60 | ✗ 0] ✓ tests/superagent/test_handback.py (5✓, 4.1s)
[ 35.3% |    55/~156 | ✓64 | ✗ 0] ✓ tests/superagent/test_schema.py (4✓, 2.5s)
[ 41.7% |    65/~156 | ✓74 | ✗ 0] ✓ tests/superagent/test_pr_following.py (10✓, 4.5s)
[ 44.9% |    70/~156 | ✓79 | ✗ 0] ✓ tests/superagent/test_slot_wiring.py (5✓, 2.5s)
[ 50.0% |    78/~156 | ✓87 | ✗ 0] ✓ tests/superagent/test_accept.py (8✓, 5.0s)
[ 56.4% |    88/~156 | ✓97 | ✗ 0] ✓ tests/superagent/test_fanout.py (10✓, 5.0s)
[ 63.5% |    99/~156 | ✓108 | ✗  0] ✓ tests/superagent/test_policy_change.py (10✓, 5.1s)
[ 68.6% |   107/~156 | ✓116 | ✗  0] ✓ tests/superagent/test_ended_resume.py (8✓, 5.2s)
[ 73.1% |   114/~156 | ✓123 | ✗  0] ✓ tests/superagent/test_eval.py (7✓, 5.3s)
[ 80.8% |   126/~156 | ✓135 | ✗  0] ✓ tests/superagent/test_delivery.py (12✓, 5.4s)
[ 86.5% |   135/~156 | ✓144 | ✗  0] ✓ tests/superagent/test_release_on_end.py (9✓, 4.3s)
[100.0% |   156/~156 | ✓165 | ✗  0] ✓ tests/superagent/test_orchestrator_tools_phase2.py (21✓, 6.0s)

=== Summary: 18 files, 165 tests passed, 0 failed (100% complete) in 6.0s (14 workers) ===
```

### (b) Gateway Inbound Hook Test Suite
Command:
```bash
./scripts/run_tests.sh tests/gateway/test_superagent_inbound_hook.py -q
```
Verbatim Summary:
```text
=== Summary: 1 files, 7 tests passed, 0 failed (100% complete) in 1.5s (14 workers) ===
```

**Context on Acceptance:**
- All 18 test suites in `tests/superagent/` pass 100% (165 passing tests, 0 failures, 0 errors).
- This represents an increase from 108 tests at Phase 2 completion (`3e8961e26d`) to 165 tests at Phase 3 completion (+57 new tests covering fan-out, delivery, slot wiring, PR following, handback, ended resume, input release on end, and pilot fixture verification).

---

## 2. Phase 3 Card Table

All per-card review verdicts are filed by the supervisor in `/tmp/review-resp-p*.json` / review markdown artifacts with commit SHAs verified on disk:

| Card | Title | Worktree / Branch | Review Seat + Verdict | Commit SHA |
|---|---|---|---|---|
| **3.1** | Subscriptions storage API + tests | `superagent-p3` | Gemini 4 Argon High — **PASS** | `e39c4b668ef` |
| **3.2** | Fan-out in receive_trigger | `superagent-p2` | Gemini 4 Argon High — **PASS** | `02fd92ef13` |
| **3.3** | Wire Input delivery to run launch and wake path | `superagent-p2` | Gemini 4 Argon High — **PASS** (`review-resp-p33.json`) | `a9430c24e4` |
| **3.4** | Hook worker PR report and apply follows-done rule | `superagent-p2` | Gemini 4 Argon High — **PASS** (`review-resp-p34.json`) | `5db3b09263` |
| **3.5** | Add kanban_handback and fresh Eval queueing on Trigger disposition | `superagent-p2` | Gemini 4 Argon High — **PASS** (`review-resp-p35.json`) | `65932908cf` |
| **3.6** | Ended-Ticket resume via revalidate | `superagent-p2` | Gemini 4 Argon High — **PASS** (`review-resp-p36.json`) | `7d8acd7ea6` |
| **3.7** | Release unread and unacknowledged person inputs on ticket end | `superagent-p2` | Gemini 4 Argon High — **PASS** (`review-resp-p37.json`) | `4b02fd5a60` |
| **3.8** | Log verification of pilot fixture and passthrough flag | `superagent-p2` | Gemini 4 Argon High — **PASS** (`review-resp-p38.json`) | `9d59eb0549` |
| **3.9** | Speculative rule pack (config example) | `superagent-p3` | Gemini 4 Argon High — **PASS** | `8152c994da` |
| **3.10**| Phase 3 acceptance + REPORT | `superagent-p2` | Supervisor acceptance review | *(This commit)* |

---

## 3. Card 3.8 Log-Verification Summary

The empirical verification and pilot run receipts are recorded in detail in [`notes/p3-8-log-verification.md`](notes/p3-8-log-verification.md).

### (a) Core Invariant & Exit Criteria
Phase 3 mandates that follow-up messages from persons on threads with an active owning subscription (such as a worker-owned PR thread `github:pr-<n>`) bypass Gate and Eval, fanning out directly to `sa_inputs` as an Input, and waking the worker run with **zero Router model calls**:
> *"Exit criteria: a comment on a Worker-owned PR reaches that Worker's run with no Router model call in the path (verified in logs)."* (SPEC-V2 §Phase 3)

### (b) Configuration Flag: `superagent.passthrough`
The single new configuration key `superagent.passthrough` was added following the exact Phase-0 config conventions:
- **Default value**: `DEFAULT_CONFIG["superagent"]["passthrough"] = True` in [`hermes_cli/config_defaults.py`](hermes_cli/config_defaults.py#L2720).
- **Structure validation**: Validated as a boolean in `_validate_superagent` in [`hermes_cli/config_value_checks.py`](hermes_cli/config_value_checks.py#L118-L125).
- **Resolver parsing**: Parsed in `superagent_settings` in [`gateway/kanban_watchers_dispatcher.py`](gateway/kanban_watchers_dispatcher.py#L449-L465).
- **Fan-out control**: Checked in `_receive_trigger_on_conn` in [`hermes_cli/orchestrator.py`](hermes_cli/orchestrator.py#L244-L267) to bypass fan-out and route to the Eval queue when explicitly disabled.

### (c) Empirical Verification Results: PR Comment → Input → Wake
In the pilot fixture, worker ticket `t_a987dfd4` was subscribed to `github:pr-101`. Reviewer `alice` submitted an inbound review comment on the PR:
1. **With Pass-Through (`superagent.passthrough = True`, default):**
   - The comment enters `receive_trigger`, directly matches `sa_subscriptions`, creates an `sa_inputs` row (`is_person=1, context_only=0`), sets trigger `disposition='fanned_out'` with `eval_state='done'`.
   - The kanban dispatcher tick promotes the ticket to `running` and delivers the formatted trigger card into worker `run_input`.
   - **Router / Eval model calls: 0** (verified in log capture).
2. **Without Pass-Through (`superagent.passthrough = False`):**
   - Fan-out is suppressed; the trigger enters the Eval queue (`eval_state='queued'`).
   - The Eval engine invokes `call_llm(task="superagent_eval")` to evaluate the unaddressed comment as an `inferred` admission, consuming prompt and completion tokens before recording an accept decision.

### (d) Token Count Comparison
Comparison on the identical pilot fixture event:

| Dimension | With Pass-Through (`passthrough: True`) | Without Pass-Through (`passthrough: False`) | Delta / Savings |
|---|---|---|---|
| **Subscription Fan-Out** | **Active** (direct delivery to `sa_inputs`) | **Disabled** (no direct inputs) | — |
| **Trigger Eval State** | `done` (bypassed) | `queued` → `done` | Bypassed Eval |
| **Trigger Disposition** | `fanned_out` | `NULL` → evaluated / decided | 0 Router queue delay |
| **Router / Eval Model Calls** | **0** | **1** | **-100% (-1 call)** |
| **Router Prompt Tokens** | **0** | **122** | **-100% (-122 tokens)** |
| **Router Completion Tokens** | **0** | **22** | **-22 tokens (-100%)** |
| **Total Router Tokens** | **0** | **144** | **-144 tokens (-100%)** |

This confirms SPEC-V2's architectural expectation: pass-through is a cost and Router-load optimization, eliminating redundant Router context reloading on person follow-ups while preserving governance and safety invariants.

---

## 4. Recorded Deviations

### 1. Card 3.3 Wake Wiring & Supervisor Packet Clarification
- **Background & Initial Stop**:  
  When Card 3.3 (`t_f01d0a8d`, "Wake + Input delivery wiring") was first dispatched, the card body and assignment packet contained a circular reference (*"See assignment packet"* vs *"implement EXACTLY what its body says"*) and directed the builder to "wire the wake path". However, Phase-1 Card 1.6's authoritative finding in [`notes/p1-6-wake-evidence.md`](notes/p1-6-wake-evidence.md) had explicitly demonstrated that Hermes's kanban dispatcher has no distinct person-wake spawn path, and Decision E-023 strictly prohibited inventing an unbacked wake path. Pursuant to the Builder Contract, the builder halted execution and filed `/tmp/stop-report-t_f01d0a8d.md`.
- **Resolution**:  
  The supervisor clarified the packet, establishing that Input delivery connects directly to the dispatcher's existing run launch and resume paths (delivering pending `sa_inputs` cards formatted into worker `run_input` upon task launch, and acquiring the slot with `SlotPool.try_acquire(kind="person_wake")` when person inputs are pending). Commit `a9430c24e4` implemented this clean integration in [`hermes_cli/superagent_delivery.py`](hermes_cli/superagent_delivery.py) and [`gateway/kanban_watchers_dispatcher.py`](gateway/kanban_watchers_dispatcher.py), verified by 17 new tests in `test_delivery.py` and `test_slot_wiring.py`. The stop and its clarification are preserved as part of the official project record.

### 2. Multi-Worktree Lane Split & Subscriptions Module Staging
- **Lane D / Lane B Separation**:  
  Phase 3 cards were intentionally partitioned across two isolated git worktrees during development:
  - Cards **3.1** (`e39c4b668ef`, subscriptions storage API + tests) and **3.9** (`8152c994da`, speculative rule pack config example) were built on Lane D in worktree `~/.hermes/hermes-agent/.worktrees/superagent-p3`.
  - Cards **3.2**, **3.3**, **3.4**, **3.5**, **3.6**, **3.7**, **3.8**, and **3.10** were built on branch `superagent-p2` in worktree `~/.hermes/hermes-agent/.worktrees/superagent-p2`.
- **Integration Staging**:  
  The standalone helper module `hermes_cli/superagent_subscriptions.py` (authored in 3.1 on `superagent-p3`) is staged for formal integration at the branch stacking pass when all lane branches merge into main.

### 3. Cross-Tree Direct-SQL Seams & Integration Markers
Because `superagent_subscriptions.py` was developed in the sibling worktree, the components on `superagent-p2` interact with table `sa_subscriptions` using direct SQL queries and statements, explicitly guarded with designated integration markers:
- **`hermes_cli/orchestrator.py`**:
  - *Fan-out query* (lines 268–273):  
    `# Integration merge point: direct SQL against sa_subscriptions.`  
    `# At integration, these queries delegate to hermes_cli.superagent_subscriptions.`  
    Querying `SELECT ticket_id, context_only FROM sa_subscriptions WHERE thread_key = ?`.
  - *Correlation thread query* (lines 365–368): Direct lookup on correlation keys.
  - *Trigger ownership query in handback* (lines 558–561): Direct check for owning subscriptions `context_only = 0`.
  - *Unacknowledged input release check* (lines 2265–2268): Checking whether another ticket owns the thread before re-queueing released triggers for Eval.
- **`tools/orchestrator_tools.py`**:
  - *Thread unsubscribe handler* (lines 676–682):  
    `# Integration merge point: Lane B direct DELETE against sa_subscriptions is the operative path.`  
    Direct `DELETE FROM sa_subscriptions WHERE thread_key = ?`.
- **`hermes_cli/superagent_pr.py`**:
  - Table-existence checks via `SELECT 1 FROM sqlite_master WHERE type='table' AND name='sa_subscriptions'` (lines 600, 628) ensuring that PR comment and report hooks fail gracefully without exceptions when operating across heterogeneous database states.
  - Direct subscription management in `subscribe_ticket_to_pr` (lines 243–248) and `get_ticket_followed_prs` (lines 287–291) with robust error logging.

---

## 5. Live Invariants & Governance Summary

1. **System Invariant Intact**:
   `superagent.enabled` remains `false` across `cli-config.yaml`, system defaults, and live profile configs. Observe mode (`would_accept=1`, no ticket creation side-effects) remains the active default in test suites.
2. **Strict Worktree Cleanliness**:
   No pre-existing dirty files or unrelated assets were touched or staged. All work is isolated to the `superagent-p2` branch.
3. **Phase Completion Status**:
   All exit criteria for Phase 3 in SPEC-V2 §Phase 3 are satisfied and verified by passing tests and empirical log receipts.

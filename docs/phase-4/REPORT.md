# Phase 4 Report — Outbox, Pause, and Health Triggers

Date: 2026-11-03. Branch `superagent-p4`, worktree `superagent-p4`.
Base commit: `e86086a094` (Phase 1 completion) with cherry-picked Phase 2 schema (`f61a64c352`, `9c53e97a91`). All changes remain unmerged to main, `superagent.enabled` remains `false` everywhere, and live profile configs remain untouched.

---

## 1. Test Runs & Acceptance Evidence

### (a) Superagent Test Suite
Command:
```bash
bash scripts/run_tests.sh tests/superagent/ -q
```
Pasted summary:
```
Discovered 7 test files (~61 tests) under ['tests/superagent']; running with -j 14
[  9.8% |     6/~61 | ✓6 | ✗0] ✓ tests/superagent/test_slots.py (6✓, 0.4s)
[ 36.1% |    22/~61 | ✓22 | ✗ 0] ✓ tests/superagent/test_config.py (16✓, 0.7s)
[ 42.6% |    26/~61 | ✓26 | ✗ 0] ✓ tests/superagent/test_schema.py (4✓, 1.0s)
[ 55.7% |    34/~61 | ✓34 | ✗ 0] ✓ tests/superagent/test_pause.py (8✓, 1.6s)
[ 68.9% |    42/~61 | ✓42 | ✗ 0] ✓ tests/superagent/test_outbox.py (8✓, 1.7s)
[ 83.6% |    51/~61 | ✓51 | ✗ 0] ✓ tests/superagent/test_outbox_drain.py (9✓, 1.8s)
[100.0% |    61/~61 | ✓61 | ✗ 0] ✓ tests/superagent/test_health.py (10✓, 1.9s)

=== Summary: 7 files, 61 tests passed, 0 failed (100% complete) in 1.9s (14 workers) ===
```

### (b) Suite Breakdown & Capabilities Verified
- `tests/superagent/test_slots.py` (6 passed): Worker slot limits and concurrency invariants.
- `tests/superagent/test_config.py` (16 passed): Configuration parsing, defaults, and type enforcement.
- `tests/superagent/test_schema.py` (4 passed): DDL verification for superagent tables (`sa_triggers`, `sa_decisions`, `sa_mandates`, `sa_outbox`, `sa_meta`).
- `tests/superagent/test_outbox.py` (8 passed):
  - Immediate eligibility (`status='queued'` returned by `due_replies`).
  - Head-of-line ordering gate: subsequent rows for the same ticket and address identity `(ticket_id, platform, chat_id, thread_id)` are blocked while an earlier row is `'queued'`, `'sending'`, or `'failed'` with `attempts < 3`.
  - Independent ordering for distinct destination addresses.
  - Exponential doubling backoff sequence (5s, 10s, 20s, 40s, capped at 300s).
  - External flood hints (`flood_hint_seconds`) overriding default backoff.
  - Permanent dead-lettering once attempts reach 3 (`attempts >= 3` never due).
  - Terminal drop (`mark_dropped` rows never retried, do not block later rows).
- `tests/superagent/test_outbox_drain.py` (9 passed):
  - In-order dispatch and atomic status transition to `'sent'` with recorded `receipt_json`.
  - Conservative unreachable recipient mapping (e.g. chat not found, bot blocked, user deactivated $\rightarrow$ `mark_dropped`).
  - Flood and rate-limit error mapping via `gateway/delivery_ledger.py` helpers.
  - 100-word chat platform body limiter (`telegram`, `slack`, `discord`, `dingtalk`) marking `last_error='over_limit'` and tracking notice counts in `sa_meta.over_limit_notices`.
  - Non-chat platforms (e.g. `github`) exempt from 100-word limit.
  - Crash safety: recovery of stale `'sending'` rows older than 10 minutes, resetting to `'failed'` without double-counting attempt increments.
- `tests/superagent/test_pause.py` (8 passed):
  - Pause state roundtrip and timestamp recording in `sa_meta` (`paused` and `paused_at`).
  - Idempotent double-pause invariant: subsequent `set_paused(True)` calls preserve the original `paused_at`.
  - Elapsed pause delta computation.
  - Shift arithmetic helper `shifted_deadline`.
  - Resume shift helper `apply_pause_shift`: shifts `eval_deadline` by the elapsed pause duration for all pending triggers (`eval_state IN ('queued', 'evaluating')`), preserving all other `meta_json` keys.
- `tests/superagent/test_health.py` (10 passed):
  - Pinned trigger row shape matching `receive_trigger` specifications (`id=trg_<uuid4hex12>`, `source='internal'`, `scope='health'`, `kind='health'`, `eval_state='queued'`).
  - Hourly deduplication bucket via `idempotency_key=f"health:{kind}:{subject}:{now // 3600}"`.
  - Subject isolation: distinct subjects produce separate triggers.
  - Repository dispatcher event vocabulary mapping across all four health categories (`reclaimed`, `orphaned_running`, `circuit_breaker`, `gave_up`).
  - Strict input validation rejecting unknown event kinds with `ValueError`.

---

## 2. Card Table

All per-card review verdicts are filed by the supervisor in the review log and payload records, with commit SHAs recomputed on disk and matching:

| Card | Title | Builder Tier | Review Seat + Verdict | Commit SHA |
|---|---|---|---|---|
| **4.1** | Recon (no code) | Tier F (`gemini-3.8-flash-high`) | Gemini 4 Argon High — **PASS** (`review-resp-p41.json`) | `db67093a1e8` |
| **2.2** *(prereq)* | Schema for the superagent tables | Tier F (`gemini-3.8-flash-high`) | Gemini 4 Argon High — **PASS** (Phase 2 review) | `f61a64c352` |
| **4.2** | Outbox API in a NEW module hermes_cli/superagent_outbox.py | Tier F (`gemini-3.8-flash-high`) | Gemini 4 Argon High — **PASS** (`review-resp-p42.json`) | `e495f6d4621` |
| **4.3** | Outbox drain | Tier F (`gemini-3.8-flash-high`) | Gemini 4 Argon High — **PASS** (`review-resp-p43.json`) | `a4e071fa8b0` |
| **4.7** | Pause (set_paused + deadline shift) | Tier F (`gemini-3.8-flash-high`) | Gemini 4 Argon High — **PASS** (`review-resp-p47.json`) | `874d5935d1b` |
| **2.2fix** *(prereq)* | sa_mandates ticket_id not unique (E-044) | Tier F (`gemini-3.8-flash-high`) | Gemini 4 Argon High — **PASS** (Phase 2 review) | `9c53e97a91` |
| **4.6** | Health triggers (dispatcher tick) | Tier F (`gemini-3.8-flash-high`) | Gemini 4 Argon High — **PASS** (`review-resp-p46.json`) | `f733e1b9667` |
| **4.4** | Move Router replies + notifier onto Outbox | Integration card | Landing later on stacked tree | *(Deferred to integration)* |
| **4.5** | Cron migration to Outbox | Integration card | Landing later on stacked tree | *(Deferred to integration)* |
| **4.8** | Phase 4 acceptance + REPORT | Tier F (`gemini-3.8-flash-high`) | Supervisor acceptance review | *(This commit)* |

---

## 3. End-to-End Chain Dump: Outbox, Health Triggers, and Pause Lifecycle

The following verbatim output was generated against a temporary database built via `hermes_cli.kanban_db_connect.init_db()` in this worktree, executing full end-to-end chains across the Phase 4 subsystems:

```
=== TABLE: sa_outbox ===
{
  "id": 1,
  "ticket_id": "t_test_01",
  "source": "telegram",
  "address_json": "{\"chat_id\": \"-100123\", \"platform\": \"telegram\", \"thread_id\": null}",
  "purpose": "reply",
  "body": "Task completed successfully",
  "status": "sent",
  "attempts": 0,
  "next_attempt_at": 1762180001,
  "last_error": null,
  "dropped_reason": null,
  "receipt_json": "{\"message_id\": 999, \"sent_at\": 1762180001}",
  "created_at": 1762180000,
  "sent_at": 1762180001
}

=== TABLE: sa_triggers (health) ===
{
  "id": "trg_9e099bdb2300",
  "source": "internal",
  "scope": "health",
  "thread_key": "health:reclaimed:run_001",
  "actor_source_id": null,
  "actor_is_bot": 0,
  "addressed": 0,
  "kind": "health",
  "body": "Health alert [reclaimed] on run_001: stale worker reclaimed",
  "meta_json": "{\"detail\": \"stale worker reclaimed\"}",
  "correlation_json": "[]",
  "idempotency_key": "health:reclaimed:run_001:489494",
  "eval_state": "queued",
  "eval_requeues": 0,
  "disposition": null,
  "created_at": 1762180000
}

=== TABLE: sa_meta (paused) ===
paused: 1
paused_at: 1762180000

=== TABLE: sa_triggers (deadline shifted after resume) ===
{
  "id": "trg_eval_01",
  "source": "internal",
  "scope": "health",
  "thread_key": "th_1",
  "actor_source_id": null,
  "actor_is_bot": 0,
  "addressed": 0,
  "kind": "health",
  "body": null,
  "meta_json": "{\"eval_deadline\": 1762180160, \"extra\": \"data\"}",
  "correlation_json": "[]",
  "idempotency_key": "idemp_eval_01",
  "eval_state": "queued",
  "eval_requeues": 0,
  "disposition": null,
  "created_at": 1762180000
}

=== TABLE: sa_meta (resumed) ===
paused: 0
paused_at: 
```

---

## 4. Integration Cards: Cards 4.4 & 4.5 Staged for Stacked Tree

**Cards 4.4 and 4.5 are integration cards landing later on the stacked tree.**

### Scope of Deferred Cards
1. **Card 4.4 — Move Router replies + notifier onto Outbox**:
   - Updates `tools/orchestrator_tools.py` (`orchestrator_reply`) and `gateway/kanban_watchers_notifier.py` to route reply transmissions into `sa_outbox` via `superagent_outbox.queue_reply` rather than directly invoking platform adapters inline.
2. **Card 4.5 — Cron migration to Outbox**:
   - Updates `cron/delivery_queue.py` and `cron/scheduler.py` to route scheduled notifications and jobs through `sa_outbox`, deprecating the separate `cron/deliveries.db` delivery queue in favor of the unified superagent outbox store.

### Architectural Rationale for Stacked Tree Landing
- **Cross-Lane Concurrency**: Lane C focused on the core Outbox engine, drain runner, pause manager, and health trigger emitter. Cards 4.4 and 4.5 mutate gateway watchers (`gateway/kanban_watchers_notifier.py`), tool runtime dispatch (`tools/orchestrator_tools.py`), and cron queues (`cron/scheduler.py`). Because Lane B was concurrently refactoring orchestrator tools and Lane A was refining dispatcher processes, wiring active execution callers directly in this worktree would create multi-lane branch conflicts.
- **Verification Decoupling**: By isolating the engine into clean standalone modules (`superagent_outbox.py`, `superagent_pause.py`, `superagent_health.py`), all unit, backoff, ordering, drain, and shift behaviors are 100% verified without touching live runtime loops. The caller redirects will be applied atomically when lanes are stacked and integrated by the supervisor.

---

## 5. Recorded Deviations

1. **Prerequisite Schema Cherry-Pick (`f61a64c352`, `9c53e97a91`)**:
   `superagent-p4` branched from Phase 1 (`e86086a094`), which did not include Lane B's schema additions. Card 4.2 paused under Stop Protocol when `sa_outbox` was found missing. Schema commits `f61a64c352` (Card 2.2) and `9c53e97a91` (Card 2.2fix E-044) were applied to provide table definitions for `sa_outbox`, `sa_meta`, and `sa_triggers`.
2. **Supervisor Lane Module Decoupling**:
   Rather than placing Outbox and Pause functions in `hermes_cli/orchestrator.py` (which is Lane B's single-writer module) or embedding health emissions directly in `gateway/kanban_watchers_dispatcher.py`, the implementations were created in three dedicated modules:
   - `hermes_cli/superagent_outbox.py`
   - `hermes_cli/superagent_pause.py`
   - `hermes_cli/superagent_health.py`
   All function signatures match the target interface exactly, enabling seamless re-export from `orchestrator.py` during integration.
3. **Outbox Crash Safety Mechanism**:
   Selected and implemented the stale-sending reset pattern: prior to fetching due rows, `drain_outbox` detects rows in `'sending'` status whose `next_attempt_at` is older than 600 seconds (10 minutes) and resets them to `'failed'` with `attempts` unchanged, allowing safe redelivery after a crash without burning retry attempts.
4. **IM 100-Word Cap Enforcement**:
   Card 4.3 enforced the 100-word limit across IM platform adapters (`telegram`, `slack`, `discord`, `dingtalk`). Over-limit payloads fail with `last_error='over_limit'` and increment `over_limit_notices` in `sa_meta`. Non-IM sources (`github`) are explicitly permitted to exceed 100 words.
5. **Observe Mode & Live Invariant Protection**:
   `superagent.enabled` remains `false` across all profiles, system configs, and `DEFAULT_CONFIG`. Observe mode remains default, and zero live processes or profile configurations were modified.

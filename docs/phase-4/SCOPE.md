# Phase 4 — Transactional Outbox & health sync

**Objective:** All agent-originated channel messages go through the store-then-do Outbox in kanban.db; the dispatcher tick drains it, surfaces health to the Router, and pause processing works end-to-end.

**In scope**
- `sa_outbox` table (E-028) + drain in the dispatcher tick: per-Ticket-per-conversation ordering, backoff retries calling the delivery_ledger flood helpers (E-031), dropped-message verdicts (E-029), 100-word IM cap (E-030).
- Router replies (Phase 1's `orchestrator_reply`) and Worker result notices move onto the Outbox; the state change + Outbox insert share one transaction wherever the Orchestrator issues both (E-010).
- Migration: kanban notifier sends and cron agent-originated deliveries route through the Outbox (E-031 lists what does NOT migrate).
- Health sync: the dispatcher tick (which already does heartbeat/stale/orphan/circuit-breaker work in `kanban_db_dispatch.py`) emits first-party Triggers (`source=internal`, strength explicit) for: stale run reclaimed, circuit-breaker trip, Outbox row failed 3×, Eval `unavailable` unresolved >1h. Dedup: one health Trigger per (kind, subject) per hour, enforced via idempotency key `internal:<kind>:<subject>:<hour-bucket>`.
- Pause processing: `superagent.paused` runtime flag (config-settable + panel/tool toggle later; this phase: config + an Orchestrator method) halting dispatch, new Evals, Eval expiry, Input delivery, and Outbox drain — while Trigger intake keeps recording and running turns are untouched; Eval deadlines shift by pause duration (store `paused_at`; on resume, add the delta to pending deadlines in `meta_json`).

**Out of scope**
- New LLM watchdog anything (v2 §6 deferred). Panel UI (Phase 5) — health Triggers and tables only.

**Done criteria**
1. Kill -9 the test gateway mid-drain: on restart, the queued message is delivered exactly once (fixture platform records sends; assert count == 1).
2. Two messages for the same Ticket+conversation: the second is never sent before the first succeeds or fails 3× (fixture asserts order).
3. A platform "conversation gone" error drops the row with a reason and it is never retried; an unknown error never drops.
4. A 101-word IM body fails with `over_limit` and produces a Router notice Trigger; a 101-word GitHub body sends fine.
5. Pause: during pause, fixture Triggers accumulate, nothing dispatches/drains, a running run is not interrupted; on resume, Eval deadlines are extended by ≥ the pause length.
6. Existing cron/kanban notification tests pass on the migrated paths; REPORT.md + sign-off.

**Constraints:** Contract in full. Decisions: E-010, E-028–E-031.

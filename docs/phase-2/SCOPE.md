# Phase 2 — Trigger / Gate / Eval / Mandate pipeline

**Objective:** Inbound events on pilot surfaces become Triggers; the deterministic gate + model Eval decide under a versioned Policy; accepts create Mandates and Tickets. Ships in `observe` mode.

**In scope**
- DDL: `sa_triggers`, `sa_decisions`, `sa_mandates`, `sa_source_cursors` (E-025), + `tasks.mandate_id` / `tasks.ticket_kind` columns (E-003). All in kanban.db via the kanban schema/migration path.
- `hermes_cli/orchestrator.py`: Trigger intake (idempotent), subscription check stub (Phase 3 fills it; this phase the check returns "no subscriptions" from an empty table — the table itself is Phase 3), Decision recording, Ticket+Mandate creation, policy snapshot + version (E-013).
- `hermes_cli/superagent_gate.py`: the admission table as pure functions (E-012, E-014), layered on `gateway/authz_mixin.py` verdicts (the gate consumes the allowlist result; it does not re-implement allowlists).
- Eval: batching per E-017, `call_llm` task `superagent_eval` (E-015, E-016), deadlines/requeues/`unavailable`.
- Inbound hook: gateway writes Triggers from `GatewayInboundMixin._hm_admit_event` (`gateway/run_inbound.py`) for the pilot surfaces when `superagent.enabled` — after existing authorization, before the turn is created. Non-pilot surfaces and `enabled=false`: byte-identical behavior to today.
- GitHub Source: polling per E-025 (notifications + subscribed scopes; receive-all scopes per config), cursor in `sa_source_cursors`, Triggers with thread keys per E-005.
- Policy change handling: version bump discards running Evals (requeue), computes affected live Tickets, writes policy-change Inputs — `sa_inputs` table is created in THIS phase (E-026) because policy Inputs need it; subscription fan-out rows are Phase 3.
- Revalidation function (E-013/`orchestrator.revalidate(mandate)`) implemented + tested here; the resume path that calls it is Phase 3.
- The remaining `orchestrator_*` tools (eval/eval_read/eval_decide/trigger_requeue/thread_unsubscribe/ticket_retry/ticket_request_status/ticket_note) replace their Phase-1 stubs.

**Out of scope**
- Subscriptions/pass-through (Phase 3). Outbox (Phase 4) — replies still use Phase 1's reply path. Panel (Phase 5).
- `auto` mode in production config: code supports it; the live config stays `observe` until the post-phase review (a human flips it, not the builder).

**Done criteria**
1. Duplicate delivery of the same platform event (replayed fixture) produces exactly one Trigger and at most one Ticket.
2. Gate unit tests cover every admission-table cell + bot list + first-party basis.
3. Observe mode: an inferred candidate with a positive assessment records a Decision with `would_accept` and creates NO task.
4. Policy version changes when any rule/mode/source-part changes, and only then; running Evals requeue under the new version.
5. GitHub Source: two consecutive polls over the same fixture notifications produce zero new Triggers on the second poll; cursor survives a simulated restart (new process, same DB).
6. `enabled=false`: gateway inbound tests pass unchanged; REPORT.md + sign-off.

**Constraints:** Contract in full. Decisions: E-001–E-018, E-024–E-026, E-033, E-034.

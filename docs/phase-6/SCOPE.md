# Phase 6 SCOPE — VibeHard Ticketing Source (Adam's direct order, 2026-11-03 ~19:28 PST)

Add VibeHard as a third Trigger source, on the GitHub Source pattern, stacked after Phase 4 in the integration tree.

IN SCOPE:
- `hermes_cli/superagent_vibehard.py`: a poller that reads the VibeHard ledger read-only (path from additive config `superagent.vibehard.ledger_path`, default `~/.vibehard-agent/ledger.sqlite`; enable flag `superagent.vibehard.enabled`, default false) and emits Triggers (submitted / state_change / failed / stuck) through the Orchestrator's `receive_trigger`, with cursors in `sa_source_cursors` (source='vibehard').
- Admission: first-party source, OBSERVE-ONLY like the pilots — the gate marks VibeHard triggers per the observe posture; Eval judges and forms the would-be Ticket; in observe mode nothing dispatches. No code path in v1 writes to the VibeHard ledger or calls VibeHard tools.
- Tick wiring alongside the GitHub poll (same cadence/mechanism the stacked tree uses for sources).
- Fixture tests against a temp ledger DB built from the real schema (jobs/sessions/audit DDL copied from ledger-migrations.ts v1 + the kind/heartbeat columns), covering: submitted, state_change, failed, stuck-once-per-episode, cursor idempotency (a second poll emits nothing new), disabled flag = no-op, missing ledger file = no-op with a logged warning (never a crash).

OUT OF SCOPE:
- Any write/act on VibeHard (no gate/deploy/rollback/handoff calls), any auto admission (observe only; auto is a later decision for every source), the MCP/VDP read path (rejected for v1 in the recon — DB poll instead), cross-machine polling (the gateway and the ledger are on the same Mac).

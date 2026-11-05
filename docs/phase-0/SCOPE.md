# Phase 0 — Config block, model tiers, slot controller

**Objective (one line):** Add the `superagent` config block (default `enabled: false`) and an in-memory worker-slot controller with a human reserve, without changing any live behavior.

**In scope**
- Config schema + parsing for the E-033 block, following the repo's existing config conventions (`hermes_cli/config.py` load/`cfg_get` patterns).
- Slot controller inside the kanban dispatcher package: acquire/release, worker slots from config, +1 human-reserve slot (E-023), priority rule stubbed but not yet consulted by dispatch (Phase 1 wires it).
- Sample block added to `cli-config.yaml.example`. The live `~/.hermes/config.yaml` is NOT touched (contract rule 11).

**Out of scope**
- Any change to dispatch decisions, gateway behavior, or worker spawning.
- Router profile, gate, eval, tables (Phases 1–2). No `sa_*` tables in this phase.

**Done criteria (checkable)**
1. Config with no `superagent` key loads identically to today; with the block, values parse and validate (bad `accept_mode` → clear config error naming the key).
2. `enabled: false` path: full existing kanban test set passes unchanged.
3. Slot controller unit tests pass: capacity enforcement, reserve slot only for person-wakes, release on run end, restart = all slots free.
4. REPORT.md written; reviewer sign-off line present before Phase 1 starts.

**Constraints:** BUILDER-CONTRACT.md applies in full. Decisions: E-023, E-024, E-033, E-034, E-035.

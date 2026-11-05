# Phase 0 TODOS

Acceptance command for the phase: `python -m pytest tests/superagent/ tests/ -k "kanban or config" -q` (run from repo root, in the phase worktree).

- [x] 0.1 Read first: `hermes_cli/config.py` (how blocks are loaded/validated; find one existing nested block with an enum-validated value and copy its pattern), `gateway/kanban_watchers_dispatcher.py` `_DispatcherSettings` (lines ~35–120: how `kanban.*` settings resolve), `gateway/kanban_watchers_common.py`. Write the three file:line anchors you will imitate into this todo's notes. No code yet.
- [x] 0.2 Add config parsing for the E-033 block: a `superagent_settings(config) -> dict` resolver in `gateway/kanban_watchers_dispatcher.py`'s settings module pattern (or `hermes_cli/config.py` if 0.1 shows resolvers live there — follow the anchor you found, record which). Defaults exactly per E-033. Validation: `accept_mode` ∉ {off, observe, auto} → raise a config error naming `superagent.accept_mode`; `worker_slots` < 1 → error; `human_reserve_slots` ∉ {0, 1} → error (v1 allows only 0 or 1).
- [x] 0.3 Tests first: `tests/superagent/test_config.py` — cases: missing block → defaults with `enabled=false`; full block parses; each validation error fires; `rules` list round-trips. Make them pass with 0.2's code.
- [x] 0.4 Slot controller: new class in `gateway/kanban_watchers_dispatcher.py` (or sibling `gateway/kanban_slots.py` if the dispatcher file is >1,500 lines — check and record): `SlotPool(worker_slots, reserve_slots)` with `try_acquire(kind)` where kind ∈ `launch|person_wake`, `release(token)`, `in_use()`. In-memory only (E-023). No dispatch wiring yet.
- [x] 0.5 Tests: `tests/superagent/test_slots.py` — fill worker slots → next `launch` refused; `person_wake` still acquires (reserve); second `person_wake` refused when reserve also full and no worker slot free; release frees; new SlotPool starts empty. Make pass.
- [x] 0.6 Add the sample block to `cli-config.yaml.example`, commented to state `enabled: false` = stock Hermes behavior.
- [x] 0.7 Run the phase acceptance command; also run the full existing kanban dispatcher tests and confirm zero regressions. Paste both summaries into REPORT.md, write REPORT.md, stop for review.

Dependencies: 0.2→0.3, 0.4→0.5. 0.6 independent. 0.7 last.

Supervisor sync (2026-11-03): all seven cards built and Argon-reviewed (see REPORT.md and REVIEW-p0-*.md in this folder). The Bonsai smoke gate is not part of these seven cards; it remains an open item tracked in the delegation BLOCKED.md (B-1) — gateway restart authorized by Adam, deferred by the no-turn-in-flight guard; gate runs on the next quiet window.

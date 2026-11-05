# Phase 1 REPORT — Router/Worker split (pilot)

Branch: `superagent-p1`, stacked on `superagent-p0` head af9f3c257c4. Worktree `.worktrees/superagent-p1`. Live config untouched; no merges into the live checkout. Everything is inert while `superagent.enabled` is false **with one exception**: card 1.4's E-020 worker toolset exclusions are NOT gated on the flag (see Deviations) — they change toolset policy for every Kanban worker the moment this branch merges, flag or no flag.

## Cards

| Card | Content | Builder | Review | Sha |
|---|---|---|---|---|
| 1.1 t_224d05a5 | Recon anchors (dispatch/profile/toolset paths) | Tier F | Argon PASS | 67e2f3a857e |
| 1.2 t_76029d74 | Router tools (tools/orchestrator_tools.py, 11 E-019 tools: route/read/reply real, 8 stubs) + `superagent.router_profile` | Tier S (Argon) | Opus FAIL → fix → Opus PASS | 9e23dd5fae2 → e86086a0945 |
| 1.3 t_22ed3423 | Router tool tests (identity stamp, reply fail-closed, gating) | Tier F | Argon PASS | 0e0fb520d07 |
| 1.4 t_02ce5842 | Worker toolset exclusions (E-020) — Opus-architected two-layer fix (gateway/session_kanban.py policy + delegate_tool_toolsets derived deny set + dispatcher parity) | Tier F executing Opus design | Argon PASS | 5530f89996f |
| 1.5 t_c1bdd412 | Worker toolset tests (design §4: blocklist, session_kanban additions, spawn parity) | Tier F | Argon PASS | d0b4bccc468 |
| 1.6 t_457e7851 | SlotPool wiring into dispatcher tick (enabled-gated, slot_wait events) + wake evidence note | Tier F | Argon PASS | c15856d858b |
| 1.7 t_8ec20dfc | Pilot script (tests/superagent/pilot_script.md) | Tier F | Argon FAIL (5 findings: --board placement, TASK_ID extraction, chat --oneshot/--source, config append + default toolsets, real tool names) → fix → Sonnet PASS | a157556da4 → 7cc6c36b67 |
| 1.8 t_5698b76b | E-022 terminal backgrounding recon (evidence-note outcome) | Tier F | Argon PASS | be648b1ea1 |
| 1.9 t_ff63b707 | Acceptance runs vs baseline (supervisor-executed after the builder session died on its background task) | Supervisor | Sonnet FAIL → FAIL → PASS (rounds caught a missing combined run, then a supervisor misdescription of the baseline run — both corrected in the note) | dde5e765f1 → 237ecb9b13 |

## Deviations and notes
- Card 1.2's review seat was Opus (Argon-built card, per E-040 independence). Its FAIL found a process-wide authorship-forgery pattern (mock.patch.object on kanban_tools._persisted_identity); the fix moved creation to the DB layer with created_by="router" and made orchestrator_reply fail closed. Filed: REVIEW-p1-1.2.md, REVIEW-p1-1.2-fix.md.
- Card 1.4 was escalated per the standing rule after two Argon builder stalls: Opus designed the exact edit (DESIGN-p1-1.4-opus.md). The design found the Phase-1 recon anchor function (`_resolve_worker_cli_toolsets`) has no production callers; the real enforcement point is `gateway/session_kanban.py::build_kanban_policy`.
- **Card 1.4 is not flag-gated (by E-020 design; flagged by the phase-report review).** `build_kanban_policy` strips the derived deny toolsets and rewrites the frozen `agent.disabled_toolsets` for every Kanban worker regardless of `superagent.enabled`; `_resolve_worker_cli_toolsets` likewise filters `deny` unconditionally (it has no production callers today). Cutover consequence: merging Phase 1 changes worker toolset policy for ALL Kanban workers, not only superagent ones. The Opus design and the card's Argon PASS cover this behavior; the deviation is from the phase's "inert until enabled" framing, not from the design.
- Out-of-scope churn in `hermes_cli/kanban_db_dispatch.py` (noted by the phase-report review): the p1 diff against af9f3c257c4 includes ~93 blank-line deletions between top-level definitions and one reformatted `_kb._log.debug(...)` call in `_resolve_worker_cli_toolsets`. No card named this file-wide reformat; it changes no behavior, but it is unlisted churn and is recorded here.
- Card 1.6 partial-scope note: the recon found no person-wake path in the dispatcher, so 1.6 implemented the launch side only (SlotPool wiring + slot_wait events); person-wake is carried by Phase 3 card 3.3's design, per spec. Recorded in notes/p1-6-wake-evidence.md.
- Headless dispatch deviation (program-wide, from Phase 0): cards are the tracking state; builder runs are driven as separate agy processes in the phase worktrees rather than by the live gateway's dispatcher, because per-root launcher environments were uncommitted (B-1, resolved by root pre-warming; the designed SMOKE gate then ran and its result is filed in the delegation BLOCKED.md with Adam's attribution caveat — the Bonsai lane is parked by Adam; all build/review seats are Gemini Flash/Argon and Claude Opus/Sonnet).

## Acceptance (card 1.9, notes/p1-9-acceptance.md at 237ecb9b13)
- tests/superagent in the phase tree: 43 passed, 0 failed.
- Kanban set, same command both trees: phase tree 920 passed / 129 failed / 10 errors vs baseline-head 892 / 132 / 10 — zero regressions. Baseline precision (per the phase-report review): the comparison tree is pristine `main` 845b0755ed, NOT the p0 head this branch stacks on, so the +28-passed delta covers Phase 0 + Phase 1 together (two of the five new test files — tests/superagent/test_config.py and test_slots.py — are Phase 0's). Failure-set diff verified by the reviewer (the one phase-only failure is a pre-existing environment-sensitive teardown test whose file crashed outright in the baseline run).
- Combined `tests/superagent/ tests/ -k 'kanban or gateway'` in the phase tree: 15097 passed / 2985 failed / 793 errors (the repo-wide pre-existing failure background on this machine); no baseline combined line exists (the command cannot run in baseline-head — no tests/superagent/ there).

## Phase status: COMPLETE — all 9 cards built and independently reviewed PASS; branch head 237ecb9b13.

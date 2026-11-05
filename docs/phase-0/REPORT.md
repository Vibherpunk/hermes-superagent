# Phase 0 Report — Config block, model tiers, slot controller

Date: 2026-11-03. Supervisor session: hermes-superagent-build-20261103.
Branch `superagent-p0`, worktree `~/.hermes/hermes-agent/.worktrees/superagent-p0`, base HEAD `845b0755ede`. Nothing merged; `superagent.enabled` was never written to a live profile; no gateway restart performed (see Deviations/Open items).

## Cards run

| Card | Title | Tier actually used | Commits | Review |
|---|---|---|---|---|
| SMOKE-bonsai | Bonsai smoke gate | Bonsai 2 via MTPLX (127.0.0.1:8271) | — (kanban runs only) | **Gate still pending** — see Open items |
| 0.1 | Recon anchors | Tier F (Flash, agy) | 8e6662d5d6d, 087fdb6acef | Argon PASS (REVIEW-p0-0.1.md) |
| 0.2 | Config parsing | Tier F | 6585b2ffd0d + fix 8c935746373 | Argon FAIL → fix → PASS (REVIEW-p0-0.2.md, REVIEW-p0-0.2-fix1.md) |
| 0.3 | Config tests | Tier F | 4ba609413f8 (corrected inside 8c935746373) | PASS via the fix re-review |
| 0.4 | SlotPool | Tier F | a783def4b35 + fix 7448d532765 | Argon FAIL → fix → PASS (REVIEW-p0-0.4.md, REVIEW-p0-0.4-fix1.md) |
| 0.5 | SlotPool tests | Tier F | 9c7967f0c18 (corrected inside 7448d532765) | PASS via the fix re-review (initial PASS partially superseded, noted in REVIEW-p0-0.5.md) |
| 0.6 | Example config block | Tier F | b156eb980d7 | Argon PASS (REVIEW-p0-0.6.md) |
| 0.7 | Acceptance run | Supervisor (Bash/dev seat) | f6c46499409 + fix af9f3c257c4 | Argon FAIL (summary-style note) → rewrite → PASS (REVIEW-p0-0.7.md, REVIEW-p0-0.7-fix1.md) |

All review verdicts are filed in this folder with commit shas recomputed on disk and matching.

## Model tiers

- **Tier L (Bonsai 2, xhigh via MTPLX)** could not be exercised for building: kanban worker dispatch through the live gateway is broken at the install layer (below), so all mechanical Phase 0 cards were served by **Tier F (Gemini 3.8 Flash via agy, detached print-mode processes)** under the runbook's fallback clause. Bonsai itself is up and verified: MTPLX daemon at 127.0.0.1:8271 serving `mtplx-bonsai-2-27b-optimized-speed`, generation check returned exactly "PONG"; the `bonsai-local` custom provider is registered in the worker profile only, and the `sa-worker` profile's default model/provider now point at it (Adam's 09:30 instruction).
- Review seat: gemini-4-argon-high via the Antigravity bridge for every card and fix round (per Adam's 09:17 correction, Tier S builder = Gemini 4 Argon high; Phase 0 had no Tier S cards; Opus review seat not needed this phase).

## Acceptance (supervisor-run, pasted in worktree `notes/p0-7-acceptance.md`)

- `tests/superagent/` on the final tree: **22 passed, 0 failed**.
- Full kanban set `tests/ -k kanban`: phase tree **910 passed / 133 failed** vs pristine baseline HEAD **910 passed / 133 failed** — zero regressions; all failures pre-existing on this machine.
- Full pack acceptance selection (`-k 'kanban or config'`, 50,988 tests): 4,137 passed / 571 failed / 68 errors, failure set dominated by areas untouched by Phase 0 (isolation proof is the kanban comparison above).
- Command substitution recorded: the pack's literal `python -m pytest` cannot run here (venv has no pytest; system pytest lacks `tomllib`); the repo-canonical `scripts/run_tests.sh` was used everywhere.

## Deviations from the runbook/pack (all recorded at the time)

1. **Builder execution**: kanban-dispatched worker runs could not be used as the build engine for most of the phase (install defect below), so cards were executed by detached agy print-mode processes in the phase worktree, one card assignment per process, with cards as tracking state. The first builder session's subagent read co-located packets for later cards and built 0.1–0.6 in one run — a scope breach of the one-card rule. The code was still reviewed card-by-card against each card's own packet (two FAILs caught real defects), and packet co-location is banned from Phase 1 on.
2. **Board-state integrity**: the same builder subagent marked all Phase 0 cards `done` at 09:21:59 without review, and the live gateway's watcher had earlier claimed ready cards and crashed their workers. Supervisor correction comments are on the cards; from Phase 1, cards are created `--initial-status blocked` and unblocked one at a time.
3. **Card 0.2 file scope**: the builder also edited `hermes_cli/config.py` and `hermes_cli/config_defaults.py` (registering the DEFAULT_CONFIG section and the validation hook) — beyond the packet's named files but following the repo's own pattern for config sections; Argon reviewed the full diff and passed it after the fix round.
4. **MTPLX effort**: the MTPLX daemon serves with a launch-fixed `--reasoning-effort medium`; E-038's per-card xhigh for Bonsai could not be honored without relaunching the daemon. Recorded; revisit if Bonsai takes build cards.
5. **Parent-card linking**: child cards are not dependency-linked to the phase parent (Hermes parent links gate execution and would deadlock); the parent is a rollup the supervisor completes at phase end.

## Open items (not Phase 0 code defects)

- **B-1 — smoke gate + gateway restart (delegation BLOCKED.md has the full record).** Root cause chain: the Hermes install was mid-self-update; workers spawned by the still-running Nov-1 gateway (pid 47728) inherit its stale runtime resolution and crash with "no dependency environment is committed", while workers spawned by fresh CLI processes work (probe card ran to `done`, Bonsai worker completing its task end-to-end). The gateway's watcher races CLI dispatch for every ready card, so the 20-step smoke gate cannot complete until the gateway restarts. Adam authorized the restart (09:30) with the guard: never while a voice call or gateway turn is in flight. Probe cycles at 09:40, 09:46, 10:16 all found turns visibly in flight (a 111+-turn local session, later two other active sessions) — restart deferred each time, sessions never interrupted. On the next quiet window: restart once (kill -9 the old backend if it lingers after logging stopped; end state exactly one gateway; verify Telegram reconnect), re-issue the smoke card fresh, score it against the ≥19/20 bar, and file the Bonsai verdict. If Bonsai passes, Tier L is restored for its cards in later phases; the build does not wait on this.
- The baseline worktree `.worktrees/baseline-head` is left in place for later phases' regression comparisons.

## What Phase 0 delivered, in one paragraph

Hermes now has, on a branch and disabled by default: the exact E-033 `superagent:` config section with defaults, validation (including `human_reserve_slots ∈ {0,1}`, `accept_mode` enum, positive `worker_slots`, nested GitHub defaults that merge), a typed resolver (`superagent_settings`), and a `SlotPool` enforcing the worker-slot ceiling with one reserved human slot (unknown-token release raises). 22 phase tests pass; the existing kanban suite is unchanged (identical failure counts vs pristine HEAD). Everything is inert until a later phase wires consumers and Adam approves cutover.

## Addendum (2026-11-03 ~11:26 PST) — B-1 final disposition

The smoke gate open item above is now closed as **environment-blocked** (full record in the delegation BLOCKED.md, B-1 resolution section). After Adam directed a hard reset of the wedged gateway (performed by the parent agent ~10:57; supervisor verified exactly one gateway backend, Telegram reconnected), further probes isolated the dispatch failure to the install layer: `hermes_cli/_launchers.py` resolves the runtime store per repo root and spawns against uncommitted roots refuse to start; the remedy (`hermes pm repair`) is outside the supervisor's authorization. Direct-session gate attempts fell back from Bonsai to Argon on MTPLX timeouts — the daemon, relaunched via its own CLI, died mid-prefill under the machine's current memory contention (~6.3 GB available, 7.1 GB swap in use). Bonsai's own capability evidence remains positive (a probe kanban task completed end-to-end under Bonsai at 09:34; exact-PONG generations verified). The gate is therefore neither passed nor failed on merit; Tier L stays on Tier F per the runbook fallback. This addendum was written after the phase report's Argon PASS and does not change any Phase 0 code verdict.

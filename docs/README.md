# Executor Pack — Super-agent adoption in Hermes

The build-ready refinement of `../SPEC-V2.md`. SPEC-V2 remains the architecture of record; this pack is how a builder with **no judgment calls available** executes it.

## How to run a build from this pack

**Primary route — Hermes-native (card-first).** This pack is executed *by Hermes itself*, per `HERMES-BONSAI-RUNBOOK.md`: a supervisor converts the phases into Kanban cards (one parent per phase, one child per todo, dependency-linked), and Hermes' dispatcher hands one card at a time to a worker run — Bonsai 2 locally, or a hosted tier — in a worktree, with per-card model/reasoning overrides and the goal loop on. The builder receives only its card's context packet, never the whole pack. A strong reviewer gates each card; approvals bind the commit sha reviewed. The markdown TODOS files become the archive, synced by the supervisor; cards are the live state.

**Fallback route — generic coding agent.** A competent external coding agent may instead work the pack directly from these files (read `BUILDER-CONTRACT.md`, `DECISIONS.md`, then the phase's `SCOPE.md`/`TODOS.md`, one todo at a time). This route presumes reliable tool-calling and self-managed git/test work; it is NOT the route for a raw local model.

Both routes:
1. Phases run strictly in order 0→5. A phase starts only when the previous phase's `REPORT.md` carries a reviewer sign-off line naming the commit sha reviewed.
2. Each phase works on its own branch/worktree (`superagent-p<n>`) on the Mac Studio checkout and never restarts the live gateway (contract rule 11). `superagent.enabled` stays `false` in live config until Adam flips it after Phase 5 + a week of observed `observe` data.
3. Blockers are normal output: a blocked card/todo with evidence is the process working. Blockers resolve by amending the pack (a decision added to DECISIONS.md or a packet rewritten), never by telling the builder to "use judgment."

## Contents

- `BUILDER-CONTRACT.md` — the 12 rules: one todo at a time, named files only, the stop protocol, no self-review, live-system safety.
- `DECISIONS.md` — E-001…E-035: every micro-fork pre-decided and grounded in the actual Hermes code (verified 2026-11-03: kanban schema at `hermes_cli/kanban_db.py`, `DELEGATE_BLOCKED_TOOLS`, `call_llm`, `_hm_admit_event`, delivery ledger's separate DB, etc.).
- `phase-0/` … `phase-5/` — SCOPE.md (objective, in/out, checkable done criteria) + TODOS.md (discrete, dependency-ordered, each with exact files, DDL where relevant, and an acceptance command).

## What got decided that v2 left open (highlights)

- All six+ tables live in kanban.db with an `sa_` prefix; Tickets ARE kanban `tasks` (gaining `mandate_id` + `ticket_kind`), Assignments ARE `task_runs`.
- `kanban_notify_subs` is NOT reused for Subscriptions (wrong direction — E-004).
- Eval runs through Hermes' own `call_llm` with task name `superagent_eval` (E-015); batching numbers pinned (20/thread batch, 10 Router candidates, 2 requeues, 60 s).
- Worker silence reuses the existing `DELEGATE_BLOCKED_TOOLS` constant instead of a new list (E-020); worker Reports ride the existing kanban tools (E-021); ONE new worker tool total (`kanban_handback`).
- GitHub Source polls (60 s, `gh` CLI, cursor table) — no inbound webhook listener exists to reuse (E-025).
- Slots are in-memory in the dispatcher, never a table (E-023). Stage is a pure function, never stored (E-032).

# Runbook — executing this pack inside Hermes, with Bonsai 2 as a builder

This is the execution layer the pack was missing. The pack's content (decisions, decomposition, acceptance tests) stays as written; **how the work is run** changes: Hermes' own Kanban system is the harness, cards are the unit of work, and no builder ever receives the whole pack. It uses the coding-harness features Hermes already has — kanban dispatch with worktree-isolated runs, per-card model/provider/reasoning overrides, force-loaded skills, typed blocks, heartbeat/stale reclaim, and the goal loop (an auxiliary judge re-checking the worker's output against the card until it agrees or the turn budget runs out).

Two corrections this runbook bakes in, both learned from the bb harness census/brief (`~/workspace/vibehard-bb-harness/`):
- **Enforcement lives in the harness, never in the prompt** (bb's authority rule). Every constraint below is a dispatcher setting, a toolset, or a database fact — and the refusal cases are *tested*, not trusted.
- **The record is the event stream, not a summary** (bb threads are append-only event streams; Adam's filed-record rule says the same). Reviews read diffs, test output, and `task_events` — never a worker's completion prose.

## Step 0 — Serve Bonsai 2 on the Mac Studio (human-supervised, once)

1. Install Bonsai 2 27B via its MLX pack (Apple silicon route) or PrismML's llama.cpp fork (`llama-server`), bound to `127.0.0.1` only. Its main GGUF packs do NOT load in stock Ollama/llama.cpp — do not substitute a stock build.
2. Register it in the **worker profile's** config as a custom provider (same pattern as the existing Ollama custom-provider entry): an OpenAI-compatible endpoint at the local server, model id pinned to the Bonsai 2 build. The live gateway's own config is not touched.
3. Do not treat the HuggingFace/model cache as cleanup space — it is production infrastructure on this machine (standing lesson).

## Step 1 — The smoke gate (no real card runs before this passes)

The supervisor creates ONE test card, `SMOKE-bonsai`, on a scratch board against a throwaway worktree, with `model_override` = Bonsai 2 and `reasoning_effort` = `xhigh` (Bonsai's native thinking mode). The card body contains 20 scripted micro-tasks: read a named file and quote a line; make a one-line edit; run a named pytest and report the result; post a heartbeat; post a comment; block with a typed reason; complete with a summary. **Pass bar: ≥19/20 executed as specified, zero invented tools, zero out-of-scope file touches.** Fail → Bonsai does not build; the supervisor reports and the fallback tier (see the tier table in DECISIONS E-040) takes the builder seat. This gate exists because tool-calling — not reasoning — is Bonsai's measured weak point; effort level does not fix tool-call format errors, so the gate tests the actual failure mode.

## Step 2 — Card-first import (supervisor, before any building)

The supervisor converts the pack into a board, `superagent-build`:

- One **parent card per phase**: title `SA Phase <n> — <phase name>`, body = the phase SCOPE verbatim.
- One **child card per todo**, linked to its parent (`task_links`), with dependency links exactly as the TODOS.md dependency lines state. A card whose dependencies are incomplete is not dispatchable.
- Each child card's body is a **context packet**, and it is ALL the builder gets:
  1. The todo text, verbatim.
  2. The named files with their anchor notes.
  3. The relevant DECISIONS entries — excerpted by number, full text of those entries only.
  4. The acceptance command.
  5. The stop protocol in five lines (from BUILDER-CONTRACT rule 4).
  The builder never reads SPEC-V2 or the full DECISIONS.md. If the packet and the code disagree, the card blocks — the supervisor fixes the packet, not the builder's judgment.
- Card settings: `workspace_kind` = worktree (branch `superagent-p<n>`), `model_override`/`provider_override` per the tier table, `reasoning_effort` per the tier table, `goal_mode` = 1 with `goal_max_turns` = 6 (the goal loop's judge is the supervisor-tier model, never the builder itself), `max_runtime_seconds` set per card (default 3600).

## Step 3 — Dispatch discipline

- **Serial Bonsai lane:** the board's in-progress cap for Bonsai cards is 1. One card, one run, one worktree. (bb ships a concurrency-limit plugin for the same reason: per-provider caps are a harness setting, not a hope.)
- **Provisioning check first** (bb's setup-hook lesson: a workspace that fails setup fails *before* the agent starts, in the transcript): the worker's first action on every card is verifying the environment — named files exist at their anchors, the acceptance command's test path exists — and posting the result as the run's first comment. A failed check = blocked card, kind `provisioning`; zero code written.
- **Steer, don't interrupt** (bb's steering/queueing distinction): supervisor guidance mid-run goes in as a card comment (Hermes' steer path). Follow-up work is queued as comments or new cards, never injected by killing the run.
- **Questions are structured and timed** (bb's question cards): a worker question = a typed block on the card. It waits for the supervisor; it is never auto-answered, and after 24h blocked it surfaces in the supervisor's report. Workers get no `clarify` tool (already enforced by `DELEGATE_BLOCKED_TOOLS`).

## Step 4 — Review gate per card (the loop that makes a weak builder safe)

1. Worker completes → the card carries: diff (in its worktree), acceptance output, event history.
2. The **reviewer** (supervisor-tier model, a different seat from the builder) checks, in order: acceptance command re-run passes; diff touches only the card's named files; no DECISIONS invariant violated (spot-check the card's excerpted decisions); event stream shows no invented tools or skipped steps.
3. Pass → reviewer posts `REVIEWED-PASS` **with the commit sha reviewed** (bb's content-hash-bound approval: an approval binds the exact content; if the diff changes after review, the approval is void and the card re-reviews). Supervisor then syncs the phase `TODOS.md` marker to `[x]` — the supervisor is the only writer of the markdown pack from here on; cards are the live state, markdown is the archive.
4. Fail → `kanban_request_changes` with findings written as exact, checkable items. The same card re-runs. **Two failed fix rounds on one card → the card escalates to the architect seat** (Adam's standing escalation rule); the builder tier for that card steps up one level (E-040).
5. A phase is done when its parent card's children are all terminal-passed AND the phase REPORT is assembled by the supervisor from the cards' records. Phase sign-off binds the phase's final commit sha the same way.

## Step 5 — Escalation ladder (bb's lowest-clearer rule)

Blockers route to the **lowest seat that can clear them**, never straight to the top:
worker blocks → supervisor resolves from DECISIONS/pack (packet fix) → reviewer/architect seat for design gaps (adds a DECISIONS entry, versioned) → Adam **only** for his-hands items (credentials, live-config flips, money, deleting real data). The supervisor's report to Adam lists only that last category.

## What this runbook deliberately does not do

- No second task system: Kanban *is* the tracker for the build, exactly as the finished Super-agent design says Tickets are kanban tasks.
- No prompt-trusted safety: toolsets, slot caps, and worktree isolation are dispatcher facts. The refusal tests in Step 1 and Phase 1's toolset tests are the proof, mirroring bb's §2.1 "attempt it directly against the API" acceptance style.
- No Bonsai self-judging: the goal loop's judge and every review seat are stronger models. Bonsai types; it never grades.

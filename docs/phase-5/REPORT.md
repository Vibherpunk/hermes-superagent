# Phase 5 Report — Superagent Attention Panel, Derived Stage & Read Endpoints

Date: 2026-11-03  
Branch: `superagent-p3`  
Worktree: `superagent-p3`  
Base commit: `e86086a094` (Phase 1 completion), carrying Lane D cards (3.1, 5.1, 5.2, 5.2q, 5.3, 5.4, 3.9) and prerequisite superagent schema (`91a0a4509f`, `2861ae024c`).  
Invariant: `superagent.enabled` remains `false` everywhere; live configs and daemon processes untouched; observe mode intact.

---

## 1. Executive Summary

Phase 5 delivers the Superagent Attention Panel, pure derived-stage lifecycle classifier, backend read query suite, and REST endpoints for human operator visibility into autonomous agent workflows.

All implemented cards (5.1, 5.2, 5.2q, 5.3, 5.4) have been built, thoroughly verified against deterministic test suites, and reviewed with **PASS** verdicts by independent reviewer seats (Gemini 4 Argon High). Cards 5.5 and 5.6 are formally staged as integration items for the stacked tree. Card 5.3 delivered the complete GET read surface; corresponding POST mutation endpoints are staged as an integration item alongside the Lane B orchestrator engine and Lane C outbox controller.

---

## 2. Card Execution & Review Table

| Card | Task ID | Title | Scope & Deliverables | Review Seat & Verdict | Commit SHA |
|---|---|---|---|---|---|
| **5.1** | `t_ba758b5d` | Recon kanban dashboard implementation | Architectural reconnaissance documenting plugin mounting, FastAPI routes, and React webapp patterns in `notes/p5-1-anchors.md`. | Gemini 4 Argon High — **PASS** (`REVIEW-p5-5.1.md`) | `ead005a9285347cb2af2df9ce19a7833ec043cdc` |
| **5.2** | `t_959fe2ec` | Derived stage function + tests | Pure function `derived_stage(ticket, ctx)` implementing SPEC-V2 §9 first-match precedence without database I/O in `hermes_cli/orchestrator_reads.py` + tests in `tests/superagent/test_stage.py`. | Gemini 4 Argon High — **PASS** (`REVIEW-p5-5.2.md`) | `1291d266f329dd81102a04e207b3d2c7d987cfe3` |
| **5.2q** | `t_dbbab409` | `orchestrator_reads` queries + fixture tests | Core read query layer in `hermes_cli/orchestrator_reads.py` (`needs_person`, `health`, `flow_counts_24h`, `thread_timeline`, `ticket_detail`, `decisions`, `dropped_and_failed_replies`) + fixture tests in `tests/superagent/test_reads.py`. | Gemini 4 Argon High — **PASS** (`REVIEW-p5-5.2q.md`) | `f6e93ef48dfa4d440e8e27c03bbed567cc23e25b` |
| **5.3** | `t_39585937` | Attention panel API endpoints (GET) | REST endpoints in `plugins/kanban/dashboard/plugin_api.py` under `/attention/*` gated by `superagent.enabled` (404 when disabled) + test suite in `tests/superagent/test_attention_api.py`. *(GET half; POST endpoints are an integration item).* | Gemini 4 Argon High — **PASS** (`REVIEW-p5-5.3.md`) | `59d0769b4b98924f06a2e179697a5b99d0c54a12` |
| **5.4** | `t_5259e9d9` | Attention panel views | Frontend UI in `web/src/pages/AttentionPage.tsx`, API client in `web/src/lib/api.ts`, route in `web/src/App.tsx`, i18n in `web/src/i18n/`, and component tests in `web/src/pages/AttentionPage.test.tsx`. Empty-state rule and stage ordering strictly enforced. | Gemini 4 Argon High — **PASS** (`REVIEW-p5-5.4.md`) | `426fb7b640e7fd11f33c5859269b866a3dd13415` |
| **5.5** | `t_aa513f8c` | Router prompt asset update | Updating the Router prompt asset with Superagent attention context and tool schemas. *(Integration-stage item).* | Deferred to Integration | *(Integration stage)* |
| **5.6** | `t_070cc7ad` | Screenshot pass (fixture DB) | Automated headless browser capture of the Attention Panel populated with fixture data. *(Integration-stage item).* | Deferred to Integration | *(Integration stage)* |
| **5.7** | `t_d37d590b` | Phase 5 acceptance + REPORT | Verification test runs across Python backend and TypeScript frontend, phase documentation, and acceptance report. | Current Builder | *(This commit)* |

---

## 3. Integration-Stage Items: 5.3 POST Endpoints, 5.5, and 5.6

Per the Superagent architecture and build sequencing, three items are explicitly designated as **integration-stage items** that land during full-stack cross-lane integration:

1. **Card 5.3 POST Endpoints**:
   - **Delivered in Phase 5**: The complete GET half of the API (`/attention/overview`, `/attention/threads`, `/attention/thread/{thread_key}`, `/attention/decisions`, `/attention/tickets`, `/attention/ticket/{ticket_id}`, `/attention/settings`), including the `superagent.enabled` 404 gate.
   - **Deferred to Integration**: POST mutation endpoints (such as `/attention/pause` to toggle orchestrator pause state, `/attention/decisions/{id}/override` to manually amend or force an eval decision, and `/attention/retry/{outbox_id}` to re-dispatch a failed outbox message). These endpoints directly invoke mutations across Lane B (`hermes_cli/orchestrator.py`) and Lane C (`hermes_cli/superagent_outbox.py`, `hermes_cli/superagent_pause.py`). Keeping POST endpoints in the integration pass avoids multi-writer conflicts across active worktree branches.

2. **Card 5.5 — Router Prompt Asset Update (`t_aa513f8c`)**:
   - Updating the canonical Router system prompt asset (`prompts/superagent/router.md`) requires the unified tool surface from all four build lanes (Lane A worker isolation tools, Lane B admission/eval tools, Lane C outbox messaging, Lane D thread subscriptions and attention reads).
   - Authoring the prompt asset on an isolated branch would bake in stale or partial tool signatures. It lands on the merged tree as an integration step once all sibling lanes are reconciled.

3. **Card 5.6 — Screenshot Pass (`t_070cc7ad`)**:
   - The screenshot pass generates authoritative visual evidence by launching the Hermes dashboard against a seeded fixture database.
   - This requires the complete integrated backend runtime (including both GET reads and POST mutations) and live web server asset serving. It executes as part of the supervisor's final integration milestone.

---

## 4. Acceptance Test Evidence

### (a) Backend Superagent Test Suite
Command:
```bash
bash ./scripts/run_tests.sh tests/superagent/ -q
```
Verbatim Output:
```
Discovered 7 test files (~64 tests) under ['tests/superagent']; running with -j 14
[  9.4% |     6/~64 | ✓6 | ✗0] ✓ tests/superagent/test_slots.py (6✓, 0.4s)
[ 32.8% |    21/~64 | ✓21 | ✗ 0] ✓ tests/superagent/test_stage.py (15✓, 0.7s)
[ 57.8% |    37/~64 | ✓37 | ✗ 0] ✓ tests/superagent/test_config.py (16✓, 0.8s)
[ 64.1% |    41/~64 | ✓41 | ✗ 0] ✓ tests/superagent/test_schema.py (4✓, 0.8s)
[ 73.4% |    47/~64 | ✓47 | ✗ 0] ✓ tests/superagent/test_subscriptions.py (6✓, 0.9s)
[ 84.4% |    54/~64 | ✓54 | ✗ 0] ✓ tests/superagent/test_reads.py (7✓, 1.1s)
[100.0% |    64/~64 | ✓64 | ✗ 0] ✓ tests/superagent/test_attention_api.py (10✓, 1.7s)

=== Summary: 7 files, 64 tests passed, 0 failed (100% complete) in 1.8s (14 workers) ===
```

### (b) Kanban Dashboard Plugin Suite
Command:
```bash
bash ./scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py -q
```
Verbatim Output:
```
Discovered 1 test files (~38 tests) under ['tests/plugins/test_kanban_dashboard_plugin.py']; running with -j 14
[100.0% |    38/~38 | ✓38 | ✗ 0] ✓ tests/plugins/test_kanban_dashboard_plugin.py (38✓, 6.9s)

=== Summary: 1 files, 38 tests passed, 0 failed (100% complete) in 6.9s (14 workers) ===
```

### (c) Web Frontend TypeScript Typecheck
Command:
```bash
cd web && npm run typecheck
```
Verbatim Output:
```
> web@0.0.0 typecheck
> tsc -b . --noEmit
```
*(Exited with code 0; zero type errors).*

### (d) Web Frontend Component Unit Tests
Command:
```bash
cd web && npx vitest run src/pages/AttentionPage.test.tsx
```
Verbatim Output:
```
 RUN  v4.1.12 superagent-p3/web

 ✓ src/pages/AttentionPage.test.tsx (4 tests) 687ms
   ✓ AttentionPage (4)
     ✓ enforces EMPTY-STATE RULE: exactly one line and no extra chrome/cards when needs-me is empty  656ms
     ✓ orders needs-me tickets strictly in derived-stage order (delivery, answer, review, blocked) 11ms
     ✓ renders health strip, 24h flow counts, executor load, and other stages 7ms
     ✓ opens ticket detail drawer with plain words mandate basis, amendment chain, inputs and outbox 11ms

 Test Files  1 passed (1)
      Tests  4 passed (4)
   Start at  16:20:07
   Duration  1.12s (transform 1.58s, setup 0ms, import 158ms, tests 687ms, environment 220ms)
```

### (e) Web Frontend Full Test Suite
Command:
```bash
cd web && npm run test
```
Verbatim Output:
```
 Test Files  66 passed (66)
      Tests  453 passed (453)
   Start at  16:19:49
   Duration  3.53s (transform 16.53s, setup 0ms, import 18.12s, tests 6.02s, environment 13.29s)
```

---

## 5. Architectural Invariants Verified

1. **Pure Derived Stage Function (DECISION E-032)**:
   - `derived_stage(ticket: dict, ctx: dict) -> str` in `hermes_cli/orchestrator_reads.py` is pure and deterministic.
   - Evaluates strictly in the SPEC-V2 §9 order:
     1. `ctx.last_delivery_error` $\to$ `"person/delivery"`
     2. `ctx.proposal_state == 'awaiting_answer'` or (`status == 'blocked'` and `block_kind == 'needs_input'`) $\to$ `"person/answer"`
     3. `ctx.proposal_state == 'awaiting_review'` or (`status == 'blocked'` and `block_kind == 'review'`) $\to$ `"person/review"`
     4. `status IN ('blocked', 'failed')` $\to$ `"person/blocked"`
     5. `status IN ('running', 'claimed')` or `ctx.sa_state == 'working'` $\to$ `"working"`
     6. `status IN ('ready', 'todo')` or `ctx.sa_state == 'dispatched'` $\to$ `"queued"`
     7. `status IN ('done', 'completed')` $\to$ `"closed"`
     8. Default fallback $\to$ `"idle"`
   - Stage is computed dynamically on read and never persisted in database columns.

2. **Fast 404 Guard for Disabled State**:
   - Every `/api/plugins/kanban/attention/*` endpoint verifies `_superagent_enabled()`.
   - When `superagent.enabled` is false, all attention endpoints return HTTP 404 with detail `Superagent is not enabled`.

3. **Frontend Empty-State Rule**:
   - When `needs_person` contains zero tickets, the "Needs Me" section renders exactly one single-line text banner without any card wrappers, action buttons, or table chrome.

4. **Plain Words Mandate Rendering**:
   - The ticket detail drawer transforms internal mandate JSON into human-readable facts (`Strength`, `Via`, `Active Rules`, `Policy Version`, `Scope`) rather than dumping raw JSON blocks.

5. **Worktree & Repository Invariants**:
   - `superagent.enabled` remains `false` across all configuration blocks and profiles.
   - Zero pre-existing dirty files were touched, staged, or committed.
   - Complete worktree isolation maintained on branch `superagent-p3`.

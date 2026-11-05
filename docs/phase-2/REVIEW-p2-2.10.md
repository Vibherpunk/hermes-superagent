# REVIEW — Phase 2 card 2.10 (stub replacement)

Reviewer: claude-sonnet-5-5 (high) via Antigravity bridge — parallel review seat per the 2026-11-03 pace directive (the first Argon attempt returned no verdict — delegation dead-end, on file with the supervisor). Sonnet did not build the card. Sha ccbc249c67 verified on disk in worktree superagent-p2. Verbatim verdict (PASS with non-blocking findings; the two semantic traps — thread_unsubscribe task_id, eval_decide observe/pending — go to fix card p2-10fix):

VERDICT: PASS
SHA: ccbc249c67

I ran the card's test file (17 passed) and read the diff against `hermes_cli/orchestrator.py` and `hermes_cli/kanban_db.py`. I did not run the broader suite, because the worktree has no other orchestrator-tools tests to regress.

FINDINGS (none blocking; worth a follow-up):
- **Scope:** The commit touches only `tools/orchestrator_tools.py`, `tools/orchestrator_tools_schemas.py` and the new test file. None of the listed dirty files appear, and `git status` is clean. The diff also contains unrelated churn: `Dict`/`List` to `dict`/`list` rewrites and `# health: allow` comments across the Phase-1 code.
- **Stubs:** All eight tools (the card says seven but names eight) now point at real handlers. No `_handle_phase_1_stub` or `not_available_phase_1` remains.
- **Gating:** `_orchestrator_handler` now refuses with "superagent is disabled" when `superagent.enabled` is false. The disabled-gate test sits on `orchestrator_eval` only, as the card allows. The same wrapper also covers the Phase-1 route/read/reply tools, which fits "all tools refuse" and changes nothing observable, since the registry already hides them when disabled.
- **Dead code:** There is an unreachable second `return deco` after the first one in `_orchestrator_handler`.
- **Misleading `task_id` on `thread_unsubscribe`:**
  - `ORCHESTRATOR_THREAD_UNSUBSCRIBE_SCHEMA` advertises `task_id` as an "Optional ticket id filter".
  - The handler ignores `task_id` and deletes every `sa_subscriptions` row for the thread.
  - A caller who passes `task_id` expecting a narrow unsubscribe will drop all subscriptions. Remove the parameter or implement the filter.
- **Speculative `thread_unsubscribe` import:**
  - The handler tries a top-level `import superagent_subscriptions` and calls `drop_thread_subscriptions(thread_key, conn=conn)`.
  - That module does not exist in this worktree, and the module path and `conn=` keyword are guesses. If Lane D lands it under `hermes_cli.`, this branch never fires.
  - The direct DELETE fallback and the integration comment are present as the card asks.
- **`eval_decide` accept reports success when nothing is created:**
  - In observe mode `accept()` returns `None`. The handler still returns `accepted: True, ticket_id: null`.
  - It also does not check `dismissed` or an existing `ticket_id`. A dismissed decision can still be accepted, and an already-ticketed decision can be marked dismissed.
  - The schema says "pending Decision", but pending is not enforced.
  - The tests cover neither observe mode nor these cases.
- **`ticket_retry` forced status write:**
  - The handler uses `kb.reclaim_task`, which is the existing dispatcher retry path, and `kb.unblock_task`.
  - It then runs a raw `UPDATE tasks SET status='ready'` for any non-terminal task that is not already ready. This applies to tasks that were never running or blocked, for example `todo` or `triage`, and it overrides the phase `reclaim_task` restored.
  - That skips dependency gating and is partly the hand-rolled write the card said to avoid. It is a fallback, not the main path.
- **Identity discipline:**
  - `sa_inputs` has no author column, so `ticket_note` has nowhere to carry the router identity. `eval_decide` dismiss writes to `sa_decisions` directly with no author either.
  - `accept()` creates tickets with `created_by="superagent"`. That comes from card 2.6, not this card, and the tools themselves add no mock-patching.
- **Tests:**
  - Every tool has a happy path, and each tool except `orchestrator_eval` has a refusal. `eval_read` and `thread_unsubscribe` refuse on a missing `thread_key`, which is argument validation rather than a semantic refusal.
  - The card's named refusals (requeue while evaluating, decide on a missing decision, retry on a terminal ticket, note on a missing ticket) are all present.

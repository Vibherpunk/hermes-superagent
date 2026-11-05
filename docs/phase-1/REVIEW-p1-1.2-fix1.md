# REVIEW — Phase 1, card 1.2 fix round 1

- Reviewer: claude-opus-5-5-high via Antigravity bridge (127.0.0.1:8011), independent third-party seat
- Commit reviewed: `e86086a0945` — verified on disk: `git rev-parse e86086a0945` = `e86086a0945170d767910734ff562b68da3f15f7` ✔ matches the sha in the verdict
- Date: 2026-11-03

## Verbatim verdict (condensed only for the reviewer's file-link formatting; findings complete)

VERDICT: PASS
SHA: e86086a0945
FINDINGS:
- (Not blocking, for the record) Nothing in `tests/superagent/` checks these four fixes; the suite has only `test_config.py` and `test_slots.py`, and nothing under `tests/` imports `orchestrator_tools`. So "22 passed" shows nothing broke elsewhere, but it doesn't prove the fixes. The packet only asked for the suite to pass and didn't ask for new tests, so this doesn't fail the card. To check the fixes themselves, I ran a smoke script through `scripts/run-in-hermes-env` with a temporary `HERMES_HOME`:
  - Item 4: `orchestrator_route` with no assignee returns `{"error": "orchestrator_route: assignee is required."}`. There is no `default_assignee` fallback any more.
  - Item 1: a route-created task is stored in the DB with `created_by=router`. A `kanban_create` call in the same process afterwards is stored with `created_by=default`, so the router stamp doesn't leak to other callers. The route handler calls `kb.create_task(..., created_by="router")` — the same database function `kanban_create` uses, and it takes `created_by` as an argument.
  - Item 2: if the adapter lookup raises `AttributeError` or returns `None`, the result is `sent: False` with an error. The primary adapter had a recording `send()`, and it received nothing. Any other exception from the lookup goes to the `_orchestrator_handler` wrapper, which returns a JSON error before anything is sent.
  - Item 3: grepping `tools/orchestrator_tools.py` for `mock|patch.object|_ORIG_|default_assignee|.adapters` finds nothing.
- (Not blocking) `_handle_route` repeats about 60 lines of the `_handle_create` body instead of sharing them. That's how you call the database function directly as item 1 requires. The risk is that a later fix to `kanban_create` won't reach `orchestrator_route` automatically.
- Scope: the commit changes only `tools/orchestrator_tools.py` (+92/−55), the file the card names. The worktree is clean.
- Disabled path unchanged: `_check_orchestrator_mode` still returns `False` unless `superagent.enabled` is set and `router_profile` matches the active profile.
- Tests: the reviewer ran `bash scripts/run_tests.sh tests/superagent/ -q` → 2 files, 22 tests passed, 0 failed (matches the supervisor's count).

## Supervisor disposition

PASS stands. Card 1.2 is passed on fix commit e86086a0945. The "no tests cover the tools" gap is exactly card 1.3's assignment, which now proceeds.

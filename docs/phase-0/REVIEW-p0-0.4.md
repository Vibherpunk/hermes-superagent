# REVIEW — Phase 0, card 0.4 (SlotPool controller)

- Reviewer: gemini-4-argon-high via Antigravity bridge (127.0.0.1:8011), independent third-party seat
- Commit reviewed: `a783def4b35` — verified on disk: `git rev-parse a783def4b35` = `a783def4b35d2142d4bc5f51b0930d7b7e7951da` ✔ matches the sha in the verdict
- Date: 2026-11-03

## Verbatim verdict

VERDICT: FAIL
SHA: a783def4b35
FINDINGS:
- `SlotPool.release` (gateway/kanban_watchers_dispatcher.py#L479-L483) returns `False` when releasing an unknown (or already-released) token instead of raising `ValueError`, violating the pinned requirement in the card (`release(token) frees exactly that token; releasing an unknown token raises ValueError`).
- The corresponding test `test_release_unknown_token_returns_false` (tests/superagent/test_slots.py#L61-L63) asserts `pool.release("unknown-token") is False` instead of asserting that `ValueError` is raised.

## Supervisor disposition

FAIL stands — the card packet pinned ValueError semantics; the builder implemented return-False and its card-0.5 test codified the deviation. Fix round: `SlotPool.release` raises ValueError on unknown/already-released token; the 0.5 test is corrected to assert the raise. Re-review by Argon required before cards 0.4/0.5 are marked passed. (Argon's separate review of card 0.5's commit returned PASS with no findings; the supervisor does not accept that pass for the affected test, since the same test is named in this FAIL — the fix supersedes it and both cards get re-reviewed on the fix commit.)

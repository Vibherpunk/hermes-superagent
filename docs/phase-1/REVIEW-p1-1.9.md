# REVIEW — Phase 1 card 1.9 (acceptance evidence)

Reviewer: claude-sonnet-5-5 (high) via Antigravity bridge — parallel seat per the pace directive. Sha dde5e765f1 verified on disk in worktree superagent-p1. Verbatim verdict (FAIL — combined gateway run missing from the note; fix = run it and amend the note, in progress):

Logs exist; I'll check them against the note and compare the failure sets.
Waiting on the teardown-test reruns in both trees.
Both tests are still running. I'll stop here and pick up when the timer or the test task fires.
VERDICT: FAIL
SHA: dde5e765f1
FINDINGS:
- **The card's acceptance command was never run.**
  - The todo requires `bash scripts/run_tests.sh tests/superagent/ tests/ -k 'kanban or gateway' -q`.
  - The note's "Run 1" is `tests/superagent/ -q` only (4 files, 43 passed). The `-k 'kanban or gateway'` summary line is missing.
  - `/tmp` holds only `acc-p1-superagent.log`, `acc-p1-kanban.log` and `acc-base-kanban.log`. No log for the combined command exists.
  - Phase 1 changed gateway code: `gateway/kanban_watchers_dispatcher.py` (+213), `gateway/session_kanban.py`, and `tests/gateway/test_session_kanban.py`. The "no regression" claim therefore has no evidence for gateway tests outside the `kanban` keyword.
- **The kanban numbers check out.**
  - All three pasted summary lines match the logs verbatim: 43/0 for the phase tests, 920/129/10/24 for the phase kanban run, and 892/132/10/24 with 1 crashed file for the baseline.
  - The arithmetic is right: +28 passed, -3 failed, errors and skips equal, +5 files.
  - The commit list matches `git log af9f3c257c4..HEAD`: 11 commits covering 1.1–1.8 plus the 1.2 and 1.7 fixes. The 1.9 commit itself is not listed, which is fine.
- **The regression reading skips the failing-test comparison, and the counts alone hide one difference.**
  - I diffed the per-test FAILED lines between the two kanban logs. Three baseline failures are gone in the phase tree: `tests/gateway/test_session_kanban.py::test_claim_reclaimed_before_admission_marker_is_refused` and two `test_kanban_worker_spawn_toolsets.py` tests.
  - One failure appears only in the phase tree: `tests/hermes_cli/test_kanban_worktree_teardown.py::test_tree_dirtied_between_check_and_removal_preserved`. The note does not mention it.
  - It is not a regression. In the baseline run that whole file CRASHED with SIGTERM, so none of its tests were counted. I reran the file alone in both trees and got the same failure in each: 18 passed, 1 failed, with `TEST BUG: file I/O against the REAL hermes home .../.git/worktrees/<tree>/hermes-update-pull`. It is a pre-existing environment failure.
  - The note should have named this test and explained the baseline crash instead of reporting only counts. Its "failure mass unchanged in character" wording leaves out the baseline crash and the swapped failures.
- **Scope is fine.** The commit touches only `notes/p1-9-acceptance.md`, and the note says the supervisor produced it. There is no behavior change while `superagent.enabled` is false.

The kanban no-regression reading holds, but the card's required acceptance run is missing from the note. To pass, add the `-k 'kanban or gateway'` summary line and a short note on the teardown test and the baseline crash.

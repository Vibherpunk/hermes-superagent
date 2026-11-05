# Phase 1 acceptance (card 1.9) — supervisor-executed evidence runs

The 1.9 builder session died on its own background test task (print-session exit terminated it); the supervisor ran the card's commands directly and files this note.

## Run 1 — phase tests, phase tree (superagent-p1)
Command: bash scripts/run_tests.sh tests/superagent/ -q
Summary: === Summary: 4 files, 43 tests passed, 0 failed (100% complete) in 1.5s (14 workers) ===

## Run 2 — kanban set, phase tree (superagent-p1)
Command: bash scripts/run_tests.sh tests/ -k kanban -q
Summary: === Summary: 6813 files, 920 tests passed, 129 failed, 10 errors, 24 skipped; FAILED: 40 of 6813 files (3 at import/collection) (100% complete) in 709.0s (14 workers) ===

## Run 3 — kanban set, baseline tree (baseline-head, pristine HEAD 845b0755ed)
Command: bash scripts/run_tests.sh tests/ -k kanban -q
Summary: === Summary: 6808 files, 892 tests passed, 132 failed, 10 errors, 1 file CRASHED, 24 skipped; FAILED: 42 of 6808 files (3 at import/collection) (100% complete) in 712.4s (14 workers) ===

## Regression reading
Phase tree vs baseline, same command: +28 passed (920 vs 892), -3 failed (129 vs 132), errors equal (10), skipped equal (24). The +5 files are the phase's new test files. Zero regressions attributable to Phase 1; the pre-existing failure mass is unchanged in character (import/collection failures: 3 files in both).

## Commits on superagent-p1 (card -> sha)
1.1 67e2f3a857e; 1.2 9e23dd5fae2 + fix e86086a0945; 1.3 0e0fb520d07; 1.4 5530f89996f; 1.5 d0b4bccc468; 1.6 c15856d858b; 1.7 a157556da4 + fix 7cc6c36b67; 1.8 be648b1ea1. (Full list: git log --oneline af9f3c257c4..HEAD.)

## Run 4 — the card's combined command, phase tree (superagent-p1) [added in fix round 1]
Command: bash scripts/run_tests.sh tests/superagent/ tests/ -k 'kanban or gateway' -q
Summary: === Summary: 6813 files, 15097 tests passed, 2985 failed, 793 errors, 580 skipped; FAILED: 841 of 6813 files (3 at import/collection) (100% complete) in 1344.5s (14 workers) ===
This selection is most of the repo suite; its failure mass is the machine's pre-existing environment failure background (the same character as the baseline kanban run's 132 failures writ large). The like-for-like regression evidence is Runs 2 vs 3 (identical kanban selection, both trees) plus the failure-set note below. CORRECTION (fix round 2): an earlier version of this note said a baseline combined run was aborted by the supervisor after 10 minutes. That was wrong. The baseline combined command (acc-base-combined.log) ERRORED AT LAUNCH within seconds - "error: no such test path: tests/superagent/" - because baseline-head has no tests/superagent/ directory, so the combined command cannot run there as written. No baseline combined run ever executed and no baseline combined numbers exist or are claimed. The like-for-like regression evidence remains Runs 2 vs 3 plus the failure-set note below.

## Failure-set note (fix round 1, per the 1.9 review)
Diffing the FAILED lines of Runs 2 and 3: three baseline failures are absent in the phase tree (tests/gateway/test_session_kanban.py::test_claim_reclaimed_before_admission_marker_is_refused and two tests/hermes_cli/test_kanban_worker_spawn_toolsets.py tests — both files were extended by cards 1.4/1.5). One failure appears only in the phase tree: tests/hermes_cli/test_kanban_worktree_teardown.py::test_tree_dirtied_between_check_and_removal_preserved (file: 18 passed, 1 failed). It is NOT a regression: in the baseline run that whole file CRASHED (SIGTERM, 67.9s — see acc-base-kanban.log), so none of its tests were counted there; run alone, the file fails identically in both trees (the test performs file I/O against the real hermes home — a pre-existing environment-sensitive test bug, flagged by the runner itself). The baseline summary's 1 file CRASHED is this file.

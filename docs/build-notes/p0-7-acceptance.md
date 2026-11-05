# P0.7 — Phase 0 acceptance evidence (supervisor-run)

Run by the supervisor on branch `superagent-p0` in worktree
`~/.hermes/hermes-agent/.worktrees/superagent-p0`, 2026-11-03.
(Fix round 1: rewritten with pasted outputs and the full commit list after
Argon review f6c46499409 returned FAIL on the summary-style first version.)

## Command substitution (recorded deviation)

The pack's literal acceptance command (`python -m pytest …`) cannot run on this
machine: the repo venv ships no pytest and the system pytest uses a Python
without `tomllib`. Per the repo's own AGENTS.md, the canonical runner is
`scripts/run_tests.sh`; all commands below use it. This is the equivalent
command, not a skipped gate.

## (a) Acceptance command output — the phase's own tests

Command: `bash scripts/run_tests.sh tests/superagent/ tests/ -k 'kanban or config' -q`
(the `-k` form is the pack command with the runner substituted)

Phase worktree, full run (50,988 tests selected), pasted summary line:

```
=== Summary: 6810 files, 4137 tests passed, 571 failed, 68 errors, 83 skipped; FAILED: 215 of 6810 files (3 at import/collection) (100% complete) in 648.0s (14 workers) ===
```

The phase's own test files alone, on the final tree after both fix rounds:

Command: `bash scripts/run_tests.sh tests/superagent/ -q`

```
=== Summary: 2 files, 22 tests passed, 0 failed (100% complete) in 0.5s (14 workers) ===
```

(On the builder's original tree the same command read: `2 files, 16 tests
passed, 0 failed`; the fix rounds added the reviewer-required cases.)

## (b) Full existing kanban test set output + regression comparison

Command (both trees): `bash scripts/run_tests.sh tests/ -k kanban -q`

Final phase tree (`superagent-p0` @ 7448d532765 + this note), pasted summary:

```
=== Summary: 6810 files, 910 tests passed, 133 failed, 10 errors, 24 skipped; FAILED: 42 of 6810 files (3 at import/collection) (100% complete) in 896.5s (14 workers) ===
```

Pristine baseline worktree at HEAD 845b0755ede (`.worktrees/baseline-head`),
pasted summary:

```
=== Summary: 6808 files, 910 tests passed, 133 failed, 11 errors, 24 skipped; FAILED: 43 of 6808 files (3 at import/collection) (100% complete) in 900.0s (14 workers) ===
```

Reading: identical passed (910) and failed (133) test counts on both trees —
every kanban test failure on the phase tree also fails on pristine HEAD on
this machine (pre-existing), so Phase 0 introduces **zero kanban regressions**.
The ±1 differences (errors 10 vs 11; failed files 42 vs 43; 2 extra files) are
collection-level: the phase tree contains two additional test files
(tests/superagent/test_config.py, test_slots.py — the phase's own, all passing)
and one flaky import error moves between runs.

Spot isolation run (the four kanban-named files that fail), both trees:

```
baseline-head : === Summary: 4 files, 37 tests passed, 38 failed ===
superagent-p0 : === Summary: 4 files, 37 tests passed, 38 failed ===
```

Config validation back-compat: the phase adds a new `superagent` section to
DEFAULT_CONFIG and a `_validate_superagent` check that only fires when a
`superagent` key is present; no existing key's default or validation changed
(anchors: notes/p0-1-anchors.md; diffs: cards 0.2/0.6). Nothing activates while
`superagent.enabled` is false; SlotPool is instantiated nowhere yet (Phase 1+
wiring). No gateway restart was performed during the build, and
`superagent.enabled` was never written to a live profile config.

## (c) Every commit on branch superagent-p0, with its card

```
8e6662d5d6d  p0.1  Recon anchors (no code) — first version
6585b2ffd0d  p0.2  Config parsing for the superagent block
087fdb6acef  p0.1  Recon anchors (no code) — final version (reviewed)
4ba609413f8  p0.3  Tests for config parsing
a783def4b35  p0.4  SlotPool controller
9c7967f0c18  p0.5  Tests for SlotPool
b156eb980d7  p0.6  Sample config block in example file
8c935746373  p0.2/p0.3  Fix round 1 — Argon findings (human_reserve_slots {0,1},
             receive_all default merge, bool rejection; test corrections)
7448d532765  p0.4/p0.5  Fix round 1 — release raises ValueError (test corrected)
f6c46499409  p0.7  Phase acceptance evidence, first version (Argon FAIL —
             summarized instead of pasted; superseded by this rewrite)
<this commit> p0.7  Phase acceptance evidence, fix round 1 (this file)
```

## Review chain

Filed in the pack's phase-0 folder, each with its commit sha recomputed on
disk and matching: REVIEW-p0-0.1.md (PASS), REVIEW-p0-0.2.md (FAIL) +
REVIEW-p0-0.2-fix1.md (PASS), REVIEW-p0-0.4.md (FAIL) + REVIEW-p0-0.4-fix1.md
(PASS), REVIEW-p0-0.5.md (PASS, partially superseded — see file),
REVIEW-p0-0.6.md (PASS), REVIEW-p0-0.7.md (FAIL on the first version of this
note; fix-round verdict filed alongside).

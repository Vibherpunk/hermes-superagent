# Builder Contract — Super-agent adoption in Hermes

This contract binds ANY builder (human or model) executing the todo packs in this directory. It exists so the builder never has to make a judgment call: every foreseeable decision is pre-made in `DECISIONS.md`, and anything not covered there is a stop, not a choice.

## Hermes-native mode (primary)

When the work runs inside Hermes per `HERMES-BONSAI-RUNBOOK.md`, these amendments override the generic rules below wherever they conflict:

- **A1. The card is the unit.** The builder works ONE Kanban card, in one run. Card state (heartbeat/block/complete) replaces TODOS.md marking; the builder never edits the markdown pack — the supervisor is its only writer and syncs markers from card outcomes after review.
- **A2. The context packet governs.** The builder works from the card's packet (todo verbatim + named files + excerpted decisions + acceptance command). It does not read SPEC-V2 or the full DECISIONS.md. A packet/code conflict is a block, not a research project.
- **A3. Provisioning check first.** The run's first action verifies the environment (named files at anchors, test path exists) and posts it as a comment. Failure = typed block, kind `provisioning`, zero code written.
- **A4. Approvals bind content.** A review pass names the commit sha it reviewed; any later change to that diff voids the pass.
- **A5. The builder never judges its own card.** The goal loop's judge and all review seats are stronger models, set by the supervisor on the card — never the builder's own model.

## The rules

1. **Work one todo at a time.** Mark it `[>]` in that phase's `TODOS.md` before starting, `[x]` only when its acceptance command passes, with the command's output pasted under the todo. Keep exactly one `[>]` at a time per phase. (Generic route only; Hermes mode uses A1.)
2. **Touch only the files named in the todo.** If the work seems to require a file not named, that is a blocker (rule 5), not an expansion.
3. **Never make a judgment call.** A judgment call is any of: choosing between two designs not distinguished in `DECISIONS.md`; inventing a default value; naming a new concept, table, column, tool, or config key not in the spec or `DECISIONS.md`; deciding what "should" happen in a case the spec doesn't cover; changing an interface another phase depends on. All of these are stops.
4. **The stop protocol.** When you hit anything in rule 3, or a test fails for a cause you cannot identify from the todo text alone, or a named file/function does not exist as described:
   - Mark the todo `[!]` and write the blocker on the same line: what you expected, what you found, file:line evidence.
   - Write the same blocker, plus what you tried, in a `BLOCKED.md` in the phase folder.
   - Stop work on that phase. You may continue with later todos **only if** they do not depend on the blocked one (dependencies are marked in each TODOS.md). When in doubt, they depend.
   - Never guess past a blocker to keep moving. A blocked todo reported honestly is a success of this process; a guessed decision is a defect, even if the guess turns out right.
5. **Tests are the target.** Acceptance tests are named in each todo. If a todo says "write test X then make it pass," write the test exactly as described first. Never weaken, skip, or delete an existing test to make a suite pass. If an existing test contradicts a todo, that is a blocker (rule 4).
6. **No drive-by changes.** No refactoring, renaming, reformatting, or "improvements" outside the todo's named files — even when the surrounding code looks wrong. Note it in `BLOCKED.md` under "Observations (not acted on)" and move on.
7. **No new dependencies.** Do not add packages, services, or files outside the phase's named set. Stdlib and Hermes' existing dependencies only.
8. **Schema changes only as written.** DDL appears verbatim in the phase TODOS/DECISIONS. Type it exactly, including column order and defaults. Any deviation is a blocker.
9. **You do not review your own work.** When a phase's todos are all `[x]`, write `REPORT.md` in the phase folder: what was built, acceptance outputs, every deviation or observation. Then stop. A separate reviewer (strong model, per the standing architect+reviewer rule) gates the phase. Do not start the next phase until the current phase's `REPORT.md` has a reviewer sign-off line appended (`REVIEWED: <name/date> — pass`).
10. **Commits.** One commit per todo, message format: `superagent p<phase>.<todo>: <todo title>`. Never commit with a failing acceptance command. Never amend another phase's commits.
11. **Live-system safety.** Hermes on the Mac Studio is a live personal agent (Telegram bot, mail, voice depend on it). Builder rules: work in a git worktree/branch named `superagent-p<n>`; never restart the running gateway; never edit `~/.hermes/config.yaml` or any profile `.env` except where a todo explicitly says to (Phase 0 adds a *sample* block to the example config, not the live one); never run the gateway against the live `kanban.db` — tests use temp DBs (`kanban_db_connect` test fixtures) exactly as existing kanban tests do.
12. **Vocabulary is fixed.** Use only the domain words from the v2 spec: Source, Trigger, Thread, Decision, Mandate, Ticket, Assignment, Subscription, Input, Report, Attention, Outbox, Router, Worker. In code these map to the names in `DECISIONS.md` §Naming. Do not introduce synonyms (job, task-instance, work-order) in new code.

## What the builder IS expected to do without asking

- Read the phase SCOPE.md, this contract, and DECISIONS.md before the first todo of a phase.
- Split a todo into sub-steps inside the same TODOS.md when a todo proves bigger than one session — keeping the same acceptance command.
- Fix typos and imports in files it created within the current todo.
- Re-run the full phase test set before writing REPORT.md and paste the summary.

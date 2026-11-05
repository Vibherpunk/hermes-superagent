# Phase 1 — Router / Worker split (pilot surfaces only)

**Objective:** On two pilot surfaces (GitHub via Phase-2 Source is NOT ready — pilot chat surface is **Telegram**, plus kanban board work created from it), the gateway profile acts as Router: routing tools only, direct-answer lane for conversation, all work executed by kanban Worker runs with structurally silenced toolsets.

**In scope**
- Router toolset restriction via the pilot profile's platform-toolset configuration (host-enforced config, not prompt text): Router keeps `orchestrator_*` tools (stub set this phase: route/read/reply backed by kanban tools), kanban read tools (`kanban_show`, `kanban_list`), memory/session-search (direct-answer lane). Everything else removed for the pilot profile.
- Worker toolset exclusions per E-020 for kanban-dispatched runs.
- Slot controller wired into the dispatcher tick (Phase 0's pool): launches acquire `launch`, person-wakes acquire `person_wake`.
- Shell auto-background attempt per E-022 (blocker if the terminal tool lacks a threshold setting — record, do not modify global defaults).
- Direct-answer lane: Router system-prompt section (prompt asset) instructing answer-from-recall for no-work conversation; NO new code path — the lane is the Router session's normal turn with its restricted toolset. Its boundary test is behavioral (done criterion 3), verified in review, not unit tests.

**Out of scope**
- Gate/Eval/Mandate (Phase 2): in this phase every Router-created Ticket comes from an explicit human request in chat. No proactive work, no admission table.
- Other platforms: non-pilot surfaces keep today's behavior exactly.
- Live cutover: pilot runs on a *separate test profile* against a test board, not Adam's live Telegram bot. Cutover is a separate, human-approved step after this phase's review.

**Done criteria**
1. On the test profile: a chat request creates a kanban task; a Worker run executes it; results return to the chat via the existing kanban notifier.
2. Test-profile Router transcript (reviewer-inspected) contains zero work-tool calls (no terminal/file/web tool invocations) across the acceptance script.
3. A trivial question ("what did we discuss about X yesterday") is answered with no task created (task count unchanged).
4. Worker run toolset dump shows `clarify`, `send_message`, `memory`, `cronjob_manage` absent.
5. Full kanban + gateway test sets pass; REPORT.md + reviewer sign-off.

**Constraints:** Contract in full. Decisions: E-019 (tool names; this phase implements route/read/reply only, others stubbed to return "not yet available" — stubs are allowed ONLY here and must be listed in REPORT.md), E-020, E-022, E-023, E-024.

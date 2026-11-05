# Phase 5 — Attention-first panel & quiet communication

**Objective:** A Super-agent view on the existing kanban dashboard that answers, in order: what needs a person now, is the system healthy, and what is the full history of one item (Trigger → Decision → Ticket → Run). Plus structural enforcement of the communication caps not already enforced in Phase 4.

**In scope**
- `orchestrator_reads.py`: read-only queries + the derived-stage function (E-032) shared by panel and Router tools.
- Dashboard view (extend the existing kanban dashboard surface — locate its view/API pattern first; no new app): Overview (needs-person queue = derived `person/*` stages + unresolved `unavailable` Decisions <24h + Outbox problems; health strip; 24h counts), Threads/Triggers list, Decisions list, Ticket detail (originating Trigger, Decision, Mandate summary incl. D-1 amendments, Inputs, Subscriptions, Assignments/runs), Settings read view for the `superagent` block (read-only this phase; editing stays in config file).
- Actions from the panel: stop Ticket (withdraws Mandate), retry Ticket, dismiss an `unavailable` problem, re-evaluate a Trigger, drop all subscriptions on a Thread, pause/resume processing (Phase 4's switch). Each calls the Orchestrator command of the same name — the panel contains no business logic.
- Quiet-communication audit: verify Phase 4's cap is the only length enforcement needed; add the acknowledgement-once rule to the Router prompt asset (confirm concretely, name the object + expected result) — prompt text change only, listed in REPORT.md.

**Out of scope**
- Settings editing UI, prompt editing UI (DeepSeek has both; Hermes v1 keeps config in files — deliberate, recorded here so it isn't "fixed" later).
- Any new Orchestrator commands: if a panel action has no command, that is a blocker, not an invitation to add one.

**Done criteria**
1. With 20 fixture Tickets running healthy and none needing a person, the Overview's needs-me list is empty and health shows green (screenshot in REPORT.md).
2. Every count displayed equals the length of the list it links to (asserted in view tests with fixtures).
3. Ticket detail for a fixture Ticket shows the complete chain Trigger → Decision → Mandate → runs, including a D-1 amendment entry where present.
4. Each panel action produces exactly the Orchestrator effect of the same name (API tests).
5. Stage function unit tests cover all seven rows of the v2 stage table, first-match order included.
6. REPORT.md + sign-off. This completes the v1 adoption; `auto` mode enablement remains a separate human decision after a week of panel-observed `observe` data.

**Constraints:** Contract in full. Decisions: E-032, E-034.

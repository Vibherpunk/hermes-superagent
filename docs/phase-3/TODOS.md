# Phase 3 TODOS

Acceptance: `python -m pytest tests/superagent/ -q` + the log-verification step in 3.8.

DDL:
```sql
CREATE TABLE IF NOT EXISTS sa_subscriptions (
    thread_key TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    context_only INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (thread_key, ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_sa_subs_ticket ON sa_subscriptions(ticket_id);
```

- [ ] 3.1 Schema + Orchestrator ops: `subscribe(ticket_id, thread_key, context_only=False)`, `unsubscribe(ticket_id, thread_key)`, `drop_thread_subscriptions(thread_key)` (person action), `subscriptions_for(thread_key)`, `threads_for(ticket_id)`. Tests: idempotent subscribe (same pair twice → one row); drop-all removes owning + context-only.
- [ ] 3.2 Fan-out: replace the Phase-2 stub in `receive_trigger`: after insert, for each subscription on the trigger's thread_key (correlation keys add context-only delivery to Tickets subscribed to the *primary* thread only — correlation never creates ownership): write `sa_inputs` rows; if any owning subscription exists and the trigger is a person's message, set disposition `fanned_out` and skip Eval queueing; bot/CI triggers still ALSO queue for Eval per v2 (they fan out as context and evaluate independently) — encode exactly that asymmetry; a person's message that reached an owning Ticket never enters Eval. Tests: one per row of that matrix (person+owning, person+context-only-only, bot+owning, person+no-subscription).
- [ ] 3.3 Wake + delivery: wire Input delivery to the run wake path identified in Phase 1 (todo 1.6's recorded finding). Delivery renders unread Inputs as one message (Trigger cards: source, actor, body; pointers not full dumps for bodies >2,000 chars — truncate with a `sa_inputs.id` reference the Worker can read via `kanban_show`-style read; if no such read exists, include full body and record the deviation as an observation, not a blocker). Mark `read_at` per SCOPE's rule. Tests with a fake run harness: delivery happens once per Input; a second tick does not redeliver read Inputs; delivery to a slot-full dispatcher waits (reserve-slot rule from Phase 1 applies to person Inputs).
- [ ] 3.4 PR following: hook the Worker's report path — when a run's summary/metadata names a PR number for the configured repo, or the Worker calls `kanban_comment` containing a PR URL for that repo (parse `github.com/<owner>/<repo>/pull/<n>`), subscribe the Ticket (owning) to `github:pr-<n>`. GitHub Source (Phase 2) gains the close/merge Trigger kinds if absent; on a close Trigger, Orchestrator applies the follows-done rule: Ticket ends when no followed PR it opened remains open (or all followed closed, if it opened none), unless resumed work is unfinished (an Input delivered after the close is unread → do not end). Tests: open→comment→merge lifecycle ends the Ticket with outcome merged; close with an unread person Input does not end it.
- [ ] 3.5 Handback: add `kanban_handback(trigger_id, reason)` (E-027) in `tools/kanban_tools.py` + schema; Orchestrator records the handback on the Trigger's disposition, and when no other Ticket owns it, queues it for fresh Eval with the reason attached to the batch context. Second handback of the same Trigger by the same Ticket → error string. Tests: handback → exactly one new Eval batch containing the reason; double handback refused; Ticket keeps its subscription after handback (assert row still present).
- [ ] 3.6 Ended-Ticket resume: in fan-out, if the owning Ticket is ended and not person-stopped and the trigger is a person's message: call `revalidate()`; pass → reopen (new `task_runs` row via the dispatcher path, `resume` Input written, disposition `fanned_out`); fail → no resume, message treated as unowned (Eval path), and NO notice to the Router that it reached a worker (v2 rule). Person-stopped Tickets (mandate status `withdrawn`) receive nothing, ever. Tests: the three done-criterion-3 cases + stopped-ticket case.
- [ ] 3.7 Release-on-end: when a Ticket ends, its unread, unacknowledged person Inputs (not handed back, not answered — acknowledged = the run commented/reported after delivery; use `read_at` + a run-activity check mirroring the SCOPE rule) are re-queued for Eval unless another Ticket owns them. Tests: end with one unread Input → one Eval batch; end with all read → none.
- [ ] 3.8 Log verification (evidence, not code): run the pilot fixture end-to-end with model-call logging on; capture the log segment showing a PR comment → Input → wake with no Router model call; paste into REPORT.md. Also paste token counts for the with/without-pass-through comparison on the fixture (disable fan-out via config flag `superagent.passthrough: false` — add this flag in this todo if missing; it is the ONLY new config key allowed in this phase).
- [ ] 3.9 Speculative rule pack: add to `cli-config.yaml.example` a commented example rule ("reproduce-first: on a bug report in an auto Source, reproduce before replying; deliver only if reproduced AND fix is <50 lines; otherwise record silently") + a silence-rule note. No code.
- [ ] 3.10 Run acceptance, write REPORT.md, stop for review.

Dependencies: 3.1→3.2→3.3; 3.4 after 3.2; 3.5 after 3.2; 3.6 after 3.2 (uses Phase-2 revalidate); 3.7 after 3.3; 3.8 last-but-one.

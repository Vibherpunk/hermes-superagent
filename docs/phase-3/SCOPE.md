# Phase 3 — Subscriptions & direct pass-through

**Objective:** Follow-ups on subscribed Threads reach the owning Worker directly (no Router model call, no Eval), with handback, context-only follows, and ended-Ticket resume under Mandate revalidation.

**In scope**
- `sa_subscriptions` table + Orchestrator subscription operations (subscribe owning/context-only, unsubscribe, per-thread drop-all by a person).
- Fan-out in `receive_trigger` (replacing the Phase-2 stub): owning → Input + wake; context-only → Input flagged, no ownership, no resume.
- Worker wake mechanics: reuse the dispatcher's existing steer/comment wake path if one exists (Phase 1 todo 1.6 recorded the finding); Inputs are delivered to the running/woken run as a user-turn message built from the Input rows, marked read when the run's next Report or turn-end lands (mirror `sa_inputs.read_at` semantics: set on delivery + Report/turn-end, per v2 §003 read rules — implement the simple version: `read_at` set when the run acknowledges via heartbeat/comment/complete after delivery; unread Inputs of a Ticket that ends are released to Eval per 3.5).
- PR following: when a Worker reports a PR (its branch's last path segment == ticket short name, or it calls the follow operation), the Ticket subscribes (owning) to that PR's Thread; PR close/merge ends the Ticket per the v2 follows-done rule.
- Handback tool `kanban_handback` (E-027).
- Ended-Ticket resume: person's Input on an ended, unstopped Ticket → `revalidate()` → resume (new Assignment, same workspace) or refuse + route message to Eval.
- Speculative-execution rule pack: prompt/rule text only (a `rules` entry + silence rule in the example config), no new code.

**Out of scope**
- Outbox (Phase 4) — wakes use existing paths. Cross-Source correlation routing (correlation adds context only, never routes — v2 rule).

**Done criteria**
1. Log-verified: a comment on a Worker-owned PR produces an Input and a Worker wake with **zero Router model calls** in the path.
2. A handed-back message is re-evaluated exactly once, with the handback reason in the new Eval's context.
3. Bot/CI message on an ended Ticket's Thread does NOT resume it; a person's message does (after revalidation passes); with revalidation failing (sender removed from allowlist fixture), it does not resume and the message goes to Eval.
4. Context-only subscription delivers Inputs that never wake an ended Ticket and never confer ownership (a follow-up there still takes the Eval path).
5. Pass-through benchmark note in REPORT.md: token accounting from the test runs (state the DeepSeek expectation accurately — cost/Router-load saving, not latency).
6. REPORT.md + sign-off.

**Constraints:** Contract in full. Decisions: E-004, E-005, E-026, E-027.

# Phase 4 TODOS

Acceptance: `python -m pytest tests/superagent/ -q` + cron/kanban/gateway suites green.

DDL:
```sql
CREATE TABLE IF NOT EXISTS sa_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT,
    source TEXT NOT NULL,              -- channel source, e.g. 'telegram' | 'github'
    address_json TEXT NOT NULL DEFAULT '{}',  -- {platform, chat_id, thread_id}
    purpose TEXT NOT NULL,             -- acknowledgement|answer|result|attention|notice
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',    -- queued|sending|sent|failed|dropped
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dropped_reason TEXT,
    receipt_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sa_outbox_due ON sa_outbox(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS sa_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

- [ ] 4.1 Read first: `gateway/delivery_ledger.py` (its own DB path `_db_path()`, schema init, and the flood helpers `flood_wait_seconds`/`retry_not_before` — record signatures), `hermes_cli/kanban_db_notify.py` (how notifier sends today, which platform send function it calls — that function is the drain's sender, E-028), `cron/delivery_queue.py` (what it queues and who drains it), `gateway/delivery.py` if present. Anchors in notes; if the notifier's send path differs from the cron path, record both — the drain uses the NOTIFIER's path (E-028); cron migration (4.5) adapts cron to enqueue Outbox rows instead of sending.
- [ ] 4.2 Schema + Orchestrator outbox API: `queue_reply(ticket_id, source, address, purpose, body) -> outbox_id` (insert only), `due_replies(now)`, `mark_sent(id, receipt)`, `mark_failed(id, error, flood_hint)`, `mark_dropped(id, reason)`. Ordering rule in `due_replies`: a row is due only if no earlier `queued|sending|failed(<3 attempts)` row exists for the same (ticket_id, address chat+thread). Tests: ordering gate, backoff computation uses the flood helpers when the error is a flood error (fixture error string), else 5s→300s doubling.
- [ ] 4.3 Drain: called from the dispatcher tick (and a gateway-side timer if the tick doesn't run when the gateway is up but dispatcher disabled — check and record; if no such case exists, tick only). Sends via the 4.1 sender; maps platform errors: gone/unreachable codes (per platform, conservative list written in code comments with the platform error strings from existing adapter code — copy strings from the adapters, do not invent) → dropped; flood errors → `next_attempt_at` from helpers; other errors → backoff. 100-word cap check before send for `im_cap` sources (telegram/slack/discord/dingtalk etc. = chat platforms as registered; github/mail = false) → `over_limit` failure + internal notice Trigger. Tests with a fake sender: the done-criteria 1–4 scenarios, using a temp DB and a send-log list; the kill-mid-drain test simulates by raising inside the fake sender after recording, then re-running the drain and asserting one send.
- [ ] 4.4 Move Router replies + kanban notifier onto the Outbox: `orchestrator_reply` now calls `queue_reply`; the kanban notifier's completed/blocked notifications enqueue instead of sending directly (keep its event-tail logic; only the final send changes). Where the Orchestrator both changes state and replies (accept acknowledgement, attention forward), both writes occur in one transaction (E-010) — identify those two call sites by name in the todo notes before editing. Tests: notifier fixture produces an Outbox row and, after drain, exactly one platform send; existing notifier tests updated only where they asserted direct sends (list every such test change in REPORT.md — test edits beyond that list are a blocker).
- [ ] 4.5 Cron migration: agent-originated cron deliveries enqueue Outbox rows (source = the delivery target's platform). Pairing/decline/system notices stay on the old path (E-031). Tests: cron fixture job → Outbox row → drain → one send; a pairing notice fixture → old path, zero Outbox rows.
- [ ] 4.6 Health Triggers: in the dispatcher tick, after existing stale/orphan/breaker processing, emit internal Triggers for the four SCOPE events with hourly dedup idempotency keys. Tests: a reclaimed stale run emits exactly one Trigger across two ticks in the same hour; a second distinct subject emits its own.
- [ ] 4.7 Pause: `orchestrator.set_paused(bool)` + tick/drain/eval gates check it; pause state persists in `sa_meta` (keys: `paused` = '0'|'1', `paused_at` = epoch). On resume, shift pending Eval deadlines (in `sa_triggers.meta_json`) by the pause delta. Tests: done-criterion 5 (accumulate-only intake, no dispatch/drain during pause, running run untouched, deadlines shifted on resume).
- [ ] 4.8 Run acceptance, write REPORT.md (incl. the list of updated legacy tests from 4.4), stop for review.

Dependencies: 4.2→4.3→4.4; 4.5 after 4.3; 4.6 independent after 4.2; 4.7 independent.

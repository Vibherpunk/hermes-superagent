# Phase 2 TODOS

Acceptance: `python -m pytest tests/superagent/ -q` plus existing kanban/gateway suites green.

DDL (type exactly; all tables also get `CREATE INDEX` where noted):

```sql
CREATE TABLE IF NOT EXISTS sa_triggers (
    id TEXT PRIMARY KEY,               -- trg_<uuid4hex12>, E-005
    source TEXT NOT NULL,              -- 'github' | 'telegram' | ...
    scope TEXT NOT NULL,               -- receiving scope key, e.g. 'telegram:dm', 'github:issues'
    thread_key TEXT NOT NULL,
    actor_source_id TEXT,              -- sender IN THIS SOURCE's identity space; NULL for sender-less (CI)
    actor_is_bot INTEGER NOT NULL DEFAULT 0,
    addressed INTEGER NOT NULL DEFAULT 0,   -- addressed the agent (mention/DM)
    kind TEXT NOT NULL,                -- 'message'|'comment'|'ci'|'review'|...
    body TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    correlation_json TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT NOT NULL UNIQUE,
    eval_state TEXT NOT NULL DEFAULT 'queued',  -- queued|evaluating|done|fanned_out
    eval_requeues INTEGER NOT NULL DEFAULT 0,
    disposition TEXT,                  -- filtered|evaluated|fanned_out|handed_back (latest)
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sa_triggers_thread ON sa_triggers(thread_key, eval_state);
CREATE TABLE IF NOT EXISTS sa_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key TEXT NOT NULL,
    trigger_ids_json TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL,             -- accept|stop|ignore|unavailable
    strength TEXT,                     -- explicit|inferred (for accept)
    would_accept INTEGER NOT NULL DEFAULT 0,
    ignored_from_card INTEGER NOT NULL DEFAULT 0,
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    reason_json TEXT NOT NULL DEFAULT '{}',
    policy_version TEXT NOT NULL,
    ticket_id TEXT,                    -- set on accept
    dismissed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sa_mandates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL UNIQUE,
    policy_version TEXT NOT NULL,
    scope TEXT NOT NULL,
    strength TEXT NOT NULL,            -- explicit|inferred
    basis_json TEXT NOT NULL DEFAULT '{}',  -- {via: addressed|followup|admission|bot|first_party, source, scope, row, sender, rules}
    status TEXT NOT NULL DEFAULT 'active',  -- active|withdrawn|revoked
    amended_from INTEGER,              -- D-1 amendment chain (personal tickets only); NULL normally
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sa_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    trigger_id TEXT,
    kind TEXT NOT NULL,                -- trigger|policy_change|note|resume
    body TEXT,
    is_person INTEGER NOT NULL DEFAULT 0,
    context_only INTEGER NOT NULL DEFAULT 0,
    read_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sa_source_cursors (
    source TEXT NOT NULL, scope TEXT NOT NULL, cursor TEXT,
    updated_at INTEGER NOT NULL, PRIMARY KEY (source, scope)
);
```

- [ ] 2.1 Read first: `hermes_cli/kanban_db_connect.py` (schema creation + the nullable-columns migration list — record how a column gets added), `gateway/run_inbound.py` `_hm_admit_event` (line ~143: what it returns, where an admitted event goes next — the Trigger write goes immediately after admission succeeds), `gateway/authz_mixin.py` (`_is_user_authorized` and policy helpers — record the exact function the gate will call), `agent/auxiliary_client.py` `call_llm` signature (~line 5727 region). Anchors in notes.
- [ ] 2.2 Schema: add the five CREATE TABLEs to the kanban schema creation path (same place `tasks` et al. are created in `kanban_db.py` ~line 776 region — follow its `IF NOT EXISTS` convention) and add `mandate_id`/`ticket_kind` to the migration columns list in `kanban_db_connect.py` (E-003). Test: `tests/superagent/test_schema.py` — fresh temp DB has all tables/columns; opening a pre-Phase-2 DB (fixture without them) migrates cleanly; running schema init twice is a no-op.
- [ ] 2.3 `hermes_cli/orchestrator.py` part 1: `receive_trigger(...)` — idempotent insert (E-007), returns `(trigger_id, disposition)` where duplicate → `(existing_id, 'duplicate')`. Plus `policy_snapshot()` + `policy_version` per E-013. Tests with temp DB: duplicate event id → one row; version stable across identical configs; version changes on rule edit, mode edit, allowlist edit (three separate cases).
- [ ] 2.4 `hermes_cli/superagent_gate.py`: pure gate per E-012/E-014, taking the authz verdict as an input parameter (`listed: bool` computed by the caller from `authz_mixin`). Tests: `tests/superagent/test_gate.py` — every cell of the v2 admission table, bot-list inferred, unlisted bot refused, CI (actor NULL) → last-cell only, first-party → explicit. Table-driven tests, one row per case, no DB.
- [ ] 2.5 Eval engine in `orchestrator.py` part 2: batch assembly (per-thread, ≤20, E-017), `run_eval(batch)` → gate → (explicit: Decision accept path available to Router; inferred: assessment via mocked `call_llm` in tests) → Decision rows for all outcomes incl. `would_accept` in observe. Deadlines: store `eval_deadline` in the batch's Decision-pending state via `sa_triggers.eval_state` + a `meta_json` deadline field; the dispatcher tick calls `orchestrator.expire_evals(now)` which requeues (≤2) or records `unavailable`. Tests: batch merges triggers arriving mid-Eval into the NEXT batch; cap 20; third expiry → unavailable; unparseable model JSON → unavailable (E-016); observe-mode positive → would_accept + no task (assert `tasks` unchanged).
- [ ] 2.6 Accept path: `orchestrator.accept(decision)` creates the kanban task (via the same creation path as `kanban_create`), the Mandate row, stamps `tasks.mandate_id`/`ticket_kind`, writes the acknowledgement request to Phase-1's reply path. Ticket kind: `coding` if the Source is github, else `personal` (v1 rule — record it; do not generalize). D-1 amendment: `orchestrator.amend_mandate(ticket_id, added_scope, approver)` — refuses unless ticket_kind=personal AND basis sender is the owner identity from config (`superagent.owner_identities` per source, may be empty → amendments refuse); writes a new mandate row with `amended_from` set and marks the old one `superseded` (add that status value in code, not by editing this DDL's comment). Tests for each refusal + the happy path.
- [ ] 2.7 Inbound hook: in `gateway/run_inbound.py`, after `_hm_admit_event` succeeds and only when `superagent.enabled` AND the surface is a pilot surface (config `superagent.pilot_surfaces` list): build the Trigger (thread_key from the platform's session/thread identity — use the same key components the gateway uses for session routing; record them) and call `receive_trigger`. The existing turn flow continues unchanged this phase (Router consumes candidates via tools; dual-running is intentional in observe). Tests: gateway inbound tests green with enabled=false; with enabled=true on a fixture event, exactly one Trigger row appears and the normal reply still happens.
- [ ] 2.8 GitHub Source: `hermes_cli/superagent_github.py` — poll function called from the dispatcher tick when `superagent.github.repository` set: notifications + (per receive_all) new issues/PRs/comments/CI on the default branch; cursor per scope in `sa_source_cursors`; each item → `receive_trigger` with thread keys (`github:issue-<n>`, `github:pr-<n>`, `github:ci-<branch>`), actor = GitHub login, bot detection = login ends `[bot]` or type Bot. All `gh` calls through the repo's existing subprocess helper; tests use recorded JSON fixtures, never the network: two polls → second creates zero triggers; restart (fresh process state, same DB) → zero duplicates.
- [ ] 2.9 Policy change: `orchestrator.policy_changed(old_version, new_snapshot)` — running Evals requeue; affected live Tickets (per v2 §2.5 rules) get a `policy_change` Input; unaffected get nothing. Revalidation: `orchestrator.revalidate(mandate_id) -> bool` implementing the v2 matrix (explicit-followup passes; inferred needs auto+rules; admission-based needs ≥ original strength today; bot needs still-listed). Tests: one case per matrix row, plus "policy change does not cancel a live ticket" (task status unchanged, Input present).
- [ ] 2.10 Replace Phase-1 stubs: `orchestrator_eval`, `orchestrator_eval_read`, `orchestrator_eval_decide`, `orchestrator_trigger_requeue`, `orchestrator_thread_unsubscribe` (table exists Phase 3 — this tool operates on `sa_triggers`/threads only this phase; subscription effects land Phase 3; note in REPORT), `orchestrator_ticket_retry` (ends current run via existing dispatcher retry path, starts new assignment), `orchestrator_ticket_request_status`, `orchestrator_ticket_note` (writes a `note` Input). Tests per tool: happy path + one refusal each.
- [ ] 2.11 Telegram receive check (no code): verify and document in REPORT.md whether the pilot Telegram bot receives unaddressed group messages (privacy mode) and what the DM/group `scope` keys are in practice; if privacy mode blocks them, the admission table's unaddressed cells are inert for Telegram groups — record that as a finding, do not change BotFather settings (human step, noted for the operator).
- [ ] 2.12 Run acceptance, write REPORT.md (incl. a dump of one full Trigger→Decision→Mandate→Ticket chain from the test DB), stop for review. Live config remains `observe`.

Dependencies: 2.2→2.3→2.5→2.6; 2.4 independent of 2.3; 2.7 after 2.3; 2.8 after 2.3; 2.9 after 2.6; 2.10 after 2.5+2.6.

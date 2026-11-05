# Superagent P2.1 — Architectural & Implementation Anchors

## Overview
This document records the authoritative code anchors, resolution mechanics, and architectural patterns in `hermes-agent` for Superagent Phase 2 (Data Layer). It provides the exact file:line references, type signatures, and step-by-step procedures across the kanban database migrations, gateway ingress admission, authorization gating, and auxiliary model invocation pipelines.

---

## 1. Kanban DB Schema Creation & Additive Migrations

### File & Line Anchors
- **Connection entry point**: [`hermes_cli/kanban_db_connect.py:742-785`](hermes_cli/kanban_db_connect.py#L742-L785) (`connect`) — Establishes configured SQLite connection, acquiring the cross-process initialization lock on fresh boards.
- **Cross-process initialization lock**: [`hermes_cli/kanban_db_connect.py:116-158`](hermes_cli/kanban_db_connect.py#L116-L158) (`_cross_process_init_lock`) — Bounded file-lock context manager serializing schema init and migrations across worker processes.
- **In-process schema init callback**: [`hermes_cli/kanban_db_connect.py:775-782`](hermes_cli/kanban_db_connect.py#L775-L782) (`_init_if_needed`) — Idempotently executes `SCHEMA_SQL` and migrations under `_INIT_LOCK` if path is not yet in `_INITIALIZED_PATHS`.
- **Forced re-init helper**: [`hermes_cli/kanban_db_connect.py:804-817`](hermes_cli/kanban_db_connect.py#L804-L817) (`init_db`) — Clears `_INITIALIZED_PATHS` cache entry and connects to ensure all migrations and rebuilds run.
- **Canonical schema definition**: [`hermes_cli/kanban_db.py:775-885`](hermes_cli/kanban_db.py#L775-L885) (`SCHEMA_SQL`) — Defines the foundational `CREATE TABLE` and `CREATE INDEX` statements for `tasks`, `task_events`, `task_comments`, `task_runs`, and `kanban_notify_subs`.
- **Idempotent column adder**: [`hermes_cli/sqlite_util.py:82-95`](hermes_cli/sqlite_util.py#L82-L95) (`add_column_if_missing`) — Issues `ALTER TABLE <table> ADD COLUMN <ddl>` while safely absorbing `"duplicate column name"` operational errors from concurrent races.
- **Additive migration orchestrator**: [`hermes_cli/kanban_db_connect.py:921-998`](hermes_cli/kanban_db_connect.py#L921-L998) (`_migrate_add_optional_columns`) — Applies missing columns, backfills, indexes, and invokes drifted table rebuilds.
- **Table introspection helpers**: [`hermes_cli/kanban_db_connect.py:911-918`](hermes_cli/kanban_db_connect.py#L911-L918) (`_column_names`, `_table_exists`) — Reads `PRAGMA table_info` and queries `sqlite_master` to determine schema state.
- **Drifted table rebuild definitions**: [`hermes_cli/kanban_db_connect.py:1062-1106`](hermes_cli/kanban_db_connect.py#L1062-L1106) (`_REBUILD_SPECS`) — Canonical DDL and index mappings for reconstructing tables when primary keys or types drift from AUTOINCREMENT.
- **Drift detector and rebuild executor**: [`hermes_cli/kanban_db_connect.py:1109-1240`](hermes_cli/kanban_db_connect.py#L1109-L1240) (`_table_has_drifted`, `_rebuild_drifted_tables`) — Detects non-autoincrement legacy IDs and migrates tables into canonical shape in an immediate transaction.

### Nullable-Columns Migration Lists
All additive columns in `hermes_cli/kanban_db_connect.py` are grouped into explicit declarative tuples:
1. **`_BASE_TASK_COLUMNS` ([lines 827-838](hermes_cli/kanban_db_connect.py#L827-L838))**:
   - `("body", "body TEXT")`
   - `("assignee", "assignee TEXT")`
   - `("priority", "priority INTEGER DEFAULT 0")`
   - `("created_by", "created_by TEXT")`
   - `("started_at", "started_at INTEGER")`
   - `("completed_at", "completed_at INTEGER")`
   - `("workspace_kind", "workspace_kind TEXT NOT NULL DEFAULT 'scratch'")`
   - `("workspace_path", "workspace_path TEXT")`
   - `("claim_lock", "claim_lock TEXT")`
   - `("claim_expires", "claim_expires INTEGER")`
2. **`_EARLY_TASK_COLUMNS` ([lines 842-848](hermes_cli/kanban_db_connect.py#L842-L848))**:
   - `("tenant", "tenant TEXT")`
   - `("result", "result TEXT")`
   - `("branch_name", "branch_name TEXT")`
   - `("project_id", "project_id TEXT")`
   - `("idempotency_key", "idempotency_key TEXT")`
3. **`_RENAMED_TASK_COLUMNS` ([lines 852-862](hermes_cli/kanban_db_connect.py#L852-L862))**:
   - `("consecutive_failures", "consecutive_failures INTEGER NOT NULL DEFAULT 0", "spawn_failures", "UPDATE tasks SET consecutive_failures = COALESCE(spawn_failures, 0)")`
   - `("worker_pid", "worker_pid INTEGER", None, None)`
   - `("last_failure_error", "last_failure_error TEXT", "last_spawn_error", "UPDATE tasks SET last_failure_error = last_spawn_error")`
4. **`_LATER_TASK_COLUMNS` ([lines 866-890](hermes_cli/kanban_db_connect.py#L866-L890))**:
   - `("max_runtime_seconds", "max_runtime_seconds INTEGER")`
   - `("last_heartbeat_at", "last_heartbeat_at INTEGER")`
   - `("current_run_id", "current_run_id INTEGER")`
   - `("workflow_template_id", "workflow_template_id TEXT")`
   - `("current_step_key", "current_step_key TEXT")`
   - `("skills", "skills TEXT")`
   - `("max_retries", "max_retries INTEGER")`
   - `("model_override", "model_override TEXT")`
   - `("provider_override", "provider_override TEXT")`
   - `("reasoning_effort", "reasoning_effort TEXT")`
   - `("goal_mode", "goal_mode INTEGER NOT NULL DEFAULT 0")`
   - `("goal_max_turns", "goal_max_turns INTEGER")`
   - `("completion_contract", "completion_contract TEXT")`
   - `("session_id", "session_id TEXT")`
   - `("block_kind", "block_kind TEXT")`
   - `("block_recurrences", "block_recurrences INTEGER NOT NULL DEFAULT 0")`
   - `("worker_started_at", "worker_started_at INTEGER")`
5. **`_NOTIFY_SUB_COLUMNS` ([lines 892-902](hermes_cli/kanban_db_connect.py#L892-L902))**:
   - `("last_ping_event_id", "last_ping_event_id INTEGER NOT NULL DEFAULT 0")`
   - `("notifier_profile", "notifier_profile TEXT")`
   - `("delivery_mode", "delivery_mode TEXT NOT NULL DEFAULT 'notify'")`
   - `("chat_type", "chat_type TEXT")`
   - `("user_id_alt", "user_id_alt TEXT")`
   - `("delivery_metadata", "delivery_metadata TEXT")`
6. **`_TASK_RUN_COLUMNS` ([lines 904-908](hermes_cli/kanban_db_connect.py#L904-L908))**:
   - `("worker_started_at", "worker_started_at INTEGER")`

### Exactly How a Column Gets Added to Kanban DB
Adding a column safely across fresh and legacy SQLite databases requires five coordinated steps:
1. **Define in Canonical Schema**:
   Add the column definition to `SCHEMA_SQL` in [`hermes_cli/kanban_db.py`](hermes_cli/kanban_db.py#L775) under the appropriate `CREATE TABLE` statement so all new databases possess the column upon first creation.
2. **Declare in Migration Tuple**:
   Append a `(column_name, column_ddl)` pair into the appropriate column tuple in [`hermes_cli/kanban_db_connect.py`](hermes_cli/kanban_db_connect.py#L866) (e.g., `_LATER_TASK_COLUMNS` for the `tasks` table, or a new migration list).
3. **Execute Additive Migration**:
   In `_migrate_add_optional_columns(conn)` ([lines 921-998](hermes_cli/kanban_db_connect.py#L921-L998)):
   - Check existing table columns via `cols = _column_names(conn, table)`.
   - If `name not in cols`, call `_add_column_if_missing(conn, table, name, ddl)`.
   - `_add_column_if_missing` runs `ALTER TABLE <table> ADD COLUMN <ddl>` inside a `try/except sqlite3.OperationalError` block, returning `True` if added and safely catching concurrent race conditions (`"duplicate column name"`).
4. **Order Index Creation After Column Addition**:
   Any `CREATE INDEX IF NOT EXISTS` referencing the new column must be executed in `_migrate_add_optional_columns` **strictly after** the column addition loop ([lines 946-955](hermes_cli/kanban_db_connect.py#L946-L955)), because SQLite's `executescript` parses statements against the live schema and would crash on legacy databases if an index statement precedes the `ALTER TABLE`.
5. **Synchronize Drift Rebuild Specifications**:
   If the table is subject to table reconstruction (e.g., `task_events`, `task_comments`, `task_runs`, `kanban_notify_subs` in `_REBUILD_SPECS`, [lines 1062-1106](hermes_cli/kanban_db_connect.py#L1062-L1106)), the `CREATE TABLE` and index definitions inside `_REBUILD_SPECS[table]` must be updated to match, guaranteeing that legacy tables rebuilt by `_rebuild_drifted_tables` remain byte-identical to fresh schemas.

---

## 2. Gateway Event Ingress & Admission Pipeline

### File & Line Anchors
- **Event admission gate**: [`gateway/run_inbound.py:143-244`](gateway/run_inbound.py#L143-L244) (`_hm_admit_event`) — Primary ingress gate executing route validation, channel guards, startup queues, pre-dispatch hooks, and authorization.
- **Message handler coordinator**: [`gateway/run_inbound.py:1242-1360`](gateway/run_inbound.py#L1242-L1360) (`_handle_message`) — Coordinates admission, E-stop, session authority, locks, and agent execution.
- **Admission call site**: [`gateway/run_inbound.py:1246`](gateway/run_inbound.py#L1246) — The single point in `_handle_message` where `await self._hm_admit_event(event)` is evaluated.
- **Emergency stop gate**: [`gateway/run_inbound.py:246-260`](gateway/run_inbound.py#L246-L260) & [`gateway/run.py:3820-3845`](gateway/run.py#L3820-L3845) (`_hm_estop_gate`) — Blocks new agent turns when emergency stop is active unless the message is an exempt control/approval action.
- **Session authority admission**: [`gateway/run_inbound.py:1271`](gateway/run_inbound.py#L1271) (`_hm_authority_admission`) — Hands admitted events to durable FIFO session authority when active.
- **Active session slot claim**: [`gateway/run_inbound.py:1304`](gateway/run_inbound.py#L1304) (`_claim_active_session_slot`) — Acquires concurrency lease before agent invocation to prevent racing turns.

### `_hm_admit_event` Signature & Return Behavior
- **Signature**:
  ```python
  async def _hm_admit_event(
      self, event: MessageEvent
  ) -> Optional[tuple[MessageEvent, SessionSource, bool]]:
  ```
- **What it returns**:
  - `None` when the event is dropped, refused, deferred, or filtered out. Scenarios returning `None`:
    - Parked or rejected profile route (`source.profile_route_rejected is True`).
    - Configured ignored channel (e.g., Slack ignored channel).
    - Event enqueued for startup restore replay (`_startup_restore_in_progress`).
    - Pre-gateway dispatch hook returns `None` (`_hm_pre_gateway_dispatch_hook`).
    - User is unauthorized (`_is_user_authorized_for_source` returns `False`; handles pairing code, decline message, or ignored DM report).
    - Bot rate limit exceeded in cooling-off period (`_admit_bot_message_for_source` returns `False`).
  - `(event, source, is_internal)` 3-tuple when admitted:
    - `event: MessageEvent` — The validated message event, potentially rewritten by `_hm_pre_gateway_dispatch_hook`.
    - `source: SessionSource` — The resolved and canonicalized session source.
    - `is_internal: bool` — Boolean flag indicating whether the event represents an internal/system event (`bool(getattr(event, "internal", False))`, such as background task notifications or cron triggers).

### Where an Admitted Event Goes Next
Once `_hm_admit_event` returns `(event, source, is_internal)` at [`gateway/run_inbound.py:1246-1249`](gateway/run_inbound.py#L1246-L1249), the event traverses the following pipeline in `_handle_message`:
1. **Metrics Recording** ([line 1251](gateway/run_inbound.py#L1251)): Records gateway slash command metrics if `not is_internal`.
2. **Emergency Stop (E-Stop) Check** ([line 1262](gateway/run_inbound.py#L1262)): Evaluates `self._hm_estop_gate(event, source, is_internal)`. Returns paused notice if gateway is stopped and turn is not exempt.
3. **Session Key Derivation** ([line 1266](gateway/run_inbound.py#L1266)): Derives `_quick_key = self._session_key_for_source(source)`.
4. **Pending Reply Intercepts** ([line 1267](gateway/run_inbound.py#L1267)): If `not is_internal`, checks for pending interactive prompts (e.g., update prompts, confirmations) via `_hm_pending_reply_intercepts`.
5. **Session Authority Admission** ([line 1271](gateway/run_inbound.py#L1271)): Hands the event to `_hm_authority_admission(event, source, _quick_key, is_internal)` for FIFO ordering if session authority is active.
6. **Running Session Handling / Interrupt** ([lines 1276-1280](gateway/run_inbound.py#L1276-L1280)): Evicts stale/reaped agents and routes to `_hm_handle_running_session_message` if the session already has an agent executing.
7. **Idle Slash Command Dispatch** ([lines 1282-1284](gateway/run_inbound.py#L1282-L1284)): Evaluates fast-path slash commands via `_hm_dispatch_idle_commands`.
8. **Topic Lobby & External Drain Checks** ([lines 1288-1299](gateway/run_inbound.py#L1288-L1299)): Enforces Telegram topic lobby debouncing and rejects new turns if `self._external_drain_active` is True.
9. **Active Session Slot Claim** ([line 1304](gateway/run_inbound.py#L1304)): Calls `self._claim_active_session_slot(_quick_key, source)` to acquire the concurrency slot lease.
10. **Turn Initialization & Agent Execution** ([lines 1311-1350+](gateway/run_inbound.py#L1311-L1350)): Sets `_claim_state.turn.agent = _AGENT_PENDING_SENTINEL`, starts the reply clock, persists active agents, and invokes `_run_agent(...)`.

---

## 3. Authorization & Access Policy Verification

### File & Line Anchors
- **Ingress gate caller**: [`gateway/run_inbound.py:220`](gateway/run_inbound.py#L220) — Invokes `self._is_user_authorized_for_source(source)` during admission.
- **Profile-scoped authorization wrapper**: [`gateway/run.py:3781-3792`](gateway/run.py#L3781-L3792) (`_is_user_authorized_for_source`) — Executes authorization under the live transport adapter's profile context (`_under_authorization_profile`).
- **Core authorization verifier**: [`gateway/authz_mixin.py:637-650`](gateway/authz_mixin.py#L637-L650) (`_is_user_authorized`) — Checks principal allowlists and evaluates the bot loop guard.
- **Principal allowlist evaluator**: [`gateway/authz_mixin.py:652-720`](gateway/authz_mixin.py#L652-L720) (`_principal_authorized`) — Evaluates local sources, trusted inbound, chat grants, pairing store, platform allowlists, and global allowlists.
- **Allowlist matcher**: [`gateway/authz_mixin.py:164-197`](gateway/authz_mixin.py#L164-L197) (`_principal_matches_allowlist`) — Matches sender user ID, username, or platform aliases against authorized ID sets.
- **Adapter policy resolver**: [`gateway/authz_mixin.py:430-435`](gateway/authz_mixin.py#L430-L435) (`_adapter_policy`) — Reads adapter DM/group policy configurations.
- **Adapter own-policy evaluator**: [`gateway/authz_mixin.py:472-492`](gateway/authz_mixin.py#L472-L492) (`_own_policy_authorizes`) — Queries adapters that enforce custom access policies.
- **Adapter extra allowlist evaluator**: [`gateway/authz_mixin.py:494-523`](gateway/authz_mixin.py#L494-L523) (`_adapter_extra_allowlist_authorizes`) — Checks inline allowlists declared in adapter extra configurations.
- **Chat-scoped grant evaluator**: [`gateway/authz_mixin.py:545-589`](gateway/authz_mixin.py#L545-L589) (`_chat_scoped_grant`) — Checks chat-specific user permissions and group memberships.
- **Unauthorized DM policy handler**: [`gateway/authz_mixin.py:722-763`](gateway/authz_mixin.py#L722-L763) (`_get_unauthorized_dm_behavior`) — Determines whether unauthorized DMs receive a pairing code (`"pair"`), decline text (`"decline"`), or silent drop (`"ignore"`).
- **Bot loop guard check**: [`gateway/authz_mixin.py:623-635`](gateway/authz_mixin.py#L623-L635) (`_admit_bot_message`) — Verifies bot message limits and tracks sliding-window counts.

### Exact Function Called by Ingress Gate & Signature
The ingress gate at [`gateway/run_inbound.py:220`](gateway/run_inbound.py#L220) calls:
```python
self._is_user_authorized_for_source(source)
```
- **Definition in `gateway/run.py:3781-3792`**:
  ```python
  def _is_user_authorized_for_source(
      self, source: SessionSource, *, allow_adapter_delegation: bool = True
  ) -> bool:
      """Authorize under the live transport's profile, not the routed runtime (which need not copy the
      shared bot token/allowlist); the transport home is stamped on the source for this read only."""
  ```
- **Underlying verifier in `gateway/authz_mixin.py:637-650`**:
  ```python
  def _is_user_authorized(
      self, source: SessionSource, *, allow_adapter_delegation: bool = True
  ) -> bool:
      """Whether a user may use the bot.

      Order: trusted-upstream delegation, chat-scoped group allowlists, ``{PLATFORM}_ALLOW_BOTS``,
      per-platform allow-all, adapter role auth, pairing store, env/config allowlists,
      ``GATEWAY_ALLOW_ALL_USERS``, default deny. A bot-authored message that any of these admits
      is still refused while its chat's loop guard is cooling down.
      """
  ```

---

## 4. Centralized Auxiliary LLM API

### File & Line Anchors
- **API section header & client cache**: [`agent/auxiliary_client.py:5727-5738`](agent/auxiliary_client.py#L5727-L5738) — Centralized entry point declaration, rationale, and bounded client cache (`_client_cache`).
- **Synchronous LLM caller**: [`agent/auxiliary_client.py:7998-8047`](agent/auxiliary_client.py#L7998-L8047) (`call_llm`) — Synchronous entry point managing task semaphores, scoped runtime context, progress tracking, and response streaming.
- **Asynchronous LLM caller**: [`agent/auxiliary_client.py:8295-8318`](agent/auxiliary_client.py#L8295-L8318) (`async_call_llm`) — Asynchronous non-blocking entry point for gateway watcher and background evaluation requests.
- **Async implementation dispatcher**: [`agent/auxiliary_client.py:8320-8350`](agent/auxiliary_client.py#L8320-L8350) (`_async_call_llm_impl`) — Resolves candidates, routes provider requests, and invokes async client handlers.

### Exact Signatures

#### Synchronous Entry Point (`call_llm`)
Defined at [`agent/auxiliary_client.py:7998-8007`](agent/auxiliary_client.py#L7998-L8007):
```python
@_relay_auxiliary_call
def call_llm(
    task: str | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    main_runtime: Optional[dict[str, Any]] = None,
    messages: list,
    temperature: Optional[float] = None,
    max_tokens: int | None = None,
    tools: list | None = None,
    timeout: float | None = None,
    extra_body: dict | None = None,
    reasoning_config: Optional[dict] = None,
    extra_headers: Optional[dict[str, str]] = None,
    api_mode: str | None = None,
    stream: bool = False,
    stream_options: dict | None = None,
    route_info: Optional[dict[str, str]] = None,
    latency_info: Optional[dict[str, int]] = None,
) -> Any:
```
- **Role**: Synchronously executes an auxiliary LLM request under the configured concurrency limit for `task`, binding `scoped_runtime_main(main_runtime)` and returning the parsed response or streaming generator.

#### Asynchronous Entry Point (`async_call_llm`)
Defined at [`agent/auxiliary_client.py:8295-8302`](agent/auxiliary_client.py#L8295-L8302):
```python
@_relay_auxiliary_call_async
async def async_call_llm(
    task: str | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    main_runtime: Optional[dict[str, Any]] = None,
    messages: list,
    temperature: Optional[float] = None,
    max_tokens: int | None = None,
    tools: list | None = None,
    timeout: float | None = None,
    extra_body: dict | None = None,
    reasoning_config: Optional[dict] = None,
    route_info: Optional[dict[str, str]] = None,
) -> Any:
```
- **Role**: Asynchronously executes an auxiliary LLM request without blocking the gateway event loop, acquiring the async auxiliary semaphore and binding `scoped_runtime_main(main_runtime)`.

---

## 5. Summary Table of Code Anchors

| Subsystem | File & Lines | Symbol / Function | Role |
| :--- | :--- | :--- | :--- |
| **Kanban DB** | `hermes_cli/kanban_db_connect.py:742-785` | `connect` | Opens SQLite DB, acquiring cross-process initialization lock on fresh boards |
| **Kanban DB** | `hermes_cli/kanban_db_connect.py:116-158` | `_cross_process_init_lock` | Serializes schema creation and additive migrations across worker processes |
| **Kanban DB** | `hermes_cli/kanban_db_connect.py:775-782` | `_init_if_needed` | Runs `SCHEMA_SQL` and `_migrate_add_optional_columns` on uninitialized connections |
| **Kanban DB** | `hermes_cli/kanban_db_connect.py:827-908` | `_BASE_TASK_COLUMNS`, `_LATER_TASK_COLUMNS`, etc. | Declarative column and DDL specifications for additive table upgrades |
| **Kanban DB** | `hermes_cli/kanban_db_connect.py:921-998` | `_migrate_add_optional_columns` | Inspects live tables via PRAGMA and adds missing columns/indexes |
| **Kanban DB** | `hermes_cli/kanban_db_connect.py:1062-1106` | `_REBUILD_SPECS` | Canonical CREATE TABLE and CREATE INDEX DDL for reconstructing drifted legacy tables |
| **Kanban DB** | `hermes_cli/kanban_db.py:775-885` | `SCHEMA_SQL` | Foundational DDL executed on fresh database initialization |
| **Kanban DB** | `hermes_cli/sqlite_util.py:82-95` | `add_column_if_missing` | Executes `ALTER TABLE ADD COLUMN`, absorbing race duplicate column errors |
| **Gateway Inbound** | `gateway/run_inbound.py:143-244` | `_hm_admit_event` | Ingress admission gate; returns `None` or `(event, source, is_internal)` |
| **Gateway Inbound** | `gateway/run_inbound.py:1242-1360` | `_handle_message` | Orchestrates post-admission pipeline from E-stop and session claims to agent run |
| **Gateway Authz** | `gateway/run_inbound.py:220` | `self._is_user_authorized_for_source` | Gate call site checking user authorization during inbound admission |
| **Gateway Authz** | `gateway/run.py:3781-3792` | `_is_user_authorized_for_source` | Authorizes under live transport profile before invoking `_is_user_authorized` |
| **Gateway Authz** | `gateway/authz_mixin.py:637-650` | `_is_user_authorized` | Evaluates principal authorization and bot loop guard rate limiting |
| **Gateway Authz** | `gateway/authz_mixin.py:652-720` | `_principal_authorized` | Resolves allowlists, pairing store, chat grants, and default access policies |
| **Gateway Authz** | `gateway/authz_mixin.py:722-763` | `_get_unauthorized_dm_behavior` | Determines unauthorized DM action (`"pair"`, `"decline"`, `"ignore"`) |
| **Auxiliary LLM** | `agent/auxiliary_client.py:5727-5738` | Header & `_client_cache` | Centralized LLM Call API architecture anchor and bounded client cache |
| **Auxiliary LLM** | `agent/auxiliary_client.py:7998-8007` | `call_llm` | Synchronous auxiliary LLM entry point with task concurrency control |
| **Auxiliary LLM** | `agent/auxiliary_client.py:8295-8302` | `async_call_llm` | Asynchronous auxiliary LLM entry point for non-blocking gateway operations |

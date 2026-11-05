# Superagent P1.1 — Architectural & Implementation Anchors

## Overview
This document establishes the authoritative code anchors, resolution mechanics, and architectural patterns in `hermes-agent` for Superagent Phase 1. Subsequent Phase 1 cards (including card 1.2's `orchestrator_reply` tool, orchestrator toolset resolution, and worker dispatch isolation) directly build upon and reuse these four areas without architectural drift.

---

## 1. Per-Platform Toolset Resolution Anchor

### File & Line References
- **Primary resolver**: [`hermes_cli/tools_config.py:571-646`](hermes_cli/tools_config.py#L571-L646) (`_get_platform_tools`)
- **Explicit configuration detector**: [`hermes_cli/tools_config.py:558-569`](hermes_cli/tools_config.py#L558-L569) (`_platform_toolsets_explicitly_saved`)
- **Value coercion & validation**: [`hermes_cli/tools_config.py:536-555`](hermes_cli/tools_config.py#L536-L555) (`_coerce_platform_toolsets_value`)
- **Platform default mapping**: [`hermes_cli/tools_config.py:192-195`](hermes_cli/tools_config.py#L192-L195) (`_platform_default_toolset`) & [`hermes_cli/platforms.py:14-36`](hermes_cli/platforms.py#L14-L36) (`PLATFORMS`)
- **Composite vs. Explicit resolution**:
  - [`hermes_cli/tools_config.py:488-504`](hermes_cli/tools_config.py#L488-L504) (`_explicit_toolsets`)
  - [`hermes_cli/tools_config.py:506-518`](hermes_cli/tools_config.py#L506-L518) (`_composite_toolsets`)
  - [`hermes_cli/tools_config.py:467-478`](hermes_cli/tools_config.py#L467-L478) (`_default_off_toolsets`)
- **Post-resolution recovery & pruning**:
  - [`hermes_cli/tools_config.py:709-731`](hermes_cli/tools_config.py#L709-L731) (`_recover_platform_native_toolsets`)
  - [`hermes_cli/tools_config.py:520-528`](hermes_cli/tools_config.py#L520-L528) (`_enabled_plugin_toolsets`)
  - [`hermes_cli/tools_config.py:733-750`](hermes_cli/tools_config.py#L733-L750) (`_merge_mcp_servers`)
  - [`hermes_cli/tools_config.py:649-666`](hermes_cli/tools_config.py#L649-L666) (`_prune_toolsets_stripped_by_disabled`)
  - [`hermes_cli/tools_config.py:680-706`](hermes_cli/tools_config.py#L680-L706) (`toolsets_partially_stripped`)
- **Default off & configurable declarations**:
  - [`hermes_cli/tools_config.py:67-94`](hermes_cli/tools_config.py#L67-L94) (`CONFIGURABLE_TOOLSETS`, line 82: `kanban`)
  - [`hermes_cli/tools_config.py:110`](hermes_cli/tools_config.py#L110) (`_DEFAULT_OFF_TOOLSETS` includes `"kanban"`)
  - [`toolsets.py:154-163`](toolsets.py#L154-L163) (`kanban` toolset definition)

### Resolution Pipeline
The resolution pipeline in `_get_platform_tools(config, platform, *, include_default_mcp_servers=True)` executes in sequential stages:
1. **Raw Retrieval & Coercion**:
   Extracts `platform_toolsets = config.get("platform_toolsets") or {}` and calls `_coerce_platform_toolsets_value(platform_toolsets.get(platform), platform)` to support list-literal strings from JSON editors or CLI setters.
2. **Explicit vs. Default Evaluation**:
   - `explicitly_configured = isinstance(toolset_names, list)`.
   - If not explicitly configured, falls back to `toolset_names = [_platform_default_toolset(platform)]` (e.g., `"hermes-cli"` for `"cli"`, defined in `PLATFORMS`).
3. **Legacy Alias Expansion**:
   Expands legacy bundle names (e.g., `"hermes"` -> `("hermes-cli", "hermes-api-server")`).
4. **Partition & Expansion**:
   - If any `ts in explicit_known_keys` (configurable keys + plugin keys), invokes `_explicit_toolsets(...)`, preserving user selections while expanding any mixed composite entries.
   - Otherwise, invokes `_composite_toolsets(...)`, resolving the full static tool universe and subtracting `_default_off_toolsets(...)`.
5. **Platform-Native & Plugin Recovery**:
   - `_recover_platform_native_toolsets` restores non-configurable platform-bound toolsets unless explicitly excluded.
   - `_enabled_plugin_toolsets` folds in active plugin toolsets.
   - `_context_engine_active` adds `"context_engine"` if non-default engine is active.
   - `_merge_mcp_servers` resolves configured MCP servers (unless `"no_mcp"` sentinel is present).
6. **Legacy Kanban Opt-In Fallback**:
   Lines 633-635:
   ```python
   if not explicitly_configured and "kanban" in (config.get("toolsets") or []):
       enabled_toolsets.add("kanban")
   ```
7. **Final Suppression (`agent.disabled_toolsets`)**:
   `agent.disabled_toolsets` is applied strictly last via `_prune_toolsets_stripped_by_disabled(...)`, ensuring global suppressions cannot be overridden by platform defaults.

---

## 2. Dispatcher Worker Toolset Construction & Child Isolation Anchor

### File & Line References
- **Dispatcher CLI Toolset Resolver**: [`hermes_cli/kanban_db_dispatch.py:2643-2669`](hermes_cli/kanban_db_dispatch.py#L2643-L2669) (`_resolve_worker_cli_toolsets`)
- **Worker Profile Scope**: [`hermes_cli/kanban_db_dispatch.py:2598-2640`](hermes_cli/kanban_db_dispatch.py#L2598-L2640) (`_worker_profile_scope`)
- **Worker Subprocess Spawn Path**: [`hermes_cli/kanban_db_dispatch.py:2755-2895`](hermes_cli/kanban_db_dispatch.py#L2755-L2895) (`_default_spawn`)
- **Worker Subprocess Client Entry**: [`hermes_cli/kanban_worker_client.py:62-85`](hermes_cli/kanban_worker_client.py#L62-L85) (`_submit_claim` calling gateway RPC `kanban.run`)
- **Gateway Kanban Session Policy**: [`gateway/session_kanban.py:40-70`](gateway/session_kanban.py#L40-L70) (`_session_policy_for_task` / `run_task`)
- **Delegation Child Block & Toolset Resolver**:
  - [`tools/delegate_tool_toolsets.py:14-22`](tools/delegate_tool_toolsets.py#L14-L22) (`DELEGATE_BLOCKED_TOOLS`)
  - [`tools/delegate_tool_toolsets.py:56-65`](tools/delegate_tool_toolsets.py#L56-L65) (`_strip_blocked_tools`)
  - [`tools/delegate_tool_toolsets.py:67-75`](tools/delegate_tool_toolsets.py#L67-L75) (`_blocked_toolsets_for_role`)
  - [`tools/delegate_tool_toolsets.py:77-123`](tools/delegate_tool_toolsets.py#L77-L123) (`_resolve_child_toolsets`)

### How the Dispatcher Builds a Worker's Toolsets
1. **Resolution at Dispatch Time**:
   In `hermes_cli/kanban_db_dispatch.py:2643-2661`, `_resolve_worker_cli_toolsets` binds the assigned profile scope via `_worker_profile_scope(hermes_home)` and calls:
   ```python
   toolsets = sorted(_get_platform_tools(cfg, "cli"))
   ```
2. **Subprocess Environment & Argv**:
   In `_default_spawn` (`hermes_cli/kanban_db_dispatch.py:2755-2880`), the dispatcher spawns:
   `[sys.executable, "-m", "hermes_cli.kanban_worker_client"]`
   with environment variables including:
   - `HERMES_KANBAN_TASK = task.id`
   - `HERMES_KANBAN_WORKSPACE = workspace`
   - `HERMES_KANBAN_RUN_ID = str(task.current_run_id)`
   - `HERMES_SESSION_SOURCE = "kanban"`
   - `HERMES_PROFILE = profile_arg`
   - Clears `DELEGATED_CHILD_ENV_MARKER` and `HERMES_TUI`
3. **Execution Admission via Gateway**:
   `kanban_worker_client.py` submits the claim to the gateway via `client.rpc('kanban.run', ...)`. In `gateway/session_kanban.py:69`, the gateway constructs the session policy and explicitly attaches the `kanban` toolset:
   ```python
   toolsets=tuple(sorted(set(policy.toolsets) | {'kanban'}))
   ```
   with `platform='cli'` and `source='kanban'`.

### Child Subagent Isolation (`delegate_task`)
When a worker delegates to a child agent:
1. `_strip_blocked_tools(toolsets)` (`tools/delegate_tool_toolsets.py:56-65`) removes:
   ```python
   blocked_toolset_names = {"delegation", "kanban"} | {
       name for name in TOOLSETS
       if (resolved_tools := resolve_toolset(name, include_registry=False))
       and all(tool in DELEGATE_BLOCKED_TOOLS for tool in resolved_tools)
   }
   ```
2. In `_resolve_child_toolsets(...)` (`tools/delegate_tool_toolsets.py:120-122`), `"kanban"` is explicitly and unconditionally added to `child_disabled_toolsets`:
   ```python
   child_disabled_toolsets = list(
       dict.fromkeys(inherited_disabled + _blocked_toolsets_for_role(effective_role) + ["kanban"])
   )
   ```
3. Consequently, children never inherit `kanban` tools, avoiding split-brain board mutations.

---

## 3. Tool Registration & Gating Pattern (`tools/kanban_tools.py`)

### File & Line References
- **Tool discovery scanner**: [`tools/registry.py:106-136`](tools/registry.py#L106-L136) (`discover_builtin_tools`, AST detection)
- **Uncached check_fn marker**: [`tools/registry.py:272-275`](tools/registry.py#L272-L275) (`no_cache_check_fn`)
- **Gating predicates**:
  - [`tools/kanban_tools.py:37-62`](tools/kanban_tools.py#L37-L62) (`_profile_has_kanban_toolset`)
  - [`tools/kanban_tools.py:64-70`](tools/kanban_tools.py#L64-L70) (`_delegation_ctx`)
  - [`tools/kanban_tools.py:73-74`](tools/kanban_tools.py#L73-L74) (`_is_delegated_child_context`)
  - [`tools/kanban_tools.py:77-80`](tools/kanban_tools.py#L77-L80) (`_is_dispatcher_owned_worker`)
  - [`tools/kanban_tools.py:83-91`](tools/kanban_tools.py#L83-L91) (`_visible`)
  - [`tools/kanban_tools.py:93-96`](tools/kanban_tools.py#L93-L96) (`_check_kanban_mode`)
  - [`tools/kanban_tools.py:99-102`](tools/kanban_tools.py#L99-L102) (`_check_kanban_orchestrator_mode`)
- **Handler decoration & execution wrapper**: [`tools/kanban_tools.py:153-176`](tools/kanban_tools.py#L153-L176) (`_kanban_handler`)
- **In-handler worker/child checks**:
  - [`tools/kanban_tools.py:179-187`](tools/kanban_tools.py#L179-L187) (`_reject_delegated_child_mutation`)
  - [`tools/kanban_tools.py:301-309`](tools/kanban_tools.py#L301-L309) (`_require_orchestrator_tool`)
- **Tool schemas**: [`tools/kanban_tools_schemas.py:1-400`](tools/kanban_tools_schemas.py#L1-L400)
- **Registration loop**: [`tools/kanban_tools.py:1291-1315`](tools/kanban_tools.py#L1291-L1315)

### Registration & Gating Mechanics
1. **Registration Loop**:
   Tools are declared as a tuple `_TOOLS = ((name, schema, handler, emoji), ...)`.
   `_ORCHESTRATOR_TOOLS = frozenset({"kanban_list", "kanban_unblock"})`.
   Registration executes at module import:
   ```python
   for _name, _sch, _handler, _emoji in _TOOLS:
       _gate = _check_kanban_orchestrator_mode if _name in _ORCHESTRATOR_TOOLS else _check_kanban_mode
       registry.register(name=_name, toolset="kanban", schema=_sch, handler=_handler, emoji=_emoji,
                         check_fn=_gate)
   ```
2. **Gating Semantics (`check_fn`)**:
   - `_check_kanban_mode()` returns `_visible(to_env_worker=True)`: allows lifecycle tools for workers spawned by the dispatcher (`HERMES_KANBAN_TASK`) or profiles with `kanban` toolset enabled.
   - `_check_kanban_orchestrator_mode()` returns `_visible(to_env_worker=False)`: hides board routing tools (`kanban_list`, `kanban_unblock`) from dispatcher workers, exposing them only to orchestrators / profiles with `kanban`.
   - Both checks use `@no_cache_check_fn` (`tools/registry.py:272`) because visibility depends dynamically on execution context variables and environment state rather than immutable process-wide flags.
3. **Defense-in-Depth Handler Guards**:
   Even if a tool definition is visible, `_require_orchestrator_tool(tool_name)` (`tools/kanban_tools.py:301-309`) rejects invocation with `_Reject` if `HERMES_KANBAN_TASK` is present in the environment.

---

## 4. Kanban Notifier Proactive-Send Path Anchor (Card 1.2 Reuse)

### File & Line References
- **Subscription Storage & State**:
  - Schema & delivery modes: [`hermes_cli/kanban_db_notify.py:26`](hermes_cli/kanban_db_notify.py#L26) (`_NOTIFY_DELIVERY_MODES`: `"notify"`, `"notify+wake"`, `"wake"`)
  - Subscription creation: [`hermes_cli/kanban_db_notify.py:67-142`](hermes_cli/kanban_db_notify.py#L67-L142) (`add_notify_sub`)
  - Subscription query: [`hermes_cli/kanban_db_notify.py:166-199`](hermes_cli/kanban_db_notify.py#L166-L199) (`list_notify_subs`)
  - Read-only probe: [`hermes_cli/kanban_db_notify.py:202-250`](hermes_cli/kanban_db_notify.py#L202-L250) (`count_notify_subs`)
  - Atomic event claiming: [`hermes_cli/kanban_db_notify.py:352-382`](hermes_cli/kanban_db_notify.py#L352-L382) (`claim_unseen_events_for_sub`)
  - Cursor tracking & checkpoints:
    - [`hermes_cli/kanban_db_notify.py:392-406`](hermes_cli/kanban_db_notify.py#L392-L406) (`advance_notify_cursor`)
    - [`hermes_cli/kanban_db_notify.py:408-419`](hermes_cli/kanban_db_notify.py#L408-L419) (`record_notify_ping`)
    - [`hermes_cli/kanban_db_notify.py:421-436`](hermes_cli/kanban_db_notify.py#L421-L436) (`rewind_notify_cursor`)
- **Gateway Watcher & Loop**:
  - Periodic poller loop: [`gateway/kanban_watchers.py:60-121`](gateway/kanban_watchers.py#L60-L121) (`_kanban_notifier_watcher`)
  - Collector entry: [`gateway/kanban_watchers_notifier.py:356-366`](gateway/kanban_watchers_notifier.py#L356-L366) (`_notifier_collect`)
- **Adapter Resolution & Route Matching**:
  - [`gateway/kanban_watchers_notifier.py:162-221`](gateway/kanban_watchers_notifier.py#L162-L221) (`_adapter_for_subscription`)
- **Notification Delivery Class**:
  - Initialization & routing setup: [`gateway/kanban_watchers_notifier.py:491-530`](gateway/kanban_watchers_notifier.py#L491-L530) (`_KanbanNotification.__init__`)
  - Profile runtime scoping: [`gateway/kanban_watchers_notifier.py:620-630`](gateway/kanban_watchers_notifier.py#L620-L630) (`_owner_scope`)
  - Proactive text message send: [`gateway/kanban_watchers_notifier.py:675-710`](gateway/kanban_watchers_notifier.py#L675-L710) (`_send_event`) & [`gateway/kanban_watchers_notifier.py:711-746`](gateway/kanban_watchers_notifier.py#L711-L746) (`_send_pings`)
  - Active wake dispatch: [`gateway/kanban_watchers_notifier.py:631-674`](gateway/kanban_watchers_notifier.py#L631-L674) (`wake`) calling [`gateway/wake.py:127-160`](gateway/wake.py#L127-L160) (`deliver_wake`)
  - Pipeline orchestration: [`gateway/kanban_watchers_notifier.py:748-821`](gateway/kanban_watchers_notifier.py#L748-L821) (`deliver`)
- **Base Platform Adapter Contract**:
  - [`gateway/platforms/base.py:2643-2645`](gateway/platforms/base.py#L2643-L2645) (`BasePlatformAdapter.send`)
- **Caller Session Target Resolution (Kanban Tools)**:
  - [`tools/kanban_tools.py:1183-1227`](tools/kanban_tools.py#L1183-L1227) (`_resolve_notify_target`)
  - [`tools/kanban_tools.py:1229-1258`](tools/kanban_tools.py#L1229-L1258) (`_maybe_auto_subscribe`)

### How Card 1.2's `orchestrator_reply` Must Reuse This Path
Card 1.2 introduces `orchestrator_reply` to allow an orchestrator to proactively communicate structured replies back to the origin conversation. To achieve this without duplicate routing logic:
1. **Destination Identification**:
   - Query `kanban_notify_subs` for the card's origin session using `list_notify_subs(conn, task_id)` (`hermes_cli/kanban_db_notify.py:166`).
   - If running inside an active gateway turn, extract context via `_resolve_notify_target()` (`tools/kanban_tools.py:1183`).
2. **Adapter & Route Resolution**:
   - Look up the active adapter using `_adapter_for_subscription(runner, platform, sub, owner_profile)` (`gateway/kanban_watchers_notifier.py:162`), respecting `profile_routes` and multi-tenant scoping.
3. **Message Transmission**:
   - Send the message payload directly through `adapter.send(chat_id, text, metadata=metadata)` (`gateway/platforms/base.py:2643` & `gateway/kanban_watchers_notifier.py:686`).
   - Preserve routing metadata (`thread_id`, `reply_to`, `chat_type`, `scope_id`, `telegram_reply_to_message_id`).
4. **Session Binding & Isolation**:
   - Execute under `_owner_scope()` (`gateway/kanban_watchers_notifier.py:620`) to ensure profile preferences, token secrets, and language settings match the originating subscriber rather than the ambient gateway launch profile.

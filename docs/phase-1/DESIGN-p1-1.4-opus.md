I'll check the actual code paths before giving the design.
Notable: `_resolve_worker_cli_toolsets` has no callers in that file. Checking repo-wide and the real policy path.
Now where the frozen policy toolsets become an agent's tool list (and whether `disabled_toolsets` reaches it).
Last check: confirming exactly what the proposed derived constant and deny list would contain.
# E-020 design: kanban worker toolset blocklist

**Root cause of the two empty builder runs:** the recon anchor points at the wrong function. [`_resolve_worker_cli_toolsets`](hermes_cli/kanban_db_dispatch.py#L2643-L2669) has no production callers. Only one test references it, to monkeypatch it (`test_kanban_goal_mode_worker_log.py:26`). [`_worker_argv`](hermes_cli/kanban_db_dispatch.py#L2698-L2701) no longer passes `--toolsets`; it just runs `kanban_worker_client`, which waits for a receipt. The agent actually runs inside the gateway owner process, and its toolsets are frozen by [`build_kanban_policy`](gateway/session_kanban.py#L14-L71). A strip placed only in the dispatcher changes nothing at runtime.

## 1. Where the strip belongs

**Main fix:** in `gateway/session_kanban.py::build_kanban_policy`, at the final `return replace(policy, ...)` (line ~69).

**What gets resolved where:**
- `_get_platform_tools(cfg, "cli")` returns **toolset names**, not tool names. With the default config that's `browser, clarify, code_execution, computer_use, connections, cronjob, delegation, file, image_gen, memory, session_search, skills, terminal, todo, tts, vision, web`.
- A saved list can also pass through **mixed composites** (`coding` has clarify, delegate_task and memory) and **UI toolsets**. I confirmed that `platform_toolsets.cli: [desktop_ui, project, catalog, onboarding]` all survive. A `coding` posture can also arrive via `focus_toolsets`.
- Toolset names only become tool names per turn:
  1. `run_turn._run_agent_display_settings` → `policy_turn_toolsets` (enabled), plus `agent.disabled_toolsets` from `policy.config()` (disabled).
  2. → `model_tools._select_tool_names`, which subtracts disabled toolsets **last, by tool name**.
- `policy.current_toolsets()` returns the frozen `self.toolsets` for kanban, because `source='kanban'` is not in `SURFACES`. So whatever `build_kanban_policy` freezes is what every turn uses.

**So the fix needs two layers:**
- **(a) Name layer:** drop every toolset whose tools are all blocked from `policy.toolsets`, but keep `kanban`.
- **(b) Tool layer:** merge the same deny toolsets into the frozen `config_json` under `agent.disabled_toolsets`. Blocked tools inside mixed composites are then subtracted by tool name after expansion. This is the same technique `_blocked_toolsets_for_role` uses for delegate children.

**Secondary (parity only):** apply the name layer in `_resolve_worker_cli_toolsets` too, so the documented "worker toolsets" helper doesn't disagree with runtime.

## 2. Present/UI tools to add

Yes, some are reachable by workers through a saved profile list. Add every tool in `CLIENT_SURFACE_TOOLSETS ∪ TOOLSET_SESSION_PLATFORMS.keys()` (`project`, `desktop_ui`, `catalog`, `onboarding`), derived from the toolset definitions rather than listed by hand. Verified result:

`annotate_preview, close_terminal, desktop_preview, desktop_project, drive_preview, focus_pane, gui_tour, manage_catalog, react_to_message, read_terminal, read_window_below, set_theme, setup_beat, show_tip`

The resulting deny toolsets are exactly `['catalog', 'clarify', 'cronjob', 'delegation', 'desktop_ui', 'memory', 'onboarding', 'project']`. No blocked tool that can appear in any toolset is left uncovered.

`send_message` stays in the constant, but no tool by that name is registered (`toolsets.py:186`: "deliberately no agent-callable send_message tool"). It needs no toolset entry.

## 3. Diff sketch

**`tools/delegate_tool_toolsets.py`**: add only; existing functions are byte-for-byte unchanged.
```python
# line 8: widen the existing import
from toolsets import CLIENT_SURFACE_TOOLSETS, TOOLSET_SESSION_PLATFORMS, TOOLSETS, resolve_toolset

# insert directly after `DEFAULT_TOOLSETS = [...]`
# Kanban-dispatched workers run unattended with no client attached: everything a delegate child may never
# call, plus every client-surface / desktop-session tool. Derived from the toolset definitions (never
# re-listed) so a new UI tool is blocked without touching this line.
KANBAN_WORKER_BLOCKED_TOOLS = DELEGATE_BLOCKED_TOOLS | frozenset(
    tool
    for name in CLIENT_SURFACE_TOOLSETS | TOOLSET_SESSION_PLATFORMS.keys()
    for tool in resolve_toolset(name, include_registry=False)
)

# insert directly after `_blocked_toolsets_for_role`, before `_resolve_child_toolsets`
def kanban_worker_deny_toolsets() -> list[str]:
    """Toolsets whose static tools are all KANBAN_WORKER_BLOCKED_TOOLS. A kanban worker drops these from its
    enabled names AND carries them as ``agent.disabled_toolsets`` so blocked tools inside mixed composites
    (``coding``, ``hermes-cli``) are subtracted at tool granularity after expansion. Never ``kanban``."""
    return sorted(
        name
        for name in TOOLSETS
        if name != "kanban"
        and (resolved := resolve_toolset(name, include_registry=False))
        and set(resolved) <= KANBAN_WORKER_BLOCKED_TOOLS
    )
```

**`gateway/session_kanban.py::build_kanban_policy`**: replace the final `return`.
```python
    request.update(source='kanban', board=board, task_id=task.id, run_id=task.current_run_id)
    # E-020: an unattended worker never gets the delegate-child blocklist or client UI tools. Names leave the
    # frozen set; the same names ride agent.disabled_toolsets so mixed composites lose those tools per-tool.
    from agent.skill_utils import parse_config_string_list
    from tools.delegate_tool_toolsets import kanban_worker_deny_toolsets
    deny = kanban_worker_deny_toolsets()
    frozen = json.loads(policy.config_json)
    agent_cfg = frozen.get('agent') if isinstance(frozen.get('agent'), dict) else {}
    disabled = parse_config_string_list(agent_cfg.get('disabled_toolsets'))
    frozen['agent'] = {**agent_cfg, 'disabled_toolsets': list(dict.fromkeys([*disabled, *deny]))}
    toolsets = (set(policy.toolsets) - set(deny)) | {'kanban'}
    return replace(policy, source='kanban', platform='cli', toolsets=tuple(sorted(toolsets)),
                   config_json=json.dumps(frozen, sort_keys=True),
                   request_json=json.dumps(request, sort_keys=True), kanban_json=json.dumps(context)), private
```

**`hermes_cli/kanban_db_dispatch.py::_resolve_worker_cli_toolsets`**: parity, one change inside the `with` block.
```python
        from tools.delegate_tool_toolsets import kanban_worker_deny_toolsets
        with _worker_profile_scope(hermes_home):
            cfg = load_config()
            deny = set(kanban_worker_deny_toolsets())
            toolsets = sorted(t for t in _get_platform_tools(cfg, "cli") if t not in deny)
```
Signatures stay the same. No other files change.

## 4. Tests for the follow-up card

**New file `tests/tools/test_kanban_worker_blocklist.py`:**
1. `DELEGATE_BLOCKED_TOOLS < KANBAN_WORKER_BLOCKED_TOOLS`, and the difference equals the 14 UI tool names above exactly.
2. `kanban_worker_deny_toolsets() == ['catalog','clarify','cronjob','delegation','desktop_ui','memory','onboarding','project']`, and `'kanban'` is not in it.
3. Coverage: every tool in `KANBAN_WORKER_BLOCKED_TOOLS` that appears in any `resolve_toolset(n, include_registry=False)` is covered by the union of the deny toolsets.
4. Delegate behaviour is unchanged:
   - `_blocked_toolsets_for_role('leaf') == ['clarify','cronjob','delegation','memory']`
   - `_blocked_toolsets_for_role('orchestrator') == ['clarify','cronjob','memory']`
   - `_strip_blocked_tools(['kanban','delegation','desktop_ui','terminal','memory']) == ['desktop_ui','terminal']`
   - The existing `tests/tools/test_delegate.py` passes untouched.

**Additions to `tests/gateway/test_session_kanban.py`:**
5. Config `platform_toolsets.cli: [terminal, file, memory, clarify, delegation, cronjob, desktop_ui, project]` with `agent.disabled_toolsets: ['web']`:
   - `policy.toolsets == ('file','kanban','terminal')`
   - Frozen `agent.disabled_toolsets` starts with `'web'` and contains all 8 deny toolsets.
6. String form: `agent.disabled_toolsets: "['web']"` merges correctly.
7. Mixed composite: `platform_toolsets.cli: [coding]`. Then `model_tools.get_tool_definitions(enabled_toolsets=list(policy.toolsets), disabled_toolsets=<frozen disabled>, quiet_mode=True)` returns names disjoint from `KANBAN_WORKER_BLOCKED_TOOLS`, and `terminal` is still present.
8. Scope guard: a plain `build_policy({'source':'cli','cwd':...}, same_config)` still includes `memory` and `clarify`, which proves only kanban workers change.

**Addition to `tests/hermes_cli/test_kanban_worker_spawn_toolsets.py`:**
9. A profile with cli `[terminal, memory, clarify]` makes `_resolve_worker_cli_toolsets` return `['terminal']`.

**Verification command:**
```
cd superagent-p1 && ../../.venv/bin/python -m pytest -q \
  tests/gateway/test_session_kanban.py tests/hermes_cli/test_kanban_worker_spawn_toolsets.py \
  tests/tools/test_delegate.py tests/plugins/test_kanban_model_override.py \
  tests/gateway/test_bare_custom_launch_keeps_profile_route.py tests/hermes_cli/test_welcome_trial.py \
  tests/hermes_cli/test_kanban_goal_mode_worker_log.py tests/tools/test_kanban_worker_blocklist.py
```

## 5. Traps for the builder

1. **Don't patch only the dispatcher.** `_resolve_worker_cli_toolsets` has no production callers. The gateway policy is the real enforcement point.
2. **Don't reuse `_strip_blocked_tools` for workers.** It hard-strips `kanban`, which would remove the worker's lifecycle tools (`kanban_complete` / `kanban_block`). It also doesn't cover UI tools.
3. **Don't edit `_strip_blocked_tools`, `_blocked_toolsets_for_role`, `_resolve_child_toolsets` or `DELEGATE_BLOCKED_TOOLS` itself.** That's the "orchestrator/child unchanged" rule. The orchestrator path at lines 116–119 must stay as it is.
4. **Name-level stripping alone isn't enough.** `coding`, `hermes-cli` and other mixed composites still carry clarify, memory and delegate_task. The `agent.disabled_toolsets` merge is what removes them.
5. **Never put a `hermes-*` bundle or a posture name (`coding`) into the disabled list.** `_apply_toolset_selection` only subtracts their non-core tools. Use only the derived single-purpose deny toolsets.
6. **Normalise the existing `disabled_toolsets` with `parse_config_string_list`.** It may be a JSON-array string or missing. Keep user entries first and deduplicate with `dict.fromkeys`.
7. **Keep `| {'kanban'}` after the subtraction**, and keep the `name != "kanban"` guard.
8. **Use `include_registry=False` when deriving the constant.** It's built at import time, so it must not depend on registry state.
9. **Existing assertions stay true:** `{'kanban','terminal','file'} <= policy.toolsets` and `{'terminal','web','kanban'} <= ...` hold because neither set is blocked. Don't loosen them.
10. **Sessions created before this change keep their old toolsets on retry,** because `restore_policy` reuses the saved policy. That's expected; don't try to migrate saved receipts.


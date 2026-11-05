# P1.8 Recon Evidence: Terminal Shell Auto-Background (E-022) Investigation

## Context & Binding Directives
- **Decision E-022 (verbatim)**:
  > "Shell auto-background: kanban-dispatched worker runs only; pass the existing terminal tool's background configuration for the run so foreground commands background after 5 s; if the terminal tool has no such threshold setting, this todo is a blocker (do not modify the terminal tool's global defaults)."
- **TODO 1.8 Instructions & Outcomes**:
  > "TODO 1.8 — Recon (no code unless the setting exists): determine whether the terminal tool supports a per-run foreground-timeout→background setting. Read tools/terminal_tool*.py and how kanban worker runs configure their terminal/tool environment (dispatcher spawn path anchors in notes/p1-1-anchors.md)."
  > "OUTCOMES:
  > - If the per-run setting EXISTS: set 5 s for kanban worker runs, add a test asserting the run config carries it, commit code+test.
  > - If it does NOT exist: write notes/p1-8-e022-evidence.md with the evidence (what the terminal tool does support, file:line) and the smallest proposed mechanism; commit the note only. This outcome does NOT block the phase."

---

## 1. Executive Summary & Verdict

**Verdict**: The terminal tool does **NOT** support a per-run foreground-timeout→background setting.
Specifically:
1. There is no setting in `tools/terminal_tool*.py` that automatically transitions a running foreground command to the background after 5 seconds (or any elapsed runtime duration).
2. The only foreground-to-background promotion mechanism that exists (`FOREGROUND_MAX_TIMEOUT`) is an import-time process-global integer (default 600s), not a per-run setting. Furthermore, it only acts as an upfront pre-exec check when the model explicitly passes a `timeout` argument exceeding the cap; it does not background un-annotated commands, nor does it let a command run for 5 seconds before backgrounding.
3. As instructed by Decision E-022 and TODO 1.8, we do not modify the terminal tool's global defaults or invent code. This document records the empirical evidence and details the smallest proposed mechanism.

---

## 2. Search Scope & Methodology

We examined the following modules and subsystems:
- **Terminal tool core & siblings**:
  - `tools/terminal_tool.py`
  - `tools/terminal_tool_background.py`
  - `tools/terminal_tool_config.py`
  - `tools/terminal_tool_guards.py`
  - `tools/terminal_tool_lifecycle.py`
  - `tools/terminal_tool_result.py`
  - `tools/terminal_tool_backends.py`
  - `tools/terminal_tool_sudo.py`
  - `tools/terminal_scope.py`
- **Environment execution engines**:
  - `tools/environments/base.py`
  - `tools/environments/local.py`
  - `tools/environments/docker.py`
- **Configuration & mapping**:
  - `hermes_cli/config.py` (`TERMINAL_CONFIG_ENV_MAP`)
  - `hermes_cli/config_defaults.py`
- **Kanban worker configuration & dispatch anchors**:
  - `hermes_cli/kanban_db_dispatch.py` (worker profile scoping & subprocess spawning)
  - `gateway/session_kanban.py` (kanban session policy construction)

---

## 3. Findings with File:Line Evidence

### A. What the Terminal Tool Actually Supports

1. **`FOREGROUND_MAX_TIMEOUT` (Import-time global cap, not per-run)**:
   - **`tools/terminal_tool.py:76-77`**:
     ```python
     # Hard cap on foreground timeout; override via TERMINAL_MAX_FOREGROUND_TIMEOUT env var.
     FOREGROUND_MAX_TIMEOUT = _safe_parse_import_env("TERMINAL_MAX_FOREGROUND_TIMEOUT", 600, int, "integer")
     ```
     Parsed once at module import from `os.getenv`. It is **not** scope-aware (`_tenv`), **not** per-run, and **not** configurable via `tools/terminal_scope.py`.
   - **`tools/terminal_tool.py:1225-1226`** (`_plan_execution`):
     ```python
     if timeout and timeout > FOREGROUND_MAX_TIMEOUT:
         promoted = timeout
     ```
     This check only triggers if the LLM/caller explicitly passes `timeout` in the function call arguments (`terminal(command="...", timeout=...)`). If `timeout` is omitted (standard behavior), `timeout` is `None`, and the command runs as foreground up to `config["timeout"]` (180s).
   - **`tools/terminal_tool.py:1534-1553`**:
     When `promoted` is set, `background, notify_on_complete, watch_patterns = True, True, None` is forced, and `spawn_background_process` is called **immediately** (at 0.0 seconds). It does **not** run in the foreground for any threshold before backgrounding.

2. **`TERMINAL_TIMEOUT` (Foreground execution deadline, kills rather than backgrounds)**:
   - **`tools/terminal_tool.py:824`** & **`tools/terminal_tool_config.py:32-44`**:
     `"timeout": _parse_env_var("TERMINAL_TIMEOUT", "180")` resolves via `_tenv`.
   - **`tools/terminal_tool.py:1341-1350`** (`_run_foreground`):
     ```python
     result = env.execute(command, timeout=effective_timeout, cwd=command_cwd, bounded_capture=True, ...)
     ...
     if "timeout" in str(e).lower():
         return _error_json(f"Command timed out after {effective_timeout} seconds", exit_code=124)
     ```
     Exceeding the timeout raises an exception in `env.execute` and returns exit code 124. The process is terminated; it is never backgrounded.

3. **`trailing_amp_background_command` (Syntax rewrite only)**:
   - **`tools/terminal_tool_guards.py:187-219`** & **`tools/terminal_tool.py:1220, 1481-1485`**:
     Converts commands ending in an unquoted trailing `&` to `background=True`. This is purely an AST/syntax rewrite at call time, not a timeout or duration-based background transition.

4. **`yield_to_background_handler` (User-message interruption only)**:
   - **`tools/terminal_tool_background.py:251-274`** & **`tools/environments/base.py:556-568`**:
     `_wait_for_process` calls `yield_handler` only when `consume_yield(watch_interrupt_tid)` is true. This occurs when an incoming user chat message arrives while a command is running. It is never triggered by an elapsed duration or timeout.
   - Supported only for `env_type == "local"`; sandboxes/containers return `None` (`tools/terminal_tool_background.py:261`).

5. **Async Notifications on Kanban Worker Sessions**:
   - **`tools/terminal_tool_background.py:106-123`**:
     `_apply_async_support` drops `notify_on_complete` for sessions where `async_delivery_supported()` is false (which is true for `HERMES_SESSION_SOURCE == "kanban"` in `gateway/session_context.py:35`). Thus, backgrounded processes in worker runs must be polled with `process_manage(action='poll')`.

---

### B. How Kanban Worker Runs Configure Their Terminal Environment

1. **Dispatcher Worker Subprocess Environment (`hermes_cli/kanban_db_dispatch.py:2755-2880`)**:
   - Lines 2780-2784: Injects `HERMES_KANBAN_TASK`, `HERMES_KANBAN_WORKSPACE`, `HERMES_SESSION_SOURCE = "kanban"`.
   - Lines 2796-2797: Injects `TERMINAL_CWD = workspace`.
   - Lines 2810-2813:
     ```python
     for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
         override = _worker_terminal_timeout_env(task.max_runtime_seconds, env.get(var))
         if override is not None:
             env[var] = override
     ```
   - Lines 2545-2571 (`_worker_terminal_timeout_env`):
     Computes `desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)` and only overrides if `desired > existing`. This is designed to raise the timeout ceiling for long tasks; it does not set a 5s auto-background threshold.

2. **Gateway Session Policy (`gateway/session_kanban.py:14-82`)**:
   - `build_kanban_policy`: Sets `source='kanban'`, `platform='cli'`, applies `kanban_worker_deny_toolsets()` (E-020). It does not configure any terminal auto-background threshold in `config_json` or policy parameters.

3. **Terminal Config Mapping (`hermes_cli/config.py:2046-2058`) & Terminal Scope (`tools/terminal_scope.py`)**:
   - `TERMINAL_CONFIG_ENV_MAP` contains keys for backend, cwd, images, container resources, docker flags, and ssh settings. It contains no key for `auto_background`, `background_threshold`, or `max_foreground_timeout`.
   - `tools/terminal_scope.py` does not track or expose any foreground-to-background transition threshold.

---

## 4. Smallest Proposed Mechanism

If shell auto-background after 5 seconds is to be implemented in a future phase without destabilizing core terminal invariants, the minimal surgical mechanism would be:

1. **Execution Poll Loop Handoff (`tools/environments/base.py`)**:
   In `_wait_for_process` (`tools/environments/base.py:535-585`), the poll loop already maintains `_now = time.monotonic()` and `_activity_state["start"]`, as well as the `yield_handler` abstraction.
   Add an optional `auto_background_seconds: Optional[float] = None` parameter to `execute()` and `_wait_for_process()`.
   Within the poll loop (`while proc.poll() is None:`):
   ```python
   if (
       auto_background_seconds is not None
       and yield_handler is not None
       and (time.monotonic() - _activity_state["start"]) >= auto_background_seconds
   ):
       drain_stop.set()
       drain_thread.join(timeout=1)
       handed = yield_handler(proc, output.render())
       if handed is not None:
           output.close_spill()
           return handed
       drain_stop.clear()
       drain_thread = _start_drain_thread(proc, output, drain_stop)
   ```

2. **Terminal Scope & Configuration Wiring (`tools/terminal_scope.py`, `tools/terminal_tool_config.py`, `hermes_cli/config.py`)**:
   - Add `auto_background_seconds` to `TERMINAL_CONFIG_ENV_MAP` mapped to `TERMINAL_AUTO_BACKGROUND_SECONDS`.
   - Support reading `_tenv("TERMINAL_AUTO_BACKGROUND_SECONDS")` in `tools/terminal_tool.py:_plan_execution` and attaching it to `_ExecPlan`.
   - Pass `auto_background_seconds` into `_yield_kwargs` / `env.execute` in `tools/terminal_tool.py:1341`.

3. **Worker Dispatch Scope Wiring (`gateway/session_kanban.py` & `hermes_cli/kanban_db_dispatch.py`)**:
   - In `gateway/session_kanban.py:build_kanban_policy`, inject `terminal: {auto_background_seconds: 5}` into `frozen` config for kanban worker runs.
   - In `hermes_cli/kanban_db_dispatch.py:_default_spawn`, export `TERMINAL_AUTO_BACKGROUND_SECONDS = "5"`.

4. **Backend Constraint Caveat**:
   `yield_to_background_handler` currently only works with `env_type == "local"` because it adopts a local OS `Popen` object via `process_registry.adopt_local`. Container backends (`docker`, `modal`) do not currently support adopting an in-flight `env.execute` call into a managed background session without additional backend protocol support.

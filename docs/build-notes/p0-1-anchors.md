# Superagent P0.1 — Architectural & Implementation Anchors

## Overview
This document records the authoritative code anchors and implementation patterns in the `hermes-agent` codebase identified during Phase 0.1 reconnaissance for Superagent. Subsequent phases (P0.2+) will directly imitate these patterns to add the `superagent` configuration block, validation, settings resolver, and test coverage without architectural drift.

---

## 1. Config Loading & Validation Anchor

### File & Line References
- **Config loading**: [`hermes_cli/config.py:2010-2023`](hermes_cli/config.py#L2010-L2023) (`load_config`, `load_config_readonly`) & [`hermes_cli/config.py:2248-2300`](hermes_cli/config.py#L2248-L2300) (`_load_config_impl`).
- **Defaults baseline**: [`hermes_cli/config_defaults.py:33`](hermes_cli/config_defaults.py#L33) (`DEFAULT_CONFIG`).
- **Structure validation entry point**: [`hermes_cli/config.py:1091-1132`](hermes_cli/config.py#L1091-L1132) (`validate_config_structure`).
- **Nested block & enum validation pattern to imitate**: [`hermes_cli/config_value_checks.py:9-18`](hermes_cli/config_value_checks.py#L9-L18) (`_validate_voice` called from `hermes_cli/config.py:1103`).

### How Config Blocks are Loaded
1. Callers invoke `load_config()`, which calls `_load_config_impl(want_deepcopy=True)` (or `load_config_readonly()` for immutable hot-paths).
2. The implementation caches the parsed config keyed by the config file path and signature.
3. On cache miss or invalidation, it creates a base dictionary via `copy.deepcopy(DEFAULT_CONFIG)`.
4. It reads the raw user YAML document via `read_config_doc(config_path)` and applies `_deep_merge(config, user_config)`.
5. Consequently, any omitted key in user configuration automatically inherits the exact value and nested shape provided in `DEFAULT_CONFIG`.

### How Validation Works
- `validate_config_structure(config: Optional[dict[str, Any]] = None) -> list[ConfigIssue]` is called by `hermes doctor` and startup warning printers.
- Issues are accumulated as `ConfigIssue` dataclasses via helper `_issue(issues, severity, message, hint)`.
- It delegates domain-specific checks to isolated helper functions in `hermes_cli/config_value_checks.py`.

### Existing Pattern to Imitate: `_validate_voice`
In [`hermes_cli/config_value_checks.py:9-18`](hermes_cli/config_value_checks.py#L9-L18):
```python
def _validate_voice(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    voice_cfg = config.get("voice")
    if not (isinstance(voice_cfg, dict) and "submit_mode" in voice_cfg):
        return
    submit_mode = voice_cfg.get("submit_mode")
    normalized = submit_mode.strip().lower() if isinstance(submit_mode, str) else None
    if normalized not in {"direct", "draft"}:
        _issue(issues, "error", f"voice.submit_mode must be 'direct' or 'draft', got {submit_mode!r}",
               "Set voice.submit_mode to direct (submit immediately) or draft (edit before sending)")
```
- **Superagent Mapping**:
  `superagent.accept_mode` mirrors this exact structure:
  - Extract `superagent_cfg = config.get("superagent")`.
  - Guard on `isinstance(superagent_cfg, dict) and "accept_mode" in superagent_cfg`.
  - Normalize with `.strip().lower()`.
  - Validate against the allowed enum set `{"off", "observe", "auto"}`.
  - Record a descriptive `_issue(issues, "error", ...)` with actionable remediation hints.

### Subsystem Separation Invariant
- **Schema defaults & structural issue checks** reside in `hermes_cli/config_defaults.py` and `hermes_cli/config.py` / `hermes_cli/config_value_checks.py`.
- **Subsystem settings resolvers** (such as runtime coercion, environment fallbacks, dynamic clamping, and internal dataclasses) live with the subsystem itself (e.g., `_resolve_dispatcher_settings` in `gateway/kanban_watchers_dispatcher.py`).

---

## 2. Dispatcher Settings Resolver Anchor (`_DispatcherSettings`)

### File & Line References
- **Dataclass & Resolver**: [`gateway/kanban_watchers_dispatcher.py:33-118`](gateway/kanban_watchers_dispatcher.py#L33-L118)
  - `_DispatcherSettings` definition: lines 33–45.
  - `_resolve_dispatcher_settings` implementation: lines 47–118.

### How `kanban.*` Settings Resolve
The resolver parses raw configuration safely at boot into a strongly-typed immutable dataclass:
1. **Float parsing with safety floor**:
   ```python
   try:
       interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
   except (ValueError, TypeError):
       ...
       interval = 60.0
   interval = max(interval, 1.0)  # sanity floor
   ```
2. **Positive integer caps via shared helper**:
   Uses `_positive_int_setting(kanban_cfg, "max_in_progress")` which parses ints, checks `value >= 1`, and logs warnings on invalid values.
3. **Dynamic fallback**:
   Falls back to memory-derived defaults (`_kbd().resolve_max_in_progress(...)`) when config value is unset.
4. **Integer parsing with default fallback**:
   `raw_failure_limit = kanban_cfg.get("failure_limit", kb.DEFAULT_FAILURE_LIMIT)` with fallback to default if parsing fails or `failure_limit < 1`.
5. **String sanitization**:
   `default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None` ensures clean string or `None`.
6. **Boolean coercion**:
   `reconcile_orphans = bool(kanban_cfg.get("reconcile_orphans", True))` provides explicit truthiness with default `True`.

---

## 3. Shared Helpers Anchor (`gateway/kanban_watchers_common.py`)

### File & Line References
- [`gateway/kanban_watchers_common.py:1-150`](gateway/kanban_watchers_common.py#L1-L150)

### Inventory of Helpers for Superagent Phase 0 Reuse
1. **`_positive_int_setting(kanban_cfg: dict, key: str) -> Optional[int]` (lines 49-63)**:
   - Validates that a key parses as an integer and is `>= 1`.
   - Returns `None` and logs a warning if invalid or below 1.
   - Ideal for validating `worker_slots`, `human_reserve_slots`, and `eval_timeout_seconds`.
2. **`logger` (line 18)**:
   - Standard logger namespace `logging.getLogger("gateway.run")` used across watcher routines.
3. **`_resolve_auto_decompose_settings(load_config: Callable[[], Any]) -> tuple[bool, int]` (lines 66-88)**:
   - Dynamic re-read pattern on each tick with safe fallback to `(False, 3)` upon exception.
   - Proves how live configuration can be safely re-read without restarting the gateway process.
4. **`_run_in_fresh_context(func: Callable[..., Any], /, *args: Any) -> Any` (lines 21-29) & `_to_thread_process_service` (lines 32-34)**:
   - Wraps asynchronous thread execution in an empty `Context()` so request-local `ContextVar` markers (e.g. `delegate_task`) do not leak into background service loops.
5. **`_kanban_dispatch_allowed() -> bool` (lines 100-111)**:
   - Emergency stop / pause check via `agent.estop.check_paused("kanban", logger)`.
   - Allows checking if `hermes pause` has halted dispatch loops.
6. **`_acquire_singleton_lock(lock_path) -> tuple[Optional[object], str]` (lines 113-139) & `_release_singleton_lock(handle) -> None` (lines 141-150)**:
   - Non-blocking advisory file lock implementation (`gateway.status._try_acquire_file_lock`) preventing multiple processes from running duplicate dispatchers.

---

## 4. Test Runner & Execution Environment Context

- **Release Virtual Environment**:
  The production venv python is a Python 3.11 environment reserved for production runtime. It deliberately does **not** have `pytest` installed. Running `python -m pytest` against it fails with `No module named pytest`.
- **Test Runner Environment**:
  - The designated test runner python is exposed via `__HERMES_TEST_PYTHON` (the designated test-runner python).
  - Standard test suite execution should be driven via `./scripts/run_tests.sh` or directly with the test-environment python binary.
  - Repository health checks must be verified with `python scripts/check`.

# Superagent P4.1 — Architectural & Implementation Anchors (Outbox Recon)

## Overview

This document records the authoritative architectural anchors, code locations, schema definitions, and delivery contracts across `hermes-agent` for Superagent Phase 4 (Outbox). Specifically, it investigates:
1. **`gateway/delivery_ledger.py`**: The durable delivery-obligation ledger for gateway final responses (shared `state.db`), its path resolution, schema initialization, and flood/backoff algorithms.
2. **`hermes_cli/kanban_db_notify.py` & `gateway/kanban_watchers_notifier.py`**: The notification subscription mechanics and how the notifier executes live sends via platform adapters (establishing the Outbox drain's sender contract, E-028).
3. **`cron/delivery_queue.py`**: The restart-safe worker durable handoff queue, what payloads it persists, and how the gateway drain processes it.
4. **`gateway/delivery.py`**: The `DeliveryRouter`, `DeliveryTarget`, and `DeliveryTransport` subsystems handling message dispatch, truncation, silence filtering, and channel routing.
5. **Detailed Comparative Analysis**: The structural and behavioural divergence between the notifier's send path and the cron send path, detailing how these patterns inform the Phase 4 Outbox architecture.

---

## 1. Gateway Delivery Ledger Anchor (`gateway/delivery_ledger.py`)

### Role & Architectural Purpose
The delivery ledger provides a crash-resilient, durable delivery-obligation record for gateway responses in the profile's shared `state.db`. If a gateway process crashes between finalizing a response and receiving an acknowledgement from the messaging platform, the response is never silently lost. It implements at-least-once delivery with honest markers (`RECOVERED_MARKER`, `RECONNECTED_MARKER`, `FLOOD_MARKER`) to notify the recipient that redelivery may duplicate an earlier transmission.

### File & Line References
- **Module location**: [`gateway/delivery_ledger.py:1-564`](gateway/delivery_ledger.py#L1-L564)
- **Database path resolution (`_db_path`)**: [`gateway/delivery_ledger.py:171-176`](gateway/delivery_ledger.py#L171-L176)
- **Database connection & busy timeout**: [`gateway/delivery_ledger.py:179-186`](gateway/delivery_ledger.py#L179-L186)
- **Schema initialization (`_initialize_schema`)**: [`gateway/delivery_ledger.py:188-209`](gateway/delivery_ledger.py#L188-L209)
- **Flood control helpers**:
  - `flood_wait_seconds`: [`gateway/delivery_ledger.py:108-123`](gateway/delivery_ledger.py#L108-L123)
  - `retry_not_before`: [`gateway/delivery_ledger.py:151-169`](gateway/delivery_ledger.py#L151-L169)
  - `is_flood_error`: [`gateway/delivery_ledger.py:101-105`](gateway/delivery_ledger.py#L101-L105)
  - `flood_retry_delay`: [`gateway/delivery_ledger.py:126-134`](gateway/delivery_ledger.py#L126-L134)
  - `flood_not_before`: [`gateway/delivery_ledger.py:144-148`](gateway/delivery_ledger.py#L144-L148)
  - `is_reconnect_only`: [`gateway/delivery_ledger.py:96-98`](gateway/delivery_ledger.py#L96-L98)
- **Lifecycle & State Mutations**:
  - `compute_obligation_id`: [`gateway/delivery_ledger.py:264-267`](gateway/delivery_ledger.py#L264-L267)
  - `record_obligation`: [`gateway/delivery_ledger.py:270-286`](gateway/delivery_ledger.py#L270-L286)
  - `record_crash_left_reply`: [`gateway/delivery_ledger.py:288-308`](gateway/delivery_ledger.py#L288-L308)
  - `mark_attempting`: [`gateway/delivery_ledger.py:310-312`](gateway/delivery_ledger.py#L310-L312)
  - `mark_delivered`: [`gateway/delivery_ledger.py:314-316`](gateway/delivery_ledger.py#L314-L316)
  - `mark_failed`: [`gateway/delivery_ledger.py:318-320`](gateway/delivery_ledger.py#L318-L320)
  - `release_runtime_claim`: [`gateway/delivery_ledger.py:322-340`](gateway/delivery_ledger.py#L322-L340)
  - `sweep_recoverable`: [`gateway/delivery_ledger.py:368-447`](gateway/delivery_ledger.py#L368-L447)
  - `sweep_failed_for_runtime`: [`gateway/delivery_ledger.py:449-505`](gateway/delivery_ledger.py#L449-L505)
  - `pending_retries`: [`gateway/delivery_ledger.py:508-535`](gateway/delivery_ledger.py#L508-L535)
  - `_prune_unlocked`: [`gateway/delivery_ledger.py:537-553`](gateway/delivery_ledger.py#L537-L553)
  - `ledger_enabled`: [`gateway/delivery_ledger.py:555-564`](gateway/delivery_ledger.py#L555-L564)

### Database Path Resolution (`_db_path`)
Lines 171–176:
```python
def _db_path():
    # Launch home, not get_hermes_home(): a multiplexed gateway records a served profile's replies
    # under that profile's home override, but the boot sweep reads from the launch context, so both
    # must open the one shared store (adapter_profile tells the bots apart). No get_hermes_home()
    # fallback for an unset HERMES_HOME: a default gateway run in the foreground has none.
    return get_process_hermes_home() / "state.db"
```
- **Invariant**: Uses `get_process_hermes_home() / "state.db"`. This anchors the file to the launch profile's home directory rather than dynamically switching with `get_hermes_home()`, ensuring that in multiplexed gateways, all bot profiles write to and sweep from the same shared SQLite database.
- **Connection opener**: `_connect()` uses `hermes_cli.sqlite_util.open_db` with `busy_timeout_ms=10_000`, WAL mode, and registers `initialize=_initialize_schema`.

### Schema Initialization (`_initialize_schema`)
Lines 188–209:
```python
def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            last_error TEXT,
            adapter_profile TEXT
        )"""
    )
    if "adapter_profile" not in {row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")}:
        add_column_if_missing(conn, "delivery_obligations", "adapter_profile", "adapter_profile TEXT")
```
- **Table**: `delivery_obligations`
- **Columns**:
  - `obligation_id TEXT PRIMARY KEY`: Hex digest SHA-256 derived from `session_key|message_ref|content` truncated to 24 characters.
  - `session_key TEXT NOT NULL`: Target session identifier.
  - `platform TEXT NOT NULL`: Platform name (e.g., `"telegram"`, `"discord"`, `"slack"`).
  - `chat_id TEXT NOT NULL`: Target chat ID.
  - `thread_id TEXT`: Nullable topic/thread ID.
  - `content TEXT NOT NULL`: Message payload text.
  - `state TEXT NOT NULL`: State machine status (`'pending'`, `'attempting'`, `'delivered'`, `'failed'`, `'abandoned'`).
  - `attempts INTEGER NOT NULL DEFAULT 0`: Redelivery attempt counter.
  - `created_at REAL NOT NULL`: Epoch timestamp of creation.
  - `updated_at REAL NOT NULL`: Epoch timestamp of last mutation.
  - `owner_pid INTEGER`: PID of the owning gateway process.
  - `owner_started_at INTEGER`: OS process start time of the owner for PID-reuse fencing.
  - `last_error TEXT`: Last failure message (truncated to 500 characters).
  - `adapter_profile TEXT`: The bot profile name owning the credential (defaults to `'default'`).
- **Migration**: Uses `add_column_if_missing(conn, "delivery_obligations", "adapter_profile", "adapter_profile TEXT")` to self-heal older tables.
- **Retention & Pruning**: Handled in `_prune_unlocked`: deletes rows in terminal states (`'delivered'`, `'abandoned'`) older than 7 days (`_RETENTION_SECONDS = 7 * 24 * 60 * 60`), and enforces a maximum cap of 500 rows (`_MAX_ROWS = 500`).

### Exact Signatures & Logic of Flood Helpers

#### 1. `flood_wait_seconds`
Lines 108–123:
```python
def flood_wait_seconds(error: Any, default: float = FLOOD_RETRY_DEFAULT_SECONDS) -> float:
    """The wait the platform asked for, read from ``flood_control:<seconds>``; ``default`` when unreadable."""
    text = str(error or "").strip().lower()
    wait = default
    if text.startswith(FLOOD_ERROR_PREFIX):
        try:
            wait = float(text[len(FLOOD_ERROR_PREFIX):].strip())
        except ValueError:
            wait = default
    else:
        # A row still carrying the platform's own flood wording states its delay just as precisely,
        # and the deadline must use it rather than the generic default.
        raw = _raw_flood_wait(text)
        if raw is not None:
            wait = raw
    return wait if wait > 0 else default
```
- **Constants**: `FLOOD_RETRY_DEFAULT_SECONDS = 60.0`, `FLOOD_ERROR_PREFIX = "flood_control:"`, `_RAW_FLOOD_RE = re.compile(r"flood control exceeded.*?retry in\s+(\d+(?:\.\d+)?)", re.IGNORECASE)`.
- **Behavior**: Extracts wait seconds either from normalized adapter errors (`flood_control:<seconds>`) or raw platform error strings (e.g., python-telegram-bot's `Flood control exceeded. Retry in 185 seconds`). If unparsable or `<= 0`, falls back to `default` (60.0s).

#### 2. `retry_not_before`
Lines 151–169:
```python
def retry_not_before(updated_at: Any, last_error: Any, attempts: Any) -> Optional[float]:
    """Earliest moment a failed row may be resent, or ``None`` for a row the runtime must leave alone:
    a flood refusal keeps the platform's own wait, an allowlisted reconnect error is due at once, a
    whole-chat death is final, and any other rejection backs off by the attempts already spent — but
    never spends the LAST budgeted attempt. An unclassified outage can outlast any timer, and a row
    the timer abandoned would be lost for good; leaving one attempt keeps it recoverable by the boot
    sweep after a restart, which is a real recovery signal."""
    if is_flood_error(last_error):
        return flood_not_before(updated_at, last_error)
    text = str(last_error or "").strip().lower()
    if is_reconnect_only(text):
        return _failed_stamp(updated_at)
    if classify_dead_error(text):
        return None
    spent = int(attempts or 0)
    if spent >= MAX_ATTEMPTS - 1:
        return None
    return _failed_stamp(updated_at) + _RETRY_BACKOFF_SECONDS[spent]
```
- **Constants**: `MAX_ATTEMPTS = 3`, `_RETRY_BACKOFF_SECONDS = (30.0, 120.0)`.
- **Classification Rules**:
  1. `is_flood_error(last_error)`: Returns `flood_not_before(updated_at, last_error)` = `_failed_stamp(updated_at) + flood_wait_seconds(last_error)`.
  2. `is_reconnect_only(text)` (e.g., `send_path_degraded`): Due immediately once reconnected (`_failed_stamp(updated_at)`).
  3. `classify_dead_error(text)` (permanent channel death, e.g., bot kicked, user deactivated, chat deleted): Returns `None` (never retried).
  4. Budget Exhaustion: If `spent >= MAX_ATTEMPTS - 1` (i.e. `spent >= 2`), returns `None`. Preserves the final attempt for the boot sweep on restart.
  5. Exponential Tiered Backoff: Returns `_failed_stamp(updated_at) + _RETRY_BACKOFF_SECONDS[spent]` (30s after 0 attempts spent, 120s after 1 attempt spent).

---

## 2. Kanban Notifier Anchor (`hermes_cli/kanban_db_notify.py` & `gateway/kanban_watchers_notifier.py`)

### Role & Architectural Purpose
The Kanban notifier polls Kanban boards for terminal card events (`completed`, `blocked`, `review_requested`, etc.) and delivers notifications and wakeups back to the origin chat session where the task was created.

### File & Line References
- **Database subscription logic**: [`hermes_cli/kanban_db_notify.py:1-441`](hermes_cli/kanban_db_notify.py#L1-L441)
  - `_NOTIFY_DELIVERY_MODES`: line 26 (`("notify", "notify+wake", "wake")`)
  - `_SUB_KEY_WHERE`: line 32 (`"WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?"`)
  - `_encode_notify_delivery_metadata` / `_decode_notify_delivery_metadata`: lines 39–65
  - `add_notify_sub`: lines 67–142
  - `list_notify_subs`: lines 166–199
  - `count_notify_subs`: lines 202–254
  - `remove_notify_sub`: lines 255–269
  - `purge_stale_done_notify_subs`: lines 271–309
  - `claim_unseen_events_for_sub`: lines 352–382
  - `advance_notify_cursor`: lines 392–406
  - `record_notify_ping`: lines 408–419
  - `rewind_notify_cursor`: lines 421–436
- **Gateway delivery implementation**: [`gateway/kanban_watchers_notifier.py:1-821`](gateway/kanban_watchers_notifier.py#L1-L821)
  - `TERMINAL_KINDS`: line 47
  - `MAX_SEND_FAILURES`: line 75 (`12`)
  - `_adapter_for_subscription`: lines 162–221
  - `_Collector`: lines 226–365
  - `_KanbanNotification`: lines 491–821
  - `_KanbanNotification._send_event`: lines 675–709
  - `_KanbanNotification._send_pings`: lines 711–746
  - `_KanbanNotification.deliver`: lines 748–821
  - `_KanbanNotification.delivery_failed`: lines 544–550

### How the Notifier Sends Today
1. **Periodic Tick & Collection**:
   The gateway watcher loop periodically triggers `_Collector`, which scans active Kanban boards and invokes `kanban_db_notify.claim_unseen_events_for_sub(conn, ...)`. This uses an immediate SQLite transaction to atomically read unseen events and advance `last_event_id` to the latest event ID.
2. **Adapter Authorization**:
   In `_KanbanNotification.deliver()`, the notifier resolves the platform adapter via:
   ```python
   adapter = await asyncio.to_thread(
       _adapter_for_subscription, self.runner, self.plat, self.sub, self.sub_profile or None
   )
   ```
   This delegates to `runner._authorization_adapter(platform, owner_profile)`, respecting profile boundaries and route assignments.
3. **The Platform Send Invocation (`_send_event`)**:
   Lines 675–709:
   ```python
   async def _send_event(self, ev: Any, msg: str) -> bool:
       from gateway.warning_notifications import present_notification
       sub, adapter = self.sub, self.adapter
       delivery_metadata = sub.get("delivery_metadata")
       metadata: dict[str, Any] = dict(delivery_metadata) if isinstance(delivery_metadata, dict) else {}
       if sub.get("thread_id") and not metadata.get("thread_id"):
           metadata["thread_id"] = sub["thread_id"]
       _send_res = None
       async def send_ping():
           nonlocal _send_res
           _send_res = await adapter.send(sub["chat_id"], msg, metadata=metadata)
       if not await present_notification(send_ping, platform=self.platform_str, diagnostic=diagnostic_event(ev)):
           return False
       if getattr(_send_res, "success", True) is False:
           raise RuntimeError(f"adapter send() reported failure: {getattr(_send_res, 'error', None) or 'unknown error'}")
       ...
       return True
   ```
   - **The Outbox Drain Sender (E-028)**: The direct call `await adapter.send(chat_id, text, metadata=metadata)` is the exact platform send function that the Outbox drain targets.
   - It sends directly to the adapter instance with routing metadata (`thread_id`, `scope_id`, etc.).
   - It validates `_send_res.success is not False`.
4. **Checkpointing & Error Handling**:
   - On successful send, it checkpoints `record_notify_ping(conn, ...)` (lines 735–738).
   - If wake is requested (`notify+wake` or `wake`), it calls `deliver_wake(...)` (lines 665–673).
   - If send or wake raises or fails, `delivery_failed()` is called:
     - Increments failure counter `sub_fail_counts[sub_key]`.
     - If failures `< MAX_SEND_FAILURES` (12 consecutive failures, ~60s), it calls `rewind_notify_cursor(conn, ...)` to restore `last_event_id` to `old_cursor`, ensuring the event is retried on the next tick.
     - If failures `>= 12`, it drops the subscription as a dead chat.

---

## 3. Cron Delivery Queue Anchor (`cron/delivery_queue.py`)

### Role & Architectural Purpose
`cron/delivery_queue.py` implements a durable, profile-local handoff queue for scheduled jobs. A restart-safe cron worker executing in an external subprocess (outside the gateway cgroup) does not own live messaging adapter connections (Telegram, Discord, Slack, etc.). It persists its output into SQLite, and the running gateway drains and transmits it.

### File & Line References
- **Queue implementation**: [`cron/delivery_queue.py:1-428`](cron/delivery_queue.py#L1-L428)
  - `DELIVERY_DB`: line 30
  - `MAX_TERMINAL_DELIVERIES`: line 35 (`1000`)
  - `DEFAULT_DELIVERY_WAIT_TIMEOUT_SECONDS`: line 36 (`300.0`)
  - `queue_path`: lines 77–83
  - `_initialize_schema`: lines 89–137
  - `_connect`: lines 139–151
  - `enqueue`: lines 165–204
  - `get_status`: lines 206–226
  - `claim_next`: lines 228–255
  - `_finish`: lines 257–280
  - `recover_abandoned`: lines 282–326
  - `drain`: lines 328–356
  - `_terminalize_wait_timeout`: lines 359–401
  - `enqueue_and_wait`: lines 403–428
- **Gateway drain integration**:
  - `cron.scheduler.drain_delivery_queue`: [`cron/scheduler.py:1375-1396`](cron/scheduler.py#L1375-L1396)
  - Gateway housekeeping ticks: [`gateway/run.py:4470`](gateway/run.py#L4470) & [`gateway/run.py:4484`](gateway/run.py#L4484)
  - Gateway queue watcher: [`gateway/run_delivery_queue_watch.py:20-76`](gateway/run_delivery_queue_watch.py#L20-L76)

### Database Path & Schema
- **Path**: `queue_path()` returns `get_hermes_home() / "cron" / "deliveries.db"`.
- **Table `deliveries`**:
  - `execution_id TEXT PRIMARY KEY`
  - `job_json TEXT NOT NULL`: Serialized job configuration dict.
  - `content TEXT NOT NULL`: Output message text.
  - `for_failure INTEGER NOT NULL DEFAULT 0`: Flag for failure/incident notifications.
  - `status TEXT NOT NULL CHECK(status IN ('pending','delivering','delivered','failed','unknown','suppressed'))`
  - `owner_process_id TEXT`, `owner_pid INTEGER`, `owner_started_at INTEGER`: Claim ownership and process liveness.
  - `created_at TEXT NOT NULL`, `finished_at TEXT`, `error TEXT`
- **Table `delivery_tombstones`**:
  - `execution_id TEXT PRIMARY KEY`, `terminal_status TEXT NOT NULL`, `finished_at TEXT`
- **Pruning**: `_prune_terminal_unlocked` redacts `job_json` and `content` on terminal rows, keeps up to 1000 rows, and archives excess into `delivery_tombstones`.

### What It Queues
External cron workers call `enqueue_and_wait(execution_id, job, content, for_failure=...)`. The queue stores the raw job definition (`job_json`), the target content (`content`), and the execution identifier (`execution_id`). The worker blocks polling `get_status(execution_id)` until the status transitions to terminal or reaches timeout (300s).

### Who Drains It
1. **The Gateway Drainer**:
   The live gateway process drains the queue via `cron.scheduler.drain_delivery_queue(adapters, loop)` ([`cron/scheduler.py:1375`](cron/scheduler.py#L1375)).
2. **Invocation Drivers**:
   - **Housekeeping loop**: Runs periodically in `gateway/run.py` (lines 4470, 4484).
   - **Instant File Watcher**: `CronDeliveryQueueWatch` in `gateway/run_delivery_queue_watch.py` monitors `deliveries.db` and its WAL file for filesystem modifications across all served profile homes, immediately triggering `drain_delivery_queue` on changes.
3. **Execution Semantics**:
   - `drain(send, limit=20)` executes `recover_abandoned()` first, fencing dead worker claims as `unknown`.
   - Iteratively calls `claim_next()`: moves status from `'pending'` to `'delivering'` stamped with `_PROCESS_ID` and PID.
   - Invokes `send(row["job"], row["content"], bool(row["for_failure"]))`, which delegates to `_deliver_result(...)` in `cron/scheduler_delivery.py`.
   - Calls `_finish(...)` to mark `'delivered'`, `'failed'`, or `'suppressed'`.
   - Calls `settle_quietly(job_id, execution_id)` to update cron execution status in `executions.db`.
   - **At-most-once Invariant**: If a gateway crashes while `status='delivering'`, `recover_abandoned()` transitions it to `unknown` and never retries, prioritizing zero message duplication over duplicate delivery.

---

## 4. Gateway Delivery Router Anchor (`gateway/delivery.py`)

### Role & Architectural Purpose
`gateway/delivery.py` provides message routing, platform dispatch, payload truncation, silence narration filtering, and thread/topic management for cron job outputs and outgoing gateway messages.

### File & Line References
- **Module location**: [`gateway/delivery.py:1-334`](gateway/delivery.py#L1-L334)
- **Constants & Silence Filter**:
  - `MAX_PLATFORM_OUTPUT`: line 22 (`4000`)
  - `_SILENCE_NARRATION`: lines 25–29
  - `_is_silence_narration`: lines 33–36
- **Data Structures**:
  - `PartialDeliveryError`: lines 39–46
  - `DeliveryTransport`: lines 48–64
  - `resolve_delivery_transport`: lines 66–84
  - `DeliveryTarget`: lines 110–150
- **Telegram Private Topic Helper**:
  - `_ensure_named_dm_topic`: lines 153–161
- **Router Implementation (`DeliveryRouter`)**:
  - `__init__`: lines 167–172
  - `deliver`: lines 174–208
  - `_deliver_local`: lines 210–222
  - `_save_full_output`: lines 223–229
  - `_cap_oversized_output`: lines 235–259
  - `_deliver_to_platform`: lines 260–334

### Key Components

#### 1. `DeliveryTarget`
- Encapsulates target destinations parsed from strings such as `"origin"`, `"local"`, `"telegram"`, or `"telegram:123456789:topic_1"`.
- Properties include: `platform: Platform`, `chat_id: Optional[str]`, `thread_id: Optional[str]`, `is_origin: bool`, `is_explicit: bool`, `unknown_platform: Optional[str]`.

#### 2. `DeliveryTransport`
- Encapsulates a resolved adapter and its configuration.
- Method `send(logical_platform, chat_id, content, metadata)`:
  - If relay transport: calls `self.adapter.send_for_platform(logical_platform, chat_id, content, metadata=metadata)`.
  - Otherwise: calls `self.adapter.send(chat_id, content, metadata=metadata)`.

#### 3. `DeliveryRouter`
- `deliver(...)`: Dispatches content across a list of `DeliveryTarget`s. Employs `DeadTargetRegistry` to skip targets previously confirmed unreachable.
- `_cap_oversized_output(...)`: Truncates messages exceeding 4,000 characters for non-chunking adapters, saving the full copy to `~/.hermes/cron/output/` and appending a path footer. Chunking adapters (`splits_long_messages=True`) bypass truncation.
- `_is_silence_narration(...)`: Regex filter preventing empty or silence narrations (e.g., `*(silent)*`, `🔇`, `...`) from triggering infinite bot-to-bot reply loops.
- `_deliver_to_platform(...)`:
  - Resolves transport via `resolve_delivery_transport`.
  - Ensures Telegram named DM topics via `_ensure_named_dm_topic`.
  - Executes send via `transport.send(...)`.
  - If a split chunked send fails after initial chunks land, raises `PartialDeliveryError`.

---

## 5. Comparative Analysis: Notifier Send Path vs. Cron Send Path

The following table and breakdown contrast the delivery mechanics of the Kanban Notifier vs. Cron:

| Dimension | Kanban Notifier Send Path | Cron Durable Queue Send Path |
| :--- | :--- | :--- |
| **Originating Module** | `hermes_cli/kanban_db_notify.py` & `gateway/kanban_watchers_notifier.py` | `cron/delivery_queue.py` & `cron/scheduler_delivery.py` |
| **Database Storage** | Board database (`~/.hermes/kanban/<board>.db`), table `kanban_notify_subs` | Dedicated queue (`~/.hermes/cron/deliveries.db`), table `deliveries` |
| **Delivery Semantics** | **At-least-once** with optimistic claiming and error rewind | **At-most-once** with single claim and abandon fencing |
| **Recovery Strategy** | On error, calls `rewind_notify_cursor` to restore cursor; retries up to 12 times before dropping | On crash/dead owner, `recover_abandoned` sets `status='unknown'`; never re-sends |
| **Platform Sender Function** | **Direct adapter call**: `await adapter.send(chat_id, msg, metadata=metadata)` | **Router dispatch**: `router._deliver_to_platform(...)` calling `transport.send(...)` |
| **Transport Resolution** | Direct lookup via `runner._authorization_adapter(platform, profile)` | Two-phase resolution via `resolve_delivery_transport` + Relay support |
| **Fallback Mechanism** | None (pure adapter send; drops after 12 consecutive failures) | **Standalone HTTP fallback**: If live adapter fails, falls back to direct API requests (`_deliver_standalone`) |
| **Formatting & Truncation** | Simple event formatting (`format_event`); no truncation | Rich headers/footers (`wrap_response`), 4000-char truncation with disk audit save |
| **Media & Artifacts** | Artifact staging uploaded via `runner._deliver_kanban_artifacts` | Media extraction tags parsed from text via `BasePlatformAdapter.extract_media` |
| **Wakeup Integration** | Directly dispatches synthetic turns via `deliver_wake` | Dispatches turns to Bot Chat via `_deliver_to_bot_chat` |
| **Event Loop Model** | Native async execution in gateway asyncio loop | Threaded worker queues via `safe_schedule_threadsafe` to gateway loop |

### Detailed Divergence Analysis

#### 1. Delivery Guarantees & Fault Tolerance
- **Notifier (At-least-once)**: The notifier optimizes for delivery certainty. Kanban task status transitions (e.g. `review_requested`, `blocked`, `completed`) are critical operational checkpoints. If an adapter raises a network exception, the transaction is rewound (`rewind_notify_cursor`) so the subsequent tick retries. Only after 12 consecutive failures (~60s) is the chat considered dead and dropped.
- **Cron (At-most-once)**: Cron optimizes against duplicate spam. If an external cron job produced output and the gateway crashed mid-send, re-sending could duplicate transactional notifications or alerts. Hence, `recover_abandoned` permanently fences uncertain sends to `unknown`.

#### 2. Platform Send Function & Routing Overhead
- **Notifier (Direct, Minimalist)**:
  ```python
  # gateway/kanban_watchers_notifier.py:686
  _send_res = await adapter.send(sub["chat_id"], msg, metadata=metadata)
  ```
  The notifier calls the platform adapter's `send()` method directly. It does not wrap the message in `DeliveryRouter`, does not invoke disk audit saves, and does not perform standalone HTTP fallback. This is the **Outbox drain's sender (E-028)** model: clean, unopinionated message dispatch directly to the authorized adapter.
- **Cron (Layered, Orchestrated)**:
  `cron/scheduler_delivery.py` constructs a full `DeliveryRouter` and `DeliveryTarget`, runs silence filters, formats cron response headers, truncates text at 4,000 characters, writes audit logs to disk, dispatches across gateway threads, and falls back to out-of-process standalone HTTP clients (`_deliver_standalone`) if the adapter is offline.

#### 3. Implications for Superagent Phase 4 Outbox (E-028)
- The Outbox drain must adopt the **direct adapter sender model** exemplified by `gateway/kanban_watchers_notifier.py:686`.
- For resilience against restarts, the Outbox will combine the **durable SQLite queuing** pattern from `cron/delivery_queue.py` and the **flood/backoff recovery mechanics** from `gateway/delivery_ledger.py` (`flood_wait_seconds`, `retry_not_before`), while using `adapter.send(chat_id, msg, metadata=metadata)` as its execution engine.

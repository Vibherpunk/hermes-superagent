# P1.6 Wake Evidence: Person-Wake Investigation in the Dispatcher

Per Decision E-023 / TODO 1.6:
"Person-wake side: only if the dispatcher already has a distinct person-wake spawn path today. Read the dispatcher first; if none exists, implement ONLY the launch side and write the evidence (what you searched, file:line) into notes/p1-6-wake-evidence.md, committed with the change. Do not invent a wake path."

## Search Scope and Methodology
Searched the gateway and kanban dispatch subsystems for any person-wake or human-wake worker spawn mechanisms:
- `gateway/kanban_watchers_dispatcher.py`
- `gateway/kanban_watchers.py`
- `hermes_cli/kanban_db_dispatch.py`
- `gateway/kanban_watchers_notifier.py`
- Search terms: `wake`, `person`, `human`, `person_wake`, `person-wake`, `try_acquire`

## Findings with File:Line Evidence
1. `gateway/kanban_watchers_dispatcher.py`:
   - Lines 467, 483, 493: `SlotPool` defines `try_acquire(kind)` with support for `kind="person_wake"` (from Card 0.4).
   - Lines 121–305: `_KanbanDispatcher` handles only board tick dispatching (`tick_once`, `tick_once_for_board`, `auto_decompose_tick`). No person-wake or wake-triggered spawn path exists in `_KanbanDispatcher`.

2. `hermes_cli/kanban_db_dispatch.py`:
   - 0 occurrences of `wake` or `person` anywhere in the file.
   - Tasks are dispatched strictly via `_dispatch_once_locked` from the `ready` lane (`_lane_rows(conn, "ready")`, line 2387) and `review` lane (`_lane_rows(conn, "review")`, line 2390). There is no distinct person-wake lane or trigger.

3. `gateway/kanban_watchers.py`:
   - Lines 257–332: `_kanban_dispatcher_watcher` executes a periodic tick (`dispatcher.tick_once`), reaping zombies and running per-board dispatch. No wake mechanism or input queue is connected to worker spawning.

4. `gateway/kanban_watchers_notifier.py`:
   - Contains `wake_agent` / `deliver_wake` (e.g. lines 631–675). This is part of the outbound notification system that wakes user/agent chat sessions on completed or review-requested cards via platform adapters (Slack, Telegram, Discord, etc.). It does not spawn kanban workers or interact with worker dispatch.

## Conclusion
The dispatcher today has NO distinct person-wake spawn path. As instructed, we implement ONLY the `launch` side and do not invent a wake path.

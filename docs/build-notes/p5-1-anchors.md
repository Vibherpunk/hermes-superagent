# Superagent P5.1 — Architectural & Implementation Anchors

## Overview
This document records the authoritative code anchors, route patterns, database access mechanics, and frontend component conventions for the Kanban dashboard in the `hermes-agent` repository. Subsequent Superagent Phase 5 cards (P5.3+ backend attention read endpoints and P5.4+ attention view UI) directly build upon these patterns without architectural drift.

---

## (a) Kanban Dashboard Implementation: Code Locations & Paths

The Kanban dashboard is implemented as a bundled dashboard plugin with tight integration into the Hermes web server and dashboard host:

### 1. Plugin Bundle Directory (`plugins/kanban/dashboard/`)
- [`plugins/kanban/dashboard/manifest.json`](plugins/kanban/dashboard/manifest.json):
  Plugin manifest describing metadata and mounting config:
  - `name`: `"kanban"`
  - `label`: `"Kanban"`
  - `description`: `"Multi-agent collaboration board — drag-drop cards across columns, read comment threads, see which profile is running what"`
  - `tab`: `{"path": "/kanban", "position": "after:skills"}`
  - `entry`: `"dist/index.js"` (compiled React bundle)
  - `css`: `"dist/style.css"` (theme-scoped styles)
  - `api`: `"plugin_api.py"` (FastAPI backend routes module)
- [`plugins/kanban/dashboard/plugin_api.py`](plugins/kanban/dashboard/plugin_api.py):
  FastAPI backend implementation (1,847 lines). Implements all REST endpoints and the WebSocket events feed.
- [`plugins/kanban/dashboard/dist/index.js`](plugins/kanban/dashboard/dist/index.js):
  Pre-compiled frontend IIFE bundle that registers the Kanban tab component into the host via `window.__HERMES_PLUGINS__.register("kanban", Component)`.
- [`plugins/kanban/dashboard/dist/style.css`](plugins/kanban/dashboard/dist/style.css):
  Scoped styling for board lanes, task cards, drawer inspect panels, badges, and modals.

### 2. Dashboard Host Web Server Integration (Backend)
- [`hermes_cli/web_server.py`](hermes_cli/web_server.py):
  The core FastAPI server.
  - Lines 414–442: `_plugin_api_runtime_gate` middleware gating plugin requests by profile and disabled status.
  - Line 624: Late-imports `_discover_dashboard_plugins` and `_mount_plugin_api_routes`.
  - Line 848: Calls `_mount_plugin_api_routes()` during startup initialization.
- [`hermes_cli/web_server_dashboard.py`](hermes_cli/web_server_dashboard.py):
  Core discovery, mounting, and security lifecycle:
  - Lines 998–1039: `_mount_hosted_plugin_api(app, plugin, api_file_name)` uses `importlib.util.spec_from_file_location` to dynamically import `plugin_api.py` and includes `mod.router` on the FastAPI app under prefix `/api/plugins/{name}`.
  - Lines 1041–1069: `_mount_plugin_api_routes()` orchestrates plugin API mounting.
  - Lines 1071–1111 & 1122–1149: `_try_hot_mount_plugin_api` and `_PluginApiHotMount` handle fallback route resolution (`/api/plugins/{name}/{path:path}`).
- [`hermes_cli/web_routers/dashboard_ui.py`](hermes_cli/web_routers/dashboard_ui.py):
  Lines 382–408: Serves plugin frontend assets (scripts, styles, icons) under `/dashboard-plugins/...`.
- [`hermes_cli/web_server_plugin_profiles.py`](hermes_cli/web_server_plugin_profiles.py):
  Lines 134–165: Resolves profile-scoped plugin asset and API paths (`plugin_api_path`, `_in_process_api_file`).

### 3. Dashboard Host Frontend Integration (`web/`)
- [`web/src/plugins/registry.ts`](web/src/plugins/registry.ts):
  Client-side registry exposing global entry points `window.__HERMES_PLUGINS__` and `window.__HERMES_PLUGIN_SDK__`.
- [`web/src/plugins/PluginPage.tsx`](web/src/plugins/PluginPage.tsx):
  React wrapper component that subscribes to `onPluginRegistered` via `useSyncExternalStore` and renders the active plugin tab.
- [`web/src/plugins/usePlugins.ts`](web/src/plugins/usePlugins.ts):
  React hook fetching enabled plugins from `/api/dashboard/plugins` and injecting bundle `<script>` and `<link>` elements.
- [`web/src/App.tsx`](web/src/App.tsx):
  Lines 347–367: Allocates dynamic React Router routes for plugins (`path: m.tab.path`, `element: <PluginPage name={m.name} />`).
- [`web/src/plugins/sdk.d.ts`](web/src/plugins/sdk.d.ts):
  Ambient TypeScript declarations for plugin bundles interacting with the host (`HermesPluginSDK`, `FetchJSON`, `AuthedFetch`, `BuildWsUrl`).

### 4. Database & Core Engine Dependencies
- [`hermes_cli/kanban_db.py`](hermes_cli/kanban_db.py): SQLite schema, migrations, and CRUD operations for tasks, links, comments, runs, and events.
- [`hermes_cli/kanban_db_connect.py`](hermes_cli/kanban_db_connect.py): Connection factory and WAL initialization (`kbc.connect(board=...)`).
- [`hermes_cli/kanban_db_boards.py`](hermes_cli/kanban_db_boards.py): Multi-board discovery and metadata management.
- [`hermes_cli/orchestrator_reads.py`](hermes_cli/orchestrator_reads.py): Superagent pure read functions, projections, and `derived_stage(ticket, ctx)`.

---

## (b) Route Registration Pattern

### 1. In-Plugin Route Definition
- **Router instantiation**:
  In [`plugins/kanban/dashboard/plugin_api.py:43`](plugins/kanban/dashboard/plugin_api.py#L43):
  ```python
  router = APIRouter()
  ```
- **Endpoint registration syntax**:
  Endpoints attach directly to `router` using standard FastAPI decorators:
  - `@router.get("/board")` ([line 359](plugins/kanban/dashboard/plugin_api.py#L359))
  - `@router.get("/tasks/{task_id}")` ([line 379](plugins/kanban/dashboard/plugin_api.py#L379))
  - `@router.post("/tasks")` ([line 435](plugins/kanban/dashboard/plugin_api.py#L435))
  - `@router.patch("/tasks/{task_id}")` ([line 692](plugins/kanban/dashboard/plugin_api.py#L692))
  - `@router.delete("/tasks/{task_id}")` ([line 719](plugins/kanban/dashboard/plugin_api.py#L719))
  - `@router.post("/tasks/{task_id}/comments")` ([line 802](plugins/kanban/dashboard/plugin_api.py#L802))
  - `@router.get("/diagnostics")` ([line 882](plugins/kanban/dashboard/plugin_api.py#L882))
  - `@router.get("/workers/active")` ([line 920](plugins/kanban/dashboard/plugin_api.py#L920))
  - `@router.get("/runs/{run_id}")` ([line 936](plugins/kanban/dashboard/plugin_api.py#L936))
  - `@router.get("/orchestration")` ([line 1633](plugins/kanban/dashboard/plugin_api.py#L1633))
  - `@router.put("/orchestration")` ([line 1675](plugins/kanban/dashboard/plugin_api.py#L1675))
  - `@router.websocket("/events")` ([line 1802](plugins/kanban/dashboard/plugin_api.py#L1802))

### 2. Host Application Mounting
- In [`hermes_cli/web_server_dashboard.py:1022-1025`](hermes_cli/web_server_dashboard.py#L1022-L1025):
  ```python
  sub_router = getattr(mod, "router", None)
  prefix = f"/api/plugins/{name}"
  app.include_router(sub_router, prefix=prefix)
  ```
- Because the plugin name is `"kanban"`, every route registered with `@router.get("/foo")` automatically mounts at `/api/plugins/kanban/foo`.

---

## (c) API Handler Pattern: Concrete Example

A representative API handler is `get_task` at [`plugins/kanban/dashboard/plugin_api.py:379-410`](plugins/kanban/dashboard/plugin_api.py#L379-L410):

```python
@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    board: Optional[str] = Query(None),
    run_state_type: Optional[str] = Query(None, description="With run_state_name: filter runs by column 'status' or 'outcome'"),
    run_state_name: Optional[str] = Query(None, description="With run_state_type: exact value for that run column")):
    with _board_conn(board) as (board, conn):
        if (run_state_type is None) ^ (run_state_name is None):
            raise HTTPException(status_code=400, detail="run_state_type and run_state_name must be passed together or omitted")
        if run_state_type not in (None, "status", "outcome"):
            raise HTTPException(status_code=400, detail="run_state_type must be 'status' or 'outcome'")
        task = _require_task(conn, task_id)
        # Drawer returns the FULL summary (cards on /board carry a 200-char preview).
        task_d = _task_dict(
            task, latest_summary=kanban_db.latest_summary(conn, task_id),
            current_run_started_at=kanban_db.current_run_started_ats(conn, [task_id]).get(task_id))
        links = _links_for(conn, task_id)
        child_summaries = kanban_db.latest_summaries(conn, links["children"])
        children = filter(None, (kanban_db.get_task(conn, cid) for cid in links["children"]))
        _attach_diagnostics(task_d, _compute_task_diagnostics(conn, task_ids=[task_id]).get(task_id) or [])
        return {
            "task": task_d,
            "comments": [asdict(c) for c in kanban_db.list_comments(conn, task_id)],
            "events": [asdict(e) for e in kanban_db.list_events(conn, task_id)],
            "attachments": [_attachment_dict(a) for a in kanban_db.list_attachments(conn, task_id)],
            "links": links,
            "link_tasks": _link_tasks(conn, links),
            "child_results": [
                {"id": c.id, "title": c.title, "status": c.status, "latest_summary": child_summaries.get(c.id), "result": c.result}
                for c in children],
            "runs": [asdict(r) for r in kanban_db.list_runs(conn, task_id, state_type=run_state_type, state_name=run_state_name)]}
```

### Key Pattern Characteristics
1. **Board Resolution & Connection Scoping**:
   Uses `with _board_conn(board) as (board, conn):` ([lines 101–106](plugins/kanban/dashboard/plugin_api.py#L101-L106)). This normalizes the board slug, ensures non-existent or archived boards return HTTP 404, and automatically cleans up the `sqlite3.Connection` via `contextlib.closing`.
2. **Defensive Validation**:
   Validates query arguments explicitly and raises `fastapi.HTTPException(status_code=400, detail=...)`.
3. **Existence Enforcers**:
   Calls helpers like `_require_task(conn, task_id)` ([line 132](plugins/kanban/dashboard/plugin_api.py#L132)), which raises HTTP 404 if the row does not exist.
4. **Data Marshalling**:
   Converts dataclasses to plain dicts using `dataclasses.asdict()` or custom serializers (`_task_dict`, `_attachment_dict`).
5. **Read Coalescing (for Board Listing)**:
   For high-traffic or expensive read endpoints like `/board`, the handler function is wrapped in `coalesced_read(get_board)` ([lines 356–374](plugins/kanban/dashboard/plugin_api.py#L356-L374)) from `hermes_cli.web_read_coalescing` to collapse concurrent in-flight requests into a single database read.

---

## (d) How the Dashboard Reads the Kanban Database

The existing Kanban dashboard uses a **hybrid read strategy**, combining high-level `kanban_db` library functions with raw direct SQL queries on the open connection:

### 1. High-Level `kanban_db` Module Reads
- `kanban_db.get_task(conn, task_id)`: Line 132 (in `_require_task`)
- `kanban_db.list_tasks(conn, ...)`: Line 299
- `kanban_db.latest_summary(conn, task_id)`: Line 393
- `kanban_db.latest_summaries(conn, [t.id for t in tasks])`: Lines 322, 396
- `kanban_db.current_run_started_ats(conn, [t.id for t in tasks])`: Lines 325, 394
- `kanban_db.list_comments(conn, task_id)`: Line 401
- `kanban_db.list_events(conn, task_id)`: Line 402
- `kanban_db.list_attachments(conn, task_id)`: Line 403
- `kanban_db.list_runs(conn, task_id, ...)`: Line 409
- `kanban_db.known_assignees(conn)`: Line 350

### 2. Direct Inline SQL Queries on `conn`
The dashboard executes direct raw SQL queries on the `sqlite3.Connection` for bulk aggregations and rollups to prevent N+1 overhead:
- Link counts: `conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()` ([line 304](plugins/kanban/dashboard/plugin_api.py#L304))
- Comment counts: `conn.execute("SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id")` ([line 307](plugins/kanban/dashboard/plugin_api.py#L307))
- Child completion progress: `conn.execute("SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l JOIN tasks t ON t.id = l.child_id").fetchall()` ([lines 310–311](plugins/kanban/dashboard/plugin_api.py#L310-L311))
- Event watermark: `conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]` ([line 316](plugins/kanban/dashboard/plugin_api.py#L316))
- Distinct tenants: `conn.execute("SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant")` ([line 343](plugins/kanban/dashboard/plugin_api.py#L343))
- Relational child/parent IDs: `conn.execute(f"SELECT {col} FROM task_links WHERE {other} = ? ORDER BY {col}", (task_id,))` ([line 272](plugins/kanban/dashboard/plugin_api.py#L272))
- Event tailing in `/events` WebSocket: `conn.execute("SELECT MAX(id) AS m FROM task_events").fetchone()` ([line 1827](plugins/kanban/dashboard/plugin_api.py#L1827))

### 3. CRITICAL INVARIANT: The E-032 Read Boundary
> [!IMPORTANT]
> **Per E-032, the new Attention View reads ONLY through `hermes_cli/orchestrator_reads.py`.**
> Even though the legacy Kanban dashboard elsewhere executes direct SQL queries and joins across SQLite tables, the Superagent attention view must NOT execute its own custom SQL queries or bypass the `orchestrator_reads` module boundary. All derived stage calculations, ticket aggregations, and attention queues MUST be resolved strictly via pure projection functions in `hermes_cli/orchestrator_reads.py` (such as `derived_stage(ticket, ctx)`).

---

## (e) How a New View/Route is Added (Files Touched by 5.3 / 5.4 Builders)

Following the established patterns, a new Attention View and API route spans the following exact files:

### Phase 5.3: Backend Read Operations & API Endpoint
1. [`hermes_cli/orchestrator_reads.py`](hermes_cli/orchestrator_reads.py):
   - Add database-backed reader functions (e.g. `get_attention_tickets(conn, board=...)` or `read_attention_view(conn, ...)`).
   - Query the `tasks`, `sa_mandates`, `sa_decisions`, and `sa_outbox` tables, assemble the `{ticket, ctx}` dictionaries, and project tickets into their lifecycle stage via `derived_stage(ticket, ctx)`.
2. [`plugins/kanban/dashboard/plugin_api.py`](plugins/kanban/dashboard/plugin_api.py):
   - Import `hermes_cli.orchestrator_reads`.
   - Register the endpoint on `router`, e.g.:
     ```python
     @router.get("/attention")
     def get_attention(board: Optional[str] = _BOARD_Q):
         with _board_conn(board) as (board, conn):
             return orchestrator_reads.read_attention_view(conn, board=board)
     ```
   - Matches the existing `/api/plugins/kanban/...` mount prefix, parameter conventions (`_BOARD_Q`), and connection scoping (`_board_conn`).
3. [`tests/plugins/test_kanban_dashboard_plugin.py`](tests/plugins/test_kanban_dashboard_plugin.py) and [`tests/superagent/test_attention_reads.py`](tests/superagent/):
   - Unit and REST contract tests asserting endpoint response shapes, 200/404 handling, stage derivations, and E-032 invariant compliance.

### Phase 5.4: Frontend Attention View UI
1. **If Integrated into the Kanban Plugin Bundle**:
   - [`plugins/kanban/dashboard/manifest.json`](plugins/kanban/dashboard/manifest.json):
     Update manifest if exposing a dedicated tab path or view descriptor.
   - Frontend bundle source:
     Add the Attention View component that queries `/api/plugins/kanban/attention` using `sdk.fetchJSON`, categorizing cards by their derived stage (`person/delivery`, `person/answer`, `person/review`, `person/blocked`, `working`, `queued`, `closed`, `idle`).
   - [`plugins/kanban/dashboard/dist/style.css`](plugins/kanban/dashboard/dist/style.css):
     Add scoped CSS rules under `.hermes-kanban-attention*` referencing CSS variables for consistent theme inheritance.
2. **If Integrated into the Core Dashboard Shell (`web/`)**:
   - `web/src/pages/AttentionPage.tsx`: New top-level page component following the neighboring page conventions.
   - [`web/src/lib/api.ts`](web/src/lib/api.ts): Add API client helper method `getAttentionView()`.
   - [`web/src/App.tsx`](web/src/App.tsx): Add lazy import, navigation route entry, and sidebar icon.
   - `web/src/i18n/en.ts`: Add stage labels and descriptions.

---

## (f) Frontend Component & Style Patterns

### 1. Neighboring Core Dashboard View: `SessionsPage.tsx`
The primary reference implementation for complex, multi-pane task and session management is [`web/src/pages/SessionsPage.tsx`](web/src/pages/SessionsPage.tsx):
- **Page Header & Breadcrumbs**:
  Uses `usePageHeader()` ([line 83](web/src/pages/SessionsPage.tsx#L83)) to register page title, breadcrumbs, and primary action buttons directly into the host navigation header.
- **Design System Components (`@nous-research/ui`)**:
  - `Badge` ([line 67](web/src/pages/SessionsPage.tsx#L67)): Renders status pills for active, blocked, error, or closed entities.
  - `Card`, `CardHeader`, `CardTitle`, `CardContent` ([line 68](web/src/pages/SessionsPage.tsx#L68)): Container primitives for panels, queues, and task lists.
  - `Button`, `Spinner`, `Toast`, `Segmented`, `ListItem`, `Input`: Standard atomic interaction components.
  - `DeleteConfirmDialog` ([line 69](web/src/pages/SessionsPage.tsx#L69)): Standard confirmation modal.
- **Icons**:
  Uses `lucide-react` icons ([lines 10–31](web/src/pages/SessionsPage.tsx#L10-L31): `AlertTriangle`, `CheckCircle2`, `Clock`, `ListFilter`, `Search`, `Trash2`, `Play`, `Archive`).
- **Styling Architecture**:
  Tailwind CSS combined with the `cn(...)` utility helper from `@/lib/utils` for conditional class application.

### 2. Secondary Neighbor: `CronPage.tsx`
[`web/src/pages/CronPage.tsx`](web/src/pages/CronPage.tsx) demonstrates:
- Segmented tabs (`Segmented` component) for toggling between views.
- Status badges showing schedule health and overdue warnings (`cronNextRunOverdueMs`).
- Card grids with slide-over drawers/modals for detailed configuration.

### 3. Kanban Plugin Styling Pattern
In [`plugins/kanban/dashboard/dist/style.css`](plugins/kanban/dashboard/dist/style.css):
- **Namespace Isolation**: All selectors are strictly prefixed with `.hermes-kanban*` or `.hermes-kanban-drawer*`.
- **Theme Variables**: No hardcoded color values. Every color references theme CSS variables (e.g. `var(--color-bg)`, `var(--color-border)`) and `color-mix(currentColor ...)` so that dark, light, and custom user themes reskin the board seamlessly.
- **Code Block Reset**: Lines 34–40 enforce an explicit transparent reset (`background: transparent !important; color: inherit;`) on `<code>` and `<pre>` elements to prevent global theme styling from corrupting metadata and event payloads.
- **Drawer Panels**: Slide-out drawer overlays (`.hermes-kanban-drawer`) providing tabbed access to summary, comment threads, run history, and linked task graphs.

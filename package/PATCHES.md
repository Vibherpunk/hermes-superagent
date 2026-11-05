# Core patches — seam map and apply order

This package is a **core patchset with a file-copy payload** (the
`payload/` tree). It is not a plugin and is not catalog-listable as one;
see README-PACKAGE.md for the honest framing and the full list of
behavior changes that apply even with the layer disabled.

Each patch is the verbatim `git diff` of its file set, baseline
(`pre-superagent-cutover-20261103`) → head (`bde1a3f598c1455da79156fe2356372e87b276b6`).
The file sets are disjoint, so each patch is separable and individually
appliable; the order below is the order `install.sh` applies them (config
first, tests last). Verified: all 11 apply in sequence onto a pristine
baseline checkout, and the assembled tree (baseline + patches + `payload/` +
`tests/` payloads) is byte-identical to the integrated tree on every packaged
file (build evidence notes and the human `pilot_script.md` runbook excluded —
see README-PACKAGE.md).

Apply manually with: `patch -p1 < core-patches/NN-*.patch` from the checkout root.

## 01-config.patch — Super Agent configuration block (4 files)

- `cli-config.yaml.example` — documented `superagent:` example block
  (`enabled: false`, `accept_mode: observe`, slots, models, rules, GitHub, sources).
- `hermes_cli/config_defaults.py` — `DEFAULT_CONFIG["superagent"]`
  (`enabled: False`, `accept_mode: "observe"`, timeouts, slots, models,
  rules, GitHub, VibeHard, sources, passthrough), plus the board-owned
  `kanban:` block added by the gates train: `gates`, `assignment`, and
  `escalation` sections, each with `"enabled": False` by default, read
  only through `hermes_cli.kanban_board_config.load_board_config`.
  `seats` ships with argon / opus / cheap model+provider strings;
  `sa_rule_map` and `default_specs` ship empty.
- `hermes_cli/config_value_checks.py` — `_validate_superagent` (accept_mode
  in {off, observe, auto}, slot bounds, profile/passthrough/vibehard checks).
- `hermes_cli/config.py` — wires `_validate_superagent` into
  `validate_config_structure`.

Why first: it defines the config block the rest of the layer reads.
Note the limit of that switch: several later seams are **not** gated on
`superagent.enabled` — the kanban notifier reroute (03), the cron
delivery reroute (04), and the kanban worker toolset narrowing (05) all
take effect at install time, enabled or not. See README-PACKAGE.md,
"What 'disabled' does NOT mean".

## 02-inbound-hook.patch — inbound trigger intake (1 file)

- `gateway/run_inbound.py` (+200, including the surfaces-train
  wake-category forwarding) — the inbound hook: inbound messages are
  offered to the Super Agent trigger intake path. Intake is gated by the
  `superagent.pilot_surfaces` config key: a trigger is recorded only when
  the message's surface is listed there. That key has no default, appears
  in no example config, and is not validated — with `enabled=true` alone,
  inbound intake records nothing, silently.

## 03-dispatcher-kanban.patch — dispatcher slot wiring + kanban schema/hooks (8 files)

- `gateway/kanban_watchers.py`, `gateway/kanban_watchers_dispatcher.py`
  (+345, incl. the periodic tick wiring that calls the source pollers),
  `gateway/kanban_watchers_notifier.py` — dispatcher and watcher seams that
  route Super Agent work through the existing kanban worker slots. The
  notifier seam also rewrites `_send_event`: kanban notifications are
  enqueued into the Super Agent outbox instead of being sent directly,
  **unconditionally** — there is no `enabled` check on this path (see
  README-PACKAGE.md for the consequences, including the 100-word IM cap
  and the concurrent-drain double-send risk in the outbox drain).
- `gateway/session_kanban.py` — kanban worker policy now subtracts the
  deny toolsets (see 05) and records them as disabled toolsets.
- `hermes_cli/kanban_db.py` — the `sa_*` schema block (the schema's single
  source of truth; the patched code applies it on DB connect — there is
  no standalone migration file in this package),
  `tasks.mandate_id` / `tasks.ticket_kind` in fresh-DB definitions, and the
  PR-following hooks in `add_comment` / run-report paths (failures are
  swallowed to a debug log so a hook failure never breaks kanban).
- `hermes_cli/kanban_db_connect.py` — `_LATER_TASK_COLUMNS` gains
  `mandate_id` / `ticket_kind`, so pre-existing databases get the columns
  via the existing add-column path.
- `hermes_cli/kanban_db_dispatch.py` — slot-wiring changes in `dispatch_once`
  / `_dispatch_lane_task` (Super Agent lane handling, per-profile caps,
  review-lane reservation), plus the gates train's creation-time
  assignment hook and the punch-list train's diagnosed-block auto-resume
  (`auto_resume_diagnosed_blocks`, at most once per task). Note: this file's diff also removes 96 blank
  lines and 38 comment-only lines (cosmetic churn from the build, plus 9
  more comment-only removals in the notifier and kanban_db seams of the
  same patch); the patch keeps them verbatim so it applies cleanly —
  they carry no semantic change.
- `hermes_cli/kanban_db_notify.py` — `enqueue_notify_ping`: outbox enqueue
  + ping checkpoint in one transaction.

## 04-cron-delivery.patch — periodic outbox delivery (3 files)

- `cron/delivery_queue.py` — enqueue/status helpers for the delivery queue.
  Enqueueing an agent-originated result writes **both** a `deliveries`
  row and an outbox copy; only the cron drain settles both, so a result
  sent first by the gateway outbox drain can be delivered a second time
  by the legacy drain (timing-dependent duplicate — see
  README-PACKAGE.md).
- `cron/scheduler.py` — `drain_delivery_queue` periodic drain.
- `cron/scheduler_delivery.py` — delivery-outcome handling in `_deliver_result`.
  Agent-originated results are rerouted into the kanban outbox and the
  normal delivery is skipped, **unconditionally** (no `enabled` check,
  no direct-send fallback if the enqueue fails). Delivery therefore
  depends on the kanban DB and on a drain loop running.

## 05-toolsets.patch — worker toolset gating (4 files)

- `tools/delegate_tool_toolsets.py` — `DELEGATE_BLOCKED_TOOLS` /
  `kanban_worker_deny_toolsets()`: toolsets a kanban worker must not receive.
  This narrowing (8 toolsets) applies to every kanban worker after
  install, whether the layer is enabled or not.
- `tools/kanban_tools.py`, `tools/kanban_tools_schemas.py` — kanban link
  tool + `KANBAN_LINK_SCHEMA`, plus the gates train's worker-side
  contract: `kanban_complete` refuses with a fix-and-retry message when
  a gate check fails (terminal, run closed, when the retry budget is
  exhausted) and `kanban_block` now records the diagnosis + next step
  the auto-resume rule reads.
- `toolsets.py` — one line: `kanban_handback` joins the core tool list.

## 06-kanban-plugin-api.patch — dashboard plugin API seam (1 file)

- `plugins/kanban/dashboard/plugin_api.py` (+155) — orchestration settings
  endpoint (`set_orchestration_settings`) backing the panel, plus the
  Attention action endpoints (ticket stop / retry, decision dismiss,
  pause / resume, trigger reevaluate / requeue, thread unsubscribe).
  This modifies the existing kanban dashboard plugin; it is a seam, not
  a new plugin. Note: the shipped Attention panel page only reads; the
  action endpoints are API-only, with no caller in the panel UI.

## 07-panel-frontend.patch — Attention panel wiring (5 files)

- `web/src/App.tsx` — lazy `/attention` route + nav item.
- `web/src/lib/api.ts` — Attention API client methods.
- `web/src/i18n/en.ts`, `web/src/i18n/types.ts` — Attention strings.
- `web/src/lib/resolve-page-title.ts` — page title for `/attention`.

The page component itself (`web/src/pages/AttentionPage.tsx`) is NOT in this
patch: it ships as a payload module under `payload/` and is copied by
`install.sh` before the patches run. The seam is only reachable through this
patch's route; the page file alone does nothing. The nav entry and route
are registered unconditionally, while the Attention API returns 404 when
the layer is disabled. Panel assets must be rebuilt
after patching (see README-PACKAGE.md).

## 08-tests.patch — updates to pre-existing tests (9 files)

- `tests/gateway/test_kanban_*` (8 files) and
  `tests/hermes_cli/test_kanban_worker_spawn_toolsets.py` — existing kanban
  tests updated for the new dispatcher/notifier/toolset behavior. New tests
  ship separately under `tests/` (49 files) rather than as a patch.

## 09-surfaces-gateway.patch — wake chain + API-door admission markers (5 files)

- `gateway/platforms/api_server.py` — the wake notification category is
  forwarded only when explicitly supplied (the `None` sentinel
  distinguishes "not supplied" from a supplied `"result"`); an
  unconditional forward would stamp the wake marker on every API turn.
- `gateway/platforms/api_server_openai_routes.py` — any supplied
  `hermes_notification_category` value passes through (not only
  `"diagnostic"`), so a wake self-post marks itself with its real
  category. A client setting this field only excludes its own turn.
- `gateway/session_api_turn.py` — the wake category rides the admission
  as the `wake_notification` marker (allowlisted in `check_api_turn`).
- `gateway/session_ingress.py` — a real user turn through the
  authenticated API door (an `api_turn_v1` admission carrying no wake
  marker) sets the Super Agent user-turn flag at drain time; wakes,
  notification self-posts, and automation payloads never satisfy it.
- `gateway/wake.py` — every wake self-post self-identifies with its
  real notification category, `"result"` included. Inert when the layer
  is off (no consumer acts on the field without it).

These five files were unmodified between the baseline and the previous
package head, so this patch's whole content is surfaces-train work.

## 10-surfaces-kanban.patch — gates + assignment seams in kanban (3 files)

- `hermes_cli/kanban.py` — CLI creation path: `--gate-spec` /
  `--gate-check` (mutually exclusive), `--work-class`,
  `--reasoning-effort`, `--gate-bypass-reason`; the assignment is
  precomputed before `create_task` opens its transaction, and only
  when the board config enables the feature.
- `hermes_cli/kanban_db_context.py` — gate evidence in the worker
  context: the most recent 2 `gate_failed` / `gate_env_failed` events,
  output tails capped at 3,000 chars, under a fixed heading.
- `hermes_cli/kanban_parser.py` — the CLI argument definitions for the
  gate/assignment surface above, plus the `verdict file` and
  `set-spend-ceiling` commands.

These three files were unmodified between the baseline and the previous
package head, so this patch's whole content is gates-train work. The
new payload modules that this seam calls (`hermes_cli/kanban_gates.py`,
`kanban_assignment.py`, `kanban_board_config.py`, `kanban_escalation.py`)
ship under `payload/`; the seam is inert at the shipped defaults
(all three features `enabled: false`).

## 11-surfaces-tui.patch — local-turn candidacy in the TUI prompt path (2 files)

- `tui_gateway/methods_prompt.py` — Super Agent candidacy for a local
  (desktop/TUI) turn is computed once, after turn-author / hosted-submit
  resolution, and recorded only at accepted-dispatch points; refusals
  before those points record nothing. An owner-forwarded submit records
  exactly once, on the owner side.
- `tui_gateway/session_compression.py` — the prompt-queue snapshot
  helpers the candidacy check relies on (envelope identity held by
  object, not recycled `id()`).

These two files were unmodified between the baseline and the previous
package head, so this patch's whole content is surfaces-train work. The
recording itself lives in the new payload module
`hermes_cli/superagent_inbound.py` (`record_local_turn`), armed only
when the layer is enabled and `"local"` is a pilot surface.

## Seams that could NOT be made separable

None. Every modified file fell into exactly one seam above, the seam file
sets are disjoint, and all 11 patches apply in order onto pristine baseline
(verified at package build time). The two judgment calls, both documented
rather than forced:

- `web/src/pages/AttentionPage.tsx` is a new file, but it is only reachable
  via the 07 route seam — so it ships as a payload file (a unit file)
  with a documented dependency on 07. It is not a standalone plugin, and
  this package is a core patchset, not a plugin, throughout.
- `hermes_cli/kanban_db_dispatch.py` carries cosmetic comment/blank-line
  removals alongside the real slot-wiring change; the patch is kept verbatim
  rather than hand-split, because splitting it would risk a patch that no
  longer matches the verified-live tree.

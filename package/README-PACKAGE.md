# Hermes Super Agent — core patchset package

**What this is, honestly:** a **core patchset with a file-copy payload**.
It is **not a plugin**. It is not a Hermes dashboard plugin, it is not a
directory plugin (there is no `register(ctx)` discovery), and it is **not
catalog-listable** as a plugin under the Hermes plugin-catalog rules.
Payload modules are imported by the core patches or by payload modules
the patches import; removing the payload files without also reverting
the patches breaks core. The upstream
path for this work is the enabling-hook PR plan (small seams proposed to
Nous Research so a future version could be a real plugin), not catalog
listing of this patchset.

Version of record: branch `main` @ `bde1a3f598c1455da79156fe2356372e87b276b6`,
diffed against baseline branch `pre-superagent-cutover-20261103` @
`845b0755edea89d07ad6c62ba3e0927be8cd1444` (130 files, +39,682/−337).
This is the 2026-11-04 update of the 2026-11-03 package (previous head
`ea6fe43cdca3d1c206e23f47f61f65728e7fc6ef`); the trains since then changed
43 files (+12,359/−268). The packaged version is the version running live,
minus the one punch-list item that was never built: the 5.6 screenshots
(cosmetic, no code). The 5.5 Router prompt asset, deferred in the first
package, is built in this update and ships as `payload/prompts/superagent/router.md`.
Everything else in the live stack is in.

## What changed in this update (three trains, verified against the live diff)

**Punch-list train.** (a) Tick eval drain (P1a): the periodic tick now
drains the eval queue itself — up to 3 queued batches per tick, evaluated
and (outside observe mode) accepted without waiting for a Router turn.
The drain is skipped while paused, and while paused or in observe mode
nothing is accepted. (b) Blocked-card auto-resume (P2): a `blocked` card
whose latest `blocked` event carries an in-mandate diagnosis + next step
is re-dispatched once, automatically, with the diagnosis attached to the
new run; a second block belongs to the person. Cards blocked for
`needs_input` or `dependency` are never auto-resumed, and a card that
already has an `auto_resumed` event is never resumed again. (c) Every
accepted ticket's card body now carries the worker sign-off contract
up front: a run must end with a terminal kanban call (`kanban_complete`,
`kanban_block`, or `kanban_request_review`) — a run that ends without
one is recorded as a failure. (d) A mandate's default scope now
includes restore-to-intended repairs (diagnose + smallest patch back to
documented or last-working behavior, verified, reported after); behavior
changes, schema/data changes, deletions, external sends, money movement,
and irreversible actions are never in default scope. This default is
part of the versioned policy snapshot recorded on every Decision/Mandate.

**Surfaces train.** (a) Wake chain (U2b): every wake self-post now
self-identifies with its real notification category ("result" included),
and the API door carries that marker on the admission (`wake_notification`),
so Super Agent admission can tell a wake apart from a real user turn. A
client setting `hermes_notification_category` only excludes its own turn
from candidacy; it grants no other authority. (b) Local (desktop/TUI)
turn recording: `hermes_cli/superagent_inbound.py` records an accepted
local turn as a trigger candidate, armed only when the layer is enabled
and `"local"` is listed in `superagent.pilot_surfaces`. (c) The TUI
prompt seam records Super Agent candidacy only at accepted-dispatch
points; refusals before those points (hosted, fence, slot, reattach,
lock-in) record nothing. The exact owner-identity rule for local
admission lives in the orchestrator's `_extract_owner_identities` /
`_resolve_owner_identities` and the `owner_admitted` event; this README
does not restate its details beyond what is verified here.

**Gates train.** Three board-owned kanban sub-features, each default
OFF (`kanban.gates.enabled`, `kanban.assignment.enabled`,
`kanban.escalation.enabled` in `DEFAULT_CONFIG`), read only through
`hermes_cli.kanban_board_config.load_board_config` — never through a
completing process's ambient profile — and each enabled only when its
flag is true AND its section carries no validation error. (1) Enforced
script gates: a card may carry a `gate_spec` (version 1, 1–8 checks,
check timeouts summing to at most 3600s) that must pass before the card
can complete; a reviewer verdict is filed sha-bound against the
workspace fingerprint and counts toward escalation only when filed.
An operator bypass exists (`--gate-bypass-reason`, at least 8 chars,
recorded as a `gate_bypassed` event). (2) Deterministic model
assignment: work is classified into a `work_class`
(financial, production, architecture, coding, mechanical, repair,
personal, general) and the seat (model/provider/reasoning) is resolved
at card-creation time, in the accept path and the `kanban` CLI alike;
the work_class → seat-class mapping is a code constant, and only model/
provider strings come from config. (3) Coded escalation: a gate
exhaustion or a persistently refused card (at most
`escalation threshold + 1` × `max_refused_runs` sessions, default 3)
escalates the builder seat (argon ↔ opus, otherwise the configured
frontier primary) under per-ticket token/run ceilings (default
2,000,000 tokens, 12 runs). Gate evidence (the most recent 2
`gate_failed` / `gate_env_failed` events, output tails capped at 3,000
chars) is rendered into the next worker's context. All three features
are inert at the shipped defaults; enabling them is a board-config edit
outside this package.

## What is in the package

| Path | Contents |
|---|---|
| `payload/` | The file-copy payload: 20 modules (16 `hermes_cli/` modules, 2 `tools/` modules, the Attention panel page `web/src/pages/AttentionPage.tsx`, and the Router prompt asset `prompts/superagent/router.md`) plus `manifest.json`, a provenance/inventory note (see below). Paths inside `payload/` are root-relative install paths. The directory was named `plugin/` in earlier drafts of this package; it was renamed because the contents are not a plugin. |
| `core-patches/` | 11 core patches (see `PATCHES.md`). Each applies cleanly onto a pristine baseline checkout, in the documented order. |
| `tests/` | The new test files (root-relative paths), installed by `install.sh` — 49 files. |
| `install.sh` / `remove.sh` | Install and removal scripts (below). |
| `SHA256SUMS` | Hashes of the package payload (regenerate after any edit). |

There is no `docs/` tree in the distributable and no standalone migration
file; see "Schema: one source of truth" below. Build-history documentation
(the executor pack) is kept with the release records, not shipped in this
package.

## Payload manifest — provenance note only, not a plugin manifest

`payload/manifest.json` is this package's own inventory format. Hermes's
own manifest format (`plugins/<name>/dashboard/manifest.json`) covers
dashboard plugins; this patchset is not one. The manifest lists the
payload modules, the core patches in apply order, the shipping defaults,
and the version of record. It is documentation: `install.sh` copies the
`payload/` tree wholesale and does not consult the manifest, and
`remove.sh` rebuilds the payload list from the tree, not from the
manifest. Do not treat it as a manifest an installer honors.

## Install

```sh
sh install.sh /path/to/hermes-checkout
```

What it does, in order: (1) takes a pristine backup of every file the
patches touch into `<target>/.superagent-pkg-backup/` (first run only),
including a backup of any pre-existing file that sits at a payload path
and is about to be overwritten; (2) copies the `payload/` and `tests/`
trees; (3) applies the 11 core patches in order, skipping any already
applied. No database is touched at install time (see "Schema" below).
Any other argument (including the `--db` option offered by earlier drafts
of this package) is rejected with a usage message and exit code 2.

## Remove

```sh
sh remove.sh /path/to/hermes-checkout
```

Deletes the payload files (restoring any pre-existing payload-path file
from the backup taken at install time), restores every patched file from
the pristine backup, and removes the backup directory. Migration tables
are LEFT IN PLACE by design (see below). If patched files cannot be
restored — no backup, and reverse patches fail while patch content is
still present in the tree — `remove.sh` reports the failure and exits
non-zero; it does not claim a clean removal it did not achieve.
Re-running `remove.sh` after a full removal reports "already removed"
and exits 0.

## Safety model — read this before installing

Install ships the layer's own features DISABLED and never enables them:

- Defaults from the patched config: `superagent.enabled=false`,
  `accept_mode=observe`. `accept_mode=auto` is the only acting mode and
  is never a default.
- `install.sh` never writes to a Hermes home dir or config file. Enabling
  is a deliberate edit to your own config, outside this package.
- Both scripts REFUSE to run against the live install path
  `~/.hermes/hermes-agent`, the live home dir `~/.hermes`, or **anything
  inside** `~/.hermes`. They abort before touching anything. Always
  install into a separate checkout or worktree.

### What "disabled" does NOT mean — installing changes stock behavior

Earlier drafts of this README claimed that with `enabled=false` the
checkout behaves as stock Hermes. **That claim is false for this
patchset, and it is withdrawn.** The following core changes are applied
by the patches at install time and are **not gated on
`superagent.enabled`**. They are faithful reproductions of the live
build's deliberate design (the Outbox move and the cron migration were
build decisions, reconciled during integration), and they are disclosed
here instead of being silently re-gated in the package, which would
diverge from the version of record:

1. **Kanban notifications are rerouted through the Super Agent outbox,
   unconditionally.** After install, every kanban notification (task
   completed / blocked / review / etc.) is enqueued into the `sa_outbox`
   table instead of being sent synchronously by the notifier, whether
   the layer is enabled or not. Delivery becomes asynchronous and
   depends on an outbox drain loop running.
2. **Cron deliveries are rerouted through the same outbox,
   unconditionally.** Every agent-originated cron job result is enqueued
   into `sa_outbox` and the normal direct delivery is skipped — again,
   enabled or not. Failure
   notifications (`for_failure`) and `no_agent` jobs are exempt.
3. **New kanban-DB dependency, with no fallback.** Because of items 1–2,
   stock notification and cron delivery now depend on the kanban
   database being available and on a drain loop running. If the outbox
   enqueue fails (for example, kanban DB locked or missing), the cron
   result is recorded in `delivery_errors` and the normal delivery is
   still skipped — there is **no direct-send fallback**. A kanban-DB
   outage can therefore lose deliveries that stock Hermes would have
   sent.
4. **The 100-word IM cap silently drops long messages.** Outbox rows
   addressed to IM platforms (telegram, discord, slack, whatsapp,
   signal, and similar) whose body exceeds 100 words are failed with
   `over_limit`, retried on backoff, and after 3 attempts are never
   delivered, never truncated, and never escalated — the only trace is
   an `sa_meta` counter. Because of items 1–2 this applies to ordinary
   kanban notifications and cron results, not just Super-Agent-authored
   replies. Cron results routinely exceed 100 words.
5. **Cron results live in two queues; duplicates are timing-dependent.**
   A cron result enqueued via the delivery queue exists both as a
   `deliveries` row and as an outbox copy. Only the cron drain settles
   both. If the gateway outbox drain sends the outbox copy first, the
   `deliveries` row is still pending and the legacy drain later sends
   the same content a second time. Whether the user receives one copy
   or two is a race between the two periodic drains.
6. **Concurrent outbox drains can double-send.** The outbox drain has
   no atomic claim: two drains (the gateway drain and the cron drain,
   or two gateway processes) can select the same due row and both send
   it. The stale-recovery check can also flip a row that just started
   sending back to due, so a second drain may send a message while the
   first send is still in flight.
7. **Kanban worker toolsets are narrowed, unconditionally.** After
   install, every kanban worker loses 8 toolsets (catalog, clarify,
   cronjob, delegation, desktop_ui, memory, onboarding, project)
   whether the layer is enabled or not, and every agent gains a new
   `kanban_handback` core tool.

### Known limitations of the enabled layer (faithful to the live build)

- **Pause does not shift pending eval deadlines.** Pausing shifts
  deadlines only for triggers in `queued`/`evaluating` states, but
  production code writes eval deadlines onto `pending` triggers. A
  pause therefore does not preserve pending triggers' deadlines; they
  can expire during a pause as if no pause had happened.
- **Enabling the VibeHard source backfills history.** On its first
  poll, the VibeHard source emits one `submitted` trigger for **every
  job in the ledger, including old, completed, and long-dead jobs**.
  Enabling it against a ledger with history produces one trigger per
  historical ledger job on the first tick.
- **Inbound intake is silently dead without `pilot_surfaces`.** The
  inbound hook records a trigger only when the message's surface is
  listed in the `superagent.pilot_surfaces` config key. That key has
  no default, appears in no example config, and is not validated:
  enabling the layer with `superagent.enabled=true` alone records
  zero inbound triggers, with no warning.
- **Legacy `mode` overrides `accept_mode`, and the panel misreports
  it.** If both keys are set, the legacy `mode` key wins in the
  orchestrator (`mode=auto` + `accept_mode=observe` acts as auto),
  while the panel's settings view reports `accept_mode` only. The
  panel can therefore display "observe" while the layer is acting
  as "auto".
- **GitHub notifications cannot be turned off.** When a GitHub
  repository is configured, the notifications scope is always polled
  and can create triggers, even with every documented `receive_all`
  flag set to false.
- **Attention panel actions are API-only.** The panel page only reads
  (overview and ticket detail). The action endpoints (ticket stop /
  retry, decision dismiss, pause / resume, trigger reevaluate /
  requeue, thread unsubscribe) exist in the API but have no caller in
  the shipped panel UI; there is no button for them.
- **The tick now accepts work without a Router turn.** Since this
  update, the periodic tick drains the eval queue (up to 3 batches) and
  accepts decisions itself outside observe mode; in observe mode it
  evaluates but does not accept. A paused layer drains nothing.
- **A blocked card is re-dispatched once, automatically.** Since this
  update, a card blocked with a filed in-mandate diagnosis and next
  step is resumed once with the diagnosis attached; a second block is
  the person's to decide. Cards blocked for `needs_input` or
  `dependency` are never auto-resumed.
- **A mandate's default scope includes restore-to-intended repairs.**
  Since this update, the versioned policy snapshot records
  `restore_to_intended` as a mandate scope default: diagnosis plus the
  smallest patch back to documented or last-working behavior, verified
  and reported after. It never covers behavior changes, schema/data
  changes, deletions, external sends, money movement, or irreversible
  actions; if the intended state is ambiguous, the repair escalates
  instead of patching.

## Schema: one source of truth

The layer's schema is additive-only, verified against the diff:

- 8 new tables: `sa_triggers`, `sa_decisions`, `sa_mandates`, `sa_inputs`,
  `sa_source_cursors`, `sa_outbox`, `sa_meta`, `sa_subscriptions` — all
  `CREATE TABLE IF NOT EXISTS`, plus 4 `IF NOT EXISTS` indexes.
- 2 new nullable columns on the pre-existing `tasks` table (`mandate_id`,
  `ticket_kind`), applied add-if-missing; the `tasks` table definition in
  fresh databases gains the same two columns. No pre-existing column,
  table, or row is modified, and the diff contains no code-level
  `ALTER TABLE`.

Earlier drafts of this package defined this schema three times: a
standalone migration SQL file, the schema string embedded in the patched
`hermes_cli/kanban_db.py`, and an inline copy inside `install.sh`'s
`--db` path. That redundancy is resolved by **dropping the `--db` path
and the standalone migration file**. The single source of truth is now
the patched code itself: on the next DB open after install, the patched
code runs its schema (`executescript` on connect) and the same
add-if-missing column path, so the layer self-migrates. No database is
touched at install time.

- `remove.sh` leaves the `sa_*` tables and the two columns in place.
  They are inert under stock Hermes (nothing reads them) and re-used on
  reinstall. Dropping them would destroy recorded data, so removal never
  drops them.

## Panel rebuild note

The 07-panel-frontend seam patches the panel's sources. The Attention page
appears in the served panel only after the panel assets are rebuilt with the
checkout's normal web build. Build outputs (`dist/`) are not in this package:
they are build artifacts, not part of the git diff.

## What is deliberately NOT in the package

- No live config (`~/.hermes/config.yaml`), no profile data, no keys, no
  live database. The package contains only code, config templates
  (`cli-config.yaml.example` seam), and tests from the git diff.
- No build-history documentation: the executor pack (phases 0–6 reports,
  reviews, scopes, and the build-evidence notes) is kept with the release
  records and in the publication repo's `docs/` tree; it is not part of
  this distributable and is never copied into a target checkout.
- No manual pilot procedure in the test tree: `pilot_script.md` was a
  human runbook, not a test; it was removed from `tests/` and is kept
  with the release records.
- No pre-existing dirty files from the live checkout (unrelated local
  edits: `hermes_cli/models_catalog_static.py`, its `.bak-muse-20261027`
  backup, `hermes_cli/web_server.py.orig`, `plugins/irreversible-action-guard/`,
  and its test). Those are not part of the Super Agent stack and are excluded.
- No 5.6 screenshots (cosmetic). The 5.5 Router prompt asset, deferred in
  the first package, is built and shipped in this update
  (`payload/prompts/superagent/router.md`). See `PACKAGING-NOTES.md` in
  the release delegation directory for the full exclusion list and the
  files that did not fit cleanly.

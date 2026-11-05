# Phase 6 — VibeHard Ticketing Source: SOURCE RECON (supervisor, 2026-11-03 ~19:40 PST)

Verdict: **the surface is machine-readable tonight.** No blocker. All facts below were read first-hand from the code (~/workspace/vibehard-repo on the VM) and from the live store (read-only) on the Mac Studio.

## The store
- VibeHard Agent Edition daemon (`vibehardd`, packages/reasoner-agent/src/daemon/) keeps ALL job state in a SQLite ledger: `<home>/ledger.sqlite` (bun:sqlite, WAL mode, `PRAGMA synchronous = FULL`).
- Home resolution (vibehardd.ts `resolveHome`): explicit arg → `$VIBEHARD_HOME` → `~/.vibehard-agent`.
- The dogfood home is on the build machine (a Mac Studio); its ledger is live. Hermes' gateway runs on the same machine, so the poller reads the file in place — no network, no daemon round-trip.
- Live contents at recon time: over a hundred real jobs, roughly half failed, with the rest spread across complete, awaiting_human, and pending; kinds included build, gate, deploy, and adopted.

## The read path
- **Primary: read-only SQLite** — open the ledger in the dogfood home on the build machine read-only (SQLite URI with `mode=ro`) and poll `jobs` + `audit`. The reader must never write (WAL allows concurrent readers while the daemon writes).
- Alternative surface (not needed for v1): the MCP tools `vibehard_status` / `vibehard_progress` (packages/reasoner-agent/src/bindings/mcp-stdio.ts) forward to the daemon over its local socket via the VDP protocol; `vibehard-agent mcp` processes are running on the Mac. The DB poll is simpler, matches the GitHub Source's poller shape (E-025), and works even when no daemon runner is up.
- Schema (ledger-migrations.ts, live-verified): `jobs(id TEXT PK 'j_<hex>', session_id FK, spec TEXT, rigor, status DEFAULT 'pending', created_at, updated_at, result_path, error, retries, idempotency_key UNIQUE, runner_id, claimed_at, heartbeat_at, pipeline_version, kind DEFAULT 'build', target_dir, progress_revision, output_dir, ...)`. `sessions(id, client, created_at)`. `audit(id, ts, event, detail)` — a dense event stream (10k+ rows; pipeline.*, review_*, job.* events).

## The event set (what the Source emits as Triggers)
Derived from `jobs` state on each poll, cursor = per-job last-seen (status, progress_revision, updated_at) in `sa_source_cursors`:
- **submitted** — a job id appears that the cursor has never seen.
- **state_change** — `status` differs from the cursor's last-seen status (covers →running/gating/verifying, →complete, →suspended, →awaiting_human transitions).
- **failed** — transition into `failed` (called out separately from state_change; `error` text rides along).
- **stuck** — job in a non-terminal working status (pending/claimed/running/gating/verifying) whose `heartbeat_at` (fallback `updated_at`) is older than the configured threshold (default 30 min; the ledger even maintains `idx_jobs_status_heartbeat` for exactly this). Emitted once per job per stuck episode (cursor marks it; clears when the job moves again). At recon time, a small number of pending jobs had been sitting long enough to qualify — genuine stuck candidates the pilot will surface.
- Terminal set: complete, failed. awaiting_human is NOT terminal (it is the daemon's own person-gate — a state_change the Eval should normally route to "needs person" attention, not a Ticket to work).

## The identity Adam's jobs carry
- Every job: `id` (`j_…`), `kind` (build/gate/deploy/adopted), `spec` (the full build spec in markdown — its first heading line is the natural ticket title), `rigor`, `target_dir`, `idempotency_key`, timestamps.
- Submitter identity: `session_id` → `sessions.client`. Live values include `cli`, `Antigravity`, `claude-code`, `probe`, and named per-project fleet agents — i.e., jobs are submitted by Adam and by his other agents on his behalf. The Source records actor = `vibehard:<client>` and thread_key = `vibehard:job-<job_id>`; a job is a thread of one Ticket, mirroring the GitHub PR pattern (`github:pr-<n>`).
- Trust posture: this is a FIRST-PARTY source (Adam's own factory on Adam's own machine) — but per Adam's order the admission rules are OBSERVE-ONLY like the pilots: Eval may judge and form the would-be Ticket; in observe mode nothing is worked, and the worker never acts on VibeHard itself in v1.

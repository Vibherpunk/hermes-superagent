# REVIEW — Phase 2 card 2.11 (evidence card)

Reviewer: claude-sonnet-5-5 (high) via Antigravity bridge. Verdict verbatim:

I'll check the cited file:line references directly.
VERDICT: PASS
SHA: evidence-2.11
FINDINGS:
- **Citation paths (non-blocking):** The packet places `gateway/run_inbound.py` and `tests/gateway/test_superagent_inbound_hook.py` in the `superagent-p4` worktree. Neither is there. Both are in `superagent-p2`.
  - `_hm_maybe_record_superagent_trigger` is at `gateway/run_inbound.py` L1259.
  - The `scope_id` fallback line is at L1290, inside the quoted 1285-1293 range.
  - The test asserts `row["scope"] == "-100987654"` at L148.
  - The packet's claim that this logic was added in Card 2.7 is plausible. The `superagent-p3` worktree has no copy of the hook.
- **Config detail (non-blocking):** The relevant live configuration was verified directly; the exact key, value, and line references are omitted here. A line-number correction in the packet did not affect the finding.
- **Served-profile config (non-blocking):** The served profile's configuration was also verified directly. Its relevant entry appears to be a toolset or plugin entry, not platform credentials. The packet's wording is slightly too strong.
- **Evidence for "NO" (non-blocking):** The answer rests on Telegram's default privacy mode, the Hermes docs and the absence of group traffic in the logs. The bot's actual privacy setting was not read, for example through `getMe` `can_read_all_group_messages`. I found no such flag in the logs. The "NO" is well supported but inferred.
- **Citations that resolve and support the conclusions:**
  - `adapter.py` L1797-1798 (polling) and L3131-3133 (webhook) both pass `allowed_updates=Update.ALL_TYPES`.
  - `_should_process_message` is at L6406-6443 and matches the description.
  - `_should_observe_unmentioned_group_message` is at L6173-6198 (the packet writes "messages", plural). It requires an observe flag and `observe_allowed_chats`.
  - Chat-type mapping at L7109-7110 and `_normalize_chat_type` at L807-814 match.
  - `build_source` at L7131-7135 passes no `scope_id` or `guild_id`.
  - `gateway/session.py` L84 has `scope_id`, and L686-728 `build_session_key` adds `scope_id` only for Slack.
  - `docs/.../telegram.md` L208-234 and L259 match the quoted text.
  - `hermes_cli/superagent_gate.py` in `superagent-p2` returns `inferred` with `via: "admission"` for a listed, non-addressed sender, and `explicit` with `via: "addressed"` when addressed.
  - Gateway log line 23841 shows `Connected to Telegram (polling mode)`. The `set_my_commands` entry for `BotCommandScopeAllGroupChats` is at line 23857.
- **Conclusions follow:** `source.chat_id` resolves to a positive ID for DMs and a negative ID for groups. Unaddressed group cells are inert under privacy mode. BotFather is a human step, and the packet states nothing was changed.
- **Diff scope:** The diff is empty, which fits an evidence card with no code or commit. Nothing changes when `superagent.enabled` is false. The card names no tests.


Filed by supervisor 2026-11-03. Evidence artifact: phase-2/EVIDENCE-p2-2.11-telegram.md.

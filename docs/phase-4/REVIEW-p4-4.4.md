# REVIEW — Phase 4 card 4.4 (integration: Router replies + notifier onto Outbox)

VERDICT: FAIL
SHA: d7586f10e4
FINDINGS:
- tools/orchestrator_tools.py (_deliver_subscription_reply / orchestrator_reply): orchestrator_reply still sends directly via adapter.send(...) instead of only enqueueing via queue_reply. After calling queue_reply(...) (exceptions silently swallowed), _deliver_subscription_reply still invokes adapter.send(...) inline via _run_async(_send_coro()) and immediately transitions the Outbox row to sent or failed via mark_sent / mark_failed. This violates card 4.4 and E-028/E-030: Outbox replies must remain queued to be sent by drain_outbox under per-ticket/conversation FIFO ordering and the 100-word IM cap, rather than sent directly inline.

Reviewer: gemini-4-argon (high) via Antigravity bridge (chatcmpl-7432a544a586, 2026-11-03), verbatim. Sha verified on disk in worktree superagent-int before filing. Fix round dispatched: orchestrator_reply becomes queue_reply-only; the inline send and self-marking are removed.

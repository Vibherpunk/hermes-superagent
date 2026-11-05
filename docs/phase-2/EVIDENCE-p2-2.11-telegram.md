# Evidence Card 2.11: Telegram Receive Check & Privacy Mode Audit

## Summary
The pilot Telegram bot in this Hermes installation does **not** receive unaddressed group messages. While the Hermes Telegram adapter requests `allowed_updates=Update.ALL_TYPES` across both long polling and webhook modes, upstream Telegram Bot API servers enforce **Group Privacy Mode** (enabled by default on all bots via @BotFather). Under privacy mode, Telegram servers drop all ordinary group chatter before delivery, only forwarding messages that explicitly address the bot (@mentions, direct quote-replies, or slash commands). Consequently, all unaddressed-group cells in the Superagent admission table (triggers evaluated as `strength="inferred"` via `via="admission"`) remain completely inert for Telegram groups until a human operator manually changes the BotFather setting or promotes the bot to group admin. In practice, trigger `scope` keys for Telegram resolve to `source.chat_id`: positive numeric strings (e.g. `"123456789"`) for DMs and negative numeric strings (e.g. `"-1001234567890"`) for group/supergroup chats.

---

## 1. Does the Pilot Telegram Bot Setup Receive Unaddressed Group Messages?

**Finding: NO.** The bot does not receive unaddressed group messages.

### Evidence & Analysis:

1. **Telegram Adapter Subscription (`getUpdates` and Webhook)**:
   - **Polling Mode**: In [plugins/platforms/telegram/adapter.py](plugins/platforms/telegram/adapter.py#L1797-L1798), the updater initializes polling with:
     ```python
     app.updater.start_polling(
         allowed_updates=Update.ALL_TYPES, drop_pending_updates=drop_pending_updates, error_callback=_generation_error_callback
     )
     ```
   - **Webhook Mode**: In [plugins/platforms/telegram/adapter.py](plugins/platforms/telegram/adapter.py#L3131-L3133), the webhook starts with:
     ```python
     await self._app.updater.start_webhook(
         listen=webhook_host, port=webhook_port, url_path=webhook_path, webhook_url=webhook_url,
         secret_token=webhook_secret, allowed_updates=Update.ALL_TYPES,
     )
     ```
   - Although the adapter passes `allowed_updates=Update.ALL_TYPES`, the Telegram Bot API server upstream enforces **Group Privacy Mode**.

2. **Telegram Group Privacy Mode Implication**:
   - Per Telegram Bot API specification and documented in Hermes's official guide ([website/docs/user-guide/messaging/telegram.md](website/docs/user-guide/messaging/telegram.md#L208-L234)):
     > "Telegram bots have a **privacy mode** that is **enabled by default**...
     > **With privacy mode ON**, your bot can only see:
     > - Messages that start with a `/` command
     > - Replies directly to the bot's own messages
     > - Service messages (member joins/leaves, pinned messages, etc.)
     > - Messages in channels where the bot is an admin"
   - As emphasized in [website/docs/user-guide/messaging/telegram.md](website/docs/user-guide/messaging/telegram.md#L259):
     > "This requires Telegram to deliver ordinary group messages to the gateway, so disable BotFather privacy mode or promote the bot to group admin as described above."

3. **In-Repo Adapter Group Processing Rules**:
   - In [plugins/platforms/telegram/adapter.py](plugins/platforms/telegram/adapter.py#L6406-L6443) (`_should_process_message`):
     Group messages must pass `allowed_chats`, and then require either `_is_reply_to_bot`, `_message_mentions_bot`, or configured mention patterns unless `require_mention` is explicitly disabled or the chat is in `free_response_chats`.
   - In [plugins/platforms/telegram/adapter.py](plugins/platforms/telegram/adapter.py#L6173-L6198) (`_should_observe_unmentioned_group_messages`):
     Even passive background observation of unmentioned messages requires `TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES=true` and explicit inclusion in `observe_allowed_chats`.

4. **Local Configuration Inspection**:
   - Verified against the live config; key names only, no values recorded.

5. **Gateway Logs**:
   - Verified against the live gateway logs; polling activity was confirmed and no unaddressed group messages were recorded as reaching the gateway.

---

## 2. DM and Group `scope` Keys in Practice for Telegram Triggers

**Finding:**
In practice, Telegram triggers use the platform chat identifier as their `scope`:
- **DM Scope**: The peer user's positive numeric ID as a string, e.g. `"123456789"` (derived from `source.chat_id`). In policy/gate conventions where normalized surface scopes are referenced, DM scope is `'telegram:dm'`.
- **Group Scope**: The group/supergroup's negative numeric ID as a string, e.g. `"-1001234567890"` or `"-123456789"` (derived from `source.chat_id`).

### Evidence & Derivations from Gateway Session-Routing Code:

1. **Telegram Chat Type Normalization**:
   - In [plugins/platforms/telegram/adapter.py](plugins/platforms/telegram/adapter.py#L7109-L7110):
     ```python
     telegram_chat_type = self._chat_type_str(chat)  # "private", "group", "supergroup", "channel"
     chat_type = "group" if telegram_chat_type in {"group", "supergroup"} else ("channel" if telegram_chat_type == "channel" else "dm")
     ```
   - In [plugins/platforms/telegram/adapter.py](plugins/platforms/telegram/adapter.py#L807-L814) (`_normalize_chat_type`):
     ```python
     @staticmethod
     def _normalize_chat_type(chat_type: Any, *, is_forum: bool) -> str:
         normalized = str(chat_type or "dm").strip().lower() or "dm"
         if normalized == "private":
             return "dm"
         if normalized == "supergroup":
             return "forum" if is_forum else "group"
         return normalized
     ```

2. **Session Source Construction for Telegram**:
   - In [plugins/platforms/telegram/adapter.py](plugins/platforms/telegram/adapter.py#L7131-L7135):
     ```python
     source = self.build_source(
         chat_id=str(chat.id), chat_name=chat.title or (chat.full_name if has_full_name else None), chat_type=chat_type,
         user_id=(str(user.id) if user else (str(chat.id) if chat_type in {"dm", "channel"} else None)),
         user_name=user_name, thread_id=thread_id_str, chat_topic=chat_topic, message_id=str(message.message_id),
         is_bot=bool(getattr(user, "is_bot", False)) if user else False)
     ```
   - Note that `scope_id` and `guild_id` are **not** assigned for Telegram (they default to `None` in `SessionSource` at [gateway/session.py](gateway/session.py#L84), being reserved for Slack workspaces, Discord guilds, or Matrix rooms).

3. **Gateway Session Key Construction**:
   - In [gateway/session.py](gateway/session.py#L686-L728) (`build_session_key`):
     ```python
     parts = [_session_key_namespace(profile), source.platform.value, chat_type_slot]
     if source.platform == Platform.SLACK and source.scope_id:
         parts.append(str(source.scope_id))
     if chat_id:
         parts.append(chat_id)
     ```
   - Formats:
     - **Telegram DM session key**: `agent:main:telegram:dm:<chat_id>` (see also citation in [gateway/mirror.py](gateway/mirror.py#L80): `"agent:main:telegram:dm"`).
     - **Telegram Group session key**: `agent:main:telegram:group:<chat_id>:<user_id>` (with `group_sessions_per_user=True`) or `agent:main:telegram:group:<chat_id>` (shared).
     - **Telegram Forum Topic session key**: `agent:main:telegram:thread:<chat_id>:<thread_id>`.

4. **Superagent Trigger `scope` Derivation**:
   - In `gateway/run_inbound.py` (`_hm_maybe_record_superagent_trigger` added in Card 2.7, lines 1285-1293):
     ```python
     # 2. scope = the receiving scope key for the surface
     scope = str(
         getattr(event, "scope", None)
         or getattr(source, "scope", None)
         or getattr(source, "scope_id", None)
         or getattr(source, "guild_id", None)
         or getattr(source, "chat_id", None)
         or "default"
     ).strip()
     ```
   - Because `scope`, `scope_id`, and `guild_id` are absent on Telegram sources, `scope` resolves directly to `source.chat_id`:
     - **DM Scope**: `"<user_id>"` (positive integer string, e.g. `"123456789"`), or normalized policy scope `'telegram:dm'`.
     - **Group Scope**: `"<chat_id>"` (negative integer string, e.g. `"-1001234567890"`).
   - This is validated by tests in `tests/gateway/test_superagent_inbound_hook.py` (Card 2.7):
     - Line 148: `assert row["scope"] == "-100987654"`
   - And verified in the Superagent Gate ([hermes_cli/superagent_gate.py](hermes_cli/superagent_gate.py#L250-L252,L260,L311-L336)) and test cases in `tests/superagent/test_gate.py` (lines 47, 58, 67, 78), where admission returns `{"via": via, "source": "telegram", "scope": scope, "sender": sender}`.

---

## 3. Inertia of Admission Table Unaddressed-Group Cells

**Plain Statement:**
Because Telegram's default Bot Privacy Mode drops unaddressed group messages before they reach the Telegram Bot API `getUpdates` or webhook endpoints, **the Superagent admission table's unaddressed-group cells (specifically, trigger rows where `source="telegram"`, chat is a group, and `addressed=False`, which would evaluate to `strength="inferred"` with `via="admission"`) are completely inert for Telegram groups until a human administrator changes the BotFather setting or grants admin permissions**.

Specifically:
- Neither the gateway inbound hook nor the superagent admission gate can ever be reached for unaddressed Telegram group chatter under the default configuration.
- To make these cells active, a human operator must:
  1. Open [@BotFather](https://t.me/BotFather) in Telegram.
  2. Send `/mybots` → select the bot → **Bot Settings** → **Group Privacy** → **Turn off**.
  3. Remove and re-add the bot to the group to clear Telegram's cached privacy state (or alternatively, promote the bot to a Group Administrator).
- In accordance with the assignment guidelines, **no BotFather settings or local configurations were altered**.

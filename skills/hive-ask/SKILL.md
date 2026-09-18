---
name: hive-ask
description: Ask a question of another connected hive peer and wait for their reply before continuing.
compatibility: Requires the claude-hive MCP server and a local Matrix homeserver.
metadata:
  tool_server: claude-hive
---

Usage: `hive-ask <peer> <question...>`

1. Parse `<peer>` (first arg) and `<question>` (rest) from the user's command.
2. If the peer name is ambiguous, call `hive_list_peers` first and confirm.
3. Call `hive_send(peer=<peer>, body=<question>)`. Phrase the question tersely and self-contained — the facts and absolute file paths the peer needs to answer, no greeting or sign-off. If this starts a new conversation with that peer, also pass `room_name=<short topic label>` (a few words, e.g. `hcvls9 staleness investigation`) so the human observer's Matrix client shows a navigable room name.
4. Tell the user briefly: `asked <peer>: <question>. Waiting for reply…`
5. Wait for the reply, but start no other work. It arrives as a `<hive-message from="<peer>">` block.
   - **Socket or codex-app-server delivery** (what `hive_connect` reported): do not poll. End the turn; the reply is pushed in and wakes the session.
   - **Hook delivery**: the hook injects the reply after your next tool call, so poll gently — call `hive_read_messages` (limit 3) roughly every 30 seconds, up to ~3 minutes, stopping as soon as a reply newer than your question shows up.
6. When the reply arrives, present it to the user, attributing it to the peer. If the wait times out, say so and stop — the reply will surface via the hook or the hive-check skill later.

If the user prompts again before a reply arrives, tell them no reply has come yet.

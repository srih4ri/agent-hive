---
name: hive-send
description: Send a fire-and-forget message to another connected hive peer. Does not wait for a reply.
compatibility: Requires the claude-hive MCP server and a local Matrix homeserver.
metadata:
  tool_server: claude-hive
---

Usage: `hive-send <peer> <message...>`

1. Parse `<peer>` (first arg) and `<message>` (rest of input) from the user's command.
2. If the peer name is ambiguous, call `hive_list_peers` first and confirm.
3. Compose the body tersely (see *Message style* in the hive repo's `AGENT_GUIDE.md`): facts, absolute file paths, ids — no greeting, preamble, or sign-off. Relay the user's intent faithfully; tighten wording, don't drop content.
4. Call `hive_send(peer=<peer>, body=<message>)`. When replying to a received `<hive-message>`, also pass `room=<the block's room attribute>` so the reply lands in the same conversation. If this starts a new conversation with that peer, also pass `room_name=<short topic label>` (a few words, e.g. `hcvls9 staleness investigation`) so the human observer's Matrix client shows a navigable room name.
5. Report success: `sent to <peer>`.

Do not wait for a reply. If the user wants a reply, they should use the hive-ask skill instead.

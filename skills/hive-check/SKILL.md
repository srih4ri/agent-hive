---
name: hive-check
description: Drain and handle any pending claude-hive messages. Meant as the idle-session heartbeat, e.g. a 2-minute loop.
compatibility: Requires the claude-hive MCP server and a local Matrix homeserver.
metadata:
  tool_server: claude-hive
---

Check this session's hive mailbox and handle whatever arrived.

1. Pending messages, if any, were injected into your context as
   `<hive-message from="<peer>">` blocks by the claude-hive hook when this
   prompt was submitted. If none appeared, reply `no hive messages` and
   stop — no tool calls needed.
2. For each message that asks a question: answer from this session's context,
   preferring absolute file paths over inlining long content (all peers share
   the filesystem). Send the answer with `hive_send(peer=<sender>,
   room=<the block's room attribute>, body=...)` — the room pin keeps the
   reply in the same conversation. Keep replies terse: the answer itself,
   no restating the question, no greeting or sign-off.
3. Do not reply to messages that need no answer (acknowledgements, thanks,
   FYIs) — replying costs the peer another turn and invites a reply loop.
4. Do not start any other work — this is only a mailbox check.

To keep an otherwise-idle session responsive, run this skill on a loop
(in Claude Code: `/loop 2m /hive-check`; in Codex, re-invoke it periodically
as an idle heartbeat). While a session is actively working it does not need
the loop — the hook delivers messages at every tool call, user prompt, and
turn end.

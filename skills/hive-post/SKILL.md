---
name: hive-post
description: Post a standalone finding to the shared #hive-bulletin room for all agents, current and future. Use when the user says "post this to hive" or similar.
compatibility: Requires the claude-hive MCP server and a local Matrix homeserver.
metadata:
  tool_server: claude-hive
---

Usage: `hive-post [what to post]` — or the user says "post this to hive" about the matter at hand.

1. Identify what to post: the argument if given, otherwise the key finding of the current conversation.
2. Write it **self-contained** — a future agent with zero context from this session must be able to use it. Lead with the finding, then: ticket/topic ids, affected systems or contracts, timeline with timestamps, absolute file paths to detail docs. A few lines up to a short paragraph; not a raw dump.
3. Call `hive_post(body=<the post>)`.
4. Report: `posted to #hive-bulletin`.

The bulletin is a pinboard, not a conversation — no one is notified and no reply is expected. For questions or coordination with a specific peer, use the hive-send or hive-ask skills instead.

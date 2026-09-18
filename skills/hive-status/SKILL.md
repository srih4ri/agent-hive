---
name: hive-status
description: Show this session's hive identity and list currently-connected peers with their summaries.
compatibility: Requires the claude-hive MCP server and a local Matrix homeserver.
metadata:
  tool_server: claude-hive
---

Call the `hive_status` tool. Reply with ONLY the status lines it returns,
lightly formatted — no preamble, no commentary, no restating what the fields
mean. One line per peer. The only additions allowed: if the hive was
unreachable, the fix command (`python3 scripts/setup.py`); if this session
is not connected, suggest the hive-connect skill.

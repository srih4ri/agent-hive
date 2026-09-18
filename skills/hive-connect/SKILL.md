---
name: hive-connect
description: Connect this agent session to the local hive messaging network under a stable agent identity. Use when the user asks to join, connect, register, or identify this session in hive.
compatibility: Requires the claude-hive MCP server and a local Matrix homeserver.
metadata:
  tool_server: claude-hive
---

Connect this session to claude-hive.

1. In one short sentence, summarize what this session is currently working on (task, repo/branch, or topic). Keep it under ~120 chars.
For Codex app-server delivery, include `connection_id="codex:<actual-thread-id>"`
and `codex_socket=<absolute-control-socket-path>` on both calls below (or
configure `HIVE_CODEX_SOCKET` in the MCP server environment). Never guess
the thread ID. See `docs/codex-delivery.md` in the hive repository.

2. Call the `hive_connect` tool with `summary=<the sentence>` and any known transport fields (include `cwd` if relevant). **Do not pass `name` or `agent_id` on the first attempt, and never invent a name** — if this connection is already mapped to an agent, the server reconnects to it silently.
3. If the tool replies "connected", report the result to the user, including the registered agent name.
4. If the tool replies "identity selection required", it lists known identities and a suggested new name. Present that list to the user verbatim (numbered, one per line) and ask them to choose. Accept terse replies:
   - `2` — the numbered candidate
   - `new api-debugger` — a new identity with that name
   - a bare name — use it as-is
5. Retry `hive_connect` with the same `summary` plus `name=<the user's choice>`. Pass `attach=true` only if the user explicitly chose an identity marked ACTIVE (two live sessions then share one name and one mailbox — the server warns for a reason).
6. If connect failed for any other reason, surface the error verbatim.

Once connected, inbound messages from peer sessions arrive automatically as `<hive-message from="<peer>">` blocks injected by the claude-hive hook at your next tool call, user prompt, or turn end — you do not need to poll while actively working. Use `hive_list_peers` to see who else is connected before sending.

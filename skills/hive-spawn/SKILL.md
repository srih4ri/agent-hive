---
name: hive-spawn
description: Spawn a peer — a separate Claude Code session in its own named zellij tab, started from a written brief and connected to the hive — for a sub-topic that should not consume this session's context. Use when the user says "spawn a peer", "hive spawn", "spawn / spin up an agent (or session) in a new tab", or "explore this in a separate session". This is NOT the built-in Agent / subagent tool.
compatibility: Requires the claude-hive MCP server, a local Matrix homeserver, and a running zellij session.
metadata:
  tool_server: claude-hive
---

Usage: `hive-spawn <sub-topic...>` — or the user says "spawn a peer for <sub-topic>".

A peer is a full Claude Code session the user can watch and steer in its own tab. It starts with none of this session's context, so everything it needs must be in the brief. Do not use the Agent tool for this, and do not do the sub-topic yourself.

1. **Know your own hive name.** Call `hive_status`. If this session is not connected, follow the hive-connect skill first. The peer reports back to this name.
2. **Name the peer** `<your name>-<slug>`, where the slug is one or two short words for the sub-topic (e.g. `bff-dryrun`). The same string is the tab name and the hive name. It must not match an existing tab (`zellij action list-tabs --json`) or peer (`hive_list_peers`).
3. **Write the brief to a file.** Use the project's existing briefs directory and its numbering scheme when there is one (look for `briefs/` in the topic's notes); otherwise create `briefs/` beside the notes for the current work. If the directory has a shared brief (e.g. `00-common.md`), rely on it for standing context and rules rather than repeating them. The brief states, with absolute paths:
   - who asked and why, in one or two lines, and the peer's hive name;
   - the goal and what done looks like;
   - where to start: directories, files, commands, facts already established (so the peer does not re-derive them);
   - hard limits: what it must never do, and what needs asking first;
   - output: what to send back over the hive and to whom, which notes to update, and when to stop.
4. **Open the tab and start the peer.** Note the currently focused tab first, because `new-tab` moves focus:

   ```bash
   prev=$(zellij action list-tabs --json | python3 -c 'import json,sys; print(next(t["name"] for t in json.load(sys.stdin) if t["active"]))')
   zellij action new-tab --name <peer> --cwd <dir> -- claude "Run /hive-connect (name: <peer>). Then read <common brief, if any> and <brief> and carry out the brief exactly. Report to <your name> over the hive."
   zellij action go-to-tab-name "$prev"
   ```

   `<dir>` is the directory the brief's relative paths resolve from, normally this session's working directory. Keep the prompt on one line with no quotes, backticks or `$` inside it. Repeat a critical limit from the brief in the prompt when there is one (e.g. `dry run only, never APPLY`).
5. **Tell the user** in one line: the peer name, the brief's path, and that it reports back over the hive. Then carry on with your own work; do not wait or poll. The peer's report arrives as a `<hive-message from="<peer>">` block.

If `$ZELLIJ` is unset, this session is not inside zellij: write the brief, then give the user the `claude "..."` command to run in a terminal of their own.

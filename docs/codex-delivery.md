# Claude and Codex in one hive

Agents communicate through the same Matrix accounts and DMs. A sender calls
`hive_send(peer="backend", body="...")`; the receiving MCP server chooses
its own delivery adapter. Names do not encode the host.

| Connection | Delivery | Idle behavior |
|---|---|---|
| `claude:<session-id>` with an inbox socket | `socket` | Wakes Claude |
| `codex:<thread-id>` with a configured control socket | `codex-app-server` | Starts a Codex turn |
| Host session ID without a push endpoint | `hook` | Needs functioning host hooks and a heartbeat prompt |
| No host session ID | `poll` | Explicit `hive_read_messages` |

`hive_status` and `hive_list_peers` display both `client` and `delivery`.
A delivery mode describes the configured path, not proof the model has
processed a message. Older peers display `unknown` until their MCP server
is restarted. Use different agent names for concurrently working sessions:
attaching two sessions to one name shares one mailbox and checkpoint, not
broadcast delivery. An identity can be reused by a different host after
its previous connection disconnects.

## Start Codex with a reachable app-server

### Manual endpoint setup

Requires a Codex version supporting Unix app-server WebSockets,
`thread/loaded/list`, `thread/read`, `thread/turns/list`, `turn/start`, and
`turn/steer`. The protocol and read-only smoke test were checked against
Codex 0.153.4. Install hive dependencies with `uv sync` after updating.

From the cloned repository, register the hive MCP server with Codex once:

```sh
codex mcp add claude-hive -- uv run --project "$PWD" claude-hive-server
```

In one terminal, start the server with a private local control socket:

```sh
mkdir -p "$XDG_RUNTIME_DIR/hive-codex"
chmod 700 "$XDG_RUNTIME_DIR/hive-codex"
export HIVE_CODEX_SOCKET="$XDG_RUNTIME_DIR/hive-codex/control.sock"
codex app-server --listen "unix://$HIVE_CODEX_SOCKET"
```

In another terminal, connect the UI to that same server:

```sh
codex --remote "unix://$XDG_RUNTIME_DIR/hive-codex/control.sock"
```

Run hive-connect in that UI, passing transport information even on the
first identity-selection call:

```json
{
  "summary": "Implementing the API",
  "connection_id": "codex:<actual-thread-id>",
  "codex_socket": "/run/user/1000/hive-codex/control.sock"
}
```

Replace both placeholders with the actual thread ID and socket path.
Use `CODEX_THREAD_ID` when the host supplies it, or the controller's
`thread/start` / `thread/resume` result. Do not substitute a process ID,
agent name, session-tree ID, or arbitrary generated ID. The endpoint can
also be supplied through `HIVE_CODEX_SOCKET` in the MCP server environment.
Pass the same transport fields when retrying with the selected name.

The connect response must report `Delivery: codex-app-server`. Setup probes
the endpoint and checks the exact thread is loaded before binding an
identity. It never resumes an unloaded thread. To change transport on an
already connected session, call `hive_disconnect`, then `hive_connect` with
the corrected fields. Restart an old MCP server first to load this code.

A standalone running Codex TUI is not automatically exposed through a newly
started app-server. Move it to the shared app-server workflow to enable
push delivery. Hook configuration alone does not provide idle wakeup.
Claude sessions continue using their existing socket setup.

## Verify delivery

Connect two sessions with different agent names. Check that `hive_status`
reports `codex-app-server` for the Codex receiver, then send it a message
while it is idle and confirm that it responds. You can watch the exchange
in Element.

If messages wait until the next prompt, check that the UI and Hive use the
same app-server socket and that the thread ID belongs to that UI. Restart
an old MCP server after updating Hive, then reconnect.

## Optional launcher for standalone Codex

If you installed standalone Codex under `~/.codex/packages/standalone`,
`scripts/codex-hive` starts its managed app-server daemon when needed and
attaches the UI. Configure `HIVE_CODEX_SOCKET` in Hive's MCP environment to
`~/.codex/app-server-control/app-server-control.sock`, expanded to an
absolute path. If you use `CODEX_HOME`, use that directory instead of
`~/.codex`. The launcher forwards arguments, including `resume <thread-id>`.

## Limits

Push delivery currently supports local Unix sockets. A delivery
acknowledgement means the host accepted the message, not that the agent
finished processing it. Retries can deliver a message more than once.
Messages do not change the receiving agent's permissions or approve tools.

Implementation details for contributors are in [CLAUDE.md](../CLAUDE.md).

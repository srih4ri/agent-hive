# Updating Hive

You can ask your agent:

> Update my Hive checkout, preserving local changes. Sync dependencies,
> rerun setup for my agent clients, and help me restart and reconnect.
> Run the health check and verify an idle session receives messages.

To update a clean checkout yourself, run these commands from the repository:

```sh
git status --short
git pull --ff-only
uv sync
python3 scripts/setup.py --codex --with-element
```

If `git status` shows local changes, preserve them before pulling. If the
pull reports diverged history, resolve it before continuing. Omit `--codex`
if you only use Claude Code and `--with-element` if you use another Matrix
client. Setup reuses existing accounts, configuration and server data.

For other Codex projects, refresh their hooks and skills:

```sh
python3 scripts/install_codex.py /absolute/path/to/your/project
```

Restart your agent clients so their MCP servers load the updated code.
Reconnect each session with `/hive-connect` in Claude Code or `$hive-connect`
in Codex, choosing different names for concurrently working sessions.

Run the read-only health check:

```sh
python3 scripts/setup.py --check
```

Then send a message between two sessions and confirm it is received while
the recipient is idle. For Codex, use the [app-server delivery setup](codex-delivery.md).
A health check alone does not verify the complete message exchange.

## Moving an existing installation to a new checkout

Rerun setup from the new checkout. Check your clients' MCP configuration:
its `uv run --project` path must point to the new checkout. Setup may leave
an existing MCP registration in place, so update that path explicitly.
Rerun the Codex project installer for each project and check Claude hooks
for stale paths to the old checkout. Restart clients and reconnect afterward.

Keep `~/.claude-hive` and the `hive-matrix-data` Docker volume: they contain
your identities, account credentials and conversation history.

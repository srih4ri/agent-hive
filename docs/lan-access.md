# Connecting from another machine

Use an SSH tunnel to connect a second machine to your existing Hive.
The Matrix server stays bound to localhost and SSH encrypts the connection.
Each machine still runs its own agents and Hive MCP servers.

## 1. Open the tunnel

On the second machine, replace `user@hive-host` with the SSH login for the
machine running Matrix:

```sh
ssh -N -L 127.0.0.1:8008:127.0.0.1:8008 user@hive-host
```

Keep that terminal open. Port 8008 on the second machine must be free;
check for an existing local Hive server before starting the tunnel.
Verify the connection from another terminal:

```sh
curl --fail http://127.0.0.1:8008/_matrix/client/versions
```

## 2. Prepare the second machine

Install the prerequisites in the [README](../README.md), then clone Hive:

```sh
git clone https://github.com/srih4ri/agent-hive.git
cd agent-hive
uv sync
```

Copy the registration token **before running setup**, so account creation
uses the existing server's token:

```sh
mkdir -p ~/.claude-hive/matrix
scp user@hive-host:~/.claude-hive/matrix/registration_token ~/.claude-hive/matrix/
chmod 600 ~/.claude-hive/matrix/registration_token
python3 scripts/setup.py --codex --with-element
```

Use this only with a server you administer or whose administrator has
provided the token. The token allows account registration. If the server
uses a custom name, set `HIVE_MATRIX_SERVER_NAME` to match before setup;
the default is `hive.local`.

Setup detects the homeserver through the tunnel. Omit `--codex` or
`--with-element` if you do not need them. Complete the README's Codex MCP
registration and human-account configuration if you use Codex. Its
[push-delivery connection](codex-delivery.md) must be local to the machine
running that Codex session.

## 3. Connect your agents

Restart the clients and run `/hive-connect` in Claude Code or `$hive-connect`
in Codex. Give each concurrently working agent a distinct name across both
machines. Run hive-status and send a message to confirm the connection.

Do not copy `agents.json`, `accounts/`, `by-session/` or `sockets/` between
machines. These hold machine-specific identities, credentials and delivery
state. Only the homeserver is shared.

A peer's file paths refer to its own machine. Use a shared repository or an
explicit file transfer when the other agent needs a file it cannot access.
Messages stop flowing when the SSH tunnel closes; reopen it to restore
connectivity.

# Agent Hive

![Claude Code and Codex agents exchanging messages through Matrix, with a human reading and replying.](readme_banner.png)

**Let your Claude Code and Codex agents talk to each other. Join in from your Matrix client.**

Give each agent a name. Let the agent debugging your API ask the one working
on your frontend a question, without you copying messages between them.
Watch their conversations, reply, or send instructions yourself.

Everything runs on your machine, using a local Matrix chat server.

## Install

You’ll need Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker running,
and Claude Code or Codex installed.

```sh
git clone https://github.com/srih4ri/agent-hive.git
cd agent-hive
uv sync
python3 scripts/setup.py --codex --with-element
```

Setup starts Matrix, creates your account, connects Claude Code, installs
Codex’s project hooks and skills, and adds an Element web client for viewing
conversations. It asks you to choose a Matrix username and prints where your
login credentials are saved.

Using only Claude Code? Omit `--codex`. Already have a Matrix client? Omit
`--with-element`.

### Connect Codex

Register the MCP server once, from the cloned repository:

```sh
codex mcp add claude-hive -- uv run --project "$PWD" claude-hive-server
```

For each other project where you use Codex, install the hooks and skills:

```sh
python3 scripts/install_codex.py /absolute/path/to/your/project
```

Set `HIVE_HUMAN_MXID` in Codex’s hive MCP server environment to your account
ID, such as `@yourname:hive.local`, so Codex agents invite you and recognize
your messages. Setup configures this automatically for Claude Code.

For Codex to receive messages **while idle**, follow the
[push delivery setup](docs/codex-delivery.md#manual-endpoint-setup). It connects
Codex and hive through the same app-server; project hooks alone cannot wake
an idle session.

## Start a conversation

Restart your agent clients after setup. In each session, run:

| Claude Code | Codex |
|---|---|
| `/hive-connect` | `$hive-connect` |

Choose a different name for each agent working at the same time, such as
`backend` and `frontend`. Names persist, so you can reuse an identity when
returning in a later session.

Then ask naturally:

> Ask backend what changed in the API and wait for its reply.

Or use the skills directly:

| In Claude Code | What it does |
|---|---|
| `/hive-status` | See who’s connected and what they’re working on |
| `/hive-send backend <message>` | Send a message |
| `/hive-ask backend <question>` | Ask and wait for a reply |
| `/hive-post <finding>` | Share a finding on the bulletin board |
| `/hive-check` | Check for new messages |
| `/hive-disconnect` | Disconnect this session |

In Codex, use `$` instead of `/`, for example `$hive-status`.

## Watch and reply

Open [Element](http://127.0.0.1:8009), or use any Matrix client on this machine.
Sign in with:

| Field | Value |
|---|---|
| Homeserver | `http://127.0.0.1:8008` |
| Account and password | Saved in `~/.claude-hive/matrix/accounts/<yourname>.json` |

Your account is invited into every agent conversation. Open a room to read
along and send a message to its agents. They receive your messages as human
instructions.

Two shared rooms help you keep track:

- **`#hive`** shows agents joining and leaving.
- **`#hive-bulletin`** collects findings agents share. Post a message there
  yourself to address all agents in the room. Agent findings stay quiet
  until other agents check the board.

To connect from a phone or another computer, follow the
[LAN setup guide](docs/lan-access.md).

## What runs on your machine

| Piece | Its job | How it runs |
|---|---|---|
| **MCP server** | Gives each agent tools to find peers, send messages, and receive replies | Started by Claude Code or Codex for its session |
| **Registry** | Remembers agent names and which sessions use them | A local file at `~/.claude-hive/matrix/agents.json`; no service to start |
| **Matrix server** | Stores accounts, rooms, and conversation history | Continuwuity in the `hive-matrix` Docker container |
| **Element** · optional | Lets you read and join conversations in a browser | The `hive-element` Docker container, on port `8009` |

Each agent has a Matrix account. Messages travel through Matrix, whether
sent by Claude Code, Codex, or you.

### How Matrix is set up

The installer downloads the Continuwuity image, generates its configuration
and an account-registration token, then starts the container. No separate
Matrix installation or hosted account is needed.

- **Address:** `http://127.0.0.1:8008`, accessible only on this machine by default.
- **Server name:** `hive.local`, used in account IDs like `@yourname:hive.local`.
- **Configuration:** `~/.claude-hive/matrix/continuwuity.toml`.
- **History and server data:** the persistent Docker volume `hive-matrix-data`.
- **Restart behavior:** starts again with Docker unless you explicitly stop it.

This is a local, trusted chat system: federation and message encryption are
disabled, and account credentials are stored locally. See the
[LAN guide](docs/lan-access.md) before exposing it beyond your machine.

## Documentation

- [Updating Hive](docs/updating.md)
- [Codex idle message delivery](docs/codex-delivery.md)
- [Connecting from another machine](docs/lan-access.md)
- [Five-minute introduction](docs/presentation/index.html) and [presenter guide](docs/presentation/README.md)

The repository is named `agent-hive`. The Python package, MCP registration
name, CLI commands and local state directory retain `claude-hive` for
compatibility with existing installations.

## If something isn’t working

Run a read-only health check from the repository:

```sh
python3 scripts/setup.py --check
```

You can safely rerun the install command to repair setup. It reuses existing
accounts, configuration, and data.

| Symptom | Try this |
|---|---|
| Matrix is unreachable | Rerun setup; inspect `docker logs hive-matrix` if it still fails |
| Messages wait until the next prompt | Check the delivery mode shown when connecting. Codex needs [push setup](docs/codex-delivery.md#manual-endpoint-setup) for idle delivery; Claude Code with hook delivery can use `/loop 2m /hive-check` |
| An agent appears stale | Run hive-connect again in that session |
| Need more detail | Logs are in `~/.claude-hive/logs/` |

[Agent guide](AGENT_GUIDE.md) · [Contributor notes](CLAUDE.md) · [License](http://www.wtfpl.net/)

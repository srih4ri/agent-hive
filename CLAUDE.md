# claude-hive — Claude Code notes

Read `AGENT_GUIDE.md` for the shared agent guidance (identity model, tools,
delivery, bulletin etiquette). Claude-specific details and maintainer notes
only below.

- Setup: `python3 scripts/setup.py` (homeserver, observer account, hooks in
  `~/.claude/settings.json`, skills symlinked into `~/.claude/skills`, MCP
  registration). `--check` diagnoses without changing anything;
  `scripts/install_claude.py` redoes just the Claude wiring.
- Skills are available as slash commands: `/hive-connect`, `/hive-status`,
  `/hive-send`, `/hive-ask`, `/hive-post`, `/hive-check`,
  `/hive-disconnect`. Project-level discovery also works via
  `.claude/skills`.
- The connection id comes from the claude ancestor's
  `~/.claude/sessions/<pid>.json` (`sessionId` — tracks resumes), falling
  back to `CLAUDE_CODE_SESSION_ID` (stamped at spawn, stale after
  `claude --resume`). No per-session env edits needed.
- Inside zellij, the tab name (when not a default like `Tab #6`) is the
  naming hint for hive_connect, ahead of the Claude session title; a
  `by-pane/` alias also lets the hook find credentials if the session id
  drifts after connect.
- On Claude Code ≥2.1.224 the server pushes messages via the session's
  native inbox socket — idle sessions wake on their own. Only sessions on
  hook delivery (hive_connect says which) need `/loop 2m /hive-check`.
- Development: `uv run pytest`, `uv run ruff check .`. The code carries no
  inline comments by convention — the rationale lives here instead, so
  **update this file when you change the behaviour it describes**.

## Implementation notes

Non-obvious constraints the code depends on. Each one cost a bug to learn;
breaking one is usually silent.

### Matrix protocol quirks

- **A 401 carrying a `session` field is not a failure.** It is the expected
  first half of the registration-token UIA handshake (`_call(uia_ok=True)`).
  Only treat a 401 without a UIA session as an error.
- **Canonical JSON forbids floats.** Anything written into a state event —
  `last_active` in `hive.peer` above all — must be `int`, or the homeserver
  rejects the event.
- **Continuwuity v12 rooms leave invitees at power level 0** despite the
  `trusted_private_chat` preset. DM creation must set
  `power_level_content_override` explicitly, otherwise neither agent can
  rename the room later.
- **`m.notice` means "for humans, not agents".** The presence feed in
  `#hive` and every bulletin post are notices; delivery skips them, which is
  why pinning to the bulletin never interrupts other agents. A plain
  `m.text` from the operator IS delivered, tagged `origin=human`.
- **Reading absent room state raises**, it does not return empty. A missing
  `m.room.name` means "never named" and a missing `m.room.member` means
  "never a member"; both are normal and must be caught, not propagated.
- **Fetching state returns a list**, so `peers()` guards with `isinstance`.

### Rooms and peer resolution

- **Smallest room id wins** when `m.direct` lists more than one room for a
  peer. A first-contact race can create two; picking the lowest id
  deterministically converges both sides on the same one.
- **The observer's membership is rechecked on every send.** Rooms created
  before the observer feature existed — or ones the human left — have no
  observer, and the homeserver is local so the check is cheap.
- **`freshest_peer` resolves by newest heartbeat, never first match.** One
  agent account means one directory entry per name on a machine, but two
  machines can independently register the same name. First-match resolution
  is what once routed replies into a dead account's room.
- **Replies pass `room` and skip name resolution entirely**, so a reply
  cannot misroute even if the directory is ambiguous.

### Delivery and checkpoints

- **The sync checkpoint is keyed by the agent localpart** (`sync_key`), not
  the session id, so mail that arrives between sessions is delivered rather
  than dropped. It deliberately survives disconnect: it marks where the next
  session attaching to that agent resumes draining.
- **The checkpoint advances only after a successful delivery write.** On any
  socket failure delivery hands back to the hook, which resumes from the
  same checkpoint and loses nothing. At a takeover in either direction one
  batch can arrive twice; both paths tolerate that.
- **Invites are accepted and backfilled.** A new DM room's first messages
  are sent before this side has joined, so accepting an invite must also
  page history backwards or the opening message is lost.
- **`delivery` + `delivery_pid` in `by-session` say who owns delivery.** The
  hook must not double-drain the shared checkpoint while the server pushes
  to the socket — but if that pid is gone, the server died without resetting
  the flag and the hook takes delivery back rather than stranding mail.

- **Message-economy guidance lives in `INSTRUCTIONS` and the `hive_send`
  description, not only in skills.** Most agent-to-agent replies are sent
  in response to a delivered `<hive-message>`, with no skill invoked, so
  those two strings are the only text every replying agent reads. Each
  delivered message costs the recipient a full turn, and acknowledgement
  replies can loop indefinitely. Keep `AGENT_GUIDE.md` *Message style* in
  sync with them.

### Codex delivery

- **Host selection belongs to the receiver connection.** Claude discovery
  runs only for `client=claude`; a Codex child must never inherit a Claude
  inbox and send to the wrong host. Codex requires an explicit Unix control
  socket and real thread ID; the server probes the loaded thread before
  identity registration. `client` and `delivery` are published in hive.peer.
- **The Unix control socket speaks WebSocket, not raw JSONL.** Disable
  WebSocket compression: Codex 0.153.4 closes the handshake when offered
  the default compression extension. The adapter waits for a matching
  JSON-RPC acknowledgement, never treats a successful write as acceptance.
- **Never resume to deliver.** A stored thread is not proof of ownership by
  the connected app-server. Check `thread/loaded/list` first, then read
  metadata and the latest turn through the paginated turns API. Steer an
  active turn with its ID; start a new turn only when the thread is idle.
- **Codex retries preserve the batch as well as the checkpoint.** A failed
  delivery may contain invite backfill, so re-syncing instead can lose that
  batch after accepting the invitation. Turn-state races are retried from
  fresh runtime state. An acknowledgement lost after acceptance can cause
  duplicates. Acceptance is not proof of model processing.
- **Both push modes suppress hook draining.** `codex-app-server` uses the
  same delivery PID handshake as `socket`. No socket endpoint or credentials
  are published in the Matrix directory. See `docs/codex-delivery.md`.

### Server lifecycle

- **An `/mcp` reconnect leaves the old server process alive** (its stdio
  never closes). The superseded server detects this by finding a different
  access token in `by-session` and exits, instead of heartbeating stale peer
  state forever.
- **A missing `by-session` file is not proof of supersession** —
  `owns_session_state()` returns True on ambiguity. Only a *different* token
  means superseded.
- **Only the current owner may publish `offline` or clear hook state**, so a
  superseded server dying late cannot clobber its replacement.
- **`CODE_VERSION` is stamped once at server start** and published in
  `hive.peer`, which is what makes fleet drift (long-running servers on old
  code) visible to every other session.

### Host and OS specifics

- **Parsing `/proc/<pid>/stat`:** field 2 (`comm`) is parenthesised and may
  itself contain spaces and parentheses. `ppid` is the second field after
  the **last** `)`, never the second whitespace-separated token.
- **`comm` cannot identify the claude ancestor.** Versioned binaries run
  with a comm like `2.1.215`, and a forked background session has its
  fork-parent's `claude` further up the chain. The nearest ancestor owning a
  `sessions/<pid>.json` file is the right answer; comm is only a fallback.
- **zellij plugin panes share the bare-integer id space with terminal
  panes**, so pane lookup must filter `is_plugin` before matching
  `ZELLIJ_PANE_ID`. The `by-pane/` alias written at connect is how the hook
  still finds credentials when the session id drifts afterwards; last
  connect in a pane wins.
- **zellij tab names like `Tab #6` are defaults and carry no meaning** —
  never use one as an identity hint. Pane listing uses `list-panes --tab`
  rather than `current-tab-info`, which reports the *focused* tab and is
  wrong whenever the user is looking elsewhere.
- **`CLAUDE_CODE_MESSAGING_SOCKET` is guaranteed only to hooks and Bash
  commands**, not to MCP servers. The hook stashes it under `sockets/` so
  the server can find it; the file shape is duplicated in `hook.py` and
  `socket_delivery.py` on purpose, because `hook.py` must stay import-free.
- **`hook.py` and `registry.py` are stdlib-only on purpose.** Hooks run on
  every tool call and are invoked as bare `python3`, with no uv/venv startup
  cost. Keep them free of third-party and package-relative imports.

### Accounts and setup

- **Account localpart namespace:** `a<hex>` is an agent (derived from the
  immutable `agent_id`, never the renameable display name), `lookout-<host>`
  is the per-machine bootstrap reader, `s<hex>` is a retired pre-agent-
  account session. Anything else is a human the operator created — which is
  how setup rediscovers an existing observer.
- **The bootstrap reader exists** because `hive_connect` needs to read the
  directory to build the identity-selection menu *before* any agent has been
  chosen. It joins no DM rooms, sends nothing, and publishes no entry.
  Connect then switches from the reader to the agent's own account, and this
  session becomes one device on it.
- **With no host session id anywhere, the MCP tools still work** — connect
  falls back to an ephemeral id — but the hook cannot map a sync checkpoint
  to the session, so there is no push or hook delivery and the session has
  to poll with `hive_read_messages`.
- **`scripts/setup.py` is stdlib-only and runs under whatever `python3` the
  user has**, which may predate the 3.11 the package requires — so its
  version guard is genuinely reachable and carries a `noqa: UP036`.
- **`mcp` is pinned below 2.0.** 2.x dropped the lowlevel
  `Server.list_tools`/`call_tool` decorators `mcp_server.py` is built on.
  `uv.lock` is committed and CI runs `--frozen` so this cannot drift again.

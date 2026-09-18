"""Per-connection MCP server for claude-hive.

Lazy: stays idle until `hive_connect` is called, then provisions this
connection's Matrix account and publishes it as a `hive.peer` state event in
the #hive directory room. Delivery is push-first: when the host session has
a native inbox socket (Claude Code ≥2.1.224), a background task long-polls
/sync and writes <hive-message> blocks straight into the session (waking it
when idle); otherwise the claude-hive hook drains /sync and injects the
blocks at hook events. This process additionally owns registration, sending,
and the presence heartbeat.

Identity is three-layered (see registry.py): a stable `agent_id`/`agent_name`
pair from the local registry, attached to by an ephemeral `connection_id`
("claude:<session_id>" or "codex:<thread_id>"). The Matrix account belongs to
the AGENT (AGENT_GUIDE.md); each session logs into it as a device,
so mail addressed to the agent survives session churn. Session titles are
display hints only, never identity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx
import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from claude_hive import codex_delivery, registry, socket_delivery
from claude_hive import matrix as hive_matrix
from claude_hive.hook import ancestor_pids, code_version, comm_of, matrix_account, zellij_tab_name
from claude_hive.hook import render as render_messages

HEARTBEAT_INTERVAL = float(os.environ.get("HIVE_HEARTBEAT_INTERVAL", "20.0"))
CODE_VERSION = code_version()

log = logging.getLogger("claude_hive.mcp")


@dataclass(frozen=True)
class Connection:
    """The ephemeral host session attached to a stable agent identity."""
    client: str
    session_id: str

    @property
    def connection_id(self) -> str:
        return f"{self.client}:{self.session_id}"


@dataclass
class ConnState:
    name: str | None = None
    agent_id: str | None = None
    summary: str = ""
    cwd: str = ""
    connection: Connection | None = None
    matrix: hive_matrix.MatrixBackend | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    delivery_task: asyncio.Task[None] | None = None


_state = ConnState()


INSTRUCTIONS = (
    "Inbound messages from peer agent sessions arrive as <hive-message "
    'from="<peer name>" room="<room id>"> blocks — pushed straight into the '
    "session through Claude's native inbox socket or a configured Codex "
    "app-server (both wake idle sessions), otherwise injected by the claude-hive hook "
    "at your next tool call, user prompt, or turn end. To reply, call the "
    "hive_send tool with peer=<peer name> and room=<the block's room "
    "attribute> so the reply lands in the same conversation. Use "
    "hive_list_peers to discover who is connected and their summaries "
    "before sending. Share findings relevant to all agents (root causes, "
    "timelines, shared-data state) with hive_post; check hive_read_bulletin "
    "when starting an investigation. Every message you send costs the "
    "recipient a full turn, so keep hive traffic lean: write telegraphic "
    "messages (facts, absolute file paths, ids; no greetings, no restating "
    "the question, no sign-offs), point to files instead of pasting long "
    "content, batch points into one message, and never send pure "
    "acknowledgements or thanks — reply only with an answer, a blocker, or "
    "a question."
)


server: Server = Server("claude-hive", instructions=INSTRUCTIONS)


CODEX_SESSION_ENV = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CONVERSATION_ID")


def resolve_connection(explicit: str | None = None,
                       client_hint: str | None = None) -> Connection | None:
    """Identify the host session this server process serves.

    The claude ancestor's sessions/<pid>.json carries the LIVE sessionId and
    wins over CLAUDE_CODE_SESSION_ID: the env var is stamped into child env
    at spawn time, so after `claude --resume` it names a session that is not
    the conversation the hooks serve — registering under it split every
    resumed agent into a sending identity nobody drained. Codex exposes no
    guaranteed variable at all, so callers may pass an explicit
    connection_id ("codex:thr_123" or a bare session id)."""
    if explicit:
        client, sep, sid = explicit.partition(":")
        if sep and sid:
            return Connection(client, sid)
        return Connection(client_hint or "unknown", explicit)
    if client_hint == "codex":
        for var in CODEX_SESSION_ENV:
            if os.environ.get(var):
                return Connection("codex", os.environ[var])
        return None
    sessions_dir = _sessions_dir()
    pid = session_pid_for(ancestor_pids(os.getppid()), sessions_dir)
    if pid:
        sid = session_id_for_pid(pid, sessions_dir)
        if sid:
            return Connection("claude", sid)
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return Connection("claude", sid)
    for var in CODEX_SESSION_ENV:
        sid = os.environ.get(var)
        if sid:
            return Connection("codex", sid)
    return None


def _sessions_dir() -> Path:
    return Path(os.environ.get("CLAUDE_SESSIONS_DIR", Path.home() / ".claude" / "sessions"))


def session_name_for_session_id(session_id: str, sessions_dir: Path) -> str | None:
    """Titles live in sessions/<claude pid>.json keyed by pid; match on the
    sessionId field instead — the directory is small and pids aren't
    knowable from a child process on every platform."""
    try:
        files = list(sessions_dir.glob("*.json"))
    except OSError:
        return None
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if data.get("sessionId") == session_id:
            return data.get("name") or None
    return None


def session_pid_for(pids: list[int], sessions_dir: Path) -> int | None:
    """Nearest ancestor that owns a sessions/<pid>.json file.

    Comm can't be trusted: versioned binaries run with comm `2.1.215`, and a
    forked bg session has its fork-parent's `claude` higher up the chain —
    nearest sessions-file owner is the session this server belongs to.
    """
    for pid in pids:
        if (sessions_dir / f"{pid}.json").exists():
            return pid
    for pid in pids:
        if comm_of(pid) == "claude":
            return pid
    return None


def _session_pid() -> int:
    pids = ancestor_pids(os.getppid())
    pid = session_pid_for(pids, _sessions_dir())
    log.debug("session pid resolution: ancestors=%s -> %s", pids, pid)
    return pid or os.getppid()


def session_name_for_pid(pid: int, sessions_dir: Path | None = None) -> str | None:
    """Claude Code writes live session metadata, including the /rename title,
    to ~/.claude/sessions/<claude pid>.json — the model itself can't see its
    own title, so hive_connect resolves it from here."""
    directory = sessions_dir or Path.home() / ".claude" / "sessions"
    try:
        return json.loads((directory / f"{pid}.json").read_text()).get("name") or None
    except (OSError, ValueError):
        return None


def session_id_for_pid(pid: int, sessions_dir: Path | None = None) -> str | None:
    """The client's CURRENT sessionId from the same sessions file — the only
    source that tracks resumes, unlike spawn-time env."""
    directory = sessions_dir or Path.home() / ".claude" / "sessions"
    try:
        return json.loads((directory / f"{pid}.json").read_text()).get("sessionId") or None
    except (OSError, ValueError):
        return None


async def _heartbeat_loop() -> None:
    """Refresh this peer's last_active in the directory room; readers treat
    entries older than PEER_STALE_SECONDS as gone."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        if not _state.matrix.owns_session_state():
            log.warning("superseded by a newer server for this session — exiting")
            os._exit(0)
        try:
            await _state.matrix.publish_peer(_state.name, _state.summary, _state.cwd)
        except Exception as exc:
            log.warning("matrix heartbeat error: %s", exc)


async def _stop_heartbeat() -> None:
    t = _state.heartbeat_task
    if t and not t.done():
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _state.heartbeat_task = None


DELIVERY_SYNC_TIMEOUT_MS = 25_000
DELIVERY_RETRY_SECONDS = 5.0


async def _delivery_loop(inbox: socket_delivery.InboxSocket | codex_delivery.CodexInbox) -> None:
    """Drain Matrix through the receiving host's adapter.

    Claude failures hand delivery back to hooks. Codex failures retain the
    same batch and retry with fresh runtime state, preserving invite backfill.
    Only accepted batches advance the shared checkpoint; ambiguous transport
    failures may duplicate delivery. Superseded servers stop before sending.
    """
    backend = _state.matrix
    checkpoint = backend.checkpoint_file()
    while backend.owns_session_state():
        try:
            since = checkpoint.read_text().strip() or None
        except OSError:
            since = None
        try:
            messages, next_batch = await backend.sync_once(
                since, timeout_ms=DELIVERY_SYNC_TIMEOUT_MS)
        except (hive_matrix.MatrixError, httpx.HTTPError, OSError) as exc:
            log.warning("delivery sync error (retrying): %s", exc)
            await asyncio.sleep(DELIVERY_RETRY_SECONDS)
            continue
        if not backend.owns_session_state():
            return
        if messages:
            while True:
                if not backend.owns_session_state():
                    return
                try:
                    adapter = codex_delivery if isinstance(inbox, codex_delivery.CodexInbox) else socket_delivery
                    await adapter.deliver(inbox, render_messages(messages))
                    break
                except (OSError, TimeoutError, ValueError, KeyError) as exc:
                    if isinstance(inbox, codex_delivery.CodexInbox):
                        log.warning("Codex delivery unavailable; retaining batch and retrying: %s", exc)
                        await asyncio.sleep(DELIVERY_RETRY_SECONDS)
                        continue
                    log.warning("inbox socket delivery failed (%s) — "
                                "handing delivery back to the hook", exc)
                    backend.set_delivery_mode("hook")
                    return
            log.info("%s delivered %d message(s)", backend.delivery_mode, len(messages))
            latest = {m["room"]: m["event"] for m in messages
                      if m.get("room") and m.get("event")}
            for room, event in latest.items():
                await backend.mark_read(room, event)
        if next_batch:
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text(next_batch)


async def _stop_delivery() -> None:
    t = _state.delivery_task
    if t and not t.done():
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _state.delivery_task = None


def _text(s: str) -> list[mcp_types.TextContent]:
    return [mcp_types.TextContent(type="text", text=s)]


@server.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="hive_connect",
            description=(
                "Register this session with the hive under a stable agent "
                "identity. Required before any other hive_* tool works. "
                "Provide a one-line `summary` of what this session is "
                "currently working on. On a first attempt, pass "
                "`summary`, `cwd`, and known connection/transport fields: if this connection is already "
                "mapped to an agent it reconnects silently; otherwise the "
                "tool returns candidate identities and a suggested new name "
                "for the user to choose from — never invent a `name` "
                "yourself. Then retry with the user's choice as `name` (or "
                "`agent_id`). Pass `attach=true` only when the user "
                "explicitly chose to attach to an identity that is already "
                "active from another session."
            ),
            inputSchema={
                "type": "object",
                "required": ["summary"],
                "properties": {
                    "summary": {"type": "string"},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "agent_id": {"type": "string"},
                    "cwd": {"type": "string"},
                    "client": {"type": "string"},
                    "connection_id": {"type": "string"},
                    "codex_socket": {"type": "string", "description":
                        "Absolute control socket path of the app-server owning this Codex thread. "
                        "Requires connection_id=codex:<real-thread-id>; alternatively HIVE_CODEX_SOCKET."},
                    "attach": {"type": "boolean"},
                },
            },
        ),
        mcp_types.Tool(
            name="hive_status",
            description=(
                "Show this session's hive identity (agent name, agent_id, "
                "connection, code version) and the currently-connected "
                "peers. Works even when this server process has not called "
                "hive_connect yet, by reading the connection's stored "
                "credentials."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="hive_disconnect",
            description="Leave the hive cleanly. No-op if not connected.",
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="hive_list_peers",
            description=(
                "List currently-connected hive peers with their names, "
                "summaries, cwds, and last-seen timestamps."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        mcp_types.Tool(
            name="hive_send",
            description=(
                "Send a message to another hive peer by name. Fire-and-forget; "
                "returns once the homeserver has accepted the message. The "
                "peer's hook will inject it as a <hive-message> block. When "
                "REPLYING to a received <hive-message>, pass `room`=<the "
                "block's room attribute> so the reply lands in the same "
                "conversation. When starting a new conversation, pass "
                "`room_name`: a short label for the conversation topic (e.g. "
                "'hcvls9 staleness investigation') shown in the human "
                "observer's Matrix client. Keep `body` terse: the recipient "
                "pays a full turn per message. Facts and absolute file paths "
                "only, one batched message rather than several, and no "
                "acknowledgement-only replies."
            ),
            inputSchema={
                "type": "object",
                "required": ["peer", "body"],
                "properties": {
                    "peer": {"type": "string"},
                    "body": {"type": "string"},
                    "room": {"type": "string", "maxLength": 128},
                    "room_name": {"type": "string", "maxLength": 80},
                },
            },
        ),
        mcp_types.Tool(
            name="hive_read_messages",
            description=(
                "Read this session's message history (both sent and received). "
                "Useful for catching up on past conversations."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
            },
        ),
        mcp_types.Tool(
            name="hive_post",
            description=(
                "Post a standalone update to the shared #hive-bulletin room, "
                "readable by all agents, current and future. Use when the "
                "user says to post/share something with the hive, or on your "
                "own judgement when you have established something other "
                "agents will need: a root cause, an incident timeline, the "
                "current state of shared data, a deploy or config change. "
                "Write it self-contained (context, finding, pointers — file "
                "paths, ticket ids, timestamps); it is a pinboard entry, not "
                "a conversation starter, and no one is notified. Do not post "
                "routine progress or questions (use hive_send for those)."
            ),
            inputSchema={
                "type": "object",
                "required": ["body"],
                "properties": {"body": {"type": "string"}},
            },
        ),
        mcp_types.Tool(
            name="hive_read_bulletin",
            description=(
                "Read recent posts from the shared #hive-bulletin room. "
                "Check this when starting an investigation to pick up "
                "findings other agents (or the human) have already posted."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        ),
        mcp_types.Tool(
            name="hive_set_summary",
            description="Update this session's one-line summary in the hive directory.",
            inputSchema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    if name == "hive_connect":
        return await _tool_connect(arguments)
    if name == "hive_status":
        return await _tool_status()
    if name == "hive_disconnect":
        return await _tool_disconnect()
    if name == "hive_list_peers":
        return await _tool_list_peers()
    if name == "hive_send":
        return await _tool_send(arguments)
    if name == "hive_read_messages":
        return await _tool_read_messages(arguments)
    if name == "hive_post":
        return await _tool_post(arguments)
    if name == "hive_read_bulletin":
        return await _tool_read_bulletin(arguments)
    if name == "hive_set_summary":
        return await _tool_set_summary(arguments)
    return _text(f"unknown tool: {name}")


def _ago(ts: float) -> str:
    age = max(0, time.time() - ts)
    if age < 60:
        return "active now"
    if age < 3600:
        return f"active {int(age // 60)}m ago"
    if age < 86400:
        return f"active {int(age // 3600)}h ago"
    return f"active {int(age // 86400)}d ago"


def _title_hint(conn: Connection) -> str | None:
    """A naming suggestion, never identity: the zellij tab name when the user
    has titled the tab (zellij_tab_name filters generic 'Tab #N' defaults),
    else the Claude session's /rename title. Tab names win because users who
    run one agent per tab name tabs after the agent, while Claude titles are
    often auto-derived. Codex exposes no title, but a tab name still works."""
    tab = zellij_tab_name()
    if tab:
        return tab
    if conn.client != "claude":
        return None
    name = session_name_for_session_id(conn.session_id, _sessions_dir())
    return name or session_name_for_pid(_session_pid())


def _selection_text(reg: dict, conn: Connection, peers: list[dict]) -> str:
    """The structured "needs identity selection" response: known identities
    plus a suggested new name, for the skill to put in front of the user."""
    live = {p["name"]: p for p in peers}
    lines = [
        "identity selection required — this connection is not mapped to an "
        "agent yet. Known identities:",
    ]
    hint = _title_hint(conn)
    n = 0
    for agent in registry.candidates(reg):
        n += 1
        peer = live.get(agent["name"])
        if peer:
            state = f"ACTIVE ({_ago(peer.get('last_active', 0))}, attach=true required)"
        else:
            state = f"inactive, last seen {_ago(agent.get('last_seen', 0)).replace('active ', '')}"
        match = "  ← matches this pane's tab/title" if agent["name"] == hint else ""
        lines.append(f"  {n}. {agent['name']}  — {state}, "
                     f"cwd={agent.get('last_cwd') or '?'}{match}")
    suggested = registry.suggest_name(reg, hint)
    lines.append(f"  {n + 1}. new: {suggested}")
    lines.append(
        "Ask the user to choose (terse replies like '2' or 'new <name>' are "
        "fine), then retry hive_connect with name=<their choice> and the "
        "same summary. Only pass attach=true if the user explicitly chose "
        "an ACTIVE identity."
    )
    return "\n".join(lines)


async def _tool_connect(args: dict[str, Any]) -> list[mcp_types.TextContent]:
    if _state.name:
        return _text(
            f"already connected as '{_state.name}'. Disconnect first to change identity."
        )
    conn = (resolve_connection(args["connection_id"], args.get("client"))
            if args.get("connection_id") else _state.connection or resolve_connection(
                client_hint=args.get("client")))
    hook_delivery = conn is not None and not conn.session_id.startswith("eph")
    codex_inbox = None
    try:
        if args.get("codex_socket") and (not conn or conn.client != "codex"):
            return _text("codex_socket requires connection_id=codex:<real-thread-id>.")
        if conn and conn.client == "codex":
            codex_inbox = codex_delivery.discover(conn.session_id, args.get("codex_socket"))
            if codex_inbox:
                await codex_delivery.probe(codex_inbox)
    except (OSError, TimeoutError, ValueError, KeyError) as exc:
        return _text(f"Codex delivery setup failed: {exc}. No identity was connected.")
    if conn is None:
        conn = Connection(args.get("client") or "local", "eph" + os.urandom(5).hex())
    _state.connection = conn

    reg = registry.load()
    requested_name = (args.get("name") or "").strip()
    requested_id = (args.get("agent_id") or "").strip()
    attach = bool(args.get("attach"))
    summary = args["summary"].strip()
    cwd = args.get("cwd") or os.getcwd()

    backend = hive_matrix.MatrixBackend(
        httpx.AsyncClient(base_url=hive_matrix.MATRIX_URL, timeout=10.0),
        conn.session_id,
    )
    try:
        await backend.ensure_reader_account()
        await backend.ensure_directory_room()
        peers = await backend.peers()
    except (hive_matrix.MatrixError, httpx.HTTPError, OSError) as exc:
        log.warning("matrix connect failed: %s", exc)
        await backend.http.aclose()
        return _text(f"matrix connect failed: {exc}")

    async def _bail(msg: str) -> list[mcp_types.TextContent]:
        await backend.http.aclose()
        return _text(msg)

    agent: dict | None = None
    if requested_id:
        agent = registry.agent_by_id(reg, requested_id)
        if agent is None:
            return await _bail(
                f"no agent with agent_id '{requested_id}' in the registry.\n"
                + _selection_text(reg, conn, peers))
        name = agent["name"]
    elif requested_name:
        name = requested_name
        agent = registry.agent_by_name(reg, name)
    else:
        agent = registry.agent_for_connection(reg, conn.connection_id)
        if agent is None:
            log.info("connect: no identity for %s — returning candidates",
                     conn.connection_id)
            return await _bail(_selection_text(reg, conn, peers))
        name = agent["name"]

    live = next((p for p in peers if p["name"] == name
                 and time.time() - p.get("last_active", 0) < hive_matrix.PEER_STALE_SECONDS
                 and p.get("connection_id") != conn.connection_id), None)
    if live and not attach:
        return await _bail(
            f"'{name}' is already ACTIVE in the hive from another connection "
            f"({live.get('connection_id') or live['user_id']}). Ask the user: "
            f"attach to it anyway (retry with attach=true) or pick a "
            f"different name.\n" + _selection_text(reg, conn, peers))

    agent = registry.ensure_agent(reg, name, cwd)
    registry.bind_connection(reg, conn.connection_id, agent, conn.client)
    registry.save(reg)
    log.info("connect: %s -> agent '%s' [%s]",
             conn.connection_id, name, agent["agent_id"])

    backend.name = name
    backend.version = CODE_VERSION
    backend.agent_id = agent["agent_id"]
    backend.client_name = conn.client
    backend.connection_id = conn.connection_id
    backend.delivery_mode = "codex-app-server" if codex_inbox else ("hook" if hook_delivery else "poll")
    try:
        await backend.ensure_agent_account(agent["agent_id"])
        await backend.ensure_directory_room()
        await backend.ensure_bulletin_room()
        try:
            await backend.set_displayname(name)
        except (hive_matrix.MatrixError, httpx.HTTPError) as exc:
            log.warning("set displayname failed: %s", exc)
        await backend.publish_peer(name, summary, cwd)
        await backend.announce(f"● {name} connected — {summary}")
        await backend.init_hook_state(name)
    except (hive_matrix.MatrixError, httpx.HTTPError, OSError) as exc:
        log.warning("matrix connect failed: %s", exc)
        await backend.http.aclose()
        return _text(f"matrix connect failed: {exc}")
    _state.matrix = backend
    _state.name, _state.summary, _state.cwd = name, summary, cwd
    _state.agent_id = agent["agent_id"]
    _state.heartbeat_task = asyncio.create_task(_heartbeat_loop())
    log.info("matrix connect: %s as %s", name, backend.user_id)

    inbox = None
    if hook_delivery and conn.client == "claude":
        inbox = socket_delivery.discover(
            hive_matrix.matrix_dir(), conn.session_id,
            _session_pid() if conn.client == "claude" else None)
    if codex_inbox:
        backend.set_delivery_mode("codex-app-server")
        _state.delivery_task = asyncio.create_task(_delivery_loop(codex_inbox))
        delivery = ("Delivery: codex-app-server. Inbound messages steer the active turn "
                    "or start a turn when idle. No heartbeat prompt is needed.")
    elif inbox:
        backend.set_delivery_mode("socket")
        _state.delivery_task = asyncio.create_task(_delivery_loop(inbox))
        log.info("socket delivery active: %s", inbox.path)
        delivery = (
            "Inbound peer messages are pushed straight into this session "
            "via its native inbox socket — they arrive between tool calls "
            "and wake the session when idle (no /loop heartbeat needed)."
        )
    elif hook_delivery:
        delivery = (
            "Inbound peer messages are delivered by the claude-hive hook at "
            "your next tool call, user prompt, or turn end."
        )
    else:
        delivery = (
            "No host session id was found, so hook delivery is inactive — poll "
            "with hive_read_messages instead."
        )
    backend.set_delivery_mode(backend.delivery_mode)
    try:
        await backend.publish_peer(name, summary, cwd)
    except (hive_matrix.MatrixError, httpx.HTTPError, OSError) as exc:
        log.warning("delivery status publication failed; heartbeat will retry: %s", exc)
    return _text(
        f"connected to hive as '{name}' [{agent['agent_id']}, "
        f"{conn.connection_id}]. {delivery} Use hive_list_peers to see who "
        f"else is connected, and hive_read_bulletin for findings other "
        f"agents have shared."
    )


async def _tool_status() -> list[mcp_types.TextContent]:
    """Identity + peer list, without requiring hive_connect in this process:
    a previous server for the same connection may have registered already."""
    backend = _state.matrix
    own_backend = False
    if backend is None:
        conn = _state.connection or resolve_connection()
        account = matrix_account(conn.session_id) if conn else None
        if not account:
            return _text("not connected — run the hive-connect skill to join the hive.")
        backend = hive_matrix.MatrixBackend(
            httpx.AsyncClient(base_url=hive_matrix.MATRIX_URL, timeout=10.0),
            conn.session_id,
        )
        backend.user_id = account["user_id"]
        backend.access_token = account["access_token"]
        backend.name = account.get("name")
        own_backend = True
    try:
        await backend.ensure_directory_room()
        peers = await backend.peers()
    except (hive_matrix.MatrixError, httpx.HTTPError, OSError) as exc:
        if own_backend:
            await backend.http.aclose()
        return _text(f"hive unreachable: {exc} — is the homeserver up? "
                     f"Diagnose with `python3 scripts/setup.py --check`.")
    me = next((p for p in peers if p["user_id"] == backend.user_id), None)
    lines = [f"me: {backend.name} "
             f"[{(me or {}).get('agent_id') or _state.agent_id or '?'}] "
             f"[{backend.user_id}] code={CODE_VERSION}"]
    now = time.time()
    drifted = 0
    for p in peers:
        marker = " (me)" if p["user_id"] == backend.user_id else ""
        stale = " [stale]" if now - p.get("last_active", 0) > hive_matrix.PEER_STALE_SECONDS else ""
        drift = ""
        if not marker and p.get("version") != CODE_VERSION:
            drift = f" [code: {p.get('version') or 'pre-versioning'}]"
            drifted += 1
        lines.append(f"- {p['name']}{marker}{stale}{drift} "
                     f"[client={p.get('client') or 'unknown'}, "
                     f"delivery={p.get('delivery') or 'unknown'}]: "
                     f"{p.get('summary') or '(no summary)'}"
                     f"  [cwd: {p.get('cwd') or ''}]")
    if drifted:
        lines.append(f"fleet drift: {drifted} peer(s) not on {CODE_VERSION} — "
                     f"they need an MCP reconnect + hive-connect")
    if own_backend:
        await backend.http.aclose()
    return _text("\n".join(lines))


async def _tool_disconnect() -> list[mcp_types.TextContent]:
    if not _state.name:
        return _text("not connected.")
    name = _state.name
    await _stop_heartbeat()
    await _stop_delivery()
    try:
        await _state.matrix.publish_peer(name, _state.summary, _state.cwd, offline=True)
        await _state.matrix.announce(f"○ {name} went offline")
    except Exception as exc:
        log.warning("matrix offline publish error: %s", exc)
    _state.matrix.clear_hook_state()
    await _state.matrix.http.aclose()
    if _state.connection:
        reg = registry.load()
        registry.release_connection(reg, _state.connection.connection_id)
        registry.save(reg)
    _state.matrix = None
    _state.name = None
    _state.agent_id = None
    _state.connection = None
    return _text(f"disconnected '{name}' from hive.")


async def _tool_list_peers() -> list[mcp_types.TextContent]:
    if not _state.matrix:
        return _text("not connected. Call hive_connect first.")
    try:
        peers = await _state.matrix.peers()
    except (hive_matrix.MatrixError, httpx.HTTPError) as exc:
        return _text(f"homeserver unreachable: {exc}")
    if not peers:
        return _text("no peers connected.")
    lines = []
    drifted = 0
    for p in peers:
        marker = " (me)" if p["user_id"] == _state.matrix.user_id else ""
        age = time.time() - p.get("last_active", 0)
        stale = " [stale]" if age > hive_matrix.PEER_STALE_SECONDS else ""
        drift = ""
        if not marker and p.get("version") != CODE_VERSION:
            drift = f" [code: {p.get('version') or 'pre-versioning'}]"
            drifted += 1
        lines.append(f"- {p['name']}{marker}{stale}{drift} "
                     f"[client={p.get('client') or 'unknown'}, "
                     f"delivery={p.get('delivery') or 'unknown'}]: "
                     f"{p.get('summary') or '(no summary)'}"
                     f"  [cwd: {p.get('cwd') or ''}]")
    if drifted:
        lines.append(f"({drifted} peer(s) running different code than me "
                     f"[{CODE_VERSION}] — they need /mcp reconnect + hive_connect)")
    return _text("\n".join(lines))


async def _tool_send(args: dict[str, Any]) -> list[mcp_types.TextContent]:
    if not _state.matrix:
        return _text("not connected. Call hive_connect first.")
    peer = args["peer"].strip()
    body = args["body"]
    if peer == _state.name:
        return _text("can't send to self.")
    room_name = (args.get("room_name") or "").strip() or None
    room = (args.get("room") or "").strip() or None
    try:
        event_id = await _state.matrix.send(peer, body, room_name=room_name,
                                            room=room)
    except (hive_matrix.MatrixError, httpx.HTTPError) as exc:
        return _text(f"send failed: {exc}")
    log.debug("matrix sent %s to '%s' (%d chars)", event_id, peer, len(body))
    return _text(f"sent message {event_id} to '{peer}'.")


async def _tool_read_messages(args: dict[str, Any]) -> list[mcp_types.TextContent]:
    if not _state.matrix:
        return _text("not connected.")
    limit = int(args.get("limit") or 50)
    try:
        msgs = await _state.matrix.recent_messages(limit)
    except (hive_matrix.MatrixError, httpx.HTTPError) as exc:
        return _text(f"homeserver unreachable: {exc}")
    if not msgs:
        return _text("no messages.")
    return _text("\n".join(
        f"{'→' if m['sender'] == _state.matrix.user_id else '←'} "
        f"{m['from']}: {m['body']}" for m in msgs))


async def _tool_post(args: dict[str, Any]) -> list[mcp_types.TextContent]:
    if not _state.matrix:
        return _text("not connected. Call hive_connect first.")
    body = args["body"].strip()
    if not body:
        return _text("empty post.")
    try:
        event_id = await _state.matrix.post_bulletin(body)
    except (hive_matrix.MatrixError, httpx.HTTPError) as exc:
        return _text(f"post failed: {exc}")
    return _text(f"posted {event_id} to #hive-bulletin.")


async def _tool_read_bulletin(args: dict[str, Any]) -> list[mcp_types.TextContent]:
    if not _state.matrix:
        return _text("not connected. Call hive_connect first.")
    limit = int(args.get("limit") or 20)
    try:
        posts = await _state.matrix.read_bulletin(limit)
    except (hive_matrix.MatrixError, httpx.HTTPError) as exc:
        return _text(f"homeserver unreachable: {exc}")
    if not posts:
        return _text("no bulletin posts.")
    lines = []
    for p in posts:
        day = time.strftime("%Y-%m-%d %H:%M", time.localtime(p["ts"]))
        lines.append(f"[{day}] {p['from']}: {p['body']}")
    return _text("\n".join(lines))


async def _tool_set_summary(args: dict[str, Any]) -> list[mcp_types.TextContent]:
    if not _state.matrix:
        return _text("not connected.")
    summary = args["summary"]
    try:
        await _state.matrix.publish_peer(_state.name, summary, _state.cwd)
    except (hive_matrix.MatrixError, httpx.HTTPError) as exc:
        return _text(f"set-summary failed: {exc}")
    _state.summary = summary
    return _text("summary updated.")


async def _arun() -> None:
    init_opts = server.create_initialization_options()
    async with stdio_server() as (read, write):
        try:
            await server.run(read, write, init_opts)
        finally:
            await _stop_heartbeat()
            await _stop_delivery()
            if _state.matrix and _state.matrix.owns_session_state():
                try:
                    await _state.matrix.publish_peer(
                        _state.name, _state.summary, _state.cwd, offline=True
                    )
                except Exception:
                    pass
                _state.matrix.clear_hook_state()
            if _state.matrix:
                await _state.matrix.http.aclose()


def _configure_logging() -> None:
    """Rotating file log per server process under ~/.claude-hive/logs/.

    claude_hive loggers run at DEBUG so the first weeks of use are fully
    inspectable; third-party loggers stay at HIVE_LOG_LEVEL (default INFO)
    to keep httpx/mcp noise out. HIVE_LOG_FILE overrides the path, and
    HIVE_LOG_FILE=stderr restores the old stderr behaviour.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_file = os.environ.get("HIVE_LOG_FILE")
    if log_file == "stderr":
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
    else:
        if not log_file:
            log_dir = Path(os.environ.get("HIVE_LOG_DIR", Path.home() / ".claude-hive" / "logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = str(log_dir / f"mcp-{os.getpid()}.log")
        handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=2)
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(os.environ.get("HIVE_LOG_LEVEL", "INFO").upper())
    logging.getLogger("claude_hive").setLevel(logging.DEBUG)


def main() -> None:
    _configure_logging()
    log.info("claude-hive-server starting: pid=%s ppid=%s matrix=%s",
             os.getpid(), os.getppid(), hive_matrix.MATRIX_URL)
    try:
        asyncio.run(_arun())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

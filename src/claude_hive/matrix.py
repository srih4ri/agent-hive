"""Matrix backend for claude-hive.

Agents are Matrix accounts on a local homeserver (Continuwuity); sessions
are devices on the agent's account (AGENT_GUIDE.md). Discovery
lives in the #hive directory room as `hive.peer` state events keyed by the
agent's user id — one entry per agent by construction. Pair DM rooms are
recorded in the agent account's `m.direct` account data (homeserver-side,
multi-machine safe). Delivery is either the MCP server's socket loop (see
socket_delivery.py) or the hook's drain — both walk `/sync` using the
checkpoint files written here, and the `delivery` marker in by-session says
which one currently owns it.

Filesystem layout under ~/.claude-hive/matrix/:
  registration_token          shared secret for account provisioning
  accounts/<localpart>.json   {user_id, password} — agent accounts, plus the
                              per-machine bootstrap reader
  by-session/<session_id>.json  {user_id, access_token, name, sync_key} —
                              read by the hook
  sync/<sync_key>             next_batch checkpoint; keyed by the agent
                              localpart so undelivered mail survives session
                              churn
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
import urllib.parse
from pathlib import Path

import httpx

from claude_hive import hook as hook_helpers

MATRIX_URL = os.environ.get("HIVE_MATRIX_URL", "http://127.0.0.1:8008")
SERVER_NAME = os.environ.get("HIVE_MATRIX_SERVER_NAME", "hive.local")
DIRECTORY_ALIAS = f"#hive:{SERVER_NAME}"
BULLETIN_ALIAS = f"#hive-bulletin:{SERVER_NAME}"
PEER_STALE_SECONDS = 300
HUMAN_MXID = os.environ.get("HIVE_HUMAN_MXID", "")

log = logging.getLogger("claude_hive.matrix")


def matrix_dir() -> Path:
    return Path(os.environ.get("CLAUDE_HIVE_MATRIX_DIR", Path.home() / ".claude-hive" / "matrix"))


def localpart_for_agent(agent_id: str) -> str:
    """The agent's permanent account localpart, derived from the immutable
    agent_id ("agent_<16 hex>" from registry.new_agent_id) — never from the
    display name, which stays renameable."""
    return "a" + re.sub(r"[^a-z0-9]", "", agent_id.lower().removeprefix("agent_"))


def device_id_for_session(session_id: str) -> str:
    """Device ids are opaque to Matrix but shown in admin tooling — keep the
    full session id, sanitized to be shell/URL-friendly."""
    return re.sub(r"[^A-Za-z0-9._=-]", "_", session_id)[:64]


def freshest_peer(entries: list[dict]) -> dict | None:
    """The entry to trust when several directory entries share a name: the
    one with the newest heartbeat. One agent account means one entry per
    name on a single machine, but two machines can independently register
    the same name — resolving to the freshest beats first-match, which is
    how replies once got routed to a dead account."""
    return max(entries, key=lambda p: p.get("last_active", 0), default=None)


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


class MatrixError(Exception):
    pass


class MatrixBackend:
    """One instance per MCP server process; owns this session's account."""

    def __init__(self, http: httpx.AsyncClient, session_id: str):
        self.http = http
        self.session_id = session_id
        self.user_id: str | None = None
        self.access_token: str | None = None
        self.name: str | None = None
        self.version: str | None = None
        self.sync_key: str | None = None
        self.agent_id: str | None = None
        self.client_name: str | None = None
        self.connection_id: str | None = None
        self.delivery_mode: str = "poll"
        self.directory_room: str | None = None
        self.bulletin_room: str | None = None


    async def _call(self, method: str, path: str, body: dict | None = None,
                    authed: bool = True, uia_ok: bool = False,
                    timeout: float | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token}"} if authed else {}
        r = await self.http.request(method, f"/_matrix/client/v3{path}",
                                    json=body, headers=headers,
                                    **({"timeout": timeout} if timeout else {}))
        data = r.json()
        if r.status_code >= 400:
            if uia_ok and r.status_code == 401 and "session" in data:
                return data
            raise MatrixError(f"{method} {path} -> {r.status_code}: "
                              f"{data.get('errcode')} {data.get('error')}")
        return data


    async def _register(self, localpart: str, password: str,
                        extra: dict | None = None) -> dict:
        token_file = matrix_dir() / "registration_token"
        reg_token = token_file.read_text().strip()
        body = {"username": localpart, "password": password, **(extra or {})}
        first = await self._call("POST", "/register", body,
                                 authed=False, uia_ok=True)
        if "session" not in first:
            return first
        return await self._call("POST", "/register", {
            **body,
            "auth": {"type": "m.login.registration_token",
                     "token": reg_token, "session": first["session"]},
        }, authed=False)

    async def _ensure_localpart(self, localpart: str,
                                login_extra: dict | None = None) -> None:
        """Log into accounts/<localpart>.json, registering it first if this
        machine has never seen it. login_extra rides on both calls (device_id
        etc. — servers accept it on /register too)."""
        accounts = matrix_dir() / "accounts"
        accounts.mkdir(parents=True, exist_ok=True)
        account_file = accounts / f"{localpart}.json"
        if account_file.exists():
            stored = json.loads(account_file.read_text())
            login = await self._call("POST", "/login", {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": localpart},
                "password": stored["password"],
                **(login_extra or {}),
            }, authed=False)
        else:
            password = os.urandom(16).hex()
            login = await self._register(localpart, password, login_extra)
            account_file.write_text(json.dumps(
                {"user_id": login["user_id"], "password": password}))
        self.user_id = login["user_id"]
        self.access_token = login["access_token"]

    async def ensure_agent_account(self, agent_id: str) -> None:
        """The agent's permanent account; this session becomes one device on
        it, so mail addressed to the agent outlives any single session."""
        localpart = localpart_for_agent(agent_id)
        await self._ensure_localpart(localpart, {
            "device_id": device_id_for_session(self.session_id),
            "initial_device_display_name": self.connection_id or self.session_id,
        })
        self.sync_key = localpart

    async def ensure_reader_account(self) -> None:
        """Per-machine bootstrap reader: hive_connect needs a directory read
        for the identity-selection menu before any agent is chosen. Joins no
        DM rooms, sends nothing, publishes no hive.peer entry."""
        host = re.sub(r"[^a-z0-9.-]", "-", socket.gethostname().lower())[:32]
        await self._ensure_localpart(f"lookout-{host or 'unknown'}")

    async def set_displayname(self, name: str) -> None:
        """Global profile displayname, so Matrix clients show the session's
        /rename title instead of the session-id localpart."""
        await self._call("PUT", f"/profile/{_quote(self.user_id)}/displayname",
                         {"displayname": name})


    async def ensure_directory_room(self) -> str:
        try:
            resolved = await self._call("GET", f"/directory/room/{_quote(DIRECTORY_ALIAS)}",
                                        authed=False)
            room_id = resolved["room_id"]
        except MatrixError:
            created = await self._call("POST", "/createRoom", {
                "room_alias_name": "hive",
                "name": "hive directory",
                "preset": "public_chat",
                "visibility": "private",
                "power_level_content_override": {
                    "events": {"hive.peer": 0},
                    "users_default": 0,
                },
            })
            room_id = created["room_id"]
        await self._call("POST", f"/join/{_quote(room_id)}", {})
        self.directory_room = room_id
        return room_id

    async def ensure_bulletin_room(self) -> str:
        try:
            resolved = await self._call("GET", f"/directory/room/{_quote(BULLETIN_ALIAS)}",
                                        authed=False)
            room_id = resolved["room_id"]
        except MatrixError:
            created = await self._call("POST", "/createRoom", {
                "room_alias_name": "hive-bulletin",
                "name": "hive bulletin",
                "topic": ("Standalone findings relevant to all agents: root causes, "
                          "timelines, shared-data state. A pinboard, not a conversation."),
                "preset": "public_chat",
                "visibility": "private",
            })
            room_id = created["room_id"]
            if HUMAN_MXID:
                await self._join_human(room_id, HUMAN_MXID)
        await self._call("POST", f"/join/{_quote(room_id)}", {})
        self.bulletin_room = room_id
        return room_id

    async def post_bulletin(self, body: str) -> str:
        """m.notice on purpose: the delivery hook skips notices, so a post
        lands on the pinboard without interrupting every active agent."""
        txn = f"hive-bulletin-{time.time_ns()}"
        sent = await self._call("PUT",
                                f"/rooms/{_quote(self.bulletin_room)}/send/m.room.message/{txn}",
                                {"msgtype": "m.notice", "body": body, "hive_from": self.name})
        return sent["event_id"]

    async def read_bulletin(self, limit: int = 20) -> list[dict]:
        page = await self._call("GET",
                                f"/rooms/{_quote(self.bulletin_room)}/messages?dir=b&limit={limit}")
        out = []
        for ev in page.get("chunk", []):
            if ev.get("type") != "m.room.message":
                continue
            c = ev.get("content") or {}
            out.append({"from": c.get("hive_from") or ev["sender"].split(":")[0].lstrip("@"),
                        "body": c.get("body", ""),
                        "ts": ev.get("origin_server_ts", 0) / 1000})
        return sorted(out, key=lambda m: m["ts"])

    async def publish_peer(self, name: str, summary: str, cwd: str,
                           offline: bool = False) -> None:
        await self._call("PUT",
                         f"/rooms/{_quote(self.directory_room)}/state/hive.peer/{_quote(self.user_id)}",
                         {"name": name, "agent_id": self.agent_id,
                          "client": self.client_name, "delivery": self.delivery_mode,
                          "connection_id": self.connection_id,
                          "summary": summary, "cwd": cwd,
                          "offline": offline, "version": self.version,
                          "last_active": int(time.time())})

    async def announce(self, text: str) -> None:
        """Presence feed for humans watching #hive. m.notice on purpose —
        the hook skips notices so agents never consume these."""
        txn = f"hive-notice-{time.time_ns()}"
        await self._call("PUT",
                         f"/rooms/{_quote(self.directory_room)}/send/m.room.message/{txn}",
                         {"msgtype": "m.notice", "body": text})

    async def peers(self) -> list[dict]:
        state = await self._call("GET", f"/rooms/{_quote(self.directory_room)}/state")
        out = []
        for ev in state if isinstance(state, list) else []:
            if ev.get("type") != "hive.peer":
                continue
            content = ev.get("content") or {}
            if not content.get("name") or content.get("offline"):
                continue
            out.append({"user_id": ev["state_key"], **content})
        return sorted(out, key=lambda p: p["name"])

    async def resolve_peer(self, name: str) -> str | None:
        entry = freshest_peer([p for p in await self.peers() if p["name"] == name])
        return entry["user_id"] if entry else None


    async def _direct_rooms(self) -> dict:
        """This account's m.direct account data: {user_id: [room_ids]}."""
        try:
            return await self._call(
                "GET", f"/user/{_quote(self.user_id)}/account_data/m.direct")
        except MatrixError:
            return {}

    async def record_direct_room(self, other_user: str, room_id: str) -> None:
        direct = await self._direct_rooms()
        rooms = direct.setdefault(other_user, [])
        if room_id in rooms:
            return
        rooms.append(room_id)
        rooms.sort()
        await self._call("PUT",
                         f"/user/{_quote(self.user_id)}/account_data/m.direct",
                         direct)

    async def _prepare_room(self, room_id: str, label: str,
                            rename: bool, human: str | None) -> None:
        """Reused-room upkeep: name it (explicit room_name renames; otherwise
        self-heal unnamed rooms) and re-invite the human observer."""
        try:
            await self._ensure_named(room_id, label, rename=rename)
        except MatrixError as exc:
            log.warning("room rename failed for %s: %s", room_id, exc)
        if human:
            try:
                await self._ensure_human(room_id, human)
            except Exception as exc:
                log.warning("observer backfill failed for %s: %s", room_id, exc)

    async def ensure_dm_room(self, other_user: str, peer_name: str | None = None,
                             room_name: str | None = None) -> str:
        label = room_name or f"{self.name} ↔ {peer_name or other_user}"
        human = HUMAN_MXID if HUMAN_MXID and HUMAN_MXID != self.user_id else None
        direct = await self._direct_rooms()
        rooms = sorted(direct.get(other_user) or [])
        if rooms:
            await self._prepare_room(rooms[0], label, bool(room_name), human)
            return rooms[0]
        invitees = [other_user] + ([human] if human else [])
        created = await self._call("POST", "/createRoom", {
            "name": label,
            "is_direct": True,
            "invite": invitees,
            "preset": "trusted_private_chat",
            "power_level_content_override": {
                "users": {u: 100 for u in [self.user_id, *invitees]},
            },
        })
        await self.record_direct_room(other_user, created["room_id"])
        if human:
            await self._join_human(created["room_id"], human)
        return created["room_id"]

    async def set_room_name(self, room_id: str, name: str) -> None:
        await self._call("PUT", f"/rooms/{_quote(room_id)}/state/m.room.name",
                         {"name": name})

    async def _ensure_named(self, room_id: str, label: str, rename: bool = False) -> None:
        if not rename:
            try:
                existing = (await self._call(
                    "GET", f"/rooms/{_quote(room_id)}/state/m.room.name")).get("name")
                if existing:
                    return
            except MatrixError:
                pass
        await self.set_room_name(room_id, label)

    async def _ensure_human(self, room_id: str, human: str) -> None:
        try:
            member = await self._call(
                "GET", f"/rooms/{_quote(room_id)}/state/m.room.member/{_quote(human)}")
            membership = member.get("membership")
        except MatrixError:
            membership = None
        if membership == "join":
            return
        if membership != "invite":
            await self._call("POST", f"/rooms/{_quote(room_id)}/invite", {"user_id": human})
        await self._join_human(room_id, human)

    async def _join_human(self, room_id: str, human: str) -> None:
        """Auto-accept the observer invite so agent rooms just appear in the
        human's client. Best effort — without stored credentials the invite
        stays pending for a manual accept."""
        localpart = human.split(":")[0].lstrip("@")
        account_file = matrix_dir() / "accounts" / f"{localpart}.json"
        if not account_file.exists():
            return
        try:
            stored = json.loads(account_file.read_text())
            login = await self._call("POST", "/login", {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": localpart},
                "password": stored["password"],
            }, authed=False)
            r = await self.http.post(
                f"/_matrix/client/v3/join/{_quote(room_id)}",
                json={},
                headers={"Authorization": f"Bearer {login['access_token']}"})
            r.raise_for_status()
        except Exception as exc:
            log.warning("human auto-join failed for %s: %s", room_id, exc)

    async def send(self, peer_name: str, body: str, room_name: str | None = None,
                   room: str | None = None) -> str:
        """Send to a peer. When `room` is given (a reply to a received
        <hive-message>, which carries its room id) the message goes straight
        there — replying never re-runs name resolution, so it cannot
        misroute."""
        if room is None:
            target = await self.resolve_peer(peer_name)
            if target is None:
                raise MatrixError(f"no peer named '{peer_name}' in the hive directory")
            room = await self.ensure_dm_room(target, peer_name=peer_name,
                                             room_name=room_name)
        txn = f"hive-{time.time_ns()}"
        sent = await self._call("PUT", f"/rooms/{_quote(room)}/send/m.room.message/{txn}",
                                {"msgtype": "m.text", "body": body,
                                 "hive_from": self.name})
        return sent["event_id"]

    async def recent_messages(self, limit: int = 20) -> list[dict]:
        rooms = {r for rs in (await self._direct_rooms()).values() for r in rs}
        out: list[dict] = []
        for room in sorted(rooms):
            try:
                page = await self._call("GET",
                                        f"/rooms/{_quote(room)}/messages?dir=b&limit={limit}")
            except MatrixError:
                continue
            for ev in page.get("chunk", []):
                if ev.get("type") == "m.room.message":
                    out.append({"from": (ev.get("content") or {}).get("hive_from")
                                or ev["sender"].split(":")[0].lstrip("@"),
                                "sender": ev["sender"],
                                "body": (ev.get("content") or {}).get("body", ""),
                                "ts": ev.get("origin_server_ts", 0) / 1000})
        return sorted(out, key=lambda m: m["ts"])[-limit:]


    def checkpoint_file(self) -> Path:
        return matrix_dir() / "sync" / (self.sync_key or self.session_id)

    async def mark_read(self, room_id: str, event_id: str) -> None:
        """Best-effort m.read receipt after a successful delivery, so a
        Matrix client shows which mail an agent has actually consumed."""
        try:
            await self._call("POST",
                             f"/rooms/{_quote(room_id)}/receipt/m.read/{_quote(event_id)}",
                             {})
        except (MatrixError, httpx.HTTPError, OSError) as exc:
            log.debug("read receipt failed for %s: %s", room_id, exc)

    async def sync_once(self, since: str | None,
                        timeout_ms: int = 0) -> tuple[list[dict], str]:
        """One checkpointed /sync step: inbound messages plus the next
        checkpoint. Mirrors the hook's drain (same filtering via
        hook.messages_from_sync, same invite-join + backfill) but leaves
        writing the checkpoint to the caller, so the socket delivery loop
        can advance it only after the message actually reached the session."""
        qs = f"/sync?timeout={timeout_ms}"
        if since:
            qs += f"&since={_quote(since)}"
        sync = await self._call("GET", qs, timeout=timeout_ms / 1000 + 10.0)
        messages = hook_helpers.messages_from_sync(sync, self.user_id)
        for room_id in (sync.get("rooms", {}).get("invite", {}) or {}):
            try:
                await self._call("POST", f"/join/{_quote(room_id)}", {})
                page = await self._call(
                    "GET", f"/rooms/{_quote(room_id)}/messages?dir=b&limit=20")
                backfill = [ev for ev in page.get("chunk", [])
                            if ev.get("type") == "m.room.message"
                            and ev.get("sender") != self.user_id]
                for ev in reversed(backfill):
                    content = ev.get("content") or {}
                    messages.append({"id": ev.get("event_id", "?")[:9],
                                     "event": ev.get("event_id", ""),
                                     "room": room_id,
                                     "from": content.get("hive_from")
                                     or ev["sender"].split(":")[0].lstrip("@"),
                                     "body": content.get("body", ""),
                                     "human": ev.get("sender") == HUMAN_MXID})
                await self._record_invited_dm(room_id)
            except (MatrixError, OSError) as exc:
                log.warning("sync_once join/backfill failed for %s: %s", room_id, exc)
        return messages, sync.get("next_batch", "")

    async def _record_invited_dm(self, room_id: str) -> None:
        """After accepting an invite, record the room in m.direct for each
        agent member so this side reuses it instead of creating a second pair
        room on its first outbound send."""
        try:
            members = await self._call(
                "GET", f"/rooms/{_quote(room_id)}/joined_members")
        except MatrixError:
            return
        for user in members.get("joined", {}):
            if user != self.user_id and user != HUMAN_MXID:
                await self.record_direct_room(user, room_id)

    def set_delivery_mode(self, mode: str) -> None:
        """Mark who owns delivery for this session in the by-session file the
        hook already reads: "socket" or "codex-app-server" suppress hook draining; "hook" and
        "poll" describe connections without a push adapter. The
        server pid lets the hook reclaim delivery if this process dies."""
        self.delivery_mode = mode
        f = matrix_dir() / "by-session" / f"{self.session_id}.json"
        try:
            stored = json.loads(f.read_text())
        except (OSError, ValueError):
            return
        if stored.get("access_token") != self.access_token:
            return
        stored["client"] = self.client_name
        stored["connection_id"] = self.connection_id
        stored["delivery"] = mode
        stored["delivery_pid"] = os.getpid()
        f.write_text(json.dumps(stored))

    async def init_hook_state(self, name: str) -> None:
        """Hand the hook this session's credentials and make sure a sync
        checkpoint exists. An existing agent-keyed checkpoint is left alone:
        it marks where the previous session stopped draining, so mail that
        arrived while no session was attached is delivered, not dropped."""
        checkpoint = self.checkpoint_file()
        if not (self.sync_key and checkpoint.exists()):
            sync = await self._call("GET", "/sync?timeout=0")
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text(sync["next_batch"])
        by_session = matrix_dir() / "by-session"
        by_session.mkdir(parents=True, exist_ok=True)
        (by_session / f"{self.session_id}.json").write_text(json.dumps(
            {"user_id": self.user_id, "access_token": self.access_token,
             "name": name, "sync_key": self.sync_key or self.session_id}))
        pane_key = hook_helpers.zellij_pane_key()
        if pane_key:
            by_pane = matrix_dir() / "by-pane"
            by_pane.mkdir(parents=True, exist_ok=True)
            (by_pane / f"{pane_key}.json").write_text(json.dumps(
                {"session_id": self.session_id}))

    def owns_session_state(self) -> bool:
        """False once a newer server process for this session has connected
        and rewritten by-session with its own login token. /mcp reconnect
        leaves the old server process alive (its stdio never closes), so the
        superseded one must detect this and stand down — otherwise it keeps
        heartbeating stale peer state forever."""
        try:
            stored = json.loads(
                (matrix_dir() / "by-session" / f"{self.session_id}.json").read_text())
        except (OSError, ValueError):
            return True
        return stored.get("access_token") == self.access_token

    def clear_hook_state(self) -> None:
        (matrix_dir() / "by-session" / f"{self.session_id}.json").unlink(missing_ok=True)
        pane_key = hook_helpers.zellij_pane_key()
        if pane_key:
            alias = matrix_dir() / "by-pane" / f"{pane_key}.json"
            try:
                if json.loads(alias.read_text()).get("session_id") == self.session_id:
                    alias.unlink()
            except (OSError, ValueError):
                pass

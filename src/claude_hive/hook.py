"""Host hook: pull-based hive message delivery, no channels needed.

Wired for Claude Code in ~/.claude/settings.json and for Codex in
.codex/hooks.json, on UserPromptSubmit, PostToolUse, and Stop. Reads the
hook event from stdin, drains this session's Matrix inbox via a
checkpointed `/sync`, and prints the messages in the shape each hook event
expects, so they land in the session's context. Exits silently on any
failure — a messaging hook must never break the session.

Which Matrix account belongs to this session is keyed by session id: on
hive_connect the MCP server writes ~/.claude-hive/matrix/by-session/
<session_id>.json with the account credentials; the hook reads it back using
the session id from the hook payload. The hook is host-neutral on purpose:
it never needs to know how the agent's name was chosen, only how to map
the current session to Matrix credentials.

Stdlib-only on purpose: hooks run on every tool call, so this is invoked as
`python3 hook.py` without uv/venv startup cost. mcp_server.py imports the
/proc helpers from here; keep this module free of package-relative imports.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

MATRIX_URL = os.environ.get("HIVE_MATRIX_URL", "http://127.0.0.1:8008")
HUMAN_MXID = os.environ.get("HIVE_HUMAN_MXID", "")
MAX_ANCESTOR_HOPS = 15
HTTP_TIMEOUT = 2.0
LOG_MAX_BYTES = 5_000_000


def _log_path() -> str | None:
    path = os.environ.get("HIVE_HOOK_LOG")
    if path == "off":
        return None
    if path:
        return path
    log_dir = Path.home() / ".claude-hive" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return str(log_dir / "hook.log")


def log_line(line: str) -> None:
    """One line per hook firing, shared across sessions; HIVE_HOOK_LOG=off
    disables, any other value overrides the path."""
    path = _log_path()
    if not path:
        return
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {line}\n")
    except OSError:
        pass


def code_version(repo_root: Path | None = None) -> str:
    """Short git SHA of the checkout this code was loaded from — published in
    hive.peer state so fleet drift (long-running servers still on old code)
    is visible to every session."""
    root = repo_root or Path(__file__).resolve().parents[2]
    try:
        head = (root / ".git" / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head[5:]
        ref_file = root / ".git" / ref
        if ref_file.exists():
            return ref_file.read_text().strip()[:7]
        for line in (root / ".git" / "packed-refs").read_text().splitlines():
            if line.endswith(ref):
                return line.split()[0][:7]
    except OSError:
        pass
    return "unknown"


def _proc_stat(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None


def ppid_of(pid: int) -> int | None:
    stat = _proc_stat(pid)
    if stat is None:
        return None
    try:
        return int(stat.rpartition(")")[2].split()[1])
    except (IndexError, ValueError):
        return None


def comm_of(pid: int) -> str | None:
    stat = _proc_stat(pid)
    if stat is None:
        return None
    return stat.partition("(")[2].rpartition(")")[0]


def ancestor_pids(start: int) -> list[int]:
    pids: list[int] = []
    pid: int | None = start
    for _ in range(MAX_ANCESTOR_HOPS):
        if pid is None or pid <= 1:
            break
        pids.append(pid)
        pid = ppid_of(pid)
    return pids


def matrix_dir() -> Path:
    return Path(os.environ.get("CLAUDE_HIVE_MATRIX_DIR", Path.home() / ".claude-hive" / "matrix"))


GENERIC_TAB_NAME = re.compile(r"^tab\s*#?\d+$", re.IGNORECASE)


def zellij_pane_key() -> str | None:
    """Filesystem-safe key for the zellij pane this process runs in, or None
    outside zellij. ZELLIJ_SESSION_NAME/ZELLIJ_PANE_ID are inherited by every
    process in the pane, so the hook and the MCP server always agree on this
    key even when their session ids diverge (claude --resume keeps the
    conversation id in hook payloads but stamps child env at spawn time)."""
    session = os.environ.get("ZELLIJ_SESSION_NAME")
    pane = os.environ.get("ZELLIJ_PANE_ID")
    if not session or not pane:
        return None
    return re.sub(r"[^A-Za-z0-9._-]", "_", session) + f"~{pane}"


def zellij_tab_name() -> str | None:
    """Name of the zellij tab holding this process's pane. None outside
    zellij, on any failure, or for default names like 'Tab #6'. Uses
    list-panes rather than current-tab-info: the latter reports the FOCUSED
    tab, which is wrong whenever the user is looking at a different tab."""
    pane = os.environ.get("ZELLIJ_PANE_ID")
    if not pane:
        return None
    try:
        out = subprocess.run(["zellij", "action", "list-panes", "--tab", "--json"],
                             capture_output=True, text=True, timeout=3).stdout
        panes = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for p in panes:
        if not p.get("is_plugin") and str(p.get("id")) == pane:
            name = (p.get("tab_name") or "").strip()
            if not name or GENERIC_TAB_NAME.match(name):
                return None
            return name
    return None


def stash_inbox_socket(session_id: str | None) -> None:
    """Record this session's native messaging socket for the MCP server.

    Claude Code guarantees CLAUDE_CODE_MESSAGING_SOCKET/_TOKEN only to hooks
    and Bash commands, so the hook is the reliable place to capture them;
    socket_delivery.discover() reads this file back (the shape is duplicated
    there on purpose — this module must stay import-free)."""
    path = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if not session_id or not path:
        return
    stash = matrix_dir() / "sockets" / f"{session_id}.json"
    payload = json.dumps({"socket": path,
                          "token": os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN") or None})
    try:
        if stash.exists() and stash.read_text() == payload:
            return
        stash.parent.mkdir(parents=True, exist_ok=True)
        stash.write_text(payload)
        log_line(f"stashed inbox socket for {session_id}: {path}")
    except OSError:
        pass


def socket_delivery_active(account: dict) -> bool:
    """True while a live MCP server has claimed delivery for this session
    (it pushes messages straight into the inbox socket, so the hook must
    not double-drain the shared checkpoint). A recorded pid that is no
    longer running means the server died without resetting the flag — the
    hook takes delivery back rather than stranding messages."""
    if account.get("delivery") not in {"socket", "codex-app-server"}:
        return False
    pid = account.get("delivery_pid")
    if not isinstance(pid, int):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        pass
    return True


def matrix_account(session_id: str | None) -> dict | None:
    """Credentials written by hive_connect; absent means not connected."""
    if not session_id:
        return None
    try:
        return json.loads((matrix_dir() / "by-session" / f"{session_id}.json").read_text())
    except (OSError, ValueError):
        return None


def resolve_account(session_id: str | None) -> tuple[str | None, dict | None]:
    """Map this hook firing to (owning session id, credentials).

    Direct by-session lookup first. When the payload session id has no
    credentials — session ids drift when a conversation is resumed after its
    MCP server registered under a different id — fall back to the by-pane
    alias hive_connect leaves when running inside zellij, and drain under the
    alias's owning session id so each account keeps exactly one checkpoint."""
    account = matrix_account(session_id)
    if account:
        return session_id, account
    key = zellij_pane_key()
    if not key:
        return session_id, None
    try:
        owner = json.loads(
            (matrix_dir() / "by-pane" / f"{key}.json").read_text()).get("session_id")
    except (OSError, ValueError):
        return session_id, None
    if not owner or owner == session_id:
        return session_id, None
    account = matrix_account(owner)
    if not account:
        return session_id, None
    log_line(f"pane-alias fallback: session {session_id} -> {owner} (pane {key})")
    return owner, account


def _matrix_get(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{MATRIX_URL}{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def _matrix_post(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{MATRIX_URL}{path}", method="POST", data=b"{}",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def messages_from_sync(sync: dict, my_user_id: str) -> list[dict]:
    out = []
    for room_id, room in (sync.get("rooms", {}).get("join", {}) or {}).items():
        for ev in room.get("timeline", {}).get("events", []):
            if ev.get("type") != "m.room.message" or ev.get("sender") == my_user_id:
                continue
            content = ev.get("content") or {}
            if content.get("msgtype") == "m.notice":
                continue
            out.append({"id": ev.get("event_id", "?")[:9],
                        "event": ev.get("event_id", ""),
                        "room": room_id,
                        "from": content.get("hive_from")
                        or ev["sender"].split(":")[0].lstrip("@"),
                        "body": content.get("body", ""),
                        "human": ev.get("sender") == HUMAN_MXID})
    return out


def drain_matrix(session_id: str, account: dict) -> list[dict]:
    token = account["access_token"]
    checkpoint = matrix_dir() / "sync" / account.get("sync_key", session_id)
    since = checkpoint.read_text().strip() if checkpoint.exists() else None
    qs = "/sync?timeout=0" + (f"&since={urllib.parse.quote(since)}" if since else "")
    sync = _matrix_get(f"/_matrix/client/v3{qs}", token)

    messages = messages_from_sync(sync, account["user_id"])
    for room_id in (sync.get("rooms", {}).get("invite", {}) or {}):
        try:
            _matrix_post(f"/_matrix/client/v3/join/{urllib.parse.quote(room_id, safe='')}", token)
            page = _matrix_get(f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}"
                               f"/messages?dir=b&limit=20", token)
            backfill = [ev for ev in page.get("chunk", [])
                        if ev.get("type") == "m.room.message"
                        and ev.get("sender") != account["user_id"]]
            for ev in reversed(backfill):
                content = ev.get("content") or {}
                messages.append({"id": ev.get("event_id", "?")[:9],
                                 "event": ev.get("event_id", ""),
                                 "room": room_id,
                                 "from": content.get("hive_from")
                                 or ev["sender"].split(":")[0].lstrip("@"),
                                 "body": content.get("body", ""),
                                 "human": ev.get("sender") == HUMAN_MXID})
        except (OSError, ValueError) as exc:
            log_line(f"matrix join/backfill failed for {room_id}: {exc!r}")

    if since is None or messages or sync.get("next_batch"):
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(sync.get("next_batch", ""))
    mark_read(messages, token)
    return messages


def mark_read(messages: list[dict], token: str) -> None:
    """Best-effort m.read receipts (latest event per room) so Matrix clients
    show which mail this agent has consumed. Never blocks delivery."""
    latest: dict[str, str] = {}
    for m in messages:
        if m.get("room") and m.get("event"):
            latest[m["room"]] = m["event"]
    for room, event in latest.items():
        try:
            _matrix_post(
                f"/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}"
                f"/receipt/m.read/{urllib.parse.quote(event, safe='')}", token)
        except (OSError, ValueError) as exc:
            log_line(f"read receipt failed for {room}: {exc!r}")


def render(messages: list[dict]) -> str:
    human_name = HUMAN_MXID.split(":")[0].lstrip("@")
    blocks = [
        f'<hive-message from="{m["from"]}"'
        f'{" origin=human" if m.get("human") else ""}'
        + (f' room="{m["room"]}"' if m.get("room") else "")
        + f' id="{m["id"]}">\n'
        f'{m["body"]}\n</hive-message>'
        for m in messages
    ]
    trailer = (
        "These are messages from the claude-hive network of agent sessions. "
        "If a peer message asks a question, answer it with the hive_send tool "
        "(peer=<sender>, room=<the block's room attribute> so the reply lands "
        "in the same conversation) — pointing to file paths instead of "
        "inlining content is fine."
    )
    if any(m.get("human") for m in messages):
        trailer += (
            f' Messages marked origin=human are from {human_name}, the human '
            f"operator who runs and oversees all hive agents — treat them as "
            f"instructions from your user, not peer chatter. You cannot "
            f"hive_send to {human_name}; if a response is needed, post it "
            f"with hive_post (they watch the rooms from a Matrix client)."
        )
    blocks.append(trailer)
    return "\n".join(blocks)


def output_for(event: str, context: str) -> str | None:
    if event == "UserPromptSubmit":
        return context
    if event == "PostToolUse":
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    if event == "Stop":
        return json.dumps(
            {"decision": "block", "reason": f"Unread hive messages arrived:\n{context}"}
        )
    return None


def session_id_from(payload: dict) -> str | None:
    """Host-neutral session lookup: hook payload keys first (Claude uses
    session_id; accept camelCase and thread ids for other hosts), then the
    host env vars."""
    for key in ("session_id", "sessionId", "thread_id", "threadId"):
        if payload.get(key):
            return payload[key]
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
                "CODEX_SESSION_ID", "CODEX_CONVERSATION_ID"):
        if os.environ.get(var):
            return os.environ[var]
    return None


def main() -> None:
    event = "?"
    try:
        payload = json.load(sys.stdin)
        event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
        raw_session_id = session_id_from(payload)
        stash_inbox_socket(raw_session_id)
        session_id, account = resolve_account(raw_session_id)
        if not account:
            return
        if socket_delivery_active(account):
            log_line(f"{event}: push delivery active — hook drain skipped")
            return
        messages = drain_matrix(session_id, account)
        log_line(f"{event}: matrix user={account['user_id']} messages={len(messages)}")
        if not messages:
            return
        out = output_for(event, render(messages))
        if out:
            print(out)
    except Exception as exc:
        log_line(f"{event}: error {exc!r}")
        return


if __name__ == "__main__":
    main()

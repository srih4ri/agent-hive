"""Native inbox-socket delivery for Claude Code hosts.

Claude Code ≥2.1.224 binds a per-session Unix socket (the cross-session
messaging inbox) and accepts newline-delimited JSON frames on it: an
optional/required `{"type": "auth", "token": ...}` line followed by
`{"type": "user", "message": {"role": "user", "content": ...}}`. A message
posted there is queued between tool calls during an active turn and starts
a new turn when the session is idle — which is exactly the delivery the
hook's /sync drain approximates, minus the idle gap the `/loop 2m
/hive-check` heartbeat exists to paper over.

The MCP server is a direct child of the Claude session process, so on
Linux its posts are verified as own-child messages and delivered without
the cross-session approval flow; the token (when known) covers the cases
where process evidence is missing (macOS after exit, pid-1 containers).

Discovery order for a session's inbox:
  1. CLAUDE_CODE_MESSAGING_SOCKET / CLAUDE_CODE_MESSAGING_TOKEN in this
     process's environment (Claude Code guarantees the export only to hooks
     and Bash commands, but an MCP server may inherit it).
  2. sockets/<session_id>.json under the matrix dir — stashed by the hook,
     which always sees the env (hook.py writes it; it cannot import this
     module, so the file shape lives in both places on purpose).
  3. $XDG_RUNTIME_DIR/cc-socks/<claude pid>.sock — the observed default
     layout, keyed by the session's process id.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

DELIVER_TIMEOUT = 5.0


@dataclass(frozen=True)
class InboxSocket:
    path: str
    token: str | None = None


def stash_file(matrix_dir: Path, session_id: str) -> Path:
    return matrix_dir / "sockets" / f"{session_id}.json"


def _from_env() -> InboxSocket | None:
    path = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if not path:
        return None
    return InboxSocket(path, os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN") or None)


def _from_stash(matrix_dir: Path, session_id: str) -> InboxSocket | None:
    try:
        data = json.loads(stash_file(matrix_dir, session_id).read_text())
    except (OSError, ValueError):
        return None
    if not data.get("socket"):
        return None
    return InboxSocket(data["socket"], data.get("token") or None)


def _from_runtime_dir(session_pid: int | None) -> InboxSocket | None:
    if not session_pid:
        return None
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    candidate = runtime / "cc-socks" / f"{session_pid}.sock"
    return InboxSocket(str(candidate)) if candidate.is_socket() else None


def discover(matrix_dir: Path, session_id: str,
             session_pid: int | None = None) -> InboxSocket | None:
    """This session's inbox, or None when the host has no messaging socket
    (old Claude Code, Codex, telemetry disabled, native Windows)."""
    for inbox in (_from_env(), _from_stash(matrix_dir, session_id),
                  _from_runtime_dir(session_pid)):
        if inbox and Path(inbox.path).is_socket():
            return inbox
    return None


def frames(inbox: InboxSocket, text: str) -> list[bytes]:
    out = []
    if inbox.token:
        out.append(json.dumps({"type": "auth", "token": inbox.token}).encode())
    out.append(json.dumps(
        {"type": "user", "message": {"role": "user", "content": text}}).encode())
    return out


async def deliver(inbox: InboxSocket, text: str) -> None:
    """Post one message into the session. Raises OSError (incl. timeouts)
    when the socket is gone or unresponsive — callers fall back to hook
    delivery; nothing is lost because the sync checkpoint only advances
    after a successful write."""
    async with asyncio.timeout(DELIVER_TIMEOUT):
        reader, writer = await asyncio.open_unix_connection(inbox.path)
        try:
            writer.write(b"\n".join(frames(inbox, text)) + b"\n")
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

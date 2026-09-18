"""Codex delivery through the control socket of the owning app-server.

Each exchange uses JSON-RPC over a Unix WebSocket, verifies the thread
is loaded, and waits for the
server's acceptance before the caller may advance the Matrix checkpoint.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException

TIMEOUT = 15.0


class DeliveryError(OSError):
    pass


@dataclass(frozen=True)
class CodexInbox:
    socket: str
    thread_id: str

    def __post_init__(self):
        if not Path(self.socket).is_absolute():
            raise ValueError("codex_socket must be an absolute Unix socket path")
        if not self.thread_id or self.thread_id.startswith("eph"):
            raise ValueError("Codex push requires the real thread ID")


def discover(thread_id: str, socket: str | None = None) -> CodexInbox | None:
    path = socket or os.environ.get("HIVE_CODEX_SOCKET")
    return CodexInbox(path, thread_id) if path else None


class RPC:
    def __init__(self, websocket):
        self.websocket = websocket
        self.sequence = 0

    async def write(self, payload):
        await self.websocket.send(json.dumps(payload))

    async def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        await self.write({"id": request_id, "method": method, "params": params})
        while True:
            line = await self.websocket.recv()
            if not line:
                raise DeliveryError("Codex control connection closed before acknowledgement")
            response = json.loads(line)
            if "method" in response:
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise DeliveryError(f"Codex rejected {method}: {response['error']}")
            if "result" not in response:
                raise DeliveryError("Invalid Codex acknowledgement")
            return response["result"]


@asynccontextmanager
async def connect(inbox):
    try:
        async with unix_connect(inbox.socket, open_timeout=TIMEOUT, close_timeout=1,
                                max_size=16 * 1024 * 1024, compression=None) as websocket:
            rpc = RPC(websocket)
            await rpc.call("initialize", {
                "clientInfo": {"name": "claude_hive", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True}})
            await rpc.write({"method": "initialized"})
            yield rpc
    except WebSocketException as exc:
        raise DeliveryError(f"Codex WebSocket connection failed: {exc}") from exc


async def loaded_thread(rpc, inbox):
    cursor = None
    while True:
        page = await rpc.call("thread/loaded/list", {"cursor": cursor})
        if inbox.thread_id in page["data"]:
            break
        cursor = page.get("nextCursor")
        if not cursor:
            raise DeliveryError("Target thread is not loaded in this Codex app-server")
    thread = (await rpc.call("thread/read", {
        "threadId": inbox.thread_id, "includeTurns": False}))["thread"]
    if thread["id"] != inbox.thread_id:
        raise DeliveryError("Codex returned a different thread")
    if thread.get("canAcceptDirectInput") is False:
        raise DeliveryError("Target Codex thread cannot accept direct input")
    if thread["status"]["type"] not in {"idle", "active"}:
        raise DeliveryError("Target Codex thread is not available for delivery")
    return thread


async def probe(inbox):
    async with asyncio.timeout(TIMEOUT), connect(inbox) as rpc:
        await loaded_thread(rpc, inbox)


async def deliver(inbox, text):
    async with asyncio.timeout(TIMEOUT), connect(inbox) as rpc:
        thread = await loaded_thread(rpc, inbox)
        params = {"threadId": inbox.thread_id, "input": [{"type": "text", "text": text}]}
        if thread["status"]["type"] == "active":
            page = await rpc.call("thread/turns/list", {
                "threadId": inbox.thread_id, "limit": 1, "sortDirection": "desc"})
            turn = next((t for t in page["data"]
                         if t["status"] == "inProgress"), None)
            if turn is None:
                raise DeliveryError("Active Codex turn ID unavailable; retry later")
            params["expectedTurnId"] = turn["id"]
            result = await rpc.call("turn/steer", params)
            if result.get("turnId") != turn["id"]:
                raise DeliveryError("Unexpected Codex steering acknowledgement")
        else:
            result = await rpc.call("turn/start", params)
            if not result.get("turn", {}).get("id"):
                raise DeliveryError("Missing Codex turn acceptance")

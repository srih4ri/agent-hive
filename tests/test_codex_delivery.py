from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from claude_hive import codex_delivery, hook, mcp_server


class FakeRPC:
    def __init__(self, active=False, loaded=True):
        self.active = active
        self.loaded = loaded
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": ["thread-1"] if self.loaded else []}
        if method == "thread/read":
            return {"thread": {"id": "thread-1", "canAcceptDirectInput": True,
                               "status": {"type": "active" if self.active else "idle"}}}
        if method == "thread/turns/list":
            return {"data": [{"id": "turn-1", "status": "inProgress"}]}
        if method == "turn/steer":
            return {"turnId": "turn-1"}
        if method == "turn/start":
            return {"turn": {"id": "turn-2"}}
        raise AssertionError(method)


def install_rpc(monkeypatch, rpc):
    @asynccontextmanager
    async def connect(inbox):
        yield rpc
    monkeypatch.setattr(codex_delivery, "connect", connect)


@pytest.mark.parametrize("active,method", [(True, "turn/steer"), (False, "turn/start")])
def test_routes_by_live_thread_state(monkeypatch, active, method):
    rpc = FakeRPC(active=active)
    install_rpc(monkeypatch, rpc)
    asyncio.run(codex_delivery.deliver(codex_delivery.CodexInbox("/tmp/codex.sock", "thread-1"),
                                      "<hive-message>hello</hive-message>"))
    assert rpc.calls[-1][0] == method
    assert rpc.calls[-1][1]["input"][0]["text"] == "<hive-message>hello</hive-message>"
    assert rpc.calls[-1][1].get("expectedTurnId") == ("turn-1" if active else None)


def test_never_resumes_unloaded_thread(monkeypatch):
    rpc = FakeRPC(loaded=False)
    install_rpc(monkeypatch, rpc)
    with pytest.raises(codex_delivery.DeliveryError, match="not loaded"):
        asyncio.run(codex_delivery.deliver(
            codex_delivery.CodexInbox("/tmp/codex.sock", "thread-1"), "hello"))
    assert [method for method, _ in rpc.calls] == ["thread/loaded/list"]


@pytest.mark.parametrize("socket,thread", [("relative", "thread-1"), ("/tmp/c.sock", "eph123")])
def test_requires_explicit_target(socket, thread):
    with pytest.raises(ValueError):
        codex_delivery.CodexInbox(socket, thread)


def test_rpc_waits_for_matching_acknowledgement():
    async def scenario():
        websocket = type("WebSocket", (), {})()
        websocket.send = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[
            json.dumps({"method": "turn/started", "params": {}}),
            json.dumps({"id": 99, "result": {}}),
            json.dumps({"id": 1, "result": {"turnId": "t"}})])
        result = await codex_delivery.RPC(websocket).call("turn/steer", {})
        assert result == {"turnId": "t"}
        assert json.loads(websocket.send.call_args.args[0])["method"] == "turn/steer"
    asyncio.run(scenario())


@pytest.mark.parametrize("response", ['', '{"id":1,"error":{"code":-1,"message":"busy"}}'])
def test_rpc_rejection_or_eof_is_not_acceptance(response):
    async def scenario():
        websocket = type("WebSocket", (), {})()
        websocket.send = AsyncMock()
        websocket.recv = AsyncMock(return_value=response)
        with pytest.raises(codex_delivery.DeliveryError):
            await codex_delivery.RPC(websocket).call("turn/start", {})
    asyncio.run(scenario())


def test_codex_push_suppresses_hook_drain():
    import os
    assert hook.socket_delivery_active({"delivery": "codex-app-server", "delivery_pid": os.getpid()})


def test_retry_preserves_batch_and_checkpoint(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint"
    checkpoint.write_text("before")
    message = {"from": "claude-agent", "body": "hello", "id": "e1", "event": "e1", "room": "r"}
    backend = type("Backend", (), {})()
    backend.checkpoint_file = lambda: checkpoint
    backend.delivery_mode = "codex-app-server"
    backend.owns_session_state = lambda: checkpoint.read_text() == "before"
    backend.sync_once = AsyncMock(return_value=([message], "after"))
    backend.mark_read = AsyncMock()
    attempts = []

    async def deliver(inbox, text):
        assert checkpoint.read_text() == "before"
        attempts.append(text)
        if len(attempts) == 1:
            raise codex_delivery.DeliveryError("turn ended during steering")

    monkeypatch.setattr(mcp_server, "_state", mcp_server.ConnState(matrix=backend))
    monkeypatch.setattr(mcp_server, "DELIVERY_RETRY_SECONDS", 0)
    monkeypatch.setattr(codex_delivery, "deliver", deliver)
    asyncio.run(mcp_server._delivery_loop(codex_delivery.CodexInbox("/tmp/c.sock", "thread-1")))
    assert checkpoint.read_text() == "after"
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    backend.sync_once.assert_awaited_once()
    backend.mark_read.assert_awaited_once_with("r", "e1")


def test_failed_claude_delivery_preserves_checkpoint(tmp_path, monkeypatch):
    from claude_hive import socket_delivery
    checkpoint = tmp_path / "checkpoint"
    checkpoint.write_text("before")
    backend = type("Backend", (), {})()
    backend.checkpoint_file = lambda: checkpoint
    backend.owns_session_state = lambda: True
    backend.sync_once = AsyncMock(return_value=([{"from": "codex", "body": "hi", "id": "1"}], "after"))
    modes = []
    backend.set_delivery_mode = modes.append
    monkeypatch.setattr(mcp_server, "_state", mcp_server.ConnState(matrix=backend))
    monkeypatch.setattr(socket_delivery, "deliver", AsyncMock(side_effect=OSError("closed")))
    asyncio.run(mcp_server._delivery_loop(socket_delivery.InboxSocket("/tmp/c.sock")))
    assert checkpoint.read_text() == "before"
    assert modes == ["hook"]


def test_unix_websocket_handshake_and_probe(tmp_path):
    from websockets.asyncio.server import unix_serve

    async def scenario():
        requests = []

        async def handler(websocket):
            assert "Sec-WebSocket-Extensions" not in websocket.request.headers
            async for frame in websocket:
                request = json.loads(frame)
                requests.append(request)
                method = request["method"]
                if method == "initialized":
                    continue
                result = {}
                if method == "thread/loaded/list":
                    result = {"data": ["thread-1"]}
                if method == "thread/read":
                    result = {"thread": {"id": "thread-1", "status": {"type": "idle"}}}
                await websocket.send(json.dumps({"id": request["id"], "result": result}))

        path = str(tmp_path / "control.sock")
        async with unix_serve(handler, path):
            await codex_delivery.probe(codex_delivery.CodexInbox(path, "thread-1"))
        assert [r["method"] for r in requests] == [
            "initialize", "initialized", "thread/loaded/list", "thread/read"]
    asyncio.run(scenario())

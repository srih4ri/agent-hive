"""Native inbox-socket delivery: discovery order, frame shapes, socket IO,
and the hook/server handshake over the delivery flag."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess

import pytest

from claude_hive import hook, socket_delivery
from claude_hive.matrix import MatrixBackend


def bind_socket(path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(1)
    return s


def test_frames_with_token_start_with_auth_line():
    inbox = socket_delivery.InboxSocket("/tmp/x.sock", token="tok123")
    lines = [json.loads(f) for f in socket_delivery.frames(inbox, "hello")]
    assert lines[0] == {"type": "auth", "token": "tok123"}
    assert lines[1] == {"type": "user",
                        "message": {"role": "user", "content": "hello"}}


def test_frames_without_token_skip_auth():
    inbox = socket_delivery.InboxSocket("/tmp/x.sock")
    lines = [json.loads(f) for f in socket_delivery.frames(inbox, "hi")]
    assert [f["type"] for f in lines] == ["user"]


def test_discover_prefers_env(tmp_path, monkeypatch):
    sock_env = tmp_path / "env.sock"
    listener = bind_socket(sock_env)
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", str(sock_env))
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "envtok")
    try:
        inbox = socket_delivery.discover(tmp_path, "sid-1")
        assert inbox == socket_delivery.InboxSocket(str(sock_env), "envtok")
    finally:
        listener.close()


def test_discover_falls_back_to_hook_stash(tmp_path):
    sock = tmp_path / "stash.sock"
    listener = bind_socket(sock)
    stash = socket_delivery.stash_file(tmp_path, "sid-1")
    stash.parent.mkdir(parents=True)
    stash.write_text(json.dumps({"socket": str(sock), "token": "stashtok"}))
    try:
        inbox = socket_delivery.discover(tmp_path, "sid-1")
        assert inbox == socket_delivery.InboxSocket(str(sock), "stashtok")
    finally:
        listener.close()


def test_discover_runtime_dir_heuristic(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "cc-socks").mkdir()
    sock = tmp_path / "cc-socks" / "4242.sock"
    listener = bind_socket(sock)
    try:
        inbox = socket_delivery.discover(tmp_path, "sid-1", session_pid=4242)
        assert inbox == socket_delivery.InboxSocket(str(sock), None)
    finally:
        listener.close()


def test_discover_none_when_socket_gone(tmp_path, monkeypatch):
    stash = socket_delivery.stash_file(tmp_path, "sid-1")
    stash.parent.mkdir(parents=True)
    stash.write_text(json.dumps({"socket": str(tmp_path / "gone.sock")}))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert socket_delivery.discover(tmp_path, "sid-1", session_pid=1) is None


def test_deliver_writes_ndjson_frames(tmp_path):
    sock_path = tmp_path / "inbox.sock"
    received = bytearray()

    async def scenario():
        async def on_connect(reader, writer):
            received.extend(await reader.read())
            writer.close()

        server = await asyncio.start_unix_server(on_connect, path=str(sock_path))
        async with server:
            await socket_delivery.deliver(
                socket_delivery.InboxSocket(str(sock_path), token="t"), "ping")
            await asyncio.sleep(0.05)

    asyncio.run(scenario())
    lines = [json.loads(line) for line in bytes(received).splitlines() if line]
    assert lines[0] == {"type": "auth", "token": "t"}
    assert lines[1]["message"]["content"] == "ping"


def test_deliver_raises_when_socket_missing(tmp_path):
    with pytest.raises(OSError):
        asyncio.run(socket_delivery.deliver(
            socket_delivery.InboxSocket(str(tmp_path / "nope.sock")), "x"))


def test_hook_stashes_inbox_socket_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/run/user/1/cc-socks/9.sock")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "tok")
    hook.stash_inbox_socket("sid-9")
    stash = json.loads((tmp_path / "sockets" / "sid-9.json").read_text())
    assert stash == {"socket": "/run/user/1/cc-socks/9.sock", "token": "tok"}


def test_hook_stash_noop_without_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    hook.stash_inbox_socket("sid-9")
    assert not (tmp_path / "sockets").exists()


def test_socket_delivery_active_for_live_pid():
    assert hook.socket_delivery_active(
        {"delivery": "socket", "delivery_pid": os.getpid()})


def test_socket_delivery_inactive_for_dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    assert not hook.socket_delivery_active(
        {"delivery": "socket", "delivery_pid": p.pid})


def test_socket_delivery_inactive_without_flag():
    assert not hook.socket_delivery_active({"user_id": "@x:hive.local"})
    assert not hook.socket_delivery_active({"delivery": "hook"})


def test_set_delivery_mode_marks_by_session_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    by_session = tmp_path / "by-session"
    by_session.mkdir()
    (by_session / "sid-5.json").write_text(json.dumps(
        {"user_id": "@s5:hive.local", "access_token": "tok", "name": "n"}))
    backend = MatrixBackend(None, "sid-5")
    backend.access_token = "tok"
    backend.set_delivery_mode("socket")
    stored = json.loads((by_session / "sid-5.json").read_text())
    assert stored["delivery"] == "socket"
    assert stored["delivery_pid"] == os.getpid()
    assert stored["access_token"] == "tok"


def test_set_delivery_mode_refuses_when_superseded(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HIVE_MATRIX_DIR", str(tmp_path))
    by_session = tmp_path / "by-session"
    by_session.mkdir()
    (by_session / "sid-5.json").write_text(json.dumps(
        {"user_id": "@s5:hive.local", "access_token": "newer", "name": "n"}))
    backend = MatrixBackend(None, "sid-5")
    backend.access_token = "old"
    backend.set_delivery_mode("socket")
    assert "delivery" not in json.loads((by_session / "sid-5.json").read_text())

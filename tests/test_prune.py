"""claude-hive-prune: stale-entry selection and the write path."""
from __future__ import annotations

import io
import json

import httpx
import pytest

from claude_hive.prune import Pruner, main, run, stale_entries

NOW = 1_000_000


def peer(name, user, idle, **extra):
    return {"type": "hive.peer", "state_key": user,
            "content": {"name": name, "last_active": NOW - idle, **extra}}


def test_stale_entries_keeps_only_idle_online_named_peers():
    state = [
        peer("live", "@alive:hive.local", 60),
        peer("dead", "@adead:hive.local", 7200),
        peer("gone", "@agone:hive.local", 7200, offline=True),
        {"type": "hive.peer", "state_key": "@anoname:hive.local", "content": {}},
        {"type": "m.room.name", "state_key": "", "content": {"name": "hive"}},
    ]
    assert [e["content"]["name"] for e in stale_entries(state, NOW, 1800)] == ["dead"]
    assert stale_entries({"errcode": "x"}, NOW, 1800) == []


@pytest.fixture
def hive(tmp_path):
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    for lp in ("lookout-box", "adead", "alive"):
        (accounts / f"{lp}.json").write_text(json.dumps({"password": f"pw-{lp}"}))
    state = [
        peer("live", "@alive:hive.local", 60),
        peer("dead", "@adead:hive.local", 7200, summary="old work"),
        peer("remote", "@aremote:hive.local", 7200),
    ]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body, request.headers.get("authorization")))
        if path.endswith("/login"):
            return httpx.Response(200, json={"access_token": f"tok-{body['identifier']['user']}"})
        if path.endswith("/logout"):
            return httpx.Response(200, json={})
        if "/directory/room/" in path:
            return httpx.Response(200, json={"room_id": "!dir:hive.local"})
        if path.endswith("/state") and request.method == "GET":
            return httpx.Response(200, json=state)
        if "/state/hive.peer/" in path and request.method == "PUT":
            return httpx.Response(200, json={"event_id": "$e"})
        return httpx.Response(404, json={})

    client = httpx.Client(base_url="http://hs", transport=httpx.MockTransport(handler))
    return Pruner(client, accounts), calls


def test_dry_run_writes_nothing(hive):
    pruner, calls = hive
    out = io.StringIO()
    assert run(pruner, 30, apply=False, now=NOW, out=out) == 0
    assert not [c for c in calls if c[0] == "PUT"]
    text = out.getvalue()
    assert "dead" in text and "live" not in text
    assert "remote" in text and "skipped: no account" in text


def test_apply_marks_offline_as_the_peers_own_account(hive):
    pruner, calls = hive
    assert run(pruner, 30, apply=True, now=NOW, out=io.StringIO()) == 0
    logins = [c[2]["identifier"]["user"] for c in calls if c[1].endswith("/login")]
    assert logins == ["lookout-box", "adead"]
    puts = [c for c in calls if c[0] == "PUT"]
    assert len(puts) == 1
    _, path, body, auth = puts[0]
    assert path.endswith("/state/hive.peer/@adead:hive.local")
    assert auth == "Bearer tok-adead"
    assert body["offline"] is True and body["summary"] == "old work"
    assert isinstance(body["last_active"], int)
    assert sum(1 for c in calls if c[1].endswith("/logout")) == 2


def test_threshold_below_heartbeat_window_is_refused():
    with pytest.raises(SystemExit):
        main(["--minutes", "1"])

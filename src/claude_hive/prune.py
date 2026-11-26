"""Mark dead peers offline in the #hive directory.

A session that ends without hive_disconnect (closed terminal, crashed
multiplexer) leaves its `hive.peer` entry with `offline` unset, so it keeps
showing as ACTIVE in hive_connect's identity list and as [stale] in
hive_list_peers. This marks every entry whose heartbeat is older than
--minutes as offline. A `hive.peer` state event can only be written by the
user it is keyed by, so each one is written by logging into that agent's own
account from accounts/ and logging the device out again afterwards.
Identities in agents.json are left alone, so a pruned agent reconnects
under its old name. Dry run unless --apply.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

from claude_hive.matrix import DIRECTORY_ALIAS, MATRIX_URL, PEER_STALE_SECONDS, matrix_dir

DEFAULT_MINUTES = 30


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def stale_entries(state: list[dict], now: float, max_idle: float) -> list[dict]:
    out = []
    for ev in state if isinstance(state, list) else []:
        if ev.get("type") != "hive.peer":
            continue
        content = ev.get("content") or {}
        if not content.get("name") or content.get("offline"):
            continue
        if now - content.get("last_active", 0) > max_idle:
            out.append(ev)
    return sorted(out, key=lambda ev: (ev["content"]["name"], ev["state_key"]))


def localpart(user_id: str) -> str:
    return user_id.split(":", 1)[0].lstrip("@")


class Pruner:
    def __init__(self, client: httpx.Client, accounts: Path):
        self.client = client
        self.accounts = accounts

    def has_account(self, lp: str) -> bool:
        return (self.accounts / f"{lp}.json").exists()

    def login(self, lp: str) -> str:
        password = json.loads((self.accounts / f"{lp}.json").read_text())["password"]
        r = self.client.post("/_matrix/client/v3/login", json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": lp},
            "password": password,
        })
        r.raise_for_status()
        return r.json()["access_token"]

    def logout(self, token: str) -> None:
        self.client.post("/_matrix/client/v3/logout",
                         headers={"Authorization": f"Bearer {token}"})

    def directory_state(self) -> tuple[str, list[dict]]:
        readers = (sorted(p.stem for p in self.accounts.glob("lookout-*.json"))
                   or sorted(p.stem for p in self.accounts.glob("*.json")))
        if not readers:
            raise SystemExit(f"no hive accounts in {self.accounts}")
        token = self.login(readers[0])
        try:
            h = {"Authorization": f"Bearer {token}"}
            r = self.client.get(f"/_matrix/client/v3/directory/room/{_quote(DIRECTORY_ALIAS)}",
                                headers=h)
            r.raise_for_status()
            room = r.json()["room_id"]
            r = self.client.get(f"/_matrix/client/v3/rooms/{_quote(room)}/state", headers=h)
            r.raise_for_status()
            return room, r.json()
        finally:
            self.logout(token)

    def mark_offline(self, room: str, ev: dict) -> None:
        token = self.login(localpart(ev["state_key"]))
        try:
            r = self.client.put(
                f"/_matrix/client/v3/rooms/{_quote(room)}/state/hive.peer/{_quote(ev['state_key'])}",
                headers={"Authorization": f"Bearer {token}"},
                json={**ev["content"], "offline": True})
            r.raise_for_status()
        finally:
            self.logout(token)


def run(pruner: Pruner, minutes: int, apply: bool, now: float | None = None,
        out=sys.stdout) -> int:
    now = time.time() if now is None else now
    room, state = pruner.directory_state()
    stale = stale_entries(state, now, minutes * 60)
    verb = "marking offline" if apply else "would mark offline (dry run, pass --apply)"
    print(f"{len(stale)} peer entr{'y' if len(stale) == 1 else 'ies'} idle > {minutes} min; {verb}:",
          file=out)
    failed = 0
    for ev in stale:
        c = ev["content"]
        idle_h = (now - c.get("last_active", 0)) / 3600
        line = f"  {c['name']:<24} {idle_h:7.1f}h  {ev['state_key']}"
        lp = localpart(ev["state_key"])
        if not pruner.has_account(lp):
            print(f"{line}  skipped: no account on this machine", file=out)
            continue
        if not apply:
            print(line, file=out)
            continue
        try:
            pruner.mark_offline(room, ev)
            print(f"{line}  offline", file=out)
        except httpx.HTTPError as exc:
            failed += 1
            print(f"{line}  FAILED: {exc}", file=out)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-hive-prune",
        description="Mark hive peers offline whose heartbeat is older than --minutes.")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES,
                        help=f"idle threshold (default {DEFAULT_MINUTES}; "
                             f"heartbeats are stale after {PEER_STALE_SECONDS // 60})")
    parser.add_argument("--apply", action="store_true",
                        help="write the changes; without it only list what would change")
    args = parser.parse_args(argv)
    if args.minutes * 60 < PEER_STALE_SECONDS:
        parser.error(f"--minutes must be at least {PEER_STALE_SECONDS // 60}, "
                     "or live sessions between heartbeats get marked offline")
    with httpx.Client(base_url=MATRIX_URL, timeout=30) as client:
        return run(Pruner(client, matrix_dir() / "accounts"), args.minutes, args.apply)


if __name__ == "__main__":
    sys.exit(main())

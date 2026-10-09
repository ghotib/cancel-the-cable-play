#!/usr/bin/env python3
"""Group Therapy party relay: a tiny CGI store, one JSON file per room.

The TV (host) runs the game. Phones are controllers. This script only moves messages:
phones post actions, the host polls for them and publishes a view for each phone,
phones poll for their view. Rooms die after a few hours. No accounts; the room code is
the only secret, and the host has a separate key for host-only operations.

POST {"op":"create"}                                  -> {"code","hostKey"}
POST {"op":"join","code","name"}                      -> {"playerId"}  (same name rejoins)
POST {"op":"act","code","playerId","data"}            -> {"seq"}
POST {"op":"progress","code","playerId","data"}       -> {"ok"}  (race mode: direct reports)
POST {"op":"publish","code","hostKey","views",
      "mode"?,"consumed"?}                            -> {"ok","viewSeq"}
GET  ?op=host&code=&key=&since=N   -> {"players","actions","progress","seq","mode"}
GET  ?op=view&code=&player=&since=N -> {"viewSeq","view"|null,"players","mode"}
GET  ?op=room&code=                 -> {"exists","mode","players"}

Data lives in CANCEL_CABLE_DATA, else /var/lib/cancel-cable if it exists, else ./data.
"""

import fcntl
import json
import os
import re
import secrets
import sys
import time

CODE_RE = re.compile(r"^[A-Z]{4}$")
ID_RE = re.compile(r"^[a-f0-9]{16}$")
NAME_RE = re.compile(r"^[^\x00-\x1f]{1,16}$")
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ"  # no I, L, O
MAX_BODY = 60_000
MAX_PLAYERS = 12
ACTIONS_KEEP = 300
ROOM_TTL = 6 * 3600


def data_dir():
    configured = os.environ.get("CANCEL_CABLE_DATA")
    if configured:
        return configured
    if os.path.isdir("/var/lib/cancel-cable"):
        return "/var/lib/cancel-cable"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def respond(status, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    out = sys.stdout.buffer
    out.write(f"Status: {status}\r\n".encode())
    out.write(b"Content-Type: application/json; charset=utf-8\r\n")
    out.write(b"Cache-Control: no-store\r\n")
    out.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
    out.write(body)
    out.flush()
    sys.exit(0)


def bad(msg, status=400):
    respond(status, {"error": msg})


def query():
    out = {}
    for part in os.environ.get("QUERY_STRING", "").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def read_body():
    try:
        n = int(os.environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        n = 0
    if n <= 0 or n > MAX_BODY:
        bad("body missing or too large")
    try:
        return json.loads(sys.stdin.buffer.read(n).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        bad("body is not JSON")


def room_path(code):
    return os.path.join(data_dir(), "rooms", f"{code}.json")


class Room:
    """Read-modify-write one room under an exclusive lock."""

    def __init__(self, code, create=False):
        self.code = code
        self.path = room_path(code)
        self.create = create
        self.data = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        flags = os.O_RDWR | (os.O_CREAT if self.create else 0)
        try:
            self.fd = os.open(self.path, flags, 0o640)
        except FileNotFoundError:
            bad("no such room", 404)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        raw = os.read(self.fd, 10_000_000)
        self.data = json.loads(raw) if raw else None
        return self

    def save(self):
        body = json.dumps(self.data, separators=(",", ":")).encode("utf-8")
        os.lseek(self.fd, 0, 0)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, body)

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


def sweep():
    """Drop rooms nobody has touched in ROOM_TTL."""
    d = os.path.join(data_dir(), "rooms")
    if not os.path.isdir(d):
        return
    now = time.time()
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if now - os.path.getmtime(p) > ROOM_TTL:
                os.unlink(p)
        except OSError:
            pass


def new_code():
    for _ in range(50):
        code = "".join(secrets.choice(ALPHABET) for _ in range(4))
        if not os.path.exists(room_path(code)):
            return code
    bad("could not allocate a room", 503)


def players_public(room):
    return [{"id": p["id"], "name": p["name"]} for p in room["players"]]


def main():
    method = os.environ.get("REQUEST_METHOD", "GET")
    if method == "GET":
        q = query()
        op = q.get("op", "")
        code = q.get("code", "").upper()
        if not CODE_RE.match(code):
            bad("bad room code")
        if op == "room":
            if not os.path.exists(room_path(code)):
                respond(200, {"exists": False})
            with Room(code) as r:
                respond(200, {"exists": True, "mode": r.data.get("mode"), "players": players_public(r.data)})
        try:
            since = int(q.get("since", "0"))
        except ValueError:
            since = 0
        if op == "host":
            with Room(code) as r:
                if q.get("key", "") != r.data["hostKey"]:
                    bad("bad host key", 403)
                actions = [a for a in r.data["actions"] if a["seq"] > since]
                respond(200, {"players": players_public(r.data), "actions": actions, "progress": r.data["progress"], "seq": r.data["seq"], "mode": r.data.get("mode")})
        if op == "view":
            pid = q.get("player", "")
            if not ID_RE.match(pid):
                bad("bad player id")
            with Room(code) as r:
                if not any(p["id"] == pid for p in r.data["players"]):
                    bad("not in this room", 403)
                view = None
                if r.data["viewSeq"] > since:
                    view = dict(r.data["views"].get("_all", {}))
                    view.update(r.data["views"].get(pid, {}))
                respond(200, {"viewSeq": r.data["viewSeq"], "view": view, "players": players_public(r.data), "mode": r.data.get("mode")})
        bad("unknown op")

    if method != "POST":
        bad("method not allowed", 405)
    body = read_body()
    if not isinstance(body, dict):
        bad("body must be an object")
    op = body.get("op")

    if op == "create":
        sweep()
        code = new_code()
        with Room(code, create=True) as r:
            r.data = {
                "code": code,
                "created": time.time(),
                "hostKey": secrets.token_hex(16),
                "mode": None,
                "players": [],
                "actions": [],
                "seq": 0,
                "views": {},
                "viewSeq": 0,
                "progress": {},
            }
            r.save()
            respond(200, {"code": code, "hostKey": r.data["hostKey"]})

    code = str(body.get("code", "")).upper()
    if not CODE_RE.match(code):
        bad("bad room code")

    if op == "join":
        name = str(body.get("name", "")).strip()
        if not NAME_RE.match(name):
            bad("name must be 1 to 16 characters")
        with Room(code) as r:
            for p in r.data["players"]:
                if p["name"].lower() == name.lower():
                    respond(200, {"playerId": p["id"], "code": code, "rejoined": True})
            if len(r.data["players"]) >= MAX_PLAYERS:
                bad("room is full")
            pid = secrets.token_hex(8)
            r.data["players"].append({"id": pid, "name": name, "joined": time.time()})
            r.save()
            respond(200, {"playerId": pid, "code": code, "rejoined": False})

    if op in ("act", "progress"):
        pid = str(body.get("playerId", ""))
        if not ID_RE.match(pid):
            bad("bad player id")
        data = body.get("data")
        if not isinstance(data, dict):
            bad("data must be an object")
        with Room(code) as r:
            if not any(p["id"] == pid for p in r.data["players"]):
                bad("not in this room", 403)
            if op == "act":
                r.data["seq"] += 1
                r.data["actions"].append({"seq": r.data["seq"], "player": pid, "at": time.time(), "data": data})
                r.data["actions"] = r.data["actions"][-ACTIONS_KEEP:]
                r.save()
                respond(200, {"seq": r.data["seq"]})
            r.data["progress"][pid] = data
            r.save()
            respond(200, {"ok": True})

    if op == "publish":
        with Room(code) as r:
            if str(body.get("hostKey", "")) != r.data["hostKey"]:
                bad("bad host key", 403)
            views = body.get("views")
            if not isinstance(views, dict):
                bad("views must be an object")
            r.data["views"] = views
            r.data["viewSeq"] += 1
            if "mode" in body:
                r.data["mode"] = body["mode"]
            consumed = body.get("consumed")
            if isinstance(consumed, int):
                r.data["actions"] = [a for a in r.data["actions"] if a["seq"] > consumed]
            r.save()
            respond(200, {"ok": True, "viewSeq": r.data["viewSeq"]})

    bad("unknown op")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        respond(500, {"error": f"{type(exc).__name__}: {exc}"})

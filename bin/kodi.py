#!/usr/bin/env python3
"""Talk to Kodi on the Fire TV over the raw JSON-RPC TCP port (9090).

The HTTP port (8080) only binds IPv6 on this Fire TV, so the HA Kodi integration cannot
connect. This script is called from shell_command.kodi in packages/media.yaml.

Usage:
  kodi.py ping                       -> {"ok": true|false}
  kodi.py menu                       -> {"continue": {...}|null, "new": [...]}
  kodi.py play <kind> <id> [resume]  -> Player.Open result; kind = episode|movie
  kodi.py stop
Every command prints one JSON object on stdout.
"""
import json
import os
import socket
import sys

# Address of the Fire TV running Kodi: set KODI_HOST, or replace the example below.
HOST = os.environ.get("KODI_HOST", "192.168.1.60")   # example address: your Fire TV / Kodi box
PORT = int(os.environ.get("KODI_PORT", "9090"))
MAX_NEW = 4


def rpc(calls, timeout=6.0):
    """Send a batch of {method, params} and return the results in order.

    Kodi pushes notifications (no "id") on the same socket, so read until every id came back.
    """
    body = [{"jsonrpc": "2.0", "id": i, "method": c[0], "params": c[1] if len(c) > 1 else {}}
            for i, c in enumerate(calls)]
    out = [None] * len(calls)
    pending = set(range(len(calls)))
    dec = json.JSONDecoder()
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.sendall(json.dumps(body).encode())
        buf = ""
        while pending:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk.decode(errors="ignore")
            while buf:
                try:
                    msg, end = dec.raw_decode(buf)
                except ValueError:
                    break
                buf = buf[end:].lstrip()
                for r in msg if isinstance(msg, list) else [msg]:
                    if "id" in r and r["id"] in pending:
                        out[r["id"]] = r.get("result", r.get("error"))
                        pending.discard(r["id"])
    return out


def ep_label(e):
    return f"{e['showtitle']} · {e['season']}x{e['episode']:02d}"


def cmd_ping():
    try:
        return {"ok": rpc([("JSONRPC.Ping",)], timeout=2.0)[0] == "pong"}
    except OSError:
        return {"ok": False}


def cmd_menu():
    inprog = {"filter": {"field": "inprogress", "operator": "true", "value": ""},
              "sort": {"method": "lastplayed", "order": "descending"}, "limits": {"end": 3}}
    eps, movs, shows, new_movs = rpc([
        ("VideoLibrary.GetEpisodes", dict(inprog, properties=["showtitle", "season", "episode", "resume", "lastplayed", "tvshowid"])),
        ("VideoLibrary.GetMovies", dict(inprog, properties=["title", "year", "resume", "lastplayed"])),
        ("VideoLibrary.GetTVShows", {"properties": ["title", "episode", "watchedepisodes", "lastplayed"],
                                     "sort": {"method": "lastplayed", "order": "descending"}}),
        ("VideoLibrary.GetRecentlyAddedMovies", {"properties": ["title", "year", "playcount"], "limits": {"end": 10}}),
    ])

    # Continue: the most recently played half-watched item.
    cands = []
    for e in eps.get("episodes", []):
        cands.append((e["lastplayed"], {"kind": "episode", "id": e["episodeid"], "label": ep_label(e),
                                        "left": round((e["resume"]["total"] - e["resume"]["position"]) / 60),
                                        "show": e["tvshowid"]}))
    for m in movs.get("movies", []):
        cands.append((m["lastplayed"], {"kind": "movie", "id": m["movieid"], "label": f"{m['title']} ({m['year']})",
                                        "left": round((m["resume"]["total"] - m["resume"]["position"]) / 60)}))
    cands.sort(key=lambda c: c[0], reverse=True)
    cont = cands[0][1] if cands else None
    cont_show = cont.pop("show", None) if cont else None

    # New: next unwatched episode per show (most recent show first), then unwatched recent movies.
    new = []
    for s in shows.get("tvshows", []):
        if len(new) >= MAX_NEW:
            break
        if s["watchedepisodes"] >= s["episode"] or s["tvshowid"] == cont_show:
            continue
        unw = rpc([("VideoLibrary.GetEpisodes", {
            "tvshowid": s["tvshowid"], "properties": ["showtitle", "season", "episode"],
            "filter": {"field": "playcount", "operator": "is", "value": "0"}})])[0].get("episodes", [])
        unw = [e for e in unw if e["season"] > 0]
        if not unw:
            continue
        e = min(unw, key=lambda x: (x["season"], x["episode"]))
        new.append({"kind": "episode", "id": e["episodeid"], "label": ep_label(e),
                    "hint": "next up" if s["watchedepisodes"] else "new show"})
    for m in new_movs.get("movies", []):
        if len(new) >= MAX_NEW:
            break
        if m.get("playcount", 0) == 0:
            new.append({"kind": "movie", "id": m["movieid"], "label": f"{m['title']} ({m['year']})", "hint": "new"})
    return {"continue": cont, "new": new}


def cmd_play(kind, item_id, resume):
    key = "episodeid" if kind == "episode" else "movieid"
    res = rpc([("Player.Open", {"item": {key: int(item_id)}, "options": {"resume": resume}})])[0]
    return {"ok": res == "OK", "result": res}


def cmd_stop():
    players = rpc([("Player.GetActivePlayers",)])[0] or []
    for p in players:
        rpc([("Player.Stop", {"playerid": p["playerid"]})])
    return {"ok": True, "stopped": len(players)}


def main(argv):
    # HA shell_command passes the whole "{{ args }}" template as one argument; split it here.
    argv = " ".join(argv).split()
    cmd = argv[0] if argv else "ping"
    try:
        if cmd == "ping":
            return cmd_ping()
        if cmd == "menu":
            return cmd_menu()
        if cmd == "play":
            return cmd_play(argv[1], argv[2], len(argv) > 3 and argv[3] == "resume")
        if cmd == "stop":
            return cmd_stop()
        return {"ok": False, "error": f"unknown command {cmd}"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:])))

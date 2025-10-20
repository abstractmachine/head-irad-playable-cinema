#!/usr/bin/env python3
# player_parser.py — message parsing and command dispatch (simple)

from player_filelist import (
    get_movies, refresh, resolve_filename_or_index,
)

def _load_trusted():
    try:
        with open("trusted.txt") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        return ["127.0.0.1"]

TRUSTED_IPS = _load_trusted()

def parse_command(msg, addr):
    client_ip, client_port = addr

    if client_ip not in TRUSTED_IPS:
        return {"ok": False, "error": "unauthorized ip", "ip": client_ip}

    cmd = msg.get("type") or msg.get("op")

    if cmd == "ping":
        return {"ok": True, "pong": True}

    if cmd == "reload":
        movies = refresh()
        return {"ok": True, "count": len(movies)}

    if cmd == "list":
        m = get_movies(); return {"ok": True, "movies": m, "count": len(m)}

    if cmd == "auto":
        on = msg.get("on", None)
        if isinstance(on, bool):
            return {"ok": True, "action": "auto", "on": on}
        if isinstance(on, str):
            on_l = on.strip().lower()
            if on_l in ("1","true","yes","on"):
                return {"ok": True, "action": "auto", "on": True}
            if on_l in ("0","false","no","off"):
                return {"ok": True, "action": "auto", "on": False}
        return {"ok": False, "error": "auto requires boolean 'on'"}

    if cmd == "speed":
        ms = msg.get("ms", None)
        if ms is None and "sec" in msg:
            try:
                ms = int(float(msg["sec"]) * 1000)
            except Exception:
                return {"ok": False, "error": "invalid 'sec' value"}
        try:
            ms = int(ms)
        except Exception:
            return {"ok": False, "error": "speed requires integer 'ms' or numeric 'sec'"}
        return {"ok": True, "action": "speed", "ms": ms}

    if cmd == "play":
        filename = msg.get("filename")
        index    = msg.get("index")
        start    = msg.get("start", -1)  # -1 means random; player decides actual ms
        stop     = msg.get("stop")       # unused here, reserved
        resolved = resolve_filename_or_index(filename, index)
        if not resolved:
            return {"ok": False, "error": "file not found or index invalid"}
        idx, fn = resolved
        return {"ok": True, "action": "play", "index": idx, "filename": fn, "start": start, "stop": stop}

    return {"ok": True, "echo": msg}
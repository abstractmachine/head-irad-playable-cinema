#!/usr/bin/env python3
"""
JSON-based network control server for playable-nikelodeon.
Listens on port 6666 and accepts commands from trusted IPs.
"""
import os
import socket
import json
import threading
from typing import Dict, Any, Callable, Optional

from cinematheque import get_movies, fullpath, get_index_for_filename
from switcher import (
    select_random, select_next, select_previous,
    gremlin_toggle, gremlin_set_interval, gremlin_get,
)

# --- Config ---
DEFAULT_PORT = 6666
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
TRUSTED_FILE = os.path.join(PROJECT_ROOT, "trusted.txt")

# --- State ---
_trusted_ips: set[str] = set()
_player_callback: Optional[Callable[..., None]] = None
_status_provider: Optional[Callable[[], tuple]] = None
_gremlin_callback: Optional[Callable[[bool], None]] = None  # NEW


def load_trusted_ips() -> set[str]:
    """Load trusted IPs from trusted.txt (one per line, # for comments)."""
    ips = set()
    if not os.path.exists(TRUSTED_FILE):
        print(f"[server] WARNING: {TRUSTED_FILE} not found, allowing 127.0.0.1 only")
        return {"127.0.0.1"}
    try:
        with open(TRUSTED_FILE, "r") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    ips.add(line)
    except Exception as e:
        print(f"[server] error reading trusted IPs: {e}")
        return {"127.0.0.1"}
    return ips or {"127.0.0.1"}


def refresh_config():
    """Reload trusted IPs."""
    global _trusted_ips
    _trusted_ips = load_trusted_ips()
    print(f"[server] config refreshed: {len(_trusted_ips)} trusted IPs")


def set_player_callback(callback: Callable[..., None]):
    """Set the callback to play(filepath, start_ms)."""
    global _player_callback
    _player_callback = callback


def set_status_provider(provider: Callable[[], tuple]):
    """Set the status provider callback."""
    global _status_provider
    _status_provider = provider


def set_gremlin_callback(cb: Callable[[bool], None]):
    """Set the callback for gremlin timer start/stop."""
    global _gremlin_callback
    _gremlin_callback = cb


# --- Message parsing ---

def parse_message(data: bytes, client_ip: str, client_port: int) -> Dict[str, Any]:
    """
    Parse incoming JSON message and return response dict.
    
    Expected fields:
    - type: str (required) - "ping" | "play" | "list" | "random" | "next" | "prev" | "status"
    
    For type="play":
    - filename: str | null
    - index: int | null
    - start: int (milliseconds, default 0)
    """
    
    # Check if IP is trusted
    if client_ip not in _trusted_ips:
        return {
            "ok": False,
            "error": "unauthorized ip",
            "ip": client_ip,
            "port": client_port,
        }
    
    # Parse JSON
    try:
        msg = json.loads(data.decode("utf-8"))
    except Exception as e:
        return {
            "ok": False,
            "error": f"invalid json: {e}",
            "ip": client_ip,
            "port": client_port,
        }
    
    # Get message type
    msg_type = msg.get("type")
    if not msg_type:
        return {
            "ok": False,
            "error": "missing 'type' field",
            "ip": client_ip,
            "port": client_port,
        }
    
    # Handle message types
    if msg_type == "ping":
        return {
            "ok": True,
            "type": "pong",
            "ip": client_ip,
            "port": client_port,
        }
    
    if msg_type == "list":
        movies = get_movies()
        return {
            "ok": True,
            "type": "list",
            "count": len(movies),
            "files": movies,
        }
    
    if msg_type == "status":
        if _status_provider:
            idx, title, pos_ms = _status_provider()
        else:
            idx, title, pos_ms = -1, "", 0
        enabled, interval = gremlin_get()
        return {
            "ok": True,
            "type": "status",
            "current_index": idx,
            "current_title": title,
            "current_start_ms": pos_ms,
            "gremlin_enabled": enabled,
            "gremlin_interval_s": interval,
        }

    if msg_type in ("random", "next", "prev"):
        if msg_type == "random":
            idx, title, start_ms = select_random()
        elif msg_type == "next":
            idx, title, start_ms = select_next()
        else:
            idx, title, start_ms = select_previous()
        if idx >= 0 and title and _player_callback:
            path = fullpath(title)
            # Try callback with metadata; fallback to (path, start_ms)
            try:
                _player_callback(path, start_ms, idx, title)
            except TypeError:
                _player_callback(path, start_ms)
            return {
                "ok": True,
                "type": msg_type,
                "index": idx,
                "filename": title,
                "start_ms": start_ms,
            }
        return {
            "ok": False,
            "error": "no films or player not ready",
        }

    if msg_type == "play":
        filename = msg.get("filename")
        index = msg.get("index")
        start_ms = int(msg.get("start", 0) or 0)

        movies = get_movies()
        if not movies:
            return {
                "ok": False,
                "error": "no films loaded",
            }

        if index is not None:
            try:
                idx = int(index)
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": f"invalid index: {index}",
                }
            if idx < 0 or idx >= len(movies):
                return {
                    "ok": False,
                    "error": f"index {idx} out of range [0, {len(movies)-1}]",
                }
            filename = movies[idx]
        elif filename is not None:
            idx = get_index_for_filename(filename)
            if idx is None:
                return {
                    "ok": False,
                    "error": f"filename not found: {filename}",
                }
        else:
            return {
                "ok": False,
                "error": "must provide 'filename' or 'index'",
            }

        if _player_callback:
            path = fullpath(filename)
            try:
                _player_callback(path, start_ms, idx, filename)
            except TypeError:
                _player_callback(path, start_ms)
            return {
                "ok": True,
                "type": "play",
                "index": idx,
                "filename": filename,
                "start_ms": start_ms,
            }
        return {
            "ok": False,
            "error": "player not ready",
        }

    if msg_type == "gremlin":
        action = msg.get("action", "toggle")
        if action == "toggle":
            enabled, interval = gremlin_toggle()
            if _gremlin_callback:
                _gremlin_callback(enabled)  # start/stop timer
            return {"ok": True, "type": "gremlin", "enabled": enabled, "interval_s": interval}
        if action == "on":
            enabled, interval = gremlin_get()
            if not enabled:
                enabled, interval = gremlin_toggle()
            if _gremlin_callback:
                _gremlin_callback(True)
            return {"ok": True, "type": "gremlin", "enabled": True, "interval_s": interval}
        if action == "off":
            enabled, interval = gremlin_get()
            if enabled:
                enabled, interval = gremlin_toggle()
            if _gremlin_callback:
                _gremlin_callback(False)
            return {"ok": True, "type": "gremlin", "enabled": False, "interval_s": interval}
        if action == "speed":
            seconds = int(msg.get("seconds", 5) or 5)
            enabled, interval = gremlin_set_interval(seconds)
            # If currently enabled, reapply to refresh timer
            if _gremlin_callback and enabled:
                _gremlin_callback(True)
            return {"ok": True, "type": "gremlin", "enabled": enabled, "interval_s": interval}
        return {"ok": False, "error": f"unknown gremlin action: {action}"}

    return {
        "ok": False,
        "error": f"unknown type: {msg_type}",
    }


# --- Server ---

def handle_client(conn: socket.socket, addr: tuple):
    """Handle a single client connection."""
    client_ip, client_port = addr
    try:
        data = conn.recv(65536)
        if not data:
            return
        response = parse_message(data, client_ip, client_port)
        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
    except Exception as e:
        print(f"[server] error handling {client_ip}:{client_port}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_local_ip() -> str:
    """Get the local IP address (best guess)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def run_server(port: int = DEFAULT_PORT, host: str = "0.0.0.0"):
    """Start the TCP server on the specified port."""
    refresh_config()
    local_ip = get_local_ip()
    print(f"[server] Local IP: {local_ip}")
    print(f"[server] listening on {host}:{port}")
    print(f"[server] trusted IPs: {', '.join(sorted(_trusted_ips))}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(8)

    try:
        while True:
            conn, addr = sock.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[server] shutting down")
    finally:
        sock.close()


def start_server_thread(port: int = DEFAULT_PORT):
    """Start the server in a background thread."""
    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()
    return t


# --- Standalone mode ---
if __name__ == "__main__":
    import sys
    run_server(port=int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT)
#!/usr/bin/env python3
# player_parser.py — minimal version with trusted.txt and password.txt

import random
from player_filelist import cinematheque_filelist

def load_trusted():
    """Load trusted IPs from trusted.txt (one per line)."""
    try:
        with open("trusted.txt") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        return ["127.0.0.1"]

def load_password():
    """Load password from password.txt (first non-empty line)."""
    try:
        with open("password.txt") as f:
            for line in f:
                pw = line.strip()
                if pw:
                    return pw
    except FileNotFoundError:
        return None
    return None

TRUSTED_IPS = load_trusted()
PASSWORD = load_password()

def parse_command(msg, addr):
    """Handle incoming NDJSON messages."""
    client_ip, client_port = addr

    # Check IP
    if client_ip not in TRUSTED_IPS:
        return {"ok": False, "error": "unauthorized ip", "ip": client_ip, "port": client_port}

    # Check password
    if PASSWORD and msg.get("password") != PASSWORD:
        return {"ok": False, "error": "bad password", "ip": client_ip, "port": client_port}

    cmd_type = msg.get("type")

    if cmd_type == "ping":
        return {"ok": True, "pong": True, "ip": client_ip, "port": client_port}

    elif cmd_type == "play":
        filename = msg.get("filename")
        index = msg.get("index")

        # if no filename or index and filelist is empty, error
        if not filename and index is None and not cinematheque_filelist:
            return {"ok": False, "error": "listlist empty", "type": "play", "ip": client_ip, "port": client_port}
        
        # if index given, validate and get filename
        if index is not None:
            if not (0 <= index < len(cinematheque_filelist)):
                return {"ok": False, "error": "bad index", "type": "play", "ip": client_ip, "port": client_port}
            filename = cinematheque_filelist[index]
        
        # if no filename or index, choose random file from list
        if not filename and index is None:
            index = random.randint(0, len(cinematheque_filelist) - 1)
            filename = cinematheque_filelist[index]

        start = msg.get("start", 0)
        stop  = msg.get("stop")
        return {
            "ok": True,
            "action": "play",
            "filename": filename,
            "index": index,
            "start": start,
            "stop": stop,
            "ip": client_ip,
        }
    
    elif cmd_type == "stop":
        return {"ok": True, "action": "stop", "ip": client_ip, "port": client_port}
    
    elif cmd_type == "list":
        return {"ok": True, "action": "list", "list": cinematheque_filelist, "ip": client_ip, "port": client_port}

    else:
        # remove password before echoing back
        if "password" in msg:
            msg["password"] = ""
        return {"ok": True, "echo": msg, "ip": client_ip, "port": client_port}
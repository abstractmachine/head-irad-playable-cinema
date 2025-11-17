#!/usr/bin/env python3
# player.py — start the Qt double-buffer player + NDJSON server; route messages to the player

import sys, threading, socket
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from player_server   import NDJSONServer
from player_parser   import parse_command
from player_control  import PlayerControl
from player_buffer   import DoubleBufferMPV, load_filenames, CSV_PATH

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def main():
    # 1) start Qt + player window
    files = load_filenames(CSV_PATH)
    app = QApplication(sys.argv)
    app.setOverrideCursor(Qt.BlankCursor)
    w = DoubleBufferMPV(files)

    # 2) thread-safe control bound to the player
    control = PlayerControl(w)

    # 3) handler: parse -> apply side-effects -> reply
    def handler(msg, addr):
        res = parse_command(msg, addr)

        # Extra admin endpoint handled here (not in parser):
        if isinstance(msg, dict) and (msg.get("type") or msg.get("op")) == "status":
            return {
                "ok": True,
                "status": {
                    "auto_switch": bool(w.auto_switch),
                    "interval_ms": int(w.switch_interval_ms),
                    "current_file": getattr(w.current, "current_file", None),
                    "preload_file": getattr(w.preload, "current_file", None),
                    "preload_ready": bool(getattr(w.preload, "ready", False)),
                }
            }

        if res and res.get("ok"):
            act = res.get("action")

            if act == "play":
                filename = res.get("filename")
                start    = res.get("start", -1)
                if filename:
                    print(f"[server->player] PLAY filename={filename} start={start}")
                    control.play(filename=filename, start_ms=start, cut_when_ready=True)

            elif act == "auto":
                on = bool(res.get("on", False))
                print(f"[server->player] AUTO on={on}")
                control.set_auto_switch(on)
                # reflect live state
                return {"ok": True, "applied": True, "auto_switch": bool(w.auto_switch)}

            elif act == "speed":
                ms = int(res.get("ms", 10000))
                print(f"[server->player] SPEED ms={ms}")
                control.set_speed_ms(ms)
                # reflect live state
                return {"ok": True, "applied": True, "interval_ms": int(w.switch_interval_ms)}

        return res or {"ok": False, "error": "parser returned nothing"}

    # 4) start the NDJSON server
    srv = NDJSONServer(port=6666, handler=handler)
    srv.start()
    print(f"[server] listening on {get_local_ip()}:6666")
    print(f"[player] auto_switch={w.auto_switch} interval_ms={w.switch_interval_ms}")

    # 5) run
    try:
        sys.exit(app.exec_())
    finally:
        srv.stop()

if __name__ == "__main__":
    main()
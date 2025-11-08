#!/usr/bin/env python3
# player-server.py — minimal NDJSON-over-TCP threaded server

import socket, json, threading

class NDJSONServer:
    def __init__(self, host="0.0.0.0", port=6666, handler=None):
        self.host, self.port = host, port
        self.handler = handler or (lambda msg, addr: {"ok": True, "echo": msg})
        self._srv = None
        self._alive = threading.Event()

    def start(self):
        """Start server in a background thread."""
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        self._alive.set()
        threading.Thread(target=self._accept_loop, daemon=True).start()
        print(f"[server] listening on {self.host}:{self.port}")

    def stop(self):
        """Stop server."""
        self._alive.clear()
        if self._srv:
            try: self._srv.close()
            except: pass
        print("[server] stopped")

    def _accept_loop(self):
        while self._alive.is_set():
            try:
                conn, addr = self._srv.accept()
                threading.Thread(target=self._client_loop, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _client_loop(self, conn, addr):
        print(f"[connect] {addr}")
        buf = b""
        try:
            conn.settimeout(0.5)
            while self._alive.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                        reply = self.handler(msg, addr)
                        if not isinstance(reply, dict):
                            reply = {"ok": False, "error": "invalid reply"}
                    except Exception as e:
                        reply = {"ok": False, "error": str(e)}
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        finally:
            print(f"[disconnect] {addr}")
            try: conn.close()
            except: pass

if __name__ == "__main__":
    from player_parser import parse_command
    srv = NDJSONServer(port=6666, handler=parse_command)
    srv.start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
#!/usr/bin/env python3
"""
A minimal NDJSON TCP server.
Each line is a JSON object terminated by '\n'.
Replies are also JSON lines.
"""

import socket
import json
import threading

HOST = "0.0.0.0"   # listen on all interfaces
PORT = 6666        # 👈 updated port

def handle_client(conn, addr):
    print(f"[connect] {addr}")
    buffer = b""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                    print(f"[recv] {msg}")
                    # example reply
                    reply = {"ok": True, "echo": msg}
                except Exception as e:
                    reply = {"ok": False, "error": str(e)}
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
    except ConnectionResetError:
        pass
    finally:
        print(f"[disconnect] {addr}")
        conn.close()

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(5)
    print(f"[server] listening on {HOST}:{PORT}")

    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[server] shutting down.")
    finally:
        server.close()

if __name__ == "__main__":
    main()
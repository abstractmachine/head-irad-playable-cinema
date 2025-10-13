#!/usr/bin/env python3
# player.py — entry point for the whole system

from player_server import NDJSONServer
from player_parser import parse_command
import threading

def main():
    server = NDJSONServer(port=6666, handler=parse_command)
    server.start()
    try:
        threading.Event().wait()  # keep alive
    except KeyboardInterrupt:
        server.stop()

if __name__ == "__main__":
    main()
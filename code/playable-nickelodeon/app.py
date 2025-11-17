#!/usr/bin/env python3
import sys
import os
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QCursor

from cinematheque import get_movies, CSV_PATH, MOVIES_DIR, fullpath
from switcher import (
    select_random, select_next, select_previous, randomize_time,
    gremlin_toggle, gremlin_set_interval,
)
from player import DoubleBufferMPV
import server

# Use X11/XWayland
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

def main():
    print(f"[nickelodeon] CSV_PATH={CSV_PATH}")
    print(f"[nickelodeon] MOVIES_DIR={MOVIES_DIR}")
    
    films = get_movies()
    print(f"[nickelodeon] loaded {len(films)} films")
    
    app = QApplication(sys.argv)
    app.setOverrideCursor(QCursor(Qt.BlankCursor))
    
    player = DoubleBufferMPV()
    
    # Wire server to player
    server.set_player_callback(player.play_with_meta)
    server.set_status_provider(player.get_status)
    server.set_gremlin_callback(player.set_gremlin_threadsafe)
    server.start_server_thread(port=6666)
    
    # Keyboard handlers send metadata too
    def handle_key(event):
        key = event.key()
        if key in (Qt.Key_Escape, Qt.Key_Q):
            print("[nickelodeon] quit requested")
            player.close()
            app.quit()
        elif key == Qt.Key_R:
            idx, title, start_ms = select_random()
            if idx >= 0 and title:
                print(f"[switcher] RANDOM → index={idx} title={title} start={start_ms}ms")
                player.play_with_meta(fullpath(title), start_ms, idx, title)
        elif key == Qt.Key_PageUp:
            idx, title, start_ms = select_previous()
            if idx >= 0 and title:
                print(f"[switcher] PREVIOUS → index={idx} title={title} start={start_ms}ms")
                player.play_with_meta(fullpath(title), start_ms, idx, title)
        elif key == Qt.Key_PageDown:
            idx, title, start_ms = select_next()
            if idx >= 0 and title:
                print(f"[switcher] NEXT → index={idx} title={title} start={start_ms}ms")
                player.play_with_meta(fullpath(title), start_ms, idx, title)
        elif key == Qt.Key_T:
            idx, title, start_ms = randomize_time()
            if idx >= 0 and title:
                print(f"[switcher] TIME-RANDOM → index={idx} title={title} start={start_ms}ms")
                player.play_with_meta(fullpath(title), start_ms, idx, title)
        elif key == Qt.Key_G:
            enabled, interval = gremlin_toggle()
            state = "ON" if enabled else "OFF"
            print(f"[switcher] GREMLIN → {state} every {interval}s")
            player.set_gremlin(enabled)
        elif Qt.Key_1 <= key <= Qt.Key_9:
            seconds = key - Qt.Key_0
            enabled, interval = gremlin_set_interval(seconds)
            print(f"[switcher] GREMLIN SPEED → {interval}s")
    
    player.keyPressEvent = handle_key
    return app.exec_()

if __name__ == "__main__":
    sys.exit(main())
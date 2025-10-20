#!/usr/bin/env python3
# player_control.py — thread-safe bridge from the network handler to the Qt player

import os
from PyQt5.QtCore import QObject, QTimer
from player_filelist import MOVIES_DIR

class PlayerControl(QObject):
    def __init__(self, player_window):
        super().__init__()
        self.w = player_window  # DoubleBufferMPV

    # public, thread-safe
    def play(self, filename: str, start_ms: int = -1, cut_when_ready: bool = True):
        QTimer.singleShot(0, lambda: self._play_on_ui(filename, start_ms, cut_when_ready))

    def set_auto_switch(self, on: bool):
        QTimer.singleShot(0, lambda: self.w.set_auto_switch(bool(on)))

    def set_speed_ms(self, ms: int):
        QTimer.singleShot(0, lambda: self.w.set_switch_interval_ms(int(ms)))

    # runs on UI thread
    def _play_on_ui(self, filename: str, start_ms: int, cut_when_ready: bool):
        path = os.path.join(MOVIES_DIR, filename)
        if not os.path.exists(path):
            print(f"[control] file not found: {path}")
            return
        try:
            self.w.preload.target_ms = int(start_ms) if start_ms is not None else -1
        except Exception:
            self.w.preload.target_ms = -1
        self.w.preload.load_file(path)
        if cut_when_ready:
            self._cut_when_ready()

    def _cut_when_ready(self):
        if self.w.preload.ready:
            self.w._switch_if_ready()
        else:
            QTimer.singleShot(50, self._cut_when_ready)
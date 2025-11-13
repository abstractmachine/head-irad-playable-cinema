#!/usr/bin/env python3
"""
Dual-video player using mpv, with two caption panes driven by CSV shot lists.
"""
# --- Environment setup for macOS FAISS stability ---
import os
os.environ["OMP_NUM_THREADS"] = "1"              # limit FAISS to one thread
os.environ["TOKENIZERS_PARALLELISM"] = "false"   # silence HuggingFace warning
# ---------------------------------------------------

import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeyEvent, QFont, QGuiApplication
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel

from pathlib import Path

from config import MOVIE_PATH, GAME_PATH, MOVIE_CSV, GAME_CSV
from player import MpvPlayer
from data import load_shots, find_shot_at_time
from search import ensure_embeddings, FaissMatcher

class DualPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dual Player")
        self.setStyleSheet("background:#000;")

        # Load shots (now each shot has 'row' index)
        self.movie_shots = load_shots(MOVIE_CSV)
        self.game_shots = load_shots(GAME_CSV)
        self.current_movie_shot = None
        self.current_game_shot = None

        # Ensure sidecar .txt/.npy exist and load .txt lines
        self.movie_txt_path, self.movie_npy_path = ensure_embeddings(MOVIE_CSV)
        self.game_txt_path, _ = ensure_embeddings(GAME_CSV)
        self.movie_lines = self._load_txt_lines(self.movie_txt_path)
        self.game_lines = self._load_txt_lines(self.game_txt_path)

        # FAISS matcher on movie side + last gameplay caption state
        self.matcher = FaissMatcher(MOVIE_CSV, self.movie_npy_path)
        self.last_game_caption: str = ""

        # Layout
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)

        # Top: two caption panes
        text_container = QWidget()
        text_layout = QHBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(10)

        self.movie_text = self._make_text_panel("Movie")
        self.game_text = self._make_text_panel("Game")

        text_layout.addWidget(self.movie_text["container"], 1)
        text_layout.addWidget(self.game_text["container"], 1)

        # Bottom: video stack
        video_container = QWidget()
        video_layout = QVBoxLayout(video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)

        self.movie = MpvPlayer(video_container, "Movie")
        self.game = MpvPlayer(video_container, "Game")
        # Allow full horizontal resizing (remove fixed widths)
        # self.movie.widget.setFixedWidth(640)
        # self.game.widget.setFixedWidth(640)

        video_layout.addWidget(self.movie.widget)
        video_layout.addWidget(self.game.widget)

        main_layout.addWidget(text_container, 1)
        main_layout.addWidget(video_container)

        self.setCentralWidget(main)

        # Start players shortly after window is ready
        QTimer.singleShot(100, lambda: self._init_players(str(MOVIE_PATH), str(GAME_PATH)))

        # Update timer
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)

        # Make window resizable and size to 90% width x 25% height of screen
        screen_geo = QGuiApplication.primaryScreen().availableGeometry()
        w = int(screen_geo.width() * 0.95)
        h = int(screen_geo.height() * 0.4)
        self.resize(w, h)
        self.show()

        print("Controls: 1-5 speed, ←/→ ±5s, PgUp/PgDn ±60s, Q/Esc quit")

    def _make_text_panel(self, title: str):
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        label = QLabel(title)
        label.setStyleSheet("color:#fff; font-size:12px; font-weight:bold; padding:5px;")
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setStyleSheet("QTextEdit{background:#1a1a1a; color:#fff; border:none; padding:10px; font-size:18px;}")
        edit.setFont(QFont("Arial", 18))
        edit.setText(f"{title} shot caption will appear here...")
        v.addWidget(label)
        v.addWidget(edit)
        return {"container": container, "label": label, "edit": edit}

    def _init_players(self, movie_path: str, game_path: str):
        self.movie.start(movie_path)
        self.game.start(game_path)

    def _load_txt_lines(self, path: Path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [line.rstrip("\n") for line in f]
        except OSError as e:
            print(f"Warning: could not read {path}: {e}")
            return []

    def _tick(self):
        # Poll mpv events
        self.movie.poll_events()
        self.game.poll_events()

        mpos = self.movie.get_current_time()
        gpos = self.game.get_current_time()

        mshot = find_shot_at_time(self.movie_shots, mpos)
        if mshot != self.current_movie_shot:
            self.current_movie_shot = mshot
            self._set_caption_from_txt(self.movie_text["edit"], mshot, mpos, self.movie_lines)

        gshot = find_shot_at_time(self.game_shots, gpos)
        if gshot != self.current_game_shot:
            self.current_game_shot = gshot
            # Get the raw one-line text for this gameplay shot
            game_line = self._set_caption_from_txt(self.game_text["edit"], gshot, gpos, self.game_lines)

            # Sync logic:
            normalized = (game_line or "").strip()
            if not normalized:
                # Empty → do nothing, but reset memory
                self.last_game_caption = ""
            else:
                if normalized != self.last_game_caption:
                    self.last_game_caption = normalized
                    match = self.matcher.search_best_from_text(normalized)
                    if match and match.get("start_seconds") is not None:
                        self.movie.seek(float(match["start_seconds"]), relative=False)

    def _set_caption_from_txt(self, widget, shot, pos: float, lines: list[str]):
        if not shot:
            widget.setText("No caption for this time range")
            return ""
        idx = shot.get("row")
        text = ""
        if isinstance(idx, int) and 0 <= idx < len(lines):
            text = (lines[idx] or "")
            # Convert ' | ' separators into line breaks for readability
            display = text.replace(" | ", "\n").strip()
            widget.setText(display)
            return text
        widget.setText("")
        return ""

    def keyPressEvent(self, event: QKeyEvent):
        k = event.key()
        if k in (Qt.Key_Q, Qt.Key_Escape):
            self.close()
        elif Qt.Key_1 <= k <= Qt.Key_5:
            idx = k - Qt.Key_1
            self.movie.set_speed(idx)
            self.game.set_speed(idx)
        elif k == Qt.Key_Left:
            self.movie.seek(-5); self.game.seek(-5)
        elif k == Qt.Key_Right:
            self.movie.seek(5); self.game.seek(5)
        elif k == Qt.Key_PageUp:
            self.movie.seek(-60); self.game.seek(-60)
        elif k == Qt.Key_PageDown:
            self.movie.seek(60); self.game.seek(60)

    def closeEvent(self, event):
        self.timer.stop()
        self.movie.close()
        self.game.close()
        super().closeEvent(event)

def main():
    app = QApplication(sys.argv)
    win = DualPlayer()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
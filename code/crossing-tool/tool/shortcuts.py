"""Central keyboard shortcut definitions shared across all visualizers.

Import the named constants instead of raw ``Qt.Key_*`` values so that any
future re-mapping only needs to change this file.
"""

from PyQt5.QtCore import Qt

# ── Film-list navigation ──────────────────────────────────────────────────────
KEY_PREV_MOVIE = Qt.Key_Home
KEY_NEXT_MOVIE = Qt.Key_End

# ── Scene navigation (within current film) ────────────────────────────────────
KEY_PREV_SCENE = Qt.Key_PageUp
KEY_NEXT_SCENE = Qt.Key_PageDown

# ── Shot navigation (within current scene / film) ─────────────────────────────
KEY_PREV_SHOT = Qt.Key_Up
KEY_NEXT_SHOT = Qt.Key_Down

# ── Frame-level playback stepping ─────────────────────────────────────────────
KEY_PREV_FRAME = Qt.Key_Left
KEY_NEXT_FRAME = Qt.Key_Right
KEY_PLAY_PAUSE = Qt.Key_Space

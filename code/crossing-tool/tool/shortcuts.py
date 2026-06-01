"""Central keyboard shortcut definitions shared across all visualizers.

Import the named constants instead of raw ``Qt.Key_*`` values so that any
future re-mapping only needs to change this file.
"""

from PyQt5.QtCore import Qt

# ── Title-list navigation (movies or gameplay, consistent across all visualizers) ──
KEY_PREV_TITLE = Qt.Key_Home
KEY_NEXT_TITLE = Qt.Key_End

# ── Item navigation (context-dependent: scene in shot visualizer, etc.) ──────
KEY_PREV_ITEM = Qt.Key_PageUp
KEY_NEXT_ITEM = Qt.Key_PageDown

# ── Shot navigation (within current scene / film) ─────────────────────────────
KEY_PREV_SHOT = Qt.Key_Up
KEY_NEXT_SHOT = Qt.Key_Down

# ── Frame-level playback stepping ─────────────────────────────────────────────
KEY_PREV_FRAME = Qt.Key_Left
KEY_NEXT_FRAME = Qt.Key_Right
KEY_PLAY_PAUSE = Qt.Key_Space

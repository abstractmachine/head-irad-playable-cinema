from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QComboBox, QListView, QFrame

from styles import theme


def attach_combo_popup(combo: QComboBox) -> QListView:
    """Attach a styled QListView popup to *combo* and return the view.

    Centralized popup styling/cleanup used by visualizers that need a
    consistent combo-list appearance. This mirrors the previous inline
    implementations: it creates the QListView, applies the popup stylesheet,
    removes frame/borders/margins, and attempts to clean up the container
    widget Qt may create for the popup.
    """
    _sv = QListView(combo)
    _sv.setUniformItemSizes(True)
    _sv.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    _sv.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    _sv.setFrameShape(QFrame.NoFrame)
    _sv.setLineWidth(0)
    _sv.setMidLineWidth(0)
    _sv.setContentsMargins(0, 0, 0, 0)
    # Ensure the combo's displayed text is bold and the popup items match.
    try:
        combo.setFont(theme.font_ui(bold=True))
    except Exception:
        pass

    _sv.setStyleSheet(
        f"QListView {{ background: {theme.INPUT_BG}; color: {theme.TEXT};"
        f" font-family: '{theme.FAMILY_UI}'; font-size: {theme.BASE_PT}pt; font-weight: bold;"
        f" border: 0px; margin: 0px; padding: 0px; outline: 0px; }}"
        f"QListView::item {{ background: {theme.INPUT_BG}; padding: 0px 8px;"
        f" min-height: 24px; border: 0px; font-weight: bold; }}"
        f"QListView::item:selected {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; font-weight: bold; }}"
    )
    combo.setView(_sv)
    _sv.setViewportMargins(0, 0, 0, 0)
    _sc = _sv.parentWidget()
    if _sc is not None:
        try:
            _sc.setFrameStyle(QFrame.NoFrame)
            _sc.setLineWidth(0)
            _sc.setMidLineWidth(0)
            _sc.setStyleSheet(f"QFrame {{ background: {theme.INPUT_BG}; border: 0px; margin: 0px; padding: 0px; }}")
            if _sc.layout():
                _sc.layout().setContentsMargins(0, 0, 0, 0)
                _sc.layout().setSpacing(0)
        except Exception:
            pass
    return _sv

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPainter
from PyQt5.QtWidgets import QApplication, QComboBox, QLabel

from styles import theme
from visualizers.components.collapsible_section import CollapsibleSection
from visualizers.components.combo_popup import add_combo_all_item, style_canonical_combo


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    theme.apply_theme(instance)
    return instance


def _render(combo):
    combo.resize(180, 24)
    combo.show()
    QApplication.processEvents()
    image = QImage(combo.size(), QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    combo.render(painter)
    painter.end()
    return image


def _triangle_pixels(image):
    return sum(
        image.pixelColor(x, y).name() == theme.TRIANGLE
        for x in range(theme.TRIANGLE_LEFT, theme.TRIANGLE_LEFT + theme.TRIANGLE_WIDTH)
        for y in range(5, 19)
    )


def test_combo_displays_right_triangle_before_current_value(app):
    combo = QComboBox()
    combo.addItem("Current value")

    image = _render(combo)

    assert theme._COMBO_INDICATOR_MARKER in combo.styleSheet()
    assert "subcontrol-position: center left" in combo.styleSheet()
    assert _triangle_pixels(image) >= 10
    combo.close()
    combo.deleteLater()


def test_indicator_survives_dynamic_local_stylesheet(app):
    combo = QComboBox()
    combo.addItem("Restyled value")
    _render(combo)

    combo.setStyleSheet("QComboBox { background: #123456; color: #ffffff; }")
    image = _render(combo)

    assert combo.styleSheet().startswith(
        "QComboBox { background: #123456; color: #ffffff; }"
    )
    assert theme._COMBO_INDICATOR_MARKER in combo.styleSheet()
    assert _triangle_pixels(image) >= 10
    combo.close()
    combo.deleteLater()


def test_combo_and_section_triangles_share_alignment_and_color(app):
    combo = QComboBox()
    combo.addItem("Current value")
    _render(combo)
    combo_arrow = combo.findChild(QLabel, "crossingComboIndicator")
    section = CollapsibleSection("Section", expanded=False)

    assert combo_arrow.geometry().left() == theme.TRIANGLE_LEFT
    assert section._header_arrow.geometry().left() == theme.TRIANGLE_LEFT
    assert theme.TRIANGLE in combo_arrow.styleSheet()
    assert theme.TRIANGLE in section._header_arrow.styleSheet()
    assert combo_arrow.text() == "▶"
    assert section._header_arrow.text() == "▶"

    section.set_expanded(True)
    assert section._header_arrow.text() == "▼"
    assert theme.TRIANGLE in section._header_arrow.styleSheet()
    combo.close()
    section.close()


def test_all_item_is_dimmed_but_preserves_hidden_value(app):
    combo = QComboBox()
    add_combo_all_item(combo, user_data="--all")
    combo.addItem("setting", userData="setting")
    style_canonical_combo(combo)

    assert combo.currentText() == "<all>"
    assert combo.currentData() == "--all"
    assert combo.itemData(0, Qt.ForegroundRole).name() == theme.TEXT_DIM
    assert theme.TEXT_DIM in combo.styleSheet()

    combo.setCurrentIndex(1)
    assert combo.currentText() == "setting"
    assert combo.itemData(1, Qt.ForegroundRole) is None
    assert theme.TEXT in combo.styleSheet()
    combo.close()

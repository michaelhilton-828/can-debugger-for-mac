"""Wayve brand theme for the DBC Viewer.

Primary colors are from Wayve Brand Identity Guidelines V2.0 (June 2026):
https://wayve.ai/wp-content/uploads/2026/06/Wayve_Brand_Guidelines_June_2026.pdf

Pure White #FFFFFF, Off White #F6F6F2, Deep Navy #1A1730,
Ocean Blue #03B5D1, and Steel Grey #535353.

Border, idle-tab, selection, and disabled colors are tints mixed from that
primary palette. Ocean Blue is the only accent, and it is used sparingly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPalette, QPolygon
from PySide6.QtWidgets import QApplication

# Primary palette (guidelines).
WHITE = "#FFFFFF"
OFF_WHITE = "#F6F6F2"
NAVY = "#1A1730"
OCEAN = "#03B5D1"
STEEL = "#535353"

# Tints of the primary palette, not extra brand hues.
BORDER = "#D9D9D5"  # off-white toward steel
TAB_IDLE = "#E2E2DF"
GRID = "#E6E6E2"
HOVER = "#F3F3EE"
PRESSED = "#E2E2DF"
SELECTION = "#C8EFF5"  # white toward ocean
DISABLED_TEXT = "#9E9C98"
DISABLED_BG = "#F3F3EE"
NAVY_HOVER = "#353149"  # navy toward white, default-button hover only
GROUP_FILL = WHITE
GROUP_BORDER = BORDER


def set_muted(widget) -> None:
    """Mark a label as secondary Steel Grey text."""
    widget.setProperty("muted", True)


def apply_theme(app: QApplication) -> None:
    """Apply the brand palette and the application stylesheet."""
    _apply_palette(app)
    app.setStyleSheet(application_stylesheet())


def _arrow_urls() -> dict[str, str]:
    """Small triangle images. Qt stylesheets do not turn CSS borders into arrows."""
    folder = Path(tempfile.gettempdir()) / "dbc-viewer-theme"
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "down": (folder / "arrow-down.png", "down", NAVY),
        "up": (folder / "arrow-up.png", "up", NAVY),
        "down_disabled": (folder / "arrow-down-disabled.png", "down", DISABLED_TEXT),
        "up_disabled": (folder / "arrow-up-disabled.png", "up", DISABLED_TEXT),
    }
    urls = {}
    for key, (path, direction, color) in files.items():
        _write_triangle(path, direction, color)
        urls[key] = path.as_posix()
    return urls


def _write_triangle(path: Path, direction: str, color: str) -> None:
    image = QImage(9, 6, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(color)))
    if direction == "down":
        points = [QPoint(0, 1), QPoint(8, 1), QPoint(4, 5)]
    else:
        points = [QPoint(0, 4), QPoint(8, 4), QPoint(4, 0)]
    painter.drawPolygon(QPolygon(points))
    painter.end()
    image.save(str(path))


def application_stylesheet() -> str:
    arrows = _arrow_urls()
    down = arrows["down"]
    up = arrows["up"]
    down_disabled = arrows["down_disabled"]
    up_disabled = arrows["up_disabled"]
    return f"""
    QMainWindow, QDialog, QMessageBox {{
        background: {OFF_WHITE};
        color: {NAVY};
    }}
    QWidget#appPage {{
        background: {OFF_WHITE};
        color: {NAVY};
    }}
    QWidget#plotToolGroupBody {{
        background: transparent;
    }}

    QMenuBar {{
        background: {OFF_WHITE};
        color: {NAVY};
        border-bottom: 1px solid {BORDER};
    }}
    QMenuBar::item {{
        background: transparent;
        color: {NAVY};
        padding: 4px 8px;
    }}
    QMenuBar::item:selected {{
        background: {NAVY};
        color: {WHITE};
    }}
    QMenu {{
        background: {WHITE};
        color: {NAVY};
        border: 1px solid {BORDER};
    }}
    QMenu::item {{
        background: transparent;
        color: {NAVY};
        padding: 4px 18px;
    }}
    QMenu::item:selected {{
        background: {SELECTION};
        color: {NAVY};
    }}
    QMenu::item:disabled {{
        color: {DISABLED_TEXT};
    }}
    QMenu::separator {{
        height: 1px;
        background: {BORDER};
        margin: 4px 8px;
    }}

    QTabWidget::pane {{
        background: {OFF_WHITE};
        border: 1px solid {BORDER};
        top: -1px;
    }}
    QTabBar::tab {{
        background: {TAB_IDLE};
        color: {STEEL};
        border: 1px solid {BORDER};
        border-bottom: none;
        border-top: 2px solid {TAB_IDLE};
        padding: 6px 14px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background: {WHITE};
        color: {NAVY};
        border-top: 2px solid {OCEAN};
    }}
    QTabBar::tab:hover:!selected {{
        background: {OFF_WHITE};
        color: {NAVY};
    }}

    QLabel {{
        color: {NAVY};
        background: transparent;
    }}
    QLabel[muted="true"] {{
        color: {STEEL};
    }}
    QLabel#plotToolGroupTitle {{
        color: {STEEL};
        font-size: 11px;
        font-weight: 600;
        background: transparent;
    }}
    QLabel#plotToolCaption {{
        color: {NAVY};
        background: transparent;
    }}
    QCheckBox {{
        color: {NAVY};
        background: transparent;
        spacing: 6px;
    }}

    QPushButton {{
        background: {WHITE};
        color: {NAVY};
        border: 1px solid {BORDER};
        border-radius: 3px;
        padding: 3px 10px;
    }}
    QPushButton:hover {{
        background: {HOVER};
        border-color: {NAVY};
    }}
    QPushButton:pressed {{
        background: {PRESSED};
    }}
    QPushButton:checked {{
        background: {SELECTION};
        color: {NAVY};
        border: 1px solid {OCEAN};
    }}
    QPushButton:disabled {{
        background: {DISABLED_BG};
        color: {DISABLED_TEXT};
        border: 1px solid {BORDER};
    }}
    QPushButton:default {{
        background: {NAVY};
        color: {WHITE};
        border: 1px solid {NAVY};
    }}
    QPushButton:default:hover {{
        background: {NAVY_HOVER};
        color: {WHITE};
    }}
    QPushButton:default:disabled {{
        background: {DISABLED_BG};
        color: {DISABLED_TEXT};
        border: 1px solid {BORDER};
    }}
    QPushButton#plotToolButton {{
        padding: 0 10px;
        border: 1px solid {BORDER};
        border-radius: 3px;
        background: {WHITE};
        color: {NAVY};
    }}
    QPushButton#plotToolButton:hover {{
        background: {HOVER};
        border: 1px solid {NAVY};
        color: {NAVY};
    }}
    QPushButton#plotToolButton:checked {{
        background: {SELECTION};
        border: 1px solid {OCEAN};
        color: {NAVY};
    }}
    QPushButton#plotToolButton:disabled {{
        color: {DISABLED_TEXT};
        background: {DISABLED_BG};
        border: 1px solid {BORDER};
    }}

    QLineEdit, QComboBox, QAbstractSpinBox {{
        background: {WHITE};
        color: {NAVY};
        border: 1px solid {BORDER};
        border-radius: 3px;
        padding: 2px 6px;
        selection-background-color: {SELECTION};
        selection-color: {NAVY};
    }}
    QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{
        border: 1px solid {OCEAN};
    }}
    QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {{
        background: {DISABLED_BG};
        color: {DISABLED_TEXT};
    }}
    QComboBox#plotToolControl, QAbstractSpinBox#plotToolControl {{
        padding: 0 4px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 16px;
        border: none;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: url({down});
        width: 9px;
        height: 6px;
    }}
    QComboBox::down-arrow:disabled {{
        image: url({down_disabled});
    }}
    QComboBox QAbstractItemView {{
        background: {WHITE};
        color: {NAVY};
        selection-background-color: {SELECTION};
        selection-color: {NAVY};
        border: 1px solid {BORDER};
    }}
    QSpinBox, QDoubleSpinBox {{
        padding-right: 14px;
    }}
    QSpinBox#plotToolControl, QDoubleSpinBox#plotToolControl {{
        padding-right: 14px;
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 14px;
        border: none;
        background: transparent;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 14px;
        border: none;
        background: transparent;
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url({up});
        width: 9px;
        height: 6px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url({down});
        width: 9px;
        height: 6px;
    }}
    QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{
        image: url({up_disabled});
    }}
    QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{
        image: url({down_disabled});
    }}

    QTableView, QTreeView, QListView {{
        background: {WHITE};
        color: {NAVY};
        border: 1px solid {BORDER};
        gridline-color: {GRID};
        selection-background-color: {SELECTION};
        selection-color: {NAVY};
        alternate-background-color: {OFF_WHITE};
        show-decoration-selected: 1;
    }}
    QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
        background: {SELECTION};
        color: {NAVY};
    }}
    QHeaderView::section {{
        background: {OFF_WHITE};
        color: {NAVY};
        border: none;
        border-right: 1px solid {GRID};
        border-bottom: 1px solid {BORDER};
        padding: 4px 6px;
    }}
    QTableCornerButton::section {{
        background: {OFF_WHITE};
        border: none;
        border-bottom: 1px solid {BORDER};
    }}

    QSplitter {{
        background: {OFF_WHITE};
    }}
    QSplitter::handle {{
        background: {BORDER};
    }}
    QSplitter::handle:hover {{
        background: {OCEAN};
    }}

    QScrollArea {{
        background: {WHITE};
        border: 1px solid {BORDER};
    }}
    QScrollArea > QWidget > QWidget {{
        background: {WHITE};
    }}

    QToolBar {{
        background: {OFF_WHITE};
        border: none;
        spacing: 2px;
    }}
    QToolTip {{
        background: {NAVY};
        color: {WHITE};
        border: none;
        padding: 4px 6px;
    }}
    """


def _apply_palette(app: QApplication) -> None:
    palette = app.palette()
    navy = QColor(NAVY)
    white = QColor(WHITE)
    off_white = QColor(OFF_WHITE)
    steel = QColor(STEEL)
    selection = QColor(SELECTION)
    disabled = QColor(DISABLED_TEXT)
    disabled_bg = QColor(DISABLED_BG)

    active = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive)
    for group in active:
        palette.setColor(group, QPalette.ColorRole.Window, off_white)
        palette.setColor(group, QPalette.ColorRole.WindowText, navy)
        palette.setColor(group, QPalette.ColorRole.Base, white)
        palette.setColor(group, QPalette.ColorRole.AlternateBase, off_white)
        palette.setColor(group, QPalette.ColorRole.Text, navy)
        palette.setColor(group, QPalette.ColorRole.Button, white)
        palette.setColor(group, QPalette.ColorRole.ButtonText, navy)
        palette.setColor(group, QPalette.ColorRole.Highlight, selection)
        palette.setColor(group, QPalette.ColorRole.HighlightedText, navy)
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, navy)
        palette.setColor(group, QPalette.ColorRole.ToolTipText, white)
        palette.setColor(group, QPalette.ColorRole.PlaceholderText, steel)
        palette.setColor(group, QPalette.ColorRole.Mid, QColor(BORDER))

    group = QPalette.ColorGroup.Disabled
    palette.setColor(group, QPalette.ColorRole.Window, off_white)
    palette.setColor(group, QPalette.ColorRole.WindowText, disabled)
    palette.setColor(group, QPalette.ColorRole.Base, disabled_bg)
    palette.setColor(group, QPalette.ColorRole.Text, disabled)
    palette.setColor(group, QPalette.ColorRole.Button, disabled_bg)
    palette.setColor(group, QPalette.ColorRole.ButtonText, disabled)
    palette.setColor(group, QPalette.ColorRole.Highlight, selection)
    palette.setColor(group, QPalette.ColorRole.HighlightedText, navy)
    app.setPalette(palette)

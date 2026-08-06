"""Bit-layout diagram widget: an 8-columns x DLC-rows grid showing which bits
each signal occupies, so overlaps and gaps are visible at a glance.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from .dbc_model import mux_label, signal_bit_cells

CELL_W = 40
CELL_H = 28
LABEL_COL_W = 64
LABEL_ROW_H = 24

PALETTE = [
    QColor(0x4E, 0x79, 0xA7),
    QColor(0xF2, 0x8E, 0x2B),
    QColor(0xE1, 0x57, 0x59),
    QColor(0x76, 0xB7, 0xB2),
    QColor(0x59, 0xA1, 0x4F),
    QColor(0xED, 0xC9, 0x48),
    QColor(0xB0, 0x7A, 0xA1),
    QColor(0xFF, 0x9D, 0xA7),
    QColor(0x9C, 0x75, 0x5F),
    QColor(0xBA, 0xB0, 0xAC),
    QColor(0x86, 0xBC, 0xB6),
    QColor(0xF1, 0xCE, 0x63),
]

OVERLAP_COLOR = QColor(0xD6, 0x00, 0x00)
GAP_COLOR = QColor(0xEC, 0xEC, 0xEC)
GRID_LINE = QColor(0x88, 0x88, 0x88)
MUX_HATCH_COLOR = QColor(0x00, 0x00, 0x00, 90)


def _is_safe_multiplex_group(signals) -> bool:
    """True if the signals sharing a cell can never be active simultaneously.

    Mutually-exclusive multiplexed alternatives (e.g. DiagParam1 under mux
    case 0 and DiagParam2 under mux case 1) legitimately share bits — only
    one is selected at a time by the multiplexer switch signal — so that is
    not a real layout conflict and shouldn't be flagged as one.
    """
    if len(signals) < 2:
        return True
    id_sets = []
    for sig in signals:
        ids = getattr(sig, "multiplexer_ids", None)
        if not ids:
            return False
        id_sets.append(set(ids))
    for i, a in enumerate(id_sets):
        for b in id_sets[i + 1:]:
            if a & b:
                return False
    return True


class BitLayoutWidget(QWidget):
    """Renders the bit grid for the currently selected message."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dlc = 0
        self._cell_signals: dict[tuple[int, int], list[str]] = {}
        self._cell_raw_signals: dict[tuple[int, int], list] = {}
        self._signal_colors: dict[str, QColor] = {}
        self._selected_signal: str | None = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_message(self, dlc: int, signal_rows):
        self._dlc = max(dlc, 0)
        self._cell_signals = {}
        self._cell_raw_signals = {}
        self._signal_colors = {}
        for i, row in enumerate(signal_rows):
            color = PALETTE[i % len(PALETTE)]
            self._signal_colors[row.name] = color
            for cell in signal_bit_cells(row.signal):
                byte_idx, weight = cell
                if 0 <= byte_idx < max(self._dlc, byte_idx + 1):
                    self._cell_signals.setdefault(cell, []).append(row.name)
                    self._cell_raw_signals.setdefault(cell, []).append(row.signal)
        # DLC may be 0 for an empty/placeholder message; still show something.
        self._dlc = max(self._dlc, max((c[0] + 1 for c in self._cell_signals), default=0))
        self.updateGeometry()
        self.update()

    def set_selected_signal(self, name: str | None):
        self._selected_signal = name
        self.update()

    def overlap_count(self) -> int:
        return sum(
            1
            for cell, names in self._cell_signals.items()
            if len(names) > 1 and not _is_safe_multiplex_group(self._cell_raw_signals[cell])
        )

    def sizeHint(self) -> QSize:
        rows = max(self._dlc, 1)
        return QSize(LABEL_COL_W + 8 * CELL_W + 1, LABEL_ROW_H + rows * CELL_H + 1)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _cell_rect(self, byte_idx: int, weight: int) -> QRect:
        col = 7 - weight  # bit 7 (MSB) drawn leftmost
        x = LABEL_COL_W + col * CELL_W
        y = LABEL_ROW_H + byte_idx * CELL_H
        return QRect(x, y, CELL_W, CELL_H)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        rows = max(self._dlc, 1)

        # Column headers (bit weights, MSB-first).
        painter.setPen(QPen(Qt.black))
        for col in range(8):
            weight = 7 - col
            rect = QRect(LABEL_COL_W + col * CELL_W, 0, CELL_W, LABEL_ROW_H)
            painter.drawText(rect, Qt.AlignCenter, f"b{weight}")

        # Row headers (byte index) + grid cells.
        for byte_idx in range(rows):
            header_rect = QRect(0, LABEL_ROW_H + byte_idx * CELL_H, LABEL_COL_W, CELL_H)
            painter.drawText(header_rect, Qt.AlignCenter, f"Byte {byte_idx}")

            for weight in range(8):
                rect = self._cell_rect(byte_idx, weight)
                cell = (byte_idx, weight)
                names = self._cell_signals.get(cell, [])
                raw_signals = self._cell_raw_signals.get(cell, [])
                self._paint_cell(painter, rect, names, raw_signals)

        painter.setPen(QPen(GRID_LINE))
        for col in range(9):
            x = LABEL_COL_W + col * CELL_W
            painter.drawLine(x, LABEL_ROW_H, x, LABEL_ROW_H + rows * CELL_H)
        for row in range(rows + 1):
            y = LABEL_ROW_H + row * CELL_H
            painter.drawLine(LABEL_COL_W, y, LABEL_COL_W + 8 * CELL_W, y)

    def _paint_cell(self, painter: QPainter, rect: QRect, names: list[str], raw_signals: list):
        if not names:
            painter.fillRect(rect, GAP_COLOR)
            return

        if len(names) > 1 and not _is_safe_multiplex_group(raw_signals):
            painter.fillRect(rect, OVERLAP_COLOR)
            painter.setPen(QPen(Qt.white))
            painter.drawText(rect, Qt.AlignCenter, "!")
            return

        # Either a single signal, or a set of mutually-exclusive multiplexed
        # alternatives that legitimately share these bits.
        color = self._signal_colors.get(names[0], QColor("gray"))
        painter.fillRect(rect, color)
        if len(names) > 1:
            painter.fillRect(rect, QBrush(MUX_HATCH_COLOR, Qt.BDiagPattern))
        if self._selected_signal and self._selected_signal in names:
            pen = QPen(Qt.black)
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
        painter.setPen(QPen(Qt.black))
        if len(names) > 1:
            label = "/".join(mux_label(sig) or "?" for sig in raw_signals)
        else:
            label = names[0][:6]
        painter.drawText(rect, Qt.AlignCenter, label)

    def _cell_at(self, pos):
        x, y = pos.x(), pos.y()
        if x < LABEL_COL_W or y < LABEL_ROW_H:
            return None
        col = (x - LABEL_COL_W) // CELL_W
        byte_idx = (y - LABEL_ROW_H) // CELL_H
        if col >= 8 or byte_idx >= max(self._dlc, 1):
            return None
        weight = 7 - col
        return (byte_idx, weight)

    def mouseMoveEvent(self, event):
        cell = self._cell_at(event.position().toPoint())
        if cell is None:
            QToolTip.hideText()
            return
        names = self._cell_signals.get(cell, [])
        raw_signals = self._cell_raw_signals.get(cell, [])
        byte_idx, weight = cell
        if names:
            text = f"Byte {byte_idx}, bit {weight}: " + ", ".join(names)
            if len(names) > 1:
                if _is_safe_multiplex_group(raw_signals):
                    text += " (mutually-exclusive multiplexed alternatives)"
                else:
                    text += " (OVERLAP)"
        else:
            text = f"Byte {byte_idx}, bit {weight}: (unused)"
        QToolTip.showText(event.globalPosition().toPoint(), text, self)

"""Interactive log plot: CANoe-style scaling, cursors, and Y-axis layout.

The matplotlib navigation toolbar is intentionally not used. Its pan/zoom
modes fight the time-range fields, and a selection change used to rebuild
the figure and drop any Y zoom. View limits live here and survive a new
selection.
"""

from __future__ import annotations

import bisect
import csv

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QLabel,
    QLayout,
    QPushButton,
    QRubberBand,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .log_replay_model import (
    enum_ticks,
    format_signal_value,
    marker_for_count,
    numeric_minmax,
    padded_range,
    zoom_about,
)

PALETTE = (
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
    "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC",
)
CURSOR_COLOR = "#1F4B99"
DIFF_COLOR = "#C45C26"
ZOOM_IN = 0.8
ZOOM_OUT = 1.25
_MAX_ENUM_TICKS = 24
_CTRL_HEIGHT = 28
_BTN_MIN_WIDTH = 64


def _sid(series) -> str:
    label = getattr(series, "dbc_label", "") or ""
    return f"{label}\0{series.message_name}\0{series.signal_name}"


class FlowLayout(QLayout):
    """Left-to-right layout that wraps, so the toolbar fits a narrow window."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self._spacing = spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _do_layout(self, rect, test_only) -> int:
        margins = self.contentsMargins()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        right = rect.right() - margins.right()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            minimum = item.minimumSize()
            width = max(minimum.width(), hint.width())
            widget = item.widget()
            flexible = False
            if widget is not None:
                policy = widget.sizePolicy().horizontalPolicy()
                flexible = policy in (
                    QSizePolicy.Ignored,
                    QSizePolicy.Expanding,
                    QSizePolicy.MinimumExpanding,
                )
            # right() is inclusive, so an item that ends on that pixel still fits.
            if x + width > right + 1 and line_height > 0:
                x = rect.x() + margins.left()
                y += line_height + self._spacing
                line_height = 0
            if flexible:
                width = max(minimum.width(), min(width, max(minimum.width(), right - x + 1)))
            height = max(minimum.height(), hint.height())
            if widget is not None and width > 0 and widget.hasHeightForWidth():
                height = max(minimum.height(), widget.heightForWidth(width))
            if not test_only:
                item.setGeometry(QRect(x, y, width, height))
            x += width + self._spacing
            line_height = max(line_height, height)
        return y + line_height - rect.y() + margins.bottom()


class FlowHost(QWidget):
    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self.flow = FlowLayout(self, spacing=spacing)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.flow.heightForWidth(width)

    def sizeHint(self):
        return QSize(480, self.flow.heightForWidth(480))


class ToolGroup(QFrame):
    """Compact framed toolbar section: a short title over a wrapping row of controls.

    The group is one item in the outer flow, so a narrow window moves the whole
    section to the next line. If the section itself is wider than the window,
    its own flow wraps controls instead of cutting one in half.
    """

    _MARGIN = (8, 4, 8, 6)  # left, top, right, bottom
    _GAP = 2

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.group_title = title
        self.setObjectName(f"plotToolGroup{title}")
        self.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Preferred)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("plotToolGroupTitle")
        self.title_label.setStyleSheet(
            "color: #666; font-size: 11px; font-weight: 600; background: transparent;"
        )
        self.body = QWidget(self)
        self.body.setObjectName("plotToolGroupBody")
        self.flow = FlowLayout(self.body, spacing=4)
        self._recompute_minimum_width()

    def addWidget(self, widget):
        self.flow.addWidget(widget)
        self._recompute_minimum_width()
        return widget

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor("#dddddd")))
        painter.setBrush(QColor("#fafafa"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        left, top, right, bottom = self._MARGIN
        inner = max(1, width - left - right)
        return top + self._title_height() + self._GAP + self.flow.heightForWidth(inner) + bottom

    def sizeHint(self):
        width = self._preferred_width()
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):
        return QSize(self._minimum_width(), self.heightForWidth(max(self._preferred_width(), 1)))

    def _title_height(self) -> int:
        return max(self.title_label.sizeHint().height(), 14)

    def _preferred_width(self) -> int:
        left, _top, right, _bottom = self._MARGIN
        spacing = self.flow._spacing
        total = 0
        count = self.flow.count()
        for index in range(count):
            item = self.flow.itemAt(index)
            hint = item.sizeHint()
            total += max(item.minimumSize().width(), hint.width())
        if count > 1:
            total += spacing * (count - 1)
        total = max(total, self.title_label.sizeHint().width())
        return total + left + right

    def _minimum_width(self) -> int:
        left, _top, right, _bottom = self._MARGIN
        widest = self.title_label.sizeHint().width()
        for index in range(self.flow.count()):
            widest = max(widest, self.flow.itemAt(index).minimumSize().width())
        return widest + left + right

    def _recompute_minimum_width(self):
        self.setMinimumWidth(self._minimum_width())

    def _place(self):
        left, top, right, bottom = self._MARGIN
        inner_w = max(1, self.width() - left - right)
        title_h = self._title_height()
        self.title_label.setGeometry(left, top, inner_w, title_h)
        body_y = top + title_h + self._GAP
        body_h = max(1, self.height() - body_y - bottom)
        self.body.setGeometry(left, body_y, inner_w, body_h)


class ElidingLabel(QLabel):
    """Single-line label that middle-elides and keeps the full text as a tooltip."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text: str):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        width = self.contentsRect().width()
        if width < 8:
            self.setText(self._full)
            return
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, width))


class SignalPlot(QWidget):
    """Plot of selected log signals with fit, zoom, pan, and measurement cursors."""

    view_changed = Signal(float, float)
    cursors_changed = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series = []
        self._current = None
        self._t0 = 0.0
        self._log_xmin = 0.0
        self._log_xmax = 1.0
        self._xmin = None
        self._xmax = None
        self._ylims: dict[str, tuple[float, float]] = {}
        self._colors: dict[str, str] = {}
        self._axes_by_id: dict[str, object] = {}
        self._lines = []  # (line, rel_times, values, series, ax)
        self._cursor_lines = []
        self._diff_lines = []
        self._c1 = None
        self._c2 = None
        self._undo = []
        self._redo = []
        self._wheel_undo_open = False
        self._drag = None
        self._updating_spins = False
        self._scale_mode = "x"
        self._y_mode = "stacked"
        self._grid_on = True
        self._samples_on = False
        self._highlight_only = False
        self._cursor_on = False
        self._diff_on = False
        self._snap_on = True
        self._wheel_on = True

        self._build_ui()
        self._wheel_timer = QTimer(self)
        self._wheel_timer.setSingleShot(True)
        self._wheel_timer.setInterval(400)
        self._wheel_timer.timeout.connect(self._end_wheel_undo)

        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self._sync_undo_buttons()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        self._toolbar = FlowHost(spacing=10)
        flow = self._toolbar.flow

        view = self._group("View")
        self._fit_all_btn = view.addWidget(self._button("Fit all", "Fit time and every Y axis to the data", self.fit_all))
        self._fit_x_btn = view.addWidget(self._button("Fit X", "Show the whole log on the time axis", self.fit_x))
        self._fit_y_btn = view.addWidget(self._button("Fit Y", "Fit Y axes to samples inside the current time window", self.fit_y))
        self._zoom_in_btn = view.addWidget(self._button("Zoom +", "Zoom in on the axes selected next to Wheel", lambda: self.zoom_step(ZOOM_IN)))
        self._zoom_out_btn = view.addWidget(self._button("Zoom −", "Zoom out on the axes selected next to Wheel", lambda: self.zoom_step(ZOOM_OUT)))
        self._start_btn = view.addWidget(self._button("Start", "Scroll to the start of the log without changing the window width", lambda: self.scroll_to_edge(True)))
        self._end_btn = view.addWidget(self._button("End", "Scroll to the end of the log without changing the window width", lambda: self.scroll_to_edge(False)))
        self._undo_btn = view.addWidget(self._button("Undo", "Undo the last zoom or pan", self.undo))
        self._redo_btn = view.addWidget(self._button("Redo", "Redo the last undone zoom or pan", self.redo))
        self._scale_combo = QComboBox()
        self._scale_combo.addItem("Wheel: X", "x")
        self._scale_combo.addItem("Wheel: Y", "y")
        self._scale_combo.addItem("Wheel: X+Y", "both")
        self._scale_combo.setToolTip("Which axes the mouse wheel and Zoom buttons change")
        self._scale_combo.setMinimumWidth(118)
        self._match_control(self._scale_combo)
        self._scale_combo.currentIndexChanged.connect(self._on_scale_mode)
        view.addWidget(self._scale_combo)
        self._wheel_btn = view.addWidget(self._button("Wheel", "Zoom toward the cursor with the mouse wheel", self._toggle_wheel, checkable=True))
        self._wheel_btn.setChecked(True)
        flow.addWidget(view)

        layout_group = self._group("Layout")
        self._y_combo = QComboBox()
        self._y_combo.addItem("Stacked plots", "stacked")
        self._y_combo.addItem("Y of selected", "selected")
        self._y_combo.addItem("Separate Y axes", "separate_y")
        self._y_combo.setToolTip(
            "Stacked: one diagram per signal, shared time. "
            "Y of selected: one diagram using the current signal's scale. "
            "Separate Y axes: one diagram, one scale per signal."
        )
        self._y_combo.setMinimumWidth(140)
        self._match_control(self._y_combo)
        self._y_combo.currentIndexChanged.connect(self._on_y_mode)
        layout_group.addWidget(self._y_combo)
        self._only_btn = layout_group.addWidget(self._button("Current", "Plot only the current signal", self._toggle_only, checkable=True))
        self._grid_btn = layout_group.addWidget(self._button("Grid", "Show or hide the grid", self._toggle_grid, checkable=True))
        self._grid_btn.setChecked(True)
        self._samples_btn = layout_group.addWidget(self._button(
            "Samples",
            "Always draw a marker on every sample. Off: markers appear only when zoomed in.",
            self._toggle_samples,
            checkable=True,
        ))
        flow.addWidget(layout_group)

        measure = self._group("Measure")
        self._cursor_btn = measure.addWidget(self._button(
            "Cursor",
            "Show a measurement line. Drag the line to move it, or click the plot to place it.",
            self._toggle_cursor,
            checkable=True,
        ))
        self._diff_btn = measure.addWidget(self._button(
            "Δ",
            "Second measurement line. Shift-click to place it, then drag either line. The table shows Δ value.",
            self._toggle_diff,
            checkable=True,
        ))
        self._snap_btn = measure.addWidget(self._button("Snap", "Cursors land on the nearest sample when you release them", self._toggle_snap, checkable=True))
        self._snap_btn.setChecked(True)
        measure.addWidget(self._caption("From"))
        self.range_from = self._time_spin()
        self.range_to = self._time_spin()
        self.range_from.valueChanged.connect(self._on_range_edited)
        self.range_to.valueChanged.connect(self._on_range_edited)
        measure.addWidget(self.range_from)
        measure.addWidget(self._caption("to"))
        measure.addWidget(self.range_to)
        measure.addWidget(self._caption("s"))
        flow.addWidget(measure)

        export = self._group("Export")
        export.addWidget(self._button("PNG", "Save the plot as a PNG", self.export_png))
        export.addWidget(self._button("CSV", "Save plotted samples in the cursor span, or the visible time window", self.export_csv))
        flow.addWidget(export)
        root.addWidget(self._toolbar)

        self.readout = QLabel("")
        self.readout.setWordWrap(True)
        self.readout.setMinimumHeight(18)
        root.addWidget(self.readout)

        self.figure = Figure(facecolor="white")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.canvas.setMinimumHeight(180)
        self.canvas.setContextMenuPolicy(Qt.NoContextMenu)
        self.canvas.setToolTip(
            "Left-drag draws a zoom box. Right-drag pans. "
            "Drag a measurement line to move it. Shift-click places the difference cursor."
        )
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self.canvas)
        root.addWidget(self.canvas, 1)

    def _button(self, text, tip, slot, checkable=False) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.setFocusPolicy(Qt.NoFocus)
        button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        button.setMinimumWidth(_BTN_MIN_WIDTH)
        button.setFixedHeight(_CTRL_HEIGHT)
        button.setStyleSheet(
            "QPushButton { padding: 0 10px; border: 1px solid #ccc; border-radius: 3px;"
            " background: #fff; color: #222; }"
            "QPushButton:hover { background: #f2f2f2; }"
            "QPushButton:checked { background: #d6e4f5; border: 1px solid #1F4B99; color: #1a1a1a; }"
            "QPushButton:disabled { color: #8a8a8a; background: #f4f4f4; }"
        )
        button.clicked.connect(slot)
        return button

    def _group(self, text: str) -> ToolGroup:
        return ToolGroup(text)

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setFixedHeight(_CTRL_HEIGHT)
        label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        label.setStyleSheet("color: #333; background: transparent;")
        return label

    def _match_control(self, widget):
        widget.setFixedHeight(_CTRL_HEIGHT)
        widget.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

    def _time_spin(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0, 1_000_000)
        spin.setDecimals(3)
        spin.setSingleStep(0.1)
        spin.setMaximumWidth(108)
        spin.setKeyboardTracking(False)
        self._match_control(spin)
        return spin

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = max(1, self._toolbar.width())
        height = self._toolbar.heightForWidth(width)
        if height > 0 and self._toolbar.height() != height:
            self._toolbar.setFixedHeight(height)

    # -------------------------------------------------------------- series

    def set_series(self, series, current, t0: float, log_xmax: float, *, reset_view: bool = False):
        """Show `series` (already the user's selection). `current` is drawn in front.

        X limits are kept across selection changes unless `reset_view` is set
        (a newly opened log). Y limits are kept per signal and created for
        signals that do not have one yet.
        """
        self._series = [item for item in series if item.times]
        self._current = current if current in self._series else (self._series[0] if self._series else None)
        self._t0 = t0
        self._log_xmin = 0.0
        self._log_xmax = max(float(log_xmax), 0.0)
        self._assign_colors()
        if reset_view or self._xmin is None:
            self._undo.clear()
            self._redo.clear()
            self._ylims.clear()
            self._xmin, self._xmax = padded_range(self._log_xmin, self._log_xmax, 0.01)
            self._sync_undo_buttons()
        self._autoscale_missing()
        self._rebuild()

    def color_for(self, message: str, signal: str, dbc: str = ""):
        return self._colors.get(f"{dbc}\0{message}\0{signal}")

    def limits(self) -> tuple[float, float]:
        if self._xmin is None or self._xmax is None:
            return 0.0, 0.0
        return float(self._xmin), float(self._xmax)

    def cursors(self):
        first = self._c1 if self._cursor_on else None
        second = self._c2 if self._cursor_on and self._diff_on else None
        return first, second

    def export_window(self) -> tuple[float, float]:
        """Cursor span when the difference cursor is on, otherwise the visible time window."""
        first, second = self.cursors()
        if first is not None and second is not None:
            return (min(first, second), max(first, second))
        return self.limits()

    # -------------------------------------------------------------- view

    def fit_all(self):
        if not self._series:
            return
        self._push_undo()
        self._xmin, self._xmax = padded_range(self._log_xmin, self._log_xmax, 0.01)
        self._fit_y_limits(self._xmin, self._xmax)
        self._apply_limits()

    def fit_x(self):
        if not self._series:
            return
        self._push_undo()
        self._xmin, self._xmax = padded_range(self._log_xmin, self._log_xmax, 0.01)
        self._apply_limits()

    def fit_y(self):
        if not self._series or self._xmin is None:
            return
        self._push_undo()
        self._fit_y_limits(self._xmin, self._xmax)
        self._apply_limits()

    def zoom_step(self, factor: float):
        if self._xmin is None:
            return
        self._push_undo()
        self._scale_limits(factor, (self._xmin + self._xmax) / 2, None, all_y=True)
        self._apply_limits()

    def scroll_to_edge(self, to_start: bool):
        if self._xmin is None:
            return
        self._push_undo()
        width = max(self._xmax - self._xmin, 1e-6)
        span = max(self._log_xmax - self._log_xmin, 0.0)
        if width >= span:
            self._xmin, self._xmax = padded_range(self._log_xmin, self._log_xmax, 0.01)
        elif to_start:
            self._xmin = self._log_xmin
            self._xmax = self._log_xmin + width
        else:
            self._xmax = self._log_xmax
            self._xmin = self._log_xmax - width
        self._apply_limits()

    def undo(self):
        if not self._undo:
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())

    def redo(self):
        if not self._redo:
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())

    def export_png(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export plot", "", "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        self.figure.savefig(path, dpi=120, bbox_inches="tight", facecolor="white")

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export samples", "", "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        t_min, t_max = self.export_window()
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s", "message", "signal", "value"])
            for series in self._series:
                for timestamp, value in zip(series.times, series.values):
                    rel = timestamp - self._t0
                    if t_min <= rel <= t_max:
                        writer.writerow([
                            f"{rel:.6f}",
                            series.message_name,
                            series.signal_name,
                            format_signal_value(value, series.choices),
                        ])

    # -------------------------------------------------------------- internals

    def _visible_series(self):
        if self._highlight_only and self._current is not None:
            return [self._current]
        return list(self._series)

    def _assign_colors(self):
        for series in self._series:
            sid = _sid(series)
            if sid not in self._colors:
                self._colors[sid] = PALETTE[len(self._colors) % len(PALETTE)]

    def _autoscale_missing(self):
        if self._xmin is None:
            return
        for series in self._visible_series():
            if _sid(series) not in self._ylims:
                span = self._span(series, self._xmin, self._xmax) or self._span(series, self._log_xmin, self._log_xmax)
                if span is not None:
                    self._ylims[_sid(series)] = padded_range(*span)

    def _fit_y_limits(self, t_min, t_max):
        shown = self._visible_series()
        if self._y_mode == "selected":
            target = self._current if self._current in shown else (shown[0] if shown else None)
            if target is None:
                return
            span = self._span(target, t_min, t_max)
            if span is None:
                return
            padded = padded_range(*span)
            for series in shown:
                self._ylims[_sid(series)] = padded
            return
        for series in shown:
            span = self._span(series, t_min, t_max)
            if span is not None:
                self._ylims[_sid(series)] = padded_range(*span)

    def _span(self, series, t_min, t_max):
        return numeric_minmax(series.times, series.values, t_min + self._t0, t_max + self._t0)

    def _rel_times(self, series):
        return [timestamp - self._t0 for timestamp in series.times]

    def _rebuild(self):
        self.figure.clear()
        self._axes_by_id = {}
        self._lines = []
        self._cursor_lines = []
        self._diff_lines = []
        shown = self._visible_series()
        if not shown:
            self.canvas.draw_idle()
            self._update_readout("Select signals to plot" if not self._series else "")
            self.view_changed.emit(*self.limits())
            self.cursors_changed.emit(*self.cursors())
            return
        if self._y_mode == "stacked":
            self._build_stacked(shown)
        elif self._y_mode == "separate_y":
            self._build_separate(shown)
        else:
            self._build_shared(shown)
        self._apply_limits()
        self._draw_cursor_lines()
        self.cursors_changed.emit(*self.cursors())

    def _build_stacked(self, shown):
        grid = self.figure.subplots(len(shown), 1, sharex=True, squeeze=False)
        self.figure.subplots_adjust(left=0.1, right=0.98, top=0.90, bottom=0.1, hspace=0.35)
        for row, series in enumerate(shown):
            ax = grid[row, 0]
            self._plot_on(ax, series, ylabel=True)
            if row == len(shown) - 1:
                ax.set_xlabel("Time (s)")

    def _build_shared(self, shown):
        ax = self.figure.add_subplot(111)
        self.figure.subplots_adjust(left=0.1, right=0.98, top=0.90, bottom=0.1)
        for series in shown:
            self._plot_on(ax, series, ylabel=False)
        ax.set_xlabel("Time (s)")
        if len(shown) <= 6:
            ax.legend(loc="upper left", fontsize=8, framealpha=0.85)
        current = self._current if self._current in shown else shown[0]
        self._apply_enum_ticks(ax, current)

    def _build_separate(self, shown):
        host = self.figure.add_subplot(111)
        extra = max(0, len(shown) - 1)
        right = max(0.55, 0.97 - 0.08 * extra)
        self.figure.subplots_adjust(left=0.1, right=right, top=0.90, bottom=0.1)
        host.set_zorder(0)
        for index, series in enumerate(shown):
            ax = host if index == 0 else host.twinx()
            if index > 1:
                ax.spines["right"].set_position(("outward", 52 * (index - 1)))
            if index > 0:
                ax.set_zorder(index)
                ax.patch.set_visible(False)
            self._plot_on(ax, series, ylabel=True)
        host.set_xlabel("Time (s)")

    def _plot_on(self, ax, series, ylabel: bool):
        sid = _sid(series)
        color = self._colors[sid]
        rel = self._rel_times(series)
        current = series is self._current or len(self._visible_series()) == 1
        line, = ax.plot(
            rel,
            series.values,
            color=color,
            linewidth=2.2 if current else 1.15,
            alpha=1.0 if current else 0.35,
            zorder=3 if current else 2,
            drawstyle="steps-post" if series.choices else "default",
            label=series.display_label,
        )
        self._lines.append((line, rel, series.values, series, ax))
        self._axes_by_id[sid] = ax
        ax.grid(self._grid_on, alpha=0.4)
        if ylabel:
            ax.tick_params(axis="y", labelcolor=color)
            ax.set_ylabel(series.display_label, color=color, fontsize=8)
            self._apply_enum_ticks(ax, series)

    def _apply_enum_ticks(self, ax, series):
        positions, labels = enum_ticks(series.choices)
        if not positions or len(positions) > _MAX_ENUM_TICKS:
            return
        ax.set_yticks(positions)
        ax.set_yticklabels(labels)

    def _apply_limits(self):
        axes = self._unique_axes()
        if not axes or self._xmin is None:
            self._write_spins()
            self.canvas.draw_idle()
            self.view_changed.emit(*self.limits())
            return
        for ax in axes:
            ax.set_xlim(self._xmin, self._xmax)
        if self._y_mode == "selected":
            ylim = None
            if self._current is not None:
                ylim = self._ylims.get(_sid(self._current))
            if ylim is None and self._axes_by_id:
                ylim = next(iter(self._ylims.values()), None)
            if ylim is not None:
                axes[0].set_ylim(ylim)
        else:
            seen = set()
            for sid, ax in self._axes_by_id.items():
                if id(ax) in seen:
                    continue
                ylim = self._ylims.get(sid)
                if ylim is not None:
                    ax.set_ylim(ylim)
                    seen.add(id(ax))
        self._style_markers()
        self._write_spins()
        self._update_readout()
        self.canvas.draw_idle()
        self.view_changed.emit(*self.limits())

    def _style_markers(self):
        for line, rel, _values, _series, _ax in self._lines:
            if self._samples_on:
                line.set_marker(".")
                line.set_markersize(3.5)
                continue
            if self._xmin is None:
                visible = len(rel)
            else:
                i0 = bisect.bisect_left(rel, self._xmin)
                i1 = bisect.bisect_right(rel, self._xmax)
                visible = i1 - i0
            marker = marker_for_count(visible)
            line.set_marker(marker if marker else "None")
            line.set_markersize(3.5)

    def _unique_axes(self):
        seen = []
        for ax in self._axes_by_id.values():
            if ax not in seen:
                seen.append(ax)
        return seen

    def _write_spins(self):
        if self._xmin is None:
            return
        self._updating_spins = True
        self.range_from.setValue(max(0.0, self._xmin))
        self.range_to.setValue(max(0.0, self._xmax))
        self._updating_spins = False

    def _on_range_edited(self):
        if self._updating_spins or self._xmin is None:
            return
        start = self.range_from.value()
        end = self.range_to.value()
        if end < start:
            start, end = end, start
        if end == start:
            end = start + 1e-3
        self._push_undo()
        self._xmin, self._xmax = start, end
        self._apply_limits()

    def _scale_limits(self, factor, x_center, y_center, all_y: bool, y_axes=None):
        mode = self._scale_mode
        if mode in ("x", "both") and x_center is not None:
            self._xmin, self._xmax = zoom_about(self._xmin, self._xmax, x_center, factor)
        if mode in ("y", "both"):
            targets = self._unique_axes() if all_y else (y_axes or [])
            for ax in targets:
                matched = [sid for sid, candidate in self._axes_by_id.items() if candidate is ax]
                if not matched:
                    continue
                y0, y1 = self._ylims.get(matched[0], ax.get_ylim())
                center = y_center if y_center is not None and not all_y else (y0 + y1) / 2
                updated = zoom_about(y0, y1, center, factor)
                for sid in matched:
                    self._ylims[sid] = updated

    def _store_limits_from_axes(self):
        axes = self._unique_axes()
        if not axes:
            return
        self._xmin, self._xmax = axes[0].get_xlim()
        if self._y_mode == "selected":
            ylim = axes[0].get_ylim()
            for sid in self._axes_by_id:
                self._ylims[sid] = ylim
        else:
            for sid, ax in self._axes_by_id.items():
                self._ylims[sid] = ax.get_ylim()

    # -------------------------------------------------------------- cursors

    def _draw_cursor_lines(self):
        for line in self._cursor_lines + self._diff_lines:
            line.remove()
        self._cursor_lines = []
        self._diff_lines = []
        if not self._cursor_on:
            self._update_readout()
            self.canvas.draw_idle()
            return
        axes = self._unique_axes()
        for index, ax in enumerate(axes):
            top = index == 0
            if self._c1 is not None:
                self._cursor_lines.extend(self._cursor_artists(ax, self._c1, CURSOR_COLOR, top))
            if self._diff_on and self._c2 is not None:
                self._diff_lines.extend(self._cursor_artists(ax, self._c2, DIFF_COLOR, top))
        self._update_readout()
        self.canvas.draw_idle()

    def _place_cursor(self, which: str, t: float, snap: bool = False):
        if snap:
            snapped = self._snap_time(t)
            if snapped is not None:
                t = snapped
        if which == "c2":
            self._c2 = t
        else:
            self._c1 = t
        self._draw_cursor_lines()
        self.cursors_changed.emit(*self.cursors())

    def _snap_time(self, t: float):
        best = None
        best_dist = None
        for series in self._visible_series():
            if not series.times:
                continue
            index = bisect.bisect_left(series.times, t + self._t0)
            candidates = []
            if index < len(series.times):
                candidates.append(series.times[index] - self._t0)
            if index > 0:
                candidates.append(series.times[index - 1] - self._t0)
            for rel in candidates:
                dist = abs(rel - t)
                if best_dist is None or dist < best_dist:
                    best = rel
                    best_dist = dist
        return best

    def _cursor_artists(self, ax, t: float, color: str, top: bool):
        artists = [ax.axvline(t, color=color, lw=2.0, ls="--", zorder=5)]
        if top:
            marker, = ax.plot(
                [t],
                [1.02],
                transform=ax.get_xaxis_transform(),
                marker="v",
                color=color,
                markersize=9,
                clip_on=False,
                zorder=6,
            )
            label = ax.text(
                t,
                1.02,
                f"  {t:.3f} s",
                transform=ax.get_xaxis_transform(),
                color=color,
                fontsize=8,
                ha="left",
                va="bottom",
                clip_on=False,
                zorder=6,
            )
            artists.extend((marker, label))
        return artists

    def _cursor_hit(self, event):
        if event.inaxes is None or not self._cursor_on or event.x is None:
            return None
        best = None
        best_dist = 18
        for name, lines in (("c1", self._cursor_lines), ("c2", self._diff_lines)):
            for artist in lines:
                xs = artist.get_xdata() if hasattr(artist, "get_xdata") else None
                if xs is None or len(xs) == 0:
                    position = getattr(artist, "get_position", None)
                    if position is None:
                        continue
                    xs = [position()[0]]
                px, _py = event.inaxes.transData.transform((xs[0], 0))
                dist = abs(px - event.x)
                if dist <= best_dist:
                    best = name
                    best_dist = dist
        return best

    # -------------------------------------------------------------- mouse

    def _on_scroll(self, event):
        if not self._wheel_on or event.inaxes is None or event.xdata is None or self._xmin is None:
            return
        if event.button not in ("up", "down"):
            return
        self._begin_wheel_undo()
        factor = ZOOM_IN if event.button == "up" else ZOOM_OUT
        y_axes = [event.inaxes] if self._y_mode != "selected" else self._unique_axes()[:1]
        self._scale_limits(factor, event.xdata, event.ydata, all_y=False, y_axes=y_axes)
        self._apply_limits()

    def _on_press(self, event):
        if event.inaxes is None or self._xmin is None or event.xdata is None:
            return
        if event.button == 1:
            hit = self._cursor_hit(event)
            if hit is not None:
                self._drag = {"kind": hit, "moved": False}
                self.canvas.setCursor(Qt.SizeHorCursor)
                return
            origin = self._qt_pos(event)
            self._drag = {
                "kind": "zoom",
                "both": True,
                "ax": event.inaxes,
                "x0": event.xdata,
                "y0": event.ydata,
                "x1": event.xdata,
                "y1": event.ydata,
                "origin": origin,
                "x": event.x,
                "y": event.y,
                "xdata": event.xdata,
                "moved": False,
                "undo": False,
                "shift": bool(event.key and "shift" in str(event.key)),
            }
            self._rubber.setGeometry(QRect(origin, QSize()))
            self._rubber.show()
            return
        if event.button not in (3, "3"):
            return
        self._drag = {
            "kind": "pan",
            "x": event.x,
            "y": event.y,
            "xdata": event.xdata,
            "moved": False,
            "undo": False,
            "limits": self._axis_snapshots(),
            "shift": bool(event.key and "shift" in str(event.key)),
        }
        self.canvas.setCursor(Qt.ClosedHandCursor)

    def _on_motion(self, event):
        if self._drag is not None and self._drag["kind"] == "pan":
            self._drag_pan(event)
            return
        if self._drag is not None and self._drag["kind"] == "zoom":
            current = self._qt_pos(event)
            self._rubber.setGeometry(QRect(self._drag["origin"], current).normalized())
            if None not in (event.x, event.y) and abs(event.x - self._drag["x"]) + abs(event.y - self._drag["y"]) > 4:
                self._drag["moved"] = True
                if not self._drag["undo"]:
                    self._push_undo()
                    self._drag["undo"] = True
            if event.xdata is not None:
                self._drag["x1"] = event.xdata
            if event.ydata is not None:
                self._drag["y1"] = event.ydata
            return
        if self._drag is not None and self._drag["kind"] in ("c1", "c2") and event.xdata is not None:
            self._drag["moved"] = True
            self._place_cursor(self._drag["kind"], event.xdata, snap=False)
            return
        self.canvas.setCursor(Qt.SizeHorCursor if self._cursor_hit(event) else Qt.ArrowCursor)
        self._hover(event)

    def _on_release(self, _event):
        drag = self._drag
        self._drag = None
        self._rubber.hide()
        self.canvas.setCursor(Qt.ArrowCursor)
        if drag is None:
            return
        if drag["kind"] == "pan":
            if drag["moved"]:
                self._store_limits_from_axes()
                self._apply_limits()
            return
        if drag["kind"] == "zoom":
            if not drag["moved"] or drag.get("x1") is None:
                if self._cursor_on and drag.get("xdata") is not None:
                    which = "c2" if (self._diff_on and drag.get("shift")) else "c1"
                    self._place_cursor(which, drag["xdata"], snap=self._snap_on)
                return
            self._finish_drag_zoom(drag)
            return
        if drag["kind"] in ("c1", "c2"):
            current = self._c1 if drag["kind"] == "c1" else self._c2
            if current is not None:
                self._place_cursor(drag["kind"], current, snap=self._snap_on)

    def _drag_pan(self, event):
        if event.x is None or event.y is None:
            return
        dx = event.x - self._drag["x"]
        dy = event.y - self._drag["y"]
        if abs(dx) + abs(dy) <= 6:
            return
        self._drag["moved"] = True
        if not self._drag["undo"]:
            self._push_undo()
            self._drag["undo"] = True
        for ax, xlim, ylim in self._drag["limits"]:
            x0, y0 = ax.transData.inverted().transform((0, 0))
            x1, y1 = ax.transData.inverted().transform((dx, dy))
            ax.set_xlim(xlim[0] - (x1 - x0), xlim[1] - (x1 - x0))
            ax.set_ylim(ylim[0] - (y1 - y0), ylim[1] - (y1 - y0))
        self._store_limits_from_axes()
        self._style_markers()
        self._write_spins()
        self.canvas.draw_idle()

    def _finish_drag_zoom(self, drag):
        x0, x1 = drag["x0"], drag["x1"]
        y0, y1 = drag["y0"], drag["y1"]
        mode = "both" if drag.get("both") else self._scale_mode
        changed = False
        if mode in ("x", "both") and None not in (x0, x1) and abs(x1 - x0) > 1e-9:
            self._xmin, self._xmax = (min(x0, x1), max(x0, x1))
            changed = True
        if mode in ("y", "both") and None not in (y0, y1) and abs(y1 - y0) > 1e-12:
            updated = (min(y0, y1), max(y0, y1))
            if self._y_mode == "selected":
                for sid in self._axes_by_id:
                    self._ylims[sid] = updated
            else:
                for sid, ax in self._axes_by_id.items():
                    if ax is drag["ax"]:
                        self._ylims[sid] = updated
            changed = True
        if not changed:
            self._pop_empty_undo()
            return
        self._apply_limits()

    def _axis_snapshots(self):
        seen = set()
        snapshots = []
        for ax in self._unique_axes():
            if id(ax) in seen:
                continue
            seen.add(id(ax))
            snapshots.append((ax, ax.get_xlim(), ax.get_ylim()))
        return snapshots

    def _hover(self, event):
        if event.inaxes is None or event.xdata is None:
            self._update_readout()
            return
        best = None
        best_dist = 14 * 14
        for _line, rel, values, series, ax in self._lines:
            if self._y_mode == "stacked" and ax is not event.inaxes:
                continue
            index = bisect.bisect_left(rel, event.xdata)
            candidates = []
            if index < len(rel):
                candidates.append(index)
            if index > 0:
                candidates.append(index - 1)
            for idx in candidates:
                value = values[idx]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                px, py = ax.transData.transform((rel[idx], value))
                dist = (px - event.x) ** 2 + (py - event.y) ** 2
                if dist <= best_dist:
                    best = (series, rel[idx], value)
                    best_dist = dist
        if best is None:
            self._update_readout()
            return
        series, rel, value = best
        text = f"{series.display_label} = {format_signal_value(value, series.choices)}  @ {rel:.3f} s"
        self._update_readout(text)

    def _qt_pos(self, event) -> QPoint:
        height = self.canvas.height()
        return QPoint(int(event.x), int(height - event.y))

    def _update_readout(self, hover: str = ""):
        parts = []
        if self._cursor_on and self._c1 is not None:
            parts.append(f"Cursor {self._c1:.3f} s")
        if self._cursor_on and self._diff_on and self._c1 is not None and self._c2 is not None:
            parts.append(f"Δt {abs(self._c2 - self._c1):.3f} s")
        if hover:
            parts.append(hover)
        self.readout.setText("    ".join(parts))

    # -------------------------------------------------------------- toggles

    def _on_scale_mode(self):
        self._scale_mode = self._scale_combo.currentData()

    def _on_y_mode(self):
        self._y_mode = self._y_combo.currentData()
        if self._xmin is not None:
            self._push_undo()
            self._fit_y_limits(self._xmin, self._xmax)
        self._rebuild()

    def _toggle_wheel(self):
        self._wheel_on = self._wheel_btn.isChecked()

    def _toggle_cursor(self):
        self._cursor_on = self._cursor_btn.isChecked()
        if self._cursor_on and self._c1 is None and self._xmin is not None:
            self._c1 = (self._xmin + self._xmax) / 2
            if self._snap_on:
                snapped = self._snap_time(self._c1)
                if snapped is not None:
                    self._c1 = snapped
        self._draw_cursor_lines()
        self.cursors_changed.emit(*self.cursors())

    def _toggle_diff(self):
        self._diff_on = self._diff_btn.isChecked()
        if self._diff_on and not self._cursor_on:
            self._cursor_btn.setChecked(True)
            self._cursor_on = True
        if self._diff_on and self._c2 is None and self._xmin is not None:
            self._c2 = self._xmin + (self._xmax - self._xmin) * 0.75
            if self._snap_on:
                snapped = self._snap_time(self._c2)
                if snapped is not None:
                    self._c2 = snapped
        self._draw_cursor_lines()
        self.cursors_changed.emit(*self.cursors())

    def _toggle_snap(self):
        self._snap_on = self._snap_btn.isChecked()

    def _toggle_only(self):
        self._highlight_only = self._only_btn.isChecked()
        self._autoscale_missing()
        self._rebuild()

    def _toggle_grid(self):
        self._grid_on = self._grid_btn.isChecked()
        for ax in self._unique_axes():
            ax.grid(self._grid_on, alpha=0.4)
        self.canvas.draw_idle()

    def _toggle_samples(self):
        self._samples_on = self._samples_btn.isChecked()
        self._style_markers()
        self.canvas.draw_idle()

    # -------------------------------------------------------------- undo

    def _snapshot(self):
        items = tuple(sorted((key, value) for key, value in self._ylims.items()))
        return (self._xmin, self._xmax, items)

    def _push_undo(self):
        if self._xmin is None:
            return
        snap = self._snapshot()
        if self._undo and self._undo[-1] == snap:
            return
        self._undo.append(snap)
        if len(self._undo) > 40:
            del self._undo[0]
        self._redo.clear()
        self._sync_undo_buttons()

    def _pop_empty_undo(self):
        if self._undo:
            self._undo.pop()
            self._sync_undo_buttons()

    def _restore(self, snap):
        self._xmin, self._xmax, items = snap
        self._ylims = {key: value for key, value in items}
        self._apply_limits()
        self._sync_undo_buttons()

    def _sync_undo_buttons(self):
        self._undo_btn.setEnabled(bool(self._undo))
        self._redo_btn.setEnabled(bool(self._redo))

    def _begin_wheel_undo(self):
        if not self._wheel_undo_open:
            self._push_undo()
            self._wheel_undo_open = True
        self._wheel_timer.start()

    def _end_wheel_undo(self):
        self._wheel_undo_open = False

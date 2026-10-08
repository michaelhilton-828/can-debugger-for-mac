"""Live signal plot with the same viewing controls as log replay.

The matplotlib navigation toolbar is not used. View limits and stacked row
heights live here and survive the 10 Hz refresh: each refresh updates line
data instead of clearing the figure, which used to drop zoom and flash white.
"""

from __future__ import annotations

import bisect

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QPushButton,
    QRubberBand,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .log_plot import (
    CURSOR_COLOR,
    DIFF_COLOR,
    PALETTE,
    ZOOM_IN,
    ZOOM_OUT,
    FlowHost,
    ToolGroup,
    _CTRL_HEIGHT,
)
from .log_replay_model import (
    enum_ticks,
    format_signal_value,
    marker_for_count,
    numeric_minmax,
    padded_range,
    zoom_about,
)
from .theme import STEEL

_MAX_ENUM_TICKS = 24
_BTN_MIN_WIDTH = 64
_MIN_ROW_PX = 48


def _sid(series) -> str:
    key = getattr(series, "key", None)
    if key:
        return "\0".join(str(part) for part in key)
    label = getattr(series, "dbc_label", "") or ""
    return f"{label}\0{series.message_name}\0{series.signal_name}"


def numeric_xy(series) -> tuple[list, list]:
    """Times and numeric values. Non-numeric samples are skipped."""
    xs = []
    ys = []
    for t, value in zip(series.times, series.values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = value.value if hasattr(value, "value") else value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        xs.append(t)
        ys.append(value)
    return xs, ys


class _LiveCanvas(FigureCanvasQTAgg):
    """Wheel zooms the plot when that mode is on, and otherwise scrolls the rows."""

    def __init__(self, figure, plot: "LivePlot"):
        super().__init__(figure)
        self._plot = plot

    def wheelEvent(self, event):
        if not self._plot.wheel_zoom_enabled():
            event.ignore()
            return
        super().wheelEvent(event)
        event.accept()


class LivePlot(QWidget):
    """Scrolling live plot: fit, wheel zoom, pan, cursors, and stacked row height."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series = []
        self._current = None
        self._shown_ids: tuple[str, ...] = ()
        self._note = "Select a signal"
        self._now = 0.0
        self._window = 30.0
        self._xmin = None
        self._xmax = None
        self._ylims: dict[str, tuple[float, float]] = {}
        self._colors: dict[str, str] = {}
        self._fractions: dict[str, float] = {}
        self._row_px = 120
        self._ratios: list[float] = [1.0]
        self._axes_by_id: dict[str, object] = {}
        self._lines = []  # (line, series, ax)
        self._cursor_lines = []
        self._diff_lines = []
        self._placeholder_ax = None
        self._c1 = None
        self._c2 = None
        self._undo = []
        self._redo = []
        self._wheel_undo_open = False
        self._drag = None
        self._scale_mode = "x"
        self._y_mode = "stacked"
        self._grid_on = True
        self._samples_on = False
        self._cursor_on = False
        self._diff_on = False
        self._snap_on = True
        self._wheel_on = True
        self._follow = True
        self._structure_dirty = True
        self._layout_sig = None
        self._in_sync = False
        self._resync = False
        self._hover = ""

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

    def wheel_zoom_enabled(self) -> bool:
        return self._wheel_on

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        self._toolbar = FlowHost(spacing=10)
        flow = self._toolbar.flow

        view = self._group("View")
        view.addWidget(self._button("Fit all", "Fit the time window and every Y axis to the samples", self.fit_all))
        view.addWidget(self._button("Fit X", "Follow the latest Window seconds", self.fit_x))
        view.addWidget(self._button("Fit Y", "Fit Y axes to samples inside the current time window", self.fit_y))
        view.addWidget(self._button("Zoom +", "Zoom in on the axes selected next to Wheel", lambda: self.zoom_step(ZOOM_IN)))
        view.addWidget(self._button("Zoom −", "Zoom out on the axes selected next to Wheel", lambda: self.zoom_step(ZOOM_OUT)))
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
        self._wheel_btn = view.addWidget(self._button(
            "Wheel", "Zoom toward the cursor with the mouse wheel", self._toggle_wheel, checkable=True,
        ))
        self._wheel_btn.setChecked(True)
        self._follow_btn = view.addWidget(self._button(
            "Follow",
            "Keep the time axis on the latest Window seconds. "
            "Zooming or panning time turns this off until Fit X or Follow.",
            self._toggle_follow,
            checkable=True,
        ))
        self._follow_btn.setChecked(True)
        flow.addWidget(view)

        layout_group = self._group("Layout")
        self._y_combo = QComboBox()
        self._y_combo.addItem("Stacked plots", "stacked")
        self._y_combo.addItem("Y of selected", "selected")
        self._y_combo.addItem("Separate Y axes", "separate_y")
        self._y_combo.setToolTip(
            "Stacked: one row per signal, shared time. Drag the gap between rows to resize them. "
            "Y of selected: one plot using the current signal's scale. "
            "Separate Y axes: one plot, one scale per signal."
        )
        self._y_combo.setMinimumWidth(140)
        self._match_control(self._y_combo)
        self._y_combo.currentIndexChanged.connect(self._on_y_mode)
        layout_group.addWidget(self._y_combo)
        layout_group.addWidget(self._caption("Rows"))
        self._row_spin = QSpinBox()
        self._row_spin.setRange(_MIN_ROW_PX, 420)
        self._row_spin.setSingleStep(10)
        self._row_spin.setSuffix(" px")
        self._row_spin.setValue(self._row_px)
        self._row_spin.setToolTip(
            "Height of each stacked signal row. Taller than the pane scrolls. "
            "Drag the gap between two rows to give one of them more space."
        )
        self._row_spin.setMinimumWidth(88)
        self._match_control(self._row_spin)
        self._row_spin.valueChanged.connect(self._on_row_height)
        layout_group.addWidget(self._row_spin)
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
            "Second measurement line. Shift-click to place it, then drag either line.",
            self._toggle_diff,
            checkable=True,
        ))
        self._snap_btn = measure.addWidget(self._button(
            "Snap", "Cursors land on the nearest sample when you release them", self._toggle_snap, checkable=True,
        ))
        self._snap_btn.setChecked(True)
        flow.addWidget(measure)

        export = self._group("Export")
        export.addWidget(self._button("PNG", "Save the plot as a PNG", self.export_png))
        flow.addWidget(export)
        root.addWidget(self._toolbar)

        self.readout = QLabel("")
        self.readout.setWordWrap(True)
        self.readout.setMinimumHeight(18)
        root.addWidget(self.readout)

        self.figure = Figure(facecolor="white")
        self.canvas = _LiveCanvas(self.figure, self)
        self.canvas.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.canvas.setContextMenuPolicy(Qt.NoContextMenu)
        self.canvas.setToolTip(
            "Left-drag draws a zoom box. Right-drag pans. "
            "Drag the gap between stacked rows to change their height. "
            "Drag a measurement line to move it. Shift-click places the difference cursor."
        )
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self.canvas)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self.canvas)
        self._scroll.setWidgetResizable(False)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        viewport = self._scroll.viewport()
        viewport.setAutoFillBackground(True)
        palette = viewport.palette()
        palette.setColor(QPalette.Window, QColor("white"))
        viewport.setPalette(palette)
        root.addWidget(self._scroll, 1)
        self._scroll.viewport().installEventFilter(self)

    def _button(self, text, tip, slot, checkable=False):
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.setFocusPolicy(Qt.NoFocus)
        button.setObjectName("plotToolButton")
        button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        button.setMinimumWidth(_BTN_MIN_WIDTH)
        button.setFixedHeight(_CTRL_HEIGHT)
        button.clicked.connect(slot)
        return button

    def _group(self, text: str) -> ToolGroup:
        return ToolGroup(text)

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("plotToolCaption")
        label.setFixedHeight(_CTRL_HEIGHT)
        label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        return label

    def _match_control(self, widget):
        widget.setObjectName("plotToolControl")
        widget.setFixedHeight(_CTRL_HEIGHT)
        widget.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = max(1, self._toolbar.width())
        height = self._toolbar.heightForWidth(width)
        if height > 0 and self._toolbar.height() != height:
            self._toolbar.setFixedHeight(height)

    def eventFilter(self, obj, event):
        if obj is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self.sync_canvas_size()
        return super().eventFilter(obj, event)

    # -------------------------------------------------------------- data

    def reset_view(self):
        """Drop zoom and fitted scales. Row height and layout mode stay."""
        self._ylims.clear()
        self._undo.clear()
        self._redo.clear()
        self._c1 = None
        self._c2 = None
        self._xmin = None
        self._set_follow(True)
        self._structure_dirty = True
        self._sync_undo_buttons()

    def show_signals(self, series, current, now: float, window_s: float):
        """Show `series` (the user's selection). `current` is drawn in front.

        `now` and `window_s` are seconds since connect. While Follow is on, the
        time axis is [now - window, now]. A zoom or pan of time turns Follow off
        so the next refresh does not snap the axis back.
        """
        self._now = float(now)
        self._window = max(float(window_s), 0.1)
        plottable = [item for item in series if numeric_xy(item)[0]]
        self._current = current if current in plottable else (plottable[0] if plottable else None)
        note = "" if plottable else ("No samples yet" if series else "Select a signal")
        ids = tuple(_sid(item) for item in plottable)
        self._assign_colors(plottable)
        if self._drag is not None and ids == self._shown_ids:
            self._series = plottable
            self._update_lines()
            self.canvas.draw_idle()
            return
        structure = ids != self._shown_ids or note != self._note or self._structure_dirty
        self._series = plottable
        self._shown_ids = ids
        self._note = note
        if structure:
            self._autoscale_missing()
            self.sync_canvas_size(force=True)
            return
        self._update_lines()
        self._apply_xy_limits()
        self._style_markers()
        self.canvas.draw_idle()

    def show_placeholder(self, note: str, now: float, window_s: float):
        self._series = []
        self._shown_ids = ()
        self._current = None
        self._note = note
        self._now = float(now)
        self._window = max(float(window_s), 0.1)
        self._structure_dirty = True
        self.sync_canvas_size(force=True)

    # -------------------------------------------------------------- view

    def fit_all(self):
        if not self._axes():
            return
        self._push_undo()
        self._set_follow(True)
        self._fit_y_limits(*self._window_limits())
        self._apply_xy_limits()
        self._style_markers()
        self.canvas.draw_idle()

    def fit_x(self):
        if not self._axes():
            return
        self._push_undo()
        self._set_follow(True)
        self._apply_xy_limits()
        self._style_markers()
        self.canvas.draw_idle()

    def fit_y(self):
        if not self._axes():
            return
        self._push_undo()
        t_min, t_max = self._visible_x()
        self._fit_y_limits(t_min, t_max)
        self._apply_xy_limits()
        self.canvas.draw_idle()

    def zoom_step(self, factor: float):
        if self._xmin is None and self._follow:
            self._xmin, self._xmax = self._window_limits()
        if self._xmin is None:
            return
        self._push_undo()
        if self._scale_mode in ("x", "both"):
            self._set_follow(False)
        self._scale_limits(factor, (self._xmin + self._xmax) / 2, None, all_y=True)
        self._apply_xy_limits()
        self._style_markers()
        self.canvas.draw_idle()

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

    # -------------------------------------------------------------- layout

    def sync_canvas_size(self, force: bool = False):
        """Match the canvas to the pane. Stacked rows use the row-height setting."""
        if self._in_sync:
            self._resync = True
            return
        view = self._scroll.viewport()
        width = max(view.width(), 1)
        height, ratios = self._desired_size(max(view.height(), 1))
        signature = (
            width,
            height,
            tuple(round(ratio, 3) for ratio in ratios),
            self._y_mode,
            self._shown_ids,
            self._note,
        )
        if not force and not self._structure_dirty and signature == self._layout_sig:
            return
        self._layout_sig = signature
        self._ratios = ratios
        self._in_sync = True
        try:
            if self.canvas.size() != QSize(width, height):
                self.canvas.setFixedSize(width, height)
            self._rebuild()
            self._structure_dirty = False
        finally:
            self._in_sync = False
        if self._resync:
            self._resync = False
            self.sync_canvas_size()

    def _desired_size(self, view_h: int) -> tuple[int, list[float]]:
        count = len(self._series)
        if count == 0 or self._y_mode != "stacked":
            return max(view_h, 180), [1.0]
        ratios = [self._fractions.get(sid, 1.0) for sid in self._shown_ids] or [1.0] * count
        # The spin is the height of one equal row. Dragging a gap redistributes
        # that budget, and the pane scrolls once the stack is taller than it.
        height = int(count * self._row_px + 36)
        return max(height, 120), ratios

    def _on_row_height(self, value: int):
        self._row_px = int(value)
        for sid in self._shown_ids:
            self._fractions[sid] = 1.0
        self._structure_dirty = True
        self.sync_canvas_size(force=True)

    def _on_y_mode(self):
        self._y_mode = self._y_combo.currentData()
        self._row_spin.setEnabled(self._y_mode == "stacked")
        if self._series and self._xmin is not None:
            self._push_undo()
            self._fit_y_limits(*self._visible_x())
        self._structure_dirty = True
        self.sync_canvas_size(force=True)

    # -------------------------------------------------------------- internals

    def _window_limits(self) -> tuple[float, float]:
        if self._now <= 0:
            return 0.0, float(self._window)
        return self._now - self._window, self._now

    def _visible_x(self) -> tuple[float, float]:
        if self._follow or self._xmin is None or self._xmax is None:
            return self._window_limits()
        return self._xmin, self._xmax

    def _assign_colors(self, series):
        for item in series:
            sid = _sid(item)
            if sid not in self._colors:
                self._colors[sid] = PALETTE[len(self._colors) % len(PALETTE)]

    def _autoscale_missing(self):
        t_min, t_max = self._visible_x()
        for series in self._series:
            sid = _sid(series)
            if sid in self._ylims:
                continue
            span = self._span(series, t_min, t_max) or self._span(series, None, None)
            if span is not None:
                self._ylims[sid] = self._padded_span(series, span)

    def _fit_y_limits(self, t_min, t_max):
        shown = list(self._series)
        if not shown:
            return
        if self._y_mode == "selected":
            target = self._current if self._current in shown else shown[0]
            span = self._span(target, t_min, t_max) or self._span(target, None, None)
            if span is None:
                return
            padded = self._padded_span(target, span)
            for series in shown:
                self._ylims[_sid(series)] = padded
            return
        for series in shown:
            span = self._span(series, t_min, t_max) or self._span(series, None, None)
            if span is not None:
                self._ylims[_sid(series)] = self._padded_span(series, span)

    def _padded_span(self, series, span) -> tuple[float, float]:
        lo, hi = span
        choices = getattr(series, "choices", None) or {}
        if choices and len(choices) <= _MAX_ENUM_TICKS:
            keys = [key for key in choices if isinstance(key, (int, float)) and not isinstance(key, bool)]
            if keys:
                lo = min(lo, min(keys))
                hi = max(hi, max(keys))
        return padded_range(lo, hi)

    def _span(self, series, t_min, t_max):
        xs, ys = numeric_xy(series)
        if t_min is None or t_max is None:
            if not ys:
                return None
            return min(ys), max(ys)
        return numeric_minmax(xs, ys, t_min, t_max)

    def _rebuild(self):
        self.figure.clear()
        self._axes_by_id = {}
        self._lines = []
        self._cursor_lines = []
        self._diff_lines = []
        self._placeholder_ax = None
        if not self._series:
            ax = self.figure.add_subplot(111)
            self.figure.subplots_adjust(left=0.08, right=0.98, top=0.96, bottom=0.14)
            ax.set_xlabel("Time (s, since connect)")
            ax.grid(self._grid_on, alpha=0.4)
            ax.text(
                0.5, 0.5, self._note or "Select a signal",
                transform=ax.transAxes, ha="center", va="center", color=STEEL, fontsize=12,
            )
            self._placeholder_ax = ax
            self._apply_xy_limits()
            self.canvas.draw_idle()
            self._update_readout()
            return
        if self._y_mode == "stacked":
            self._build_stacked()
        elif self._y_mode == "separate_y":
            self._build_separate()
        else:
            self._build_shared()
        self._apply_xy_limits()
        self._draw_cursors()
        # draw() not draw_idle: row-gap hit testing needs the axes bounding
        # boxes, which stay empty until the canvas is actually painted.
        self.canvas.draw()

    def _build_stacked(self):
        count = len(self._series)
        ratios = self._ratios if len(self._ratios) == count else [1.0] * count
        grid = self.figure.add_gridspec(
            count, 1, height_ratios=ratios, hspace=0.28,
            left=0.16, right=0.98, top=0.97, bottom=0.1,
        )
        share = None
        for row, series in enumerate(self._series):
            ax = self.figure.add_subplot(grid[row, 0], sharex=share)
            share = share or ax
            self._plot_on(ax, series, ylabel=True)
            if row == count - 1:
                ax.set_xlabel("Time (s, since connect)")

    def _build_shared(self):
        ax = self.figure.add_subplot(111)
        self.figure.subplots_adjust(left=0.1, right=0.98, top=0.96, bottom=0.12)
        for series in self._series:
            self._plot_on(ax, series, ylabel=False)
        ax.set_xlabel("Time (s, since connect)")
        if len(self._series) <= 6:
            ax.legend(loc="upper left", fontsize=8, framealpha=0.85)
        current = self._current if self._current in self._series else self._series[0]
        self._apply_enum_ticks(ax, current)

    def _build_separate(self):
        host = self.figure.add_subplot(111)
        extra = max(0, len(self._series) - 1)
        right = max(0.55, 0.97 - 0.08 * extra)
        self.figure.subplots_adjust(left=0.1, right=right, top=0.96, bottom=0.12)
        host.set_zorder(0)
        for index, series in enumerate(self._series):
            ax = host if index == 0 else host.twinx()
            if index > 1:
                ax.spines["right"].set_position(("outward", 52 * (index - 1)))
            if index > 0:
                ax.set_zorder(index)
                ax.patch.set_visible(False)
            self._plot_on(ax, series, ylabel=True)
        host.set_xlabel("Time (s, since connect)")

    def _plot_on(self, ax, series, ylabel: bool):
        sid = _sid(series)
        color = self._colors[sid]
        xs, ys = numeric_xy(series)
        current = series is self._current or len(self._series) == 1
        line, = ax.plot(
            xs, ys,
            color=color,
            linewidth=2.2 if current else 1.15,
            alpha=1.0 if current else 0.35,
            zorder=3 if current else 2,
            drawstyle="steps-post" if getattr(series, "choices", None) else "default",
            label=series.display_label,
        )
        self._lines.append((line, series, ax))
        self._axes_by_id[sid] = ax
        ax.grid(self._grid_on, alpha=0.4)
        ax.set_autoscalex_on(False)
        ax.set_autoscaley_on(False)
        if ylabel:
            ax.tick_params(axis="y", labelcolor=color)
            ax.set_ylabel(series.display_label, color=color, fontsize=8)
            self._apply_enum_ticks(ax, series)

    def _update_lines(self):
        by_id = {}
        for line, series, _ax in self._lines:
            by_id[_sid(series)] = line
        if len(by_id) != len(self._series):
            self._structure_dirty = True
            self.sync_canvas_size(force=True)
            return
        for series in self._series:
            line = by_id.get(_sid(series))
            if line is None:
                self._structure_dirty = True
                self.sync_canvas_size(force=True)
                return
            xs, ys = numeric_xy(series)
            line.set_data(xs, ys)

    def _apply_enum_ticks(self, ax, series):
        positions, labels = enum_ticks(getattr(series, "choices", None))
        if not positions or len(positions) > _MAX_ENUM_TICKS:
            return
        ax.set_yticks(positions)
        ax.set_yticklabels(labels)

    def _apply_xy_limits(self):
        axes = self._axes()
        if not axes:
            return
        if self._follow or self._xmin is None or self._xmax is None:
            self._xmin, self._xmax = self._window_limits()
        for ax in axes:
            ax.set_xlim(self._xmin, self._xmax)
        if not self._series:
            return
        if self._y_mode == "selected":
            ylim = None
            if self._current is not None:
                ylim = self._ylims.get(_sid(self._current))
            if ylim is None and self._ylims:
                ylim = next(iter(self._ylims.values()))
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

    def _style_markers(self):
        for line, series, _ax in self._lines:
            if self._samples_on:
                line.set_marker(".")
                line.set_markersize(3.5)
                continue
            xs, _ys = numeric_xy(series)
            if self._xmin is None:
                visible = len(xs)
            else:
                i0 = bisect.bisect_left(xs, self._xmin)
                i1 = bisect.bisect_right(xs, self._xmax)
                visible = i1 - i0
            marker = marker_for_count(visible)
            line.set_marker(marker if marker else "None")
            line.set_markersize(3.5)

    def _axes(self):
        if not self._series:
            return [self._placeholder_ax] if self._placeholder_ax is not None else []
        seen = []
        for ax in self._axes_by_id.values():
            if ax not in seen:
                seen.append(ax)
        return seen

    def _scale_limits(self, factor, x_center, y_center, all_y: bool, y_axes=None):
        mode = self._scale_mode
        if mode in ("x", "both") and x_center is not None and self._xmin is not None:
            self._xmin, self._xmax = zoom_about(self._xmin, self._xmax, x_center, factor)
        if mode in ("y", "both"):
            targets = self._axes() if all_y else (y_axes or [])
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
        axes = self._axes()
        if not axes:
            return
        self._xmin, self._xmax = axes[0].get_xlim()
        if not self._series:
            return
        if self._y_mode == "selected":
            ylim = axes[0].get_ylim()
            for sid in self._axes_by_id:
                self._ylims[sid] = ylim
        else:
            for sid, ax in self._axes_by_id.items():
                self._ylims[sid] = ax.get_ylim()

    # -------------------------------------------------------------- cursors

    def _draw_cursors(self):
        for line in self._cursor_lines + self._diff_lines:
            line.remove()
        self._cursor_lines = []
        self._diff_lines = []
        if not self._cursor_on:
            self._update_readout()
            return
        for index, ax in enumerate(self._axes()):
            top = index == 0
            if self._c1 is not None:
                self._cursor_lines.extend(self._cursor_artists(ax, self._c1, CURSOR_COLOR, top))
            if self._diff_on and self._c2 is not None:
                self._diff_lines.extend(self._cursor_artists(ax, self._c2, DIFF_COLOR, top))
        self._update_readout()

    def _cursor_artists(self, ax, t: float, color: str, top: bool):
        artists = [ax.axvline(t, color=color, lw=2.0, ls="--", zorder=5)]
        if top:
            marker, = ax.plot(
                [t], [1.02],
                transform=ax.get_xaxis_transform(),
                marker="v", color=color, markersize=9, clip_on=False, zorder=6,
            )
            label = ax.text(
                t, 1.02, f"  {t:.3f} s",
                transform=ax.get_xaxis_transform(),
                color=color, fontsize=8, ha="left", va="bottom", clip_on=False, zorder=6,
            )
            artists.extend((marker, label))
        return artists

    def _place_cursor(self, which: str, t: float, snap: bool = False):
        if snap:
            snapped = self._snap_time(t)
            if snapped is not None:
                t = snapped
        if which == "c2":
            self._c2 = t
        else:
            self._c1 = t
        self._draw_cursors()
        self.canvas.draw_idle()

    def _snap_time(self, t: float):
        best = None
        best_dist = None
        for series in self._series:
            xs, _ys = numeric_xy(series)
            if not xs:
                continue
            index = bisect.bisect_left(xs, t)
            candidates = []
            if index < len(xs):
                candidates.append(xs[index])
            if index > 0:
                candidates.append(xs[index - 1])
            for rel in candidates:
                dist = abs(rel - t)
                if best_dist is None or dist < best_dist:
                    best = rel
                    best_dist = dist
        return best

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

    def _row_boundary(self, event):
        """Index of the stacked-row gap under the cursor, or None."""
        if self._y_mode != "stacked" or event.y is None or len(self._series) < 2:
            return None
        axes = self._axes()
        if len(axes) < 2:
            return None
        for index in range(len(axes) - 1):
            gap_low = axes[index].bbox.y0
            gap_high = axes[index + 1].bbox.y1
            mid = (gap_low + gap_high) / 2.0
            if abs(event.y - mid) <= 8:
                return index
        return None

    def _resize_rows(self, index: int, dy_px: float, start_fractions: list[float], span_px: float):
        if span_px <= 1 or index + 1 >= len(start_fractions):
            return
        fractions = list(start_fractions)
        total = sum(fractions) or 1.0
        delta = dy_px / span_px * (fractions[index] + fractions[index + 1])
        upper = fractions[index] - delta
        lower = fractions[index + 1] + delta
        minimum = (fractions[index] + fractions[index + 1]) * (_MIN_ROW_PX / span_px)
        minimum = min(minimum, (fractions[index] + fractions[index + 1]) / 2)
        if upper < minimum:
            lower -= minimum - upper
            upper = minimum
        if lower < minimum:
            upper -= minimum - lower
            lower = minimum
        if upper < minimum or lower < minimum:
            return
        fractions = start_fractions[:index] + [upper, lower] + start_fractions[index + 2:]
        if abs(sum(fractions) - total) > 1e-3:
            return
        for sid, value in zip(self._shown_ids, fractions):
            self._fractions[sid] = value
        self.sync_canvas_size(force=True)

    # -------------------------------------------------------------- mouse

    def _on_scroll(self, event):
        if not self._wheel_on or event.inaxes is None or event.xdata is None or self._xmin is None:
            return
        if event.button not in ("up", "down"):
            return
        self._begin_wheel_undo()
        factor = ZOOM_IN if event.button == "up" else ZOOM_OUT
        if self._scale_mode in ("x", "both"):
            self._set_follow(False)
        y_axes = [event.inaxes] if self._y_mode != "selected" else self._axes()[:1]
        self._scale_limits(factor, event.xdata, event.ydata, all_y=False, y_axes=y_axes)
        self._apply_xy_limits()
        self._style_markers()
        self.canvas.draw_idle()

    def _on_press(self, event):
        if event.button == 1:
            boundary = self._row_boundary(event)
            if boundary is not None:
                axes = self._axes()
                span = axes[boundary].bbox.height + axes[boundary + 1].bbox.height
                self._drag = {
                    "kind": "row",
                    "index": boundary,
                    "y": event.y,
                    "fractions": [self._fractions.get(sid, 1.0) for sid in self._shown_ids],
                    "span": span,
                }
                self.canvas.setCursor(Qt.SizeVerCursor)
                return
        if event.inaxes is None or event.xdata is None:
            return
        if self._xmin is None:
            self._xmin, self._xmax = self._window_limits()
        if event.button == 1:
            hit = self._cursor_hit(event)
            if hit is not None:
                self._drag = {"kind": hit, "moved": False}
                self.canvas.setCursor(Qt.SizeHorCursor)
                return
            origin = self._qt_pos(event)
            self._drag = {
                "kind": "zoom",
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
            "moved": False,
            "undo": False,
            "limits": self._axis_snapshots(),
        }
        self.canvas.setCursor(Qt.ClosedHandCursor)

    def _on_motion(self, event):
        if self._drag is not None and self._drag["kind"] == "row":
            if event.y is None:
                return
            self._resize_rows(
                self._drag["index"],
                event.y - self._drag["y"],
                self._drag["fractions"],
                self._drag["span"],
            )
            return
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
        if self._row_boundary(event) is not None:
            self.canvas.setCursor(Qt.SizeVerCursor)
        elif self._cursor_hit(event):
            self.canvas.setCursor(Qt.SizeHorCursor)
        else:
            self.canvas.setCursor(Qt.ArrowCursor)
        self._hover_at(event)

    def _on_release(self, _event):
        drag = self._drag
        self._drag = None
        self._rubber.hide()
        self.canvas.setCursor(Qt.ArrowCursor)
        if drag is None:
            return
        if drag["kind"] == "row":
            return
        if drag["kind"] == "pan":
            if drag["moved"]:
                self._store_limits_from_axes()
                self._set_follow(False)
                self._apply_xy_limits()
                self.canvas.draw_idle()
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
            self._set_follow(False)
        for ax, xlim, ylim in self._drag["limits"]:
            x0, y0 = ax.transData.inverted().transform((0, 0))
            x1, y1 = ax.transData.inverted().transform((dx, dy))
            ax.set_xlim(xlim[0] - (x1 - x0), xlim[1] - (x1 - x0))
            ax.set_ylim(ylim[0] - (y1 - y0), ylim[1] - (y1 - y0))
        self._store_limits_from_axes()
        self._style_markers()
        self.canvas.draw_idle()

    def _finish_drag_zoom(self, drag):
        x0, x1 = drag["x0"], drag["x1"]
        y0, y1 = drag["y0"], drag["y1"]
        changed = False
        if None not in (x0, x1) and abs(x1 - x0) > 1e-9:
            self._xmin, self._xmax = (min(x0, x1), max(x0, x1))
            self._set_follow(False)
            changed = True
        if None not in (y0, y1) and abs(y1 - y0) > 1e-12:
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
        self._apply_xy_limits()
        self._style_markers()
        self.canvas.draw_idle()

    def _axis_snapshots(self):
        seen = set()
        snapshots = []
        for ax in self._axes():
            if id(ax) in seen:
                continue
            seen.add(id(ax))
            snapshots.append((ax, ax.get_xlim(), ax.get_ylim()))
        return snapshots

    def _hover_at(self, event):
        if event.inaxes is None or event.xdata is None:
            self._hover = ""
            self._update_readout()
            return
        best = None
        best_dist = 14 * 14
        for _line, series, ax in self._lines:
            if self._y_mode == "stacked" and ax is not event.inaxes:
                continue
            xs, ys = numeric_xy(series)
            index = bisect.bisect_left(xs, event.xdata)
            candidates = []
            if index < len(xs):
                candidates.append(index)
            if index > 0:
                candidates.append(index - 1)
            for idx in candidates:
                value = ys[idx]
                px, py = ax.transData.transform((xs[idx], value))
                dist = (px - event.x) ** 2 + (py - event.y) ** 2
                if dist <= best_dist:
                    best = (series, xs[idx], value)
                    best_dist = dist
        if best is None:
            self._hover = ""
        else:
            series, rel, value = best
            self._hover = (
                f"{series.display_label} = {format_signal_value(value, getattr(series, 'choices', None))}  "
                f"@ {rel:.3f} s"
            )
        self._update_readout()

    def _qt_pos(self, event) -> QPoint:
        height = self.canvas.height()
        return QPoint(int(event.x), int(height - event.y))

    def _update_readout(self):
        parts = []
        if not self._follow and self._series:
            parts.append("Follow off")
        if self._cursor_on and self._c1 is not None:
            parts.append(f"Cursor {self._c1:.3f} s")
        if self._cursor_on and self._diff_on and self._c1 is not None and self._c2 is not None:
            parts.append(f"Δt {abs(self._c2 - self._c1):.3f} s")
        if self._hover:
            parts.append(self._hover)
        self.readout.setText("    ".join(parts))

    # -------------------------------------------------------------- toggles

    def _on_scale_mode(self):
        self._scale_mode = self._scale_combo.currentData()

    def _toggle_wheel(self):
        self._wheel_on = self._wheel_btn.isChecked()

    def _toggle_follow(self):
        self._follow = self._follow_btn.isChecked()
        if self._follow:
            self._apply_xy_limits()
            self._style_markers()
            self.canvas.draw_idle()
        self._update_readout()

    def _set_follow(self, on: bool):
        self._follow = on
        self._follow_btn.blockSignals(True)
        self._follow_btn.setChecked(on)
        self._follow_btn.blockSignals(False)
        self._update_readout()

    def _toggle_cursor(self):
        self._cursor_on = self._cursor_btn.isChecked()
        if self._cursor_on and self._c1 is None and self._xmin is not None:
            self._c1 = (self._xmin + self._xmax) / 2
            if self._snap_on:
                snapped = self._snap_time(self._c1)
                if snapped is not None:
                    self._c1 = snapped
        self._draw_cursors()
        self.canvas.draw_idle()

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
        self._draw_cursors()
        self.canvas.draw_idle()

    def _toggle_snap(self):
        self._snap_on = self._snap_btn.isChecked()

    def _toggle_grid(self):
        self._grid_on = self._grid_btn.isChecked()
        for ax in self._axes():
            ax.grid(self._grid_on, alpha=0.4)
        self.canvas.draw_idle()

    def _toggle_samples(self):
        self._samples_on = self._samples_btn.isChecked()
        self._style_markers()
        self.canvas.draw_idle()

    # -------------------------------------------------------------- undo

    def _snapshot(self):
        items = tuple(sorted(self._ylims.items()))
        return (self._xmin, self._xmax, items, self._follow)

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
        self._xmin, self._xmax, items, follow = snap
        self._ylims = {key: value for key, value in items}
        self._set_follow(follow)
        self._apply_xy_limits()
        self._style_markers()
        self.canvas.draw_idle()
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

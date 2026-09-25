"""The Log Replay tab: decode a CAN log against a .dbc and plot signals.

Ports the decode/plot logic that used to live in the standalone
can-log-viewer script (plot_can.py), but replaces its input()/print()
prompts with Qt widgets and its blocking plt.show() window with a plot
embedded directly in the tab via matplotlib's Qt canvas.
"""

from __future__ import annotations

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dbc_model import DbcLoadError, format_id_hex, load_database
from .log_replay_model import (
    LogLoadError,
    enum_ticks,
    load_log,
    marker_for_count,
    numeric_minmax,
    supported_log_extensions,
)

SIGNAL_HEADERS = ["Message", "Signal", "Unit", "ID (hex)", "Samples"]


class LogReplayTab(QWidget):
    log_loaded = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.database = None
        self._browser_database = None
        self._browser_database_path = ""
        self._independent_dbc_path = ""
        self._using_independent_dbc = False

        self._signals = {}
        self._row_keys = []
        self._log_path = ""
        self._t0 = 0.0
        self._plotted = []
        self._updating_range = False
        self._in_redraw = False

        self._build_ui()
        self._update_dbc_label()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)

        dbc_row = QHBoxLayout()
        self.dbc_label = QLabel("No DBC loaded")
        self.dbc_label.setStyleSheet("color: #555;")
        self.dbc_open_button = QPushButton("Open .dbc…")
        self.dbc_open_button.clicked.connect(self._open_dbc_dialog)
        self.dbc_use_browser_button = QPushButton("Use DBC Viewer tab's DBC")
        self.dbc_use_browser_button.clicked.connect(self._use_browser_dbc)
        self.dbc_use_browser_button.setEnabled(False)
        dbc_row.addWidget(self.dbc_label, 1)
        dbc_row.addWidget(self.dbc_open_button)
        dbc_row.addWidget(self.dbc_use_browser_button)
        root.addLayout(dbc_row)

        log_row = QHBoxLayout()
        self.log_open_button = QPushButton("Open log…")
        self.log_open_button.clicked.connect(self._open_log_dialog)
        self.redecode_button = QPushButton("Re-decode")
        self.redecode_button.setEnabled(False)
        self.redecode_button.clicked.connect(self._redecode)
        self.log_path_label = QLabel("No log loaded")
        self.log_path_label.setStyleSheet("color: #555;")
        log_row.addWidget(self.log_open_button)
        log_row.addWidget(self.redecode_button)
        log_row.addWidget(self.log_path_label, 1)
        root.addLayout(log_row)

        self.status_label = QLabel("")
        root.addWidget(self.status_label)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # --- left: signal table -------------------------------------------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.signal_filter = QLineEdit()
        self.signal_filter.setPlaceholderText("Filter signals by name…")
        self.signal_filter.textChanged.connect(self._filter_signals)
        left_layout.addWidget(self.signal_filter)

        self.signal_table = QTableWidget(0, len(SIGNAL_HEADERS))
        self.signal_table.setHorizontalHeaderLabels(SIGNAL_HEADERS)
        self.signal_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.signal_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.signal_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.signal_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.signal_table.horizontalHeader().setStretchLastSection(True)
        self.signal_table.setSortingEnabled(True)
        self.signal_table.itemSelectionChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.signal_table, 1)
        splitter.addWidget(left)

        # --- right: plot options + embedded canvas -------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        options = QHBoxLayout()
        self.overlay_checkbox = QCheckBox("Overlay on one plot")
        self.overlay_checkbox.setChecked(True)
        self.overlay_checkbox.stateChanged.connect(self._redraw)
        options.addWidget(self.overlay_checkbox)
        options.addWidget(QLabel("From"))
        self.range_from = self._make_time_spin()
        self.range_to = self._make_time_spin()
        self.range_from.valueChanged.connect(self._on_range_edited)
        self.range_to.valueChanged.connect(self._on_range_edited)
        options.addWidget(self.range_from)
        options.addWidget(QLabel("to"))
        options.addWidget(self.range_to)
        options.addWidget(QLabel("s"))
        options.addStretch(1)
        right_layout.addLayout(options)
        self.stats_label = QLabel("")
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet("color: #333;")
        right_layout.addWidget(self.stats_label)

        self.figure = Figure()
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        right_layout.addWidget(self.toolbar)
        right_layout.addWidget(self.canvas, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

    @staticmethod
    def _make_time_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0, 1_000_000)
        spin.setDecimals(3)
        spin.setSingleStep(0.1)
        spin.setMaximumWidth(110)
        return spin

    # -------------------------------------------------------------- DBC sync

    def set_browser_database(self, database, path: str) -> None:
        """Slot for BrowserTab.database_loaded. Updates the shared DBC
        pointer unless the user has picked an independent DBC here. Never
        re-decodes an already-loaded log on its own - that could be an
        expensive silent operation, so we just flag the log as possibly
        stale and let the user re-open it if they want a fresh decode.
        """
        self._browser_database = database
        self._browser_database_path = path
        self.dbc_use_browser_button.setEnabled(self._using_independent_dbc)
        if not self._using_independent_dbc:
            self.database = database
            self._mark_log_stale()
        self._update_redecode_enabled()
        self._update_dbc_label()

    def _update_dbc_label(self):
        if self.database is None:
            self.dbc_label.setText("No DBC loaded")
        elif self._using_independent_dbc:
            self.dbc_label.setText(f"{self._independent_dbc_path} (independent)")
        else:
            self.dbc_label.setText(f"{self._browser_database_path} (from DBC Viewer tab)")

    def _open_dbc_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open .dbc file", "", "CAN database (*.dbc);;All files (*)")
        if not path:
            return
        try:
            database = load_database(path)
        except DbcLoadError as exc:
            QMessageBox.critical(self, "Failed to load .dbc file", str(exc))
            return

        self._independent_dbc_path = path
        self._using_independent_dbc = True
        self.database = database
        self.dbc_use_browser_button.setEnabled(True)
        self._mark_log_stale()
        self._update_redecode_enabled()
        self._update_dbc_label()

    def _use_browser_dbc(self):
        self._using_independent_dbc = False
        self.database = self._browser_database
        self.dbc_use_browser_button.setEnabled(False)
        self._mark_log_stale()
        self._update_redecode_enabled()
        self._update_dbc_label()

    def _update_redecode_enabled(self):
        self.redecode_button.setEnabled(bool(self._log_path) and self.database is not None)

    def _mark_log_stale(self):
        if not self._log_path or not self._signals:
            return
        suffix = "  DBC changed — click Re-decode."
        text = self.status_label.text()
        if not text.endswith(suffix):
            self.status_label.setText(text + suffix)

    # -------------------------------------------------------------- log

    def _open_log_dialog(self):
        exts = supported_log_extensions()
        pattern = " ".join(f"*{ext}" for ext in exts)
        filter_str = f"CAN log files ({pattern});;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Open log file", "", filter_str)
        if path:
            self.load_log_file(path)

    def load_log_file(self, path: str) -> bool:
        """Decode `path` against the current DBC. Public for drops and Open Recent."""
        if self.database is None:
            QMessageBox.warning(self, "No DBC loaded", "Load a .dbc file before opening a log.")
            return False

        try:
            signals, unmapped_ids = load_log(path, self.database)
        except LogLoadError as exc:
            QMessageBox.critical(self, "Failed to load log file", str(exc))
            return False

        self._log_path = path
        self.log_path_label.setText(path)
        self._signals = signals
        stamped = [s.times[0] for s in signals.values() if s.times]
        self._t0 = min(stamped) if stamped else 0.0
        self._row_keys = sorted(
            signals.keys(), key=lambda k: (signals[k].arbitration_id, k[0], k[1])
        )
        self._populate_signal_table()
        self._set_full_range()
        self._update_redecode_enabled()

        message_count = len({s.message_name for s in signals.values()})
        status = f"Decoded {len(signals)} signal(s) from {message_count} message(s)."
        if unmapped_ids:
            status += f" {len(unmapped_ids)} unmapped ID(s) shown as \"Unknown\"."
        self.status_label.setText(status)
        self._redraw()
        self.log_loaded.emit(path)
        return True

    def _redecode(self):
        if self._log_path:
            self.load_log_file(self._log_path)

    def _set_full_range(self):
        end = 0.0
        for series in self._signals.values():
            if series.times:
                end = max(end, max(series.times) - self._t0)
        self._updating_range = True
        self.range_from.setValue(0)
        self.range_to.setValue(end)
        self._updating_range = False

    def _on_range_edited(self):
        if self._updating_range or self._in_redraw:
            return
        self._redraw()

    # ------------------------------------------------------------- signals

    def _populate_signal_table(self):
        self.signal_table.setSortingEnabled(False)
        self.signal_table.setRowCount(len(self._row_keys))
        for row_idx, key in enumerate(self._row_keys):
            series = self._signals[key]
            values = [
                series.message_name,
                series.signal_name,
                series.unit,
                format_id_hex(series.arbitration_id, series.arbitration_id > 0x7FF),
                str(len(series.times)),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole + 1, row_idx)
                self.signal_table.setItem(row_idx, col, item)
        self.signal_table.setSortingEnabled(True)

    def _filter_signals(self, text: str):
        text = text.strip().lower()
        for r in range(self.signal_table.rowCount()):
            row_idx = self.signal_table.item(r, 0).data(Qt.UserRole + 1)
            key = self._row_keys[row_idx]
            haystack = f"{key[0]} {key[1]}".lower()
            self.signal_table.setRowHidden(r, text not in haystack)

    def _selected_series(self):
        seen_rows = set()
        selected = []
        for item in self.signal_table.selectedItems():
            row_idx = item.data(Qt.UserRole + 1)
            if row_idx in seen_rows:
                continue
            seen_rows.add(row_idx)
            selected.append(self._signals[self._row_keys[row_idx]])
        return selected

    def _on_selection_changed(self):
        self._redraw()

    # --------------------------------------------------------------- plot

    def _redraw(self):
        if self._in_redraw:
            return
        self._in_redraw = True
        try:
            self._redraw_plot()
        finally:
            self._in_redraw = False

    def _redraw_plot(self):
        self.figure.clear()
        self._plotted = []
        selected = [s for s in self._selected_series() if s.times]
        if not selected:
            self.stats_label.setText("")
            self.canvas.draw_idle()
            return

        overlay = self.overlay_checkbox.isChecked() or len(selected) == 1
        if overlay:
            axes = [self.figure.add_subplot(111)]
            for s in selected:
                self._plot_series(axes[0], s, label=s.display_label)
            axes[0].set_xlabel("Time (s)")
            axes[0].legend()
            axes[0].grid(True)
            primary = axes[0]
        else:
            axes = self.figure.subplots(len(selected), 1, sharex=True)
            if len(selected) == 1:
                axes = [axes]
            for ax, s in zip(axes, selected):
                self._plot_series(ax, s)
                ax.set_ylabel(s.display_label)
                ax.grid(True)
            axes[-1].set_xlabel("Time (s)")
            primary = axes[-1]

        t_min, t_max = self._range_limits()
        self._updating_range = True
        primary.set_xlim(t_min, t_max)
        self._updating_range = False
        self._style_markers(t_min, t_max)
        self._update_stats(t_min, t_max)
        self.figure.tight_layout()
        primary.callbacks.connect("xlim_changed", self._on_xlim_changed)
        self.canvas.draw_idle()

    def _plot_series(self, ax, series, label=None):
        rel = [x - self._t0 for x in series.times]
        kwargs = {}
        if label:
            kwargs["label"] = label
        line, = ax.plot(rel, series.values, **kwargs)
        self._plotted.append((line, rel, series.values, series.signal_name))
        positions, labels = enum_ticks(series.choices)
        if positions:
            ax.set_yticks(positions)
            ax.set_yticklabels(labels)

    def _range_limits(self) -> tuple[float, float]:
        start = self.range_from.value()
        end = self.range_to.value()
        if end < start:
            return end, start
        if end == start:
            return start, start + 1e-3
        return start, end

    def _style_markers(self, t_min: float, t_max: float):
        for line, times, _values, _name in self._plotted:
            visible = sum(1 for t in times if t_min <= t <= t_max)
            line.set_marker(marker_for_count(visible))

    def _update_stats(self, t_min: float, t_max: float):
        parts = []
        for _line, times, values, name in self._plotted:
            span = numeric_minmax(times, values, t_min, t_max)
            if span is None:
                continue
            lo, hi = span
            parts.append(f"{name} min {lo:g} max {hi:g}")
        self.stats_label.setText(
            "   ".join(parts) if parts else "No numeric samples in this time range"
        )

    def _on_xlim_changed(self, ax):
        if self._updating_range:
            return
        xmin, xmax = ax.get_xlim()
        self._updating_range = True
        self.range_from.setValue(max(0.0, xmin))
        self.range_to.setValue(max(0.0, xmax))
        self._updating_range = False
        self._style_markers(xmin, xmax)
        self._update_stats(xmin, xmax)
        self.canvas.draw_idle()

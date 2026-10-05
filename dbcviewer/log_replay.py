"""The Log Replay tab: decode a CAN log against the Home DBC list and plot signals.

Ports the decode/plot logic that used to live in the standalone
can-log-viewer script (plot_can.py), but replaces its input()/print()
prompts with Qt widgets and its blocking plt.show() window with a plot
embedded directly in the tab via matplotlib's Qt canvas.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
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

from .dbc_library import DbcLibrary
from .ui_state import remember_splitter
from .dbc_model import format_id_hex
from .log_plot import ElidingLabel, SignalPlot
from .table_columns import install_column_config
from .log_replay_model import (
    LogLoadError,
    format_signal_value,
    load_log,
    nearest_index,
    numeric_minmax,
    supported_log_extensions,
)

SIGNAL_HEADERS = ["Color", "DBC", "Message", "Signal", "Unit", "ID (hex)", "Samples", "Value", "Δ", "Min", "Max"]
COL_COLOR = 0
COL_DBC = 1
COL_MESSAGE = 2
COL_SIGNAL = 3
COL_SAMPLES = 6
COL_VALUE = 7
COL_DELTA = 8
COL_MIN = 9
COL_MAX = 10


class LogReplayTab(QWidget):
    log_loaded = Signal(str)

    def __init__(self, library: DbcLibrary, parent=None):
        super().__init__(parent)
        self.library = library
        self._signals = {}
        self._row_keys = []
        self._log_path = ""
        self._decode_key: tuple[str, ...] | None = None
        self._t0 = 0.0
        self._log_end = 0.0
        self._fit_on_next = False

        self._build_ui()
        self.library.changed.connect(self._on_library_changed)
        self._on_library_changed()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        self.dbc_label = QLabel("No DBCs loaded on Home")
        self.dbc_label.setStyleSheet("color: #555;")
        root.addWidget(self.dbc_label)

        log_row = QHBoxLayout()
        self.log_open_button = QPushButton("Open log…")
        self.log_open_button.clicked.connect(self._open_log_dialog)
        self.redecode_button = QPushButton("Re-decode")
        self.redecode_button.setEnabled(False)
        self.redecode_button.clicked.connect(self._redecode)
        self.log_path_label = ElidingLabel("No log loaded")
        log_row.addWidget(self.log_open_button)
        log_row.addWidget(self.redecode_button)
        log_row.addWidget(self.log_path_label, 1)
        root.addLayout(log_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
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
        header = self.signal_table.horizontalHeader()
        self.signal_table.setColumnWidth(COL_COLOR, 56)
        self.signal_table.setColumnWidth(COL_DBC, 110)
        self.signal_table.setColumnWidth(COL_MESSAGE, 120)
        self.signal_table.setColumnWidth(COL_SIGNAL, 160)
        self.signal_table.setColumnWidth(COL_SAMPLES, 72)
        self.signal_table.setColumnWidth(COL_VALUE, 88)
        self.signal_table.setColumnWidth(COL_DELTA, 72)
        self.signal_table.setColumnWidth(COL_MIN, 72)
        self.signal_table.setColumnWidth(COL_MAX, 72)
        self.signal_table.setSortingEnabled(True)
        self.signal_table.itemSelectionChanged.connect(self._on_selection_changed)
        install_column_config(self.signal_table, "log.signals")
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(COL_COLOR, QHeaderView.Fixed)
        left_layout.addWidget(self.signal_table, 1)
        left.setMinimumWidth(180)
        splitter.addWidget(left)

        self.plot = SignalPlot()
        self.plot.setMinimumWidth(260)
        self.plot.view_changed.connect(lambda _lo, _hi: self._refresh_measurement_columns())
        self.plot.cursors_changed.connect(lambda _a, _b: self._refresh_measurement_columns())
        splitter.addWidget(self.plot)

        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        remember_splitter(splitter, "log.signals-plot")

    # -------------------------------------------------------------- DBC sync

    def _source_key(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.library.entries())

    def _decode_sources(self):
        return [(entry.label, entry.database) for entry in self.library.entries()]

    def _update_dbc_label(self):
        entries = self.library.entries()
        if not entries:
            text = "No DBCs loaded on Home"
        else:
            text = "DBCs from Home: " + ", ".join(entry.label for entry in entries)
        self.dbc_label.setText(text)
        self.dbc_label.setToolTip(text)

    def _on_library_changed(self):
        self._update_dbc_label()
        self._update_redecode_enabled()
        if self._log_path and self._signals and self._source_key() != self._decode_key:
            self._mark_log_stale()

    def _update_redecode_enabled(self):
        self.redecode_button.setEnabled(bool(self._log_path) and bool(self.library.entries()))

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
        if not self.library.entries():
            QMessageBox.warning(
                self,
                "No DBC loaded",
                "Add a .dbc file on the Home tab before opening a log.",
            )
            return False

        try:
            signals, unmapped_ids = load_log(path, self._decode_sources())
        except LogLoadError as exc:
            QMessageBox.critical(self, "Failed to load log file", str(exc))
            return False

        self._log_path = path
        self._decode_key = self._source_key()
        self.log_path_label.set_full_text(path)
        self._signals = signals
        stamped = [s.times[0] for s in signals.values() if s.times]
        self._t0 = min(stamped) if stamped else 0.0
        self._log_end = 0.0
        for series in signals.values():
            if series.times:
                self._log_end = max(self._log_end, max(series.times) - self._t0)
        self._row_keys = sorted(
            signals.keys(),
            key=lambda k: (
                signals[k].arbitration_id,
                signals[k].dbc_label,
                signals[k].message_name,
                signals[k].signal_name,
            ),
        )
        self._populate_signal_table()
        self._fit_on_next = True
        self._update_redecode_enabled()

        message_count = len({s.message_name for s in signals.values()})
        status = f"Decoded {len(signals)} signal(s) from {message_count} message(s)."
        if unmapped_ids:
            status += f" {len(unmapped_ids)} unmapped ID(s) shown as \"Unknown\"."
        self.status_label.setText(status)
        self._sync_plot()
        self.log_loaded.emit(path)
        return True

    def _redecode(self):
        if self._log_path:
            self.load_log_file(self._log_path)

    # ------------------------------------------------------------- signals

    def _populate_signal_table(self):
        self.signal_table.setSortingEnabled(False)
        self.signal_table.setRowCount(len(self._row_keys))
        for row_idx, key in enumerate(self._row_keys):
            series = self._signals[key]
            values = [
                "",
                series.dbc_label,
                series.message_name,
                series.signal_name,
                series.unit,
                format_id_hex(series.arbitration_id, series.arbitration_id > 0x7FF),
                str(len(series.times)),
                "",
                "",
                "",
                "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole + 1, row_idx)
                self.signal_table.setItem(row_idx, col, item)
        self.signal_table.setSortingEnabled(True)

    def _filter_signals(self, text: str):
        text = text.strip().lower()
        for r in range(self.signal_table.rowCount()):
            row_idx = self.signal_table.item(r, COL_MESSAGE).data(Qt.UserRole + 1)
            series = self._signals[self._row_keys[row_idx]]
            haystack = f"{series.dbc_label} {series.message_name} {series.signal_name}".lower()
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
        self._sync_plot()

    def _current_series(self):
        row = self.signal_table.currentRow()
        if row < 0:
            return None
        item = self.signal_table.item(row, COL_MESSAGE)
        if item is None:
            return None
        row_idx = item.data(Qt.UserRole + 1)
        series = self._signals[self._row_keys[row_idx]]
        if series not in self._selected_series():
            return None
        return series

    def _sync_plot(self):
        reset = self._fit_on_next
        self._fit_on_next = False
        self.plot.set_series(
            self._selected_series(),
            self._current_series(),
            self._t0,
            self._log_end,
            reset_view=reset,
        )
        self._refresh_measurement_columns()

    def _refresh_measurement_columns(self):
        t_min, t_max = self.plot.limits()
        cursor, diff = self.plot.cursors()
        current = self._current_series()
        selected = {series.key for series in self._selected_series()}
        bold = QFont()
        bold.setBold(True)
        normal = QFont()
        sorting = self.signal_table.isSortingEnabled()
        self.signal_table.setSortingEnabled(False)
        try:
            for row in range(self.signal_table.rowCount()):
                message_item = self.signal_table.item(row, COL_MESSAGE)
                if message_item is None:
                    continue
                row_idx = message_item.data(Qt.UserRole + 1)
                series = self._signals[self._row_keys[row_idx]]
                plotted = series.key in selected
                color = self.plot.color_for(series.message_name, series.signal_name, series.dbc_label) if plotted else None
                swatch = self.signal_table.item(row, COL_COLOR)
                if swatch is not None:
                    swatch.setBackground(QColor(color) if color else QColor(Qt.transparent))
                name_item = self.signal_table.item(row, COL_SIGNAL)
                if name_item is not None:
                    name_item.setFont(bold if series is current else normal)
                if not plotted:
                    for col in (COL_VALUE, COL_DELTA, COL_MIN, COL_MAX):
                        self._set_cell(row, col, "")
                    continue
                self._set_cell(row, COL_VALUE, self._value_text(series, cursor))
                self._set_cell(row, COL_DELTA, self._delta_text(series, cursor, diff))
                span = numeric_minmax(series.times, series.values, t_min + self._t0, t_max + self._t0)
                if span is None:
                    self._set_cell(row, COL_MIN, "")
                    self._set_cell(row, COL_MAX, "")
                else:
                    lo, hi = span
                    self._set_cell(row, COL_MIN, f"{lo:g}")
                    self._set_cell(row, COL_MAX, f"{hi:g}")
        finally:
            self.signal_table.setSortingEnabled(sorting)

    def _value_text(self, series, rel_time):
        if rel_time is None:
            return ""
        sample = self._sample_at(series, rel_time)
        if sample is None:
            return ""
        return format_signal_value(sample, series.choices)

    def _delta_text(self, series, start, end):
        if start is None or end is None:
            return ""
        first = self._sample_at(series, start)
        second = self._sample_at(series, end)
        if not _is_number(first) or not _is_number(second):
            return ""
        return f"{second - first:g}"

    def _sample_at(self, series, rel_time):
        index = nearest_index(series.times, rel_time + self._t0)
        if index is None:
            return None
        return series.values[index]

    def _set_cell(self, row: int, col: int, text: str):
        item = self.signal_table.item(row, col)
        if item is None:
            return
        if item.text() != text:
            item.setText(text)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)

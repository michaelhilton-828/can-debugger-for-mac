"""The Log Replay tab: decode a CAN log against a .dbc and plot signals.

Ports the decode/plot logic that used to live in the standalone
can-log-viewer script (plot_can.py), but replaces its input()/print()
prompts with Qt widgets and its blocking plt.show() window with a plot
embedded directly in the tab via matplotlib's Qt canvas.
"""

from __future__ import annotations

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
from .log_replay_model import LogLoadError, enum_ticks, load_log, supported_log_extensions

SIGNAL_HEADERS = ["Message", "Signal", "Unit", "ID (hex)", "Samples"]


class LogReplayTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.database = None
        self._browser_database = None
        self._browser_database_path = ""
        self._independent_dbc_path = ""
        self._using_independent_dbc = False

        self._signals = {}
        self._row_keys = []

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
        self.dbc_use_browser_button = QPushButton("Use Browse tab's DBC")
        self.dbc_use_browser_button.clicked.connect(self._use_browser_dbc)
        self.dbc_use_browser_button.setEnabled(False)
        dbc_row.addWidget(self.dbc_label, 1)
        dbc_row.addWidget(self.dbc_open_button)
        dbc_row.addWidget(self.dbc_use_browser_button)
        root.addLayout(dbc_row)

        log_row = QHBoxLayout()
        self.log_open_button = QPushButton("Open log…")
        self.log_open_button.clicked.connect(self._open_log_dialog)
        self.log_path_label = QLabel("No log loaded")
        self.log_path_label.setStyleSheet("color: #555;")
        log_row.addWidget(self.log_open_button)
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
        self.overlay_checkbox = QCheckBox("Overlay on one plot")
        self.overlay_checkbox.setChecked(True)
        self.overlay_checkbox.stateChanged.connect(self._redraw)
        right_layout.addWidget(self.overlay_checkbox)

        self.figure = Figure()
        self.canvas = FigureCanvasQTAgg(self.figure)
        right_layout.addWidget(self.canvas, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

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
            if self._signals:
                self.status_label.setText(
                    self.status_label.text() + "  (DBC changed - reopen the log to re-decode)"
                )
        self._update_dbc_label()

    def _update_dbc_label(self):
        if self.database is None:
            self.dbc_label.setText("No DBC loaded")
        elif self._using_independent_dbc:
            self.dbc_label.setText(f"{self._independent_dbc_path} (independent)")
        else:
            self.dbc_label.setText(f"{self._browser_database_path} (from Browse tab)")

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
        self._update_dbc_label()

    def _use_browser_dbc(self):
        self._using_independent_dbc = False
        self.database = self._browser_database
        self.dbc_use_browser_button.setEnabled(False)
        self._update_dbc_label()

    # -------------------------------------------------------------- log

    def _open_log_dialog(self):
        exts = supported_log_extensions()
        pattern = " ".join(f"*{ext}" for ext in exts)
        filter_str = f"CAN log files ({pattern});;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Open log file", "", filter_str)
        if path:
            self._load_log(path)

    def _load_log(self, path: str):
        if self.database is None:
            QMessageBox.warning(self, "No DBC loaded", "Load a .dbc file before opening a log.")
            return

        try:
            signals, unmapped_ids = load_log(path, self.database)
        except LogLoadError as exc:
            QMessageBox.critical(self, "Failed to load log file", str(exc))
            return

        self.log_path_label.setText(path)
        self._signals = signals
        self._row_keys = sorted(
            signals.keys(), key=lambda k: (signals[k].arbitration_id, k[0], k[1])
        )
        self._populate_signal_table()

        message_count = len({s.message_name for s in signals.values()})
        status = f"Decoded {len(signals)} signal(s) from {message_count} message(s)."
        if unmapped_ids:
            status += f" {len(unmapped_ids)} unmapped ID(s) skipped."
        self.status_label.setText(status)
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
        self.figure.clear()
        selected = self._selected_series()
        if not selected:
            self.canvas.draw_idle()
            return

        overlay = self.overlay_checkbox.isChecked() or len(selected) == 1
        t0 = min(min(s.times) for s in selected)

        if overlay:
            ax = self.figure.add_subplot(111)
            for s in selected:
                t = [x - t0 for x in s.times]
                ax.plot(t, s.values, marker=".", label=s.display_label)
                positions, labels = enum_ticks(s.choices)
                if positions:
                    ax.set_yticks(positions)
                    ax.set_yticklabels(labels)
            ax.set_xlabel("Time (s)")
            ax.legend()
            ax.grid(True)
        else:
            axes = self.figure.subplots(len(selected), 1, sharex=True)
            for ax, s in zip(axes, selected):
                t = [x - t0 for x in s.times]
                ax.plot(t, s.values, marker=".")
                ax.set_ylabel(s.display_label)
                positions, labels = enum_ticks(s.choices)
                if positions:
                    ax.set_yticks(positions)
                    ax.set_yticklabels(labels)
                ax.grid(True)
            axes[-1].set_xlabel("Time (s)")

        self.figure.tight_layout()
        self.canvas.draw_idle()

"""The Log Replay tab: decode a CAN log against up to two .dbc files and
plot signals.

Ports the decode/plot logic that used to live in the standalone
can-log-viewer script (plot_can.py), but replaces its input()/print()
prompts with Qt widgets and its blocking plt.show() window with a plot
embedded directly in the tab via matplotlib's Qt canvas.

Two DBC slots rather than one, because a bus is commonly described by a
separate .dbc per sending node. Slot 1 defaults to whatever the DBC Viewer
tab has open; slot 2 is always picked here.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
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

# "DBC" is last so it inherits the header's stretch (it holds the longest
# text) and so the table's default sort column stays "Message" as before.
SIGNAL_HEADERS = ["Message", "Signal", "Unit", "ID (hex)", "Samples", "DBC"]
SLOT_COUNT = 2
STALE_NOTE = "  (DBC changed - reopen the log to re-decode)"


@dataclass
class _DbcSlot:
    """One DBC slot: the loaded database, where it came from, and whether it
    is tracking the DBC Viewer tab rather than a file picked here."""

    database: object = None
    path: str = ""
    from_browser: bool = False


class LogReplayTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Slot 1 follows the DBC Viewer tab until the user opens a file into it.
        self._slots = [_DbcSlot(from_browser=(i == 0)) for i in range(SLOT_COUNT)]
        self._browser_database = None
        self._browser_database_path = ""

        self._signals = {}
        self._row_keys = []

        self._build_ui()
        self._update_dbc_labels()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)

        self.dbc_labels = []
        self.dbc_clear_buttons = []
        for index in range(SLOT_COUNT):
            dbc_row = QHBoxLayout()
            dbc_row.addWidget(QLabel(f"DBC {index + 1}:"))

            path_label = QLabel("empty")
            path_label.setStyleSheet("color: #555;")
            dbc_row.addWidget(path_label, 1)
            self.dbc_labels.append(path_label)

            open_button = QPushButton("Open .dbc…")
            open_button.clicked.connect(lambda _checked=False, i=index: self._open_dbc_dialog(i))
            dbc_row.addWidget(open_button)

            clear_button = QPushButton("Clear")
            clear_button.clicked.connect(lambda _checked=False, i=index: self._clear_slot(i))
            clear_button.setEnabled(False)
            dbc_row.addWidget(clear_button)
            self.dbc_clear_buttons.append(clear_button)

            if index == 0:
                self.dbc_use_browser_button = QPushButton("Use DBC Viewer tab's DBC")
                self.dbc_use_browser_button.clicked.connect(self._use_browser_dbc)
                self.dbc_use_browser_button.setEnabled(False)
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
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        right_layout.addWidget(self.toolbar)
        right_layout.addWidget(self.canvas, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

    # -------------------------------------------------------------- DBC sync

    def set_browser_database(self, database, path: str) -> None:
        """Slot for BrowserTab.database_loaded. Updates DBC slot 1 unless the
        user has opened an independent file into it. Never re-decodes an
        already-loaded log on its own - that could be an expensive silent
        operation, so we just flag the log as possibly stale and let the user
        re-open it if they want a fresh decode.
        """
        self._browser_database = database
        self._browser_database_path = path
        slot = self._slots[0]
        if slot.from_browser:
            slot.database = database
            slot.path = path
            self._mark_log_stale()
        self._refresh_dbc_controls()

    def _dbc_sources(self) -> list[tuple[str, object]]:
        """The (label, database) pairs to decode against, one per slot and in
        slot order. Empty slots are kept as (label, None) so an empty slot 1
        does not renumber slot 2."""
        return [
            (f"{i + 1}: {os.path.basename(slot.path)}" if slot.database is not None else "", slot.database)
            for i, slot in enumerate(self._slots)
        ]

    def _loaded_slot_count(self) -> int:
        return sum(1 for slot in self._slots if slot.database is not None)

    def _mark_log_stale(self):
        """Note on the status line that the decoded log no longer matches the
        loaded DBCs. Idempotent, so repeated DBC changes don't stack up."""
        if self._signals and STALE_NOTE not in self.status_label.text():
            self.status_label.setText(self.status_label.text() + STALE_NOTE)

    def _refresh_dbc_controls(self):
        self._update_dbc_labels()
        for index, slot in enumerate(self._slots):
            self.dbc_clear_buttons[index].setEnabled(slot.database is not None)
        self.dbc_use_browser_button.setEnabled(
            self._browser_database is not None and not self._slots[0].from_browser
        )

    def _update_dbc_labels(self):
        for index, slot in enumerate(self._slots):
            if slot.database is None:
                text = "empty"
            elif slot.from_browser:
                text = f"{slot.path} (from DBC Viewer tab)"
            else:
                text = slot.path
            self.dbc_labels[index].setText(text)

    def _open_dbc_dialog(self, index: int):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Open .dbc file for DBC {index + 1}", "", "CAN database (*.dbc);;All files (*)"
        )
        if not path:
            return
        try:
            database = load_database(path)
        except DbcLoadError as exc:
            QMessageBox.critical(self, "Failed to load .dbc file", str(exc))
            return

        self._slots[index] = _DbcSlot(database=database, path=path, from_browser=False)
        self._mark_log_stale()
        self._refresh_dbc_controls()

    def _clear_slot(self, index: int):
        self._slots[index] = _DbcSlot()
        self._mark_log_stale()
        self._refresh_dbc_controls()

    def _use_browser_dbc(self):
        self._slots[0] = _DbcSlot(
            database=self._browser_database,
            path=self._browser_database_path,
            from_browser=True,
        )
        self._mark_log_stale()
        self._refresh_dbc_controls()

    # -------------------------------------------------------------- log

    def _open_log_dialog(self):
        exts = supported_log_extensions()
        pattern = " ".join(f"*{ext}" for ext in exts)
        filter_str = f"CAN log files ({pattern});;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Open log file", "", filter_str)
        if path:
            self._load_log(path)

    def _load_log(self, path: str):
        if self._loaded_slot_count() == 0:
            QMessageBox.warning(
                self, "No DBC loaded", "Load at least one .dbc file before opening a log."
            )
            return

        sources = self._dbc_sources()
        try:
            signals, unmapped_ids, conflicts = load_log(path, sources)
        except LogLoadError as exc:
            QMessageBox.critical(self, "Failed to load log file", str(exc))
            return

        self.log_path_label.setText(path)
        self._signals = signals
        self._row_keys = sorted(
            signals.keys(),
            key=lambda k: (
                signals[k].arbitration_id,
                signals[k].slot,
                signals[k].message_name,
                signals[k].signal_name,
            ),
        )
        self._populate_signal_table()
        self.status_label.setText(self._decode_summary(signals, unmapped_ids, conflicts, sources))
        self._redraw()

    def _decode_summary(self, signals, unmapped_ids, conflicts, sources) -> str:
        decoded = [s for s in signals.values() if s.slot]
        message_count = len({(s.slot, s.message_name) for s in decoded})
        status = f"Decoded {len(decoded)} signal(s) from {message_count} message(s)"

        # Only break the count down per DBC when there is more than one to
        # attribute it to.
        if self._loaded_slot_count() > 1:
            per_slot = Counter(s.slot for s in decoded)
            breakdown = ", ".join(
                f"DBC {i + 1}: {per_slot.get(i + 1, 0)}"
                for i, (_label, database) in enumerate(sources)
                if database is not None
            )
            status += f" ({breakdown})"
        status += "."

        if unmapped_ids:
            status += f" {len(unmapped_ids)} unmapped ID(s) shown as \"Unknown\"."
        if conflicts:
            ids = ", ".join(f"0x{i:X}" for i in sorted(conflicts))
            status += (
                f"  ⚠ {len(conflicts)} ID(s) defined differently by both DBCs, "
                f"decoded under each: {ids}."
            )
        return status

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
                series.source or "—",
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
            series = self._signals[self._row_keys[row_idx]]
            haystack = f"{series.message_name} {series.signal_name} {series.source}".lower()
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

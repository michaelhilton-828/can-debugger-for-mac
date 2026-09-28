"""The Live Signal Viewer tab: connect to a PEAK PCAN adapter over USB and
plot live CAN/CAN-FD signals against a loaded .dbc in real time.

Connection config and DBC-sharing follow the same conventions as
log_replay.py's LogReplayTab. The two things genuinely new here: a
background QThread (LiveCaptureWorker) receives frames off the GUI thread,
and the plot is redrawn on a fixed-rate QTimer rather than per selection
change, since data arrives continuously rather than being decoded once.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import QItemSelectionModel, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dbc_model import DbcLoadError, format_id_hex, load_database
from .live_capture_model import BusConfig, DecodeDatabase, LiveSignalBuffer, build_bus_kwargs
from .live_capture_worker import LiveCaptureWorker
from .log_replay_model import enum_ticks

SIGNAL_HEADERS = ["DBC", "Message", "Signal", "Unit", "ID (hex)", "Samples"]
SAMPLES_COL = SIGNAL_HEADERS.index("Samples")
CLASSIC_BITRATES = [125_000, 250_000, 500_000, 1_000_000]
REDRAW_INTERVAL_MS = 100  # 10 Hz
MAX_DBCS = 5


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


@dataclass
class _LoadedDbc:
    database: object
    path: str
    follows_viewer: bool = False

    @property
    def label(self) -> str:
        return Path(self.path).name

    @property
    def display(self) -> str:
        if self.follows_viewer:
            return f"{self.label}  (from DBC Viewer)"
        return self.label


class LiveSignalViewerTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._dbcs: list[_LoadedDbc] = []
        self._browser_database = None
        self._browser_database_path = ""
        self._follow_viewer = True

        self._buffers: dict[tuple[str, str], LiveSignalBuffer] = {}
        self._row_keys: list[tuple[str, str]] = []
        self._worker: LiveCaptureWorker | None = None
        self._connect_t0 = 0.0
        self._paused = False
        self._paused_now = 0.0
        self._frame_count = 0
        self._error_count = 0
        self._unmapped_count = 0
        self._session_active = False

        self._build_ui()
        self._refresh_dbc_list()
        self._on_mode_changed(self.mode_combo.currentIndex())

        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(REDRAW_INTERVAL_MS)
        self._redraw_timer.timeout.connect(self._redraw)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)

        dbc_row = QHBoxLayout()
        self.dbc_count_label = QLabel(f"DBC files (0/{MAX_DBCS})")
        self.add_dbc_button = QPushButton("Add another DBC…")
        self.add_dbc_button.setToolTip(f"Add a DBC file. At most {MAX_DBCS} can be loaded.")
        self.add_dbc_button.clicked.connect(self._add_dbc_dialog)
        self.remove_dbc_button = QPushButton("Remove")
        self.remove_dbc_button.clicked.connect(self._remove_selected_dbc)
        self.dbc_use_browser_button = QPushButton("Use DBC Viewer tab's DBC")
        self.dbc_use_browser_button.clicked.connect(self._use_browser_dbc)
        self.dbc_use_browser_button.setEnabled(False)
        dbc_row.addWidget(self.dbc_count_label)
        dbc_row.addStretch(1)
        dbc_row.addWidget(self.add_dbc_button)
        dbc_row.addWidget(self.remove_dbc_button)
        dbc_row.addWidget(self.dbc_use_browser_button)
        root.addLayout(dbc_row)

        self.dbc_list = QListWidget()
        self.dbc_list.setMaximumHeight(96)
        self.dbc_list.setToolTip(
            "Loaded DBC files, up to 5. If two files define the same CAN ID, "
            "the one higher in this list is used to decode it."
        )
        self.dbc_list.currentRowChanged.connect(self._on_dbc_row_changed)
        root.addWidget(self.dbc_list)

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Channel:"))
        self.channel_edit = QLineEdit("PCAN_USBBUS1")
        self.channel_edit.setMaximumWidth(140)
        conn_row.addWidget(self.channel_edit)

        conn_row.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Classic CAN", "CAN FD"])
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        conn_row.addWidget(self.mode_combo)

        self.classic_bitrate_combo = QComboBox()
        for rate in CLASSIC_BITRATES:
            self.classic_bitrate_combo.addItem(f"{rate:,} bit/s", rate)
        self.classic_bitrate_combo.setCurrentIndex(CLASSIC_BITRATES.index(500_000))
        conn_row.addWidget(self.classic_bitrate_combo)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._on_disconnect_clicked)
        self.disconnect_button.setEnabled(False)
        conn_row.addWidget(self.connect_button)
        conn_row.addWidget(self.disconnect_button)
        conn_row.addStretch(1)
        root.addLayout(conn_row)

        self.fd_nom_bitrate_spin = self._make_bitrate_spin(500_000)
        self.fd_data_bitrate_spin = self._make_bitrate_spin(2_000_000)
        self.fd_nom_sp_spin = self._make_sample_point_spin(80.0)
        self.fd_data_sp_spin = self._make_sample_point_spin(80.0)
        self.fd_row = QWidget()
        fd_layout = QHBoxLayout(self.fd_row)
        fd_layout.setContentsMargins(0, 0, 0, 0)
        for label, widget in [
            ("Nom. bitrate:", self.fd_nom_bitrate_spin),
            ("Data bitrate:", self.fd_data_bitrate_spin),
            ("Nom. SP%:", self.fd_nom_sp_spin),
            ("Data SP%:", self.fd_data_sp_spin),
        ]:
            fd_layout.addWidget(QLabel(label))
            fd_layout.addWidget(widget)
        fd_layout.addStretch(1)
        root.addWidget(self.fd_row)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: #555;")
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(QLabel("Window (s):"))
        self.window_spin = QSpinBox()
        self.window_spin.setRange(1, 600)
        self.window_spin.setValue(30)
        status_row.addWidget(self.window_spin)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.setEnabled(False)
        self.pause_button.toggled.connect(self._on_pause_toggled)
        status_row.addWidget(self.pause_button)
        root.addLayout(status_row)

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
        left_layout.addWidget(self.signal_table, 1)
        splitter.addWidget(left)

        # --- right: plot options + embedded canvas -------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.overlay_checkbox = QCheckBox("Overlay on one plot")
        self.overlay_checkbox.setChecked(True)
        right_layout.addWidget(self.overlay_checkbox)

        self.figure = Figure()
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        right_layout.addWidget(self.toolbar)
        right_layout.addWidget(self.canvas, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

    @staticmethod
    def _make_bitrate_spin(default: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(1_000, 10_000_000)
        spin.setSingleStep(1_000)
        spin.setValue(default)
        return spin

    @staticmethod
    def _make_sample_point_spin(default: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(50.0, 95.0)
        spin.setSingleStep(1.0)
        spin.setValue(default)
        return spin

    def _on_mode_changed(self, index: int):
        is_fd = index == 1
        self.classic_bitrate_combo.setVisible(not is_fd)
        self.fd_row.setVisible(is_fd)

    # -------------------------------------------------------------- DBC sync

    def set_browser_database(self, database, path: str) -> None:
        """Slot for BrowserTab.database_loaded.

        The viewer file occupies one slot and stays current until the user
        removes it. A connection already in progress keeps the databases it
        started with; the list updates for the next Connect.
        """
        self._browser_database = database
        self._browser_database_path = path
        if self._follow_viewer and self._worker is None:
            self._upsert_viewer_slot()
        else:
            self._refresh_dbc_list()

    def _viewer_slot(self) -> _LoadedDbc | None:
        for slot in self._dbcs:
            if slot.follows_viewer or _same_path(slot.path, self._browser_database_path):
                return slot
        return None

    def _upsert_viewer_slot(self):
        if self._browser_database is None:
            self._refresh_dbc_list()
            return
        existing = self._viewer_slot()
        if existing is not None:
            existing.database = self._browser_database
            existing.path = self._browser_database_path
            existing.follows_viewer = True
            self._refresh_after_dbc_change()
            return
        if len(self._dbcs) >= MAX_DBCS:
            self._refresh_dbc_list()
            return
        self._dbcs.insert(
            0,
            _LoadedDbc(self._browser_database, self._browser_database_path, follows_viewer=True),
        )
        self._refresh_after_dbc_change()

    def _add_dbc_dialog(self):
        if len(self._dbcs) >= MAX_DBCS:
            QMessageBox.information(
                self,
                "DBC limit reached",
                f"A maximum of {MAX_DBCS} DBC files can be loaded. Remove one to add another.",
            )
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add DBC files", "", "CAN database (*.dbc);;All files (*)"
        )
        if not paths:
            return
        added = 0
        duplicates = 0
        stopped_early = False
        for path in paths:
            if len(self._dbcs) >= MAX_DBCS:
                stopped_early = True
                break
            if any(_same_path(slot.path, path) for slot in self._dbcs):
                duplicates += 1
                continue
            try:
                database = load_database(path)
            except DbcLoadError as exc:
                QMessageBox.critical(self, "Failed to load .dbc file", str(exc))
                continue
            self._dbcs.append(_LoadedDbc(database, path, follows_viewer=False))
            added += 1
        if stopped_early:
            QMessageBox.information(
                self,
                "DBC limit reached",
                f"Stopped at {MAX_DBCS} DBC files. Extra selections were not loaded.",
            )
        elif added == 0 and duplicates:
            QMessageBox.information(self, "Already loaded", "Those DBC files are already in the list.")
        if added:
            self._refresh_after_dbc_change()
        else:
            self._refresh_dbc_list()

    def _remove_selected_dbc(self):
        row = self.dbc_list.currentRow()
        if row < 0 or row >= len(self._dbcs):
            return
        removed = self._dbcs.pop(row)
        if removed.follows_viewer:
            self._follow_viewer = False
        self._refresh_after_dbc_change()

    def _use_browser_dbc(self):
        if self._browser_database is None:
            return
        if self._viewer_slot() is None and len(self._dbcs) >= MAX_DBCS:
            QMessageBox.information(
                self,
                "DBC limit reached",
                f"Remove a DBC file first. The maximum is {MAX_DBCS}.",
            )
            return
        self._follow_viewer = True
        self._upsert_viewer_slot()

    def _on_dbc_row_changed(self, _row: int):
        self.remove_dbc_button.setEnabled(self._worker is None and self.dbc_list.currentRow() >= 0)

    def _refresh_dbc_list(self):
        selected = self.dbc_list.currentRow()
        self.dbc_list.blockSignals(True)
        self.dbc_list.clear()
        for slot in self._dbcs:
            item = QListWidgetItem(slot.display)
            item.setToolTip(slot.path)
            self.dbc_list.addItem(item)
        if 0 <= selected < self.dbc_list.count():
            self.dbc_list.setCurrentRow(selected)
        self.dbc_list.blockSignals(False)
        self.dbc_count_label.setText(f"DBC files ({len(self._dbcs)}/{MAX_DBCS})")
        editable = self._worker is None
        self.add_dbc_button.setEnabled(editable and len(self._dbcs) < MAX_DBCS)
        self.remove_dbc_button.setEnabled(editable and self.dbc_list.currentRow() >= 0)
        viewer_missing = self._browser_database is not None and self._viewer_slot() is None
        self.dbc_use_browser_button.setEnabled(editable and viewer_missing)

    def _refresh_after_dbc_change(self):
        self._refresh_dbc_list()
        if self._worker is None:
            self._rebuild_signal_catalog()

    def _decode_databases(self) -> list[DecodeDatabase]:
        return [
            DecodeDatabase(source_id=slot.path, label=slot.label, database=slot.database)
            for slot in self._dbcs
        ]

    # ------------------------------------------------------------ connection

    def _read_bus_config(self) -> BusConfig:
        return BusConfig(
            channel=self.channel_edit.text().strip() or "PCAN_USBBUS1",
            fd=self.mode_combo.currentIndex() == 1,
            classic_bitrate=self.classic_bitrate_combo.currentData(),
            fd_nom_bitrate=self.fd_nom_bitrate_spin.value(),
            fd_data_bitrate=self.fd_data_bitrate_spin.value(),
            fd_nom_sample_point=self.fd_nom_sp_spin.value(),
            fd_data_sample_point=self.fd_data_sp_spin.value(),
        )

    def _on_connect_clicked(self):
        if not self._dbcs:
            QMessageBox.warning(self, "No DBC loaded", "Add a .dbc file before connecting.")
            return

        config = self._read_bus_config()
        bus_kwargs = {"interface": "pcan", **build_bus_kwargs(config)}

        self._clear_samples()
        self._frame_count = 0
        self._error_count = 0
        self._unmapped_count = 0
        self._session_active = True
        self.figure.clear()
        self.canvas.draw_idle()

        self._worker = LiveCaptureWorker(bus_kwargs, self._decode_databases())
        self._worker.connected.connect(self._on_connected)
        self._worker.samples_ready.connect(self._on_samples_ready)
        self._worker.traffic_counts.connect(self._on_traffic_counts)
        self._worker.error.connect(self._on_worker_error)
        self._set_ui_connecting()
        self._worker.start()

    def _on_connected(self):
        self._connect_t0 = time.monotonic()
        self.disconnect_button.setEnabled(True)
        self.pause_button.setEnabled(True)
        self._update_status(f"Connected: {self.channel_edit.text()}")
        self._redraw_timer.start()

    def _on_traffic_counts(self, frames: int, errors: int, unmapped: int):
        self._frame_count += frames
        self._error_count += errors
        self._unmapped_count += unmapped
        prefix = "Paused" if self._paused else f"Connected: {self.channel_edit.text()}"
        self._update_status(prefix)

    def _on_pause_toggled(self, checked: bool):
        self._paused = checked
        self.pause_button.setText("Resume" if checked else "Pause")
        if checked:
            self._paused_now = time.monotonic() - self._connect_t0
        prefix = "Paused" if checked else f"Connected: {self.channel_edit.text()}"
        self._update_status(prefix)

    def _update_status(self, prefix: str):
        if not self._session_active:
            self.status_label.setText(prefix)
            return
        self.status_label.setText(
            f"{prefix} — {self._frame_count} frames, "
            f"{self._error_count} errors, {self._unmapped_count} unmapped"
        )

    def _on_disconnect_clicked(self):
        self._stop_worker()
        self._update_status("Disconnected")

    def _on_worker_error(self, message: str):
        self._stop_worker()
        self._update_status("Disconnected (error)")
        QMessageBox.critical(self, "Connection error", message)

    def _stop_worker(self):
        self._redraw_timer.stop()
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker.wait()
            self._worker.deleteLater()
            self._worker = None
        self._set_ui_disconnected()
        self._clear_pause()

    def shutdown(self):
        """Called from MainWindow.closeEvent so a connected session is torn
        down cleanly on app quit. No-op if never connected."""
        self._stop_worker()

    def _set_ui_connecting(self):
        self.connect_button.setEnabled(False)
        self.channel_edit.setEnabled(False)
        self.mode_combo.setEnabled(False)
        self.classic_bitrate_combo.setEnabled(False)
        self.fd_row.setEnabled(False)
        self.add_dbc_button.setEnabled(False)
        self.remove_dbc_button.setEnabled(False)
        self.dbc_use_browser_button.setEnabled(False)
        self.dbc_list.setEnabled(False)
        self.status_label.setText("Connecting…")

    def _set_ui_disconnected(self):
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.channel_edit.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.classic_bitrate_combo.setEnabled(True)
        self.fd_row.setEnabled(True)
        self.dbc_list.setEnabled(True)
        self._refresh_dbc_list()

    def _clear_pause(self):
        self._paused = False
        self.pause_button.blockSignals(True)
        self.pause_button.setChecked(False)
        self.pause_button.setText("Pause")
        self.pause_button.blockSignals(False)

    # ------------------------------------------------------------- signals

    def _rebuild_signal_catalog(self):
        """List every DBC signal immediately, with a zero sample count until traffic arrives."""
        selected = {buf.key for buf in self._selected_series()} if self._row_keys else set()
        self._buffers = {}
        self._row_keys = []
        self.signal_table.setSortingEnabled(False)
        self.signal_table.setRowCount(0)
        if not self._dbcs:
            self.signal_table.setSortingEnabled(True)
            return
        keys = []
        for slot in self._dbcs:
            for msg in slot.database.messages:
                for sig in msg.signals:
                    key = (slot.path, msg.name, sig.name)
                    if key in self._buffers:
                        continue
                    self._buffers[key] = LiveSignalBuffer(
                        message_name=msg.name,
                        signal_name=sig.name,
                        arbitration_id=msg.frame_id,
                        unit=sig.unit or "",
                        source_id=slot.path,
                        dbc_label=slot.label,
                        choices=sig.choices or {},
                    )
                    keys.append(key)
        self._append_signal_rows(keys)
        if selected:
            self._restore_selection(selected)

    def _restore_selection(self, keys: set[tuple[str, str]]):
        model = self.signal_table.selectionModel()
        if model is None:
            return
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        self.signal_table.blockSignals(True)
        model.clearSelection()
        for r in range(self.signal_table.rowCount()):
            item = self.signal_table.item(r, 0)
            if item is None:
                continue
            row_idx = item.data(Qt.UserRole + 1)
            if self._buffers[self._row_keys[row_idx]].key in keys:
                index = self.signal_table.model().index(r, 0)
                model.select(index, flags)
        self.signal_table.blockSignals(False)

    def _clear_samples(self):
        for buf in self._buffers.values():
            buf.times.clear()
            buf.values.clear()
        if self.signal_table.rowCount():
            self._update_sample_counts()

    def _on_samples_ready(self, samples: list) -> None:
        """Slot for worker.samples_ready. Only mutates buffers/table - never
        touches self.figure/self.canvas directly; the QTimer-driven _redraw
        is the sole place that happens, decoupling render rate from the
        rate samples actually arrive at."""
        new_keys = []
        for s in samples:
            key = s.key
            buf = self._buffers.get(key)
            if buf is None:
                buf = LiveSignalBuffer(
                    message_name=s.message_name,
                    signal_name=s.signal_name,
                    arbitration_id=s.arbitration_id,
                    unit=s.unit,
                    source_id=s.source_id,
                    dbc_label=s.dbc_label,
                    choices=s.choices,
                )
                self._buffers[key] = buf
                new_keys.append(key)
            buf.append(s.time, s.value)

        if new_keys:
            self._append_signal_rows(new_keys)

    def _append_signal_rows(self, new_keys: list[tuple[str, str]]) -> None:
        """Appends rows for newly-observed keys without disturbing existing
        selection/sort/scroll position - never clears/rebuilds the table
        while connected, unlike LogReplayTab's one-shot populate."""
        self.signal_table.setSortingEnabled(False)
        start_row = self.signal_table.rowCount()
        self.signal_table.setRowCount(start_row + len(new_keys))
        for i, key in enumerate(new_keys):
            row_idx = len(self._row_keys)
            self._row_keys.append(key)
            buf = self._buffers[key]
            values = [
                buf.dbc_label,
                buf.message_name,
                buf.signal_name,
                buf.unit,
                format_id_hex(buf.arbitration_id, buf.arbitration_id > 0x7FF),
                str(len(buf.times)),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole + 1, row_idx)
                self.signal_table.setItem(start_row + i, col, item)
        self.signal_table.setSortingEnabled(True)
        self._filter_signals(self.signal_filter.text())

    def _filter_signals(self, text: str):
        text = text.strip().lower()
        for r in range(self.signal_table.rowCount()):
            row_idx = self.signal_table.item(r, 0).data(Qt.UserRole + 1)
            buf = self._buffers[self._row_keys[row_idx]]
            haystack = f"{buf.dbc_label} {buf.message_name} {buf.signal_name}".lower()
            self.signal_table.setRowHidden(r, text not in haystack)

    def _selected_series(self):
        seen_rows = set()
        selected = []
        for item in self.signal_table.selectedItems():
            row_idx = item.data(Qt.UserRole + 1)
            if row_idx in seen_rows:
                continue
            seen_rows.add(row_idx)
            selected.append(self._buffers[self._row_keys[row_idx]])
        return selected

    def _update_sample_counts(self):
        for r in range(self.signal_table.rowCount()):
            item = self.signal_table.item(r, 0)
            count_item = self.signal_table.item(r, SAMPLES_COL)
            if item is None or count_item is None:
                continue
            row_idx = item.data(Qt.UserRole + 1)
            text = str(len(self._buffers[self._row_keys[row_idx]].times))
            if count_item.text() != text:
                count_item.setText(text)

    # --------------------------------------------------------------- plot

    def _redraw(self):
        now = self._paused_now if self._paused else time.monotonic() - self._connect_t0
        window_s = self.window_spin.value()
        if not self._paused:
            for buf in self._buffers.values():
                buf.trim(now, window_s)
        self._update_sample_counts()

        self.figure.clear()
        selected = [buf for buf in self._selected_series() if buf.times]
        if not selected:
            self.canvas.draw_idle()
            return

        overlay = self.overlay_checkbox.isChecked() or len(selected) == 1

        if overlay:
            ax = self.figure.add_subplot(111)
            for buf in selected:
                ax.plot(list(buf.times), list(buf.values), label=buf.display_label)
                positions, labels = enum_ticks(buf.choices)
                if positions:
                    ax.set_yticks(positions)
                    ax.set_yticklabels(labels)
            ax.set_xlabel("Time (s, since connect)")
            ax.legend()
            ax.grid(True)
            primary = ax
        else:
            axes = self.figure.subplots(len(selected), 1, sharex=True)
            if len(selected) == 1:
                axes = [axes]
            for ax, buf in zip(axes, selected):
                ax.plot(list(buf.times), list(buf.values))
                ax.set_ylabel(buf.display_label)
                positions, labels = enum_ticks(buf.choices)
                if positions:
                    ax.set_yticks(positions)
                    ax.set_yticklabels(labels)
                ax.grid(True)
            axes[-1].set_xlabel("Time (s, since connect)")
            primary = axes[-1]

        self.figure.tight_layout()
        if self._paused:
            primary.set_xlim(self._paused_now - window_s, self._paused_now)
        self.canvas.draw_idle()

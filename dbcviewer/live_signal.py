"""The Live Signal Viewer tab: connect to a PEAK PCAN adapter over USB and
plot live CAN/CAN-FD signals against a loaded .dbc in real time.

Connection config and DBC-sharing follow the same conventions as
log_replay.py's LogReplayTab. The two things genuinely new here: a
background QThread (LiveCaptureWorker) receives frames off the GUI thread,
and the plot is redrawn on a fixed-rate QTimer rather than per selection
change, since data arrives continuously rather than being decoded once.
"""

from __future__ import annotations

import time

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, QTimer
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
from .live_capture_model import BusConfig, LiveSignalBuffer, build_bus_kwargs
from .live_capture_worker import LiveCaptureWorker
from .log_replay_model import enum_ticks

SIGNAL_HEADERS = ["Message", "Signal", "Unit", "ID (hex)", "Samples"]
CLASSIC_BITRATES = [125_000, 250_000, 500_000, 1_000_000]
REDRAW_INTERVAL_MS = 100  # 10 Hz


class LiveSignalViewerTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.database = None
        self._browser_database = None
        self._browser_database_path = ""
        self._independent_dbc_path = ""
        self._using_independent_dbc = False

        self._buffers: dict[tuple[str, str], LiveSignalBuffer] = {}
        self._row_keys: list[tuple[str, str]] = []
        self._worker: LiveCaptureWorker | None = None
        self._connect_t0 = 0.0

        self._build_ui()
        self._update_dbc_label()
        self._on_mode_changed(self.mode_combo.currentIndex())

        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(REDRAW_INTERVAL_MS)
        self._redraw_timer.timeout.connect(self._redraw)

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

        self.fd_nom_bitrate_spin = self._make_bitrate_spin(500_000)
        self.fd_data_bitrate_spin = self._make_bitrate_spin(2_000_000)
        self.fd_nom_sp_spin = self._make_sample_point_spin(80.0)
        self.fd_data_sp_spin = self._make_sample_point_spin(80.0)
        for label, widget in [
            ("Nom. bitrate:", self.fd_nom_bitrate_spin),
            ("Data bitrate:", self.fd_data_bitrate_spin),
            ("Nom. SP%:", self.fd_nom_sp_spin),
            ("Data SP%:", self.fd_data_sp_spin),
        ]:
            conn_row.addWidget(QLabel(label))
            conn_row.addWidget(widget)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._on_disconnect_clicked)
        self.disconnect_button.setEnabled(False)
        conn_row.addWidget(self.connect_button)
        conn_row.addWidget(self.disconnect_button)
        root.addLayout(conn_row)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: #555;")
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(QLabel("Window (s):"))
        self.window_spin = QSpinBox()
        self.window_spin.setRange(1, 600)
        self.window_spin.setValue(30)
        status_row.addWidget(self.window_spin)
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
        for widget in (
            self.fd_nom_bitrate_spin,
            self.fd_data_bitrate_spin,
            self.fd_nom_sp_spin,
            self.fd_data_sp_spin,
        ):
            widget.setVisible(is_fd)

    # -------------------------------------------------------------- DBC sync

    def set_browser_database(self, database, path: str) -> None:
        """Slot for BrowserTab.database_loaded - identical logic to
        LogReplayTab.set_browser_database. Never affects a session already
        connected; a new connection always uses whatever DBC is current
        when Connect is clicked."""
        self._browser_database = database
        self._browser_database_path = path
        self.dbc_use_browser_button.setEnabled(self._using_independent_dbc)
        if not self._using_independent_dbc:
            self.database = database
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
        self._update_dbc_label()

    def _use_browser_dbc(self):
        self._using_independent_dbc = False
        self.database = self._browser_database
        self.dbc_use_browser_button.setEnabled(False)
        self._update_dbc_label()

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
        if self.database is None:
            QMessageBox.warning(self, "No DBC loaded", "Load a .dbc file before connecting.")
            return

        config = self._read_bus_config()
        bus_kwargs = {"interface": "pcan", **build_bus_kwargs(config)}

        self._buffers = {}
        self._row_keys = []
        self.signal_table.setRowCount(0)
        self.figure.clear()
        self.canvas.draw_idle()

        self._worker = LiveCaptureWorker(bus_kwargs, self.database)
        self._worker.connected.connect(self._on_connected)
        self._worker.samples_ready.connect(self._on_samples_ready)
        self._worker.error.connect(self._on_worker_error)
        self._set_ui_connecting()
        self._worker.start()

    def _on_connected(self):
        self._connect_t0 = time.monotonic()
        self.status_label.setText(f"Connected: {self.channel_edit.text()}")
        self.disconnect_button.setEnabled(True)
        self._redraw_timer.start()

    def _on_disconnect_clicked(self):
        self._stop_worker()
        self.status_label.setText("Disconnected")

    def _on_worker_error(self, message: str):
        self._stop_worker()
        self.status_label.setText("Disconnected (error)")
        QMessageBox.critical(self, "Connection error", message)

    def _stop_worker(self):
        self._redraw_timer.stop()
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker.wait()
            self._worker.deleteLater()
            self._worker = None
        self._set_ui_disconnected()

    def shutdown(self):
        """Called from MainWindow.closeEvent so a connected session is torn
        down cleanly on app quit. No-op if never connected."""
        self._stop_worker()

    def _set_ui_connecting(self):
        self.connect_button.setEnabled(False)
        self.channel_edit.setEnabled(False)
        self.mode_combo.setEnabled(False)
        self.classic_bitrate_combo.setEnabled(False)
        self.fd_nom_bitrate_spin.setEnabled(False)
        self.fd_data_bitrate_spin.setEnabled(False)
        self.fd_nom_sp_spin.setEnabled(False)
        self.fd_data_sp_spin.setEnabled(False)
        self.status_label.setText("Connecting…")

    def _set_ui_disconnected(self):
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.channel_edit.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.classic_bitrate_combo.setEnabled(True)
        self.fd_nom_bitrate_spin.setEnabled(True)
        self.fd_data_bitrate_spin.setEnabled(True)
        self.fd_nom_sp_spin.setEnabled(True)
        self.fd_data_sp_spin.setEnabled(True)

    # ------------------------------------------------------------- signals

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
            selected.append(self._buffers[self._row_keys[row_idx]])
        return selected

    def _update_sample_counts(self):
        for r in range(self.signal_table.rowCount()):
            row_idx = self.signal_table.item(r, 0).data(Qt.UserRole + 1)
            buf = self._buffers[self._row_keys[row_idx]]
            self.signal_table.item(r, 4).setText(str(len(buf.times)))

    # --------------------------------------------------------------- plot

    def _redraw(self):
        now = time.monotonic() - self._connect_t0
        window_s = self.window_spin.value()
        for buf in self._buffers.values():
            buf.trim(now, window_s)
        self._update_sample_counts()

        self.figure.clear()
        selected = self._selected_series()
        if not selected:
            self.canvas.draw_idle()
            return

        overlay = self.overlay_checkbox.isChecked() or len(selected) == 1

        if overlay:
            ax = self.figure.add_subplot(111)
            for buf in selected:
                ax.plot(list(buf.times), list(buf.values), marker=".", label=buf.display_label)
                positions, labels = enum_ticks(buf.choices)
                if positions:
                    ax.set_yticks(positions)
                    ax.set_yticklabels(labels)
            ax.set_xlabel("Time (s, since connect)")
            ax.legend()
            ax.grid(True)
        else:
            axes = self.figure.subplots(len(selected), 1, sharex=True)
            for ax, buf in zip(axes, selected):
                ax.plot(list(buf.times), list(buf.values), marker=".")
                ax.set_ylabel(buf.display_label)
                positions, labels = enum_ticks(buf.choices)
                if positions:
                    ax.set_yticks(positions)
                    ax.set_yticklabels(labels)
                ax.grid(True)
            axes[-1].set_xlabel("Time (s, since connect)")

        self.figure.tight_layout()
        self.canvas.draw_idle()

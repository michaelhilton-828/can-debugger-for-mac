"""The Live Signal Viewer tab: connect to a PEAK PCAN adapter over USB and
plot live CAN/CAN-FD signals against a loaded .dbc in real time.

Every received arbitration ID is listed in the frames table, with or
without a DBC. Decoded signals still need a DBC that defines the ID.

Decode uses the Home tab's DBC list, in that order. A connection keeps the
databases it started with until disconnect. The two things genuinely new
here: a background QThread (LiveCaptureWorker) receives frames off the GUI
thread, and the plot is redrawn on a fixed-rate QTimer rather than per
selection change, since data arrives continuously rather than being decoded
once.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import QEvent, QItemSelectionModel, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
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

from .blf_log import BlfLogSession
from .dbc_library import DbcLibrary
from .dbc_model import format_id_hex
from .live_capture_model import (
    BusConfig,
    DecodeDatabase,
    LiveSignalBuffer,
    build_bus_kwargs,
    frame_type_label,
)
from .table_columns import install_column_config
from .ui_state import remember_splitter
from .live_capture_worker import LiveCaptureWorker
from .log_replay_model import enum_ticks
from .theme import STEEL, set_muted

SIGNAL_HEADERS = ["DBC", "Message", "Signal", "Unit", "ID (hex)", "Samples"]
SAMPLES_COL = SIGNAL_HEADERS.index("Samples")
FRAME_HEADERS = ["ID (hex)", "Message", "Type", "Len", "Count", "Hz", "Data"]
FRAME_ID_COL = 0
FRAME_MESSAGE_COL = 1
FRAME_TYPE_COL = 2
FRAME_LEN_COL = 3
FRAME_COUNT_COL = 4
FRAME_HZ_COL = 5
FRAME_DATA_COL = 6
CLASSIC_BITRATES = [125_000, 250_000, 500_000, 1_000_000]
REDRAW_INTERVAL_MS = 100  # 10 Hz


class _SortItem(QTableWidgetItem):
    """Sorts by the value stored in UserRole when both sides have one."""

    def __lt__(self, other):
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole) if isinstance(other, QTableWidgetItem) else None
        if left is not None and right is not None and type(left) is type(right):
            return left < right
        return super().__lt__(other)


@dataclass
class _LiveFrame:
    """One row in the live frames table, accumulated across receive batches."""

    arbitration_id: int
    is_extended: bool
    is_fd: bool
    bitrate_switch: bool
    dlc: int
    data: bytes
    count: int
    first_time: float
    last_time: float
    is_error: bool
    is_remote: bool
    message_name: str

    def hz(self) -> float | None:
        span = self.last_time - self.first_time
        if self.count < 2 or span <= 0:
            return None
        return (self.count - 1) / span


class LiveSignalViewerTab(QWidget):
    def __init__(self, library: DbcLibrary, parent=None):
        super().__init__(parent)
        self.library = library
        self._catalog_key: tuple[str, ...] | None = None
        self._capture_entries = None
        self._capture_key: tuple[str, ...] = ()

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
        self._frames: dict[int, _LiveFrame] = {}
        self._logging = False
        self._log_path = ""
        self._log_session: BlfLogSession | None = None

        self._build_ui()
        self._install_space_filter()
        self.library.changed.connect(self._on_library_changed)
        self._on_library_changed()
        self._on_mode_changed(self.mode_combo.currentIndex())

        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(REDRAW_INTERVAL_MS)
        self._redraw_timer.timeout.connect(self._redraw)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)

        self.dbc_label = QLabel("No DBCs loaded on Home")
        set_muted(self.dbc_label)
        root.addWidget(self.dbc_label)

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Channel:"))
        self.channel_edit = QLineEdit("PCAN_USBBUS1")
        self.channel_edit.setMaximumWidth(140)
        conn_row.addWidget(self.channel_edit)

        conn_row.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Classic CAN", "CAN FD"])
        self.mode_combo.setCurrentIndex(1)
        self.mode_combo.setToolTip(
            "CAN FD uses the nominal and data bitrates on the next row. "
            "500 kbit/s nominal and 2 Mbit/s data is the default."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        conn_row.addWidget(self.mode_combo)

        self.classic_bitrate_combo = QComboBox()
        for rate in CLASSIC_BITRATES:
            self.classic_bitrate_combo.addItem(f"{rate:,} bit/s", rate)
        self.classic_bitrate_combo.setCurrentIndex(CLASSIC_BITRATES.index(500_000))
        conn_row.addWidget(self.classic_bitrate_combo)

        self.connect_button = QPushButton("Connect")
        self.connect_button.setToolTip("Start the live connection. Space does this when a text field is not focused.")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setToolTip("Stop the live connection. Space does this when a text field is not focused.")
        self.disconnect_button.clicked.connect(self._on_disconnect_clicked)
        self.disconnect_button.setEnabled(False)
        conn_row.addWidget(self.connect_button)
        conn_row.addWidget(self.disconnect_button)
        self.log_button = QPushButton("Start log…")
        self.log_button.setToolTip("Record every received frame to a Vector .blf file.")
        self.log_button.clicked.connect(self._on_log_clicked)
        conn_row.addWidget(self.log_button)
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
        set_muted(self.status_label)
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
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip(
            "Clear the frames table, signal history, and plot. The connection stays open."
        )
        self.clear_button.clicked.connect(self._on_clear_clicked)
        status_row.addWidget(self.clear_button)
        root.addLayout(status_row)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # --- left: received frames, then decoded signals -------------------
        left = QSplitter(Qt.Vertical)
        left.setChildrenCollapsible(False)

        frames_panel = QWidget()
        frames_layout = QVBoxLayout(frames_panel)
        frames_layout.setContentsMargins(0, 0, 0, 0)
        frames_layout.addWidget(QLabel("Frames"))
        self.frame_filter = QLineEdit()
        self.frame_filter.setPlaceholderText("Filter frames by ID, message, or data…")
        self.frame_filter.textChanged.connect(self._filter_frames)
        frames_layout.addWidget(self.frame_filter)
        self.frame_table = QTableWidget(0, len(FRAME_HEADERS))
        self.frame_table.setHorizontalHeaderLabels(FRAME_HEADERS)
        self.frame_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.frame_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.frame_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.frame_table.setTextElideMode(Qt.ElideRight)
        frame_header = self.frame_table.horizontalHeader()
        frame_header.setStretchLastSection(True)
        self.frame_table.setSortingEnabled(True)
        install_column_config(self.frame_table, "live.frames")
        frame_header.setSectionResizeMode(QHeaderView.Interactive)
        frame_header.setSectionResizeMode(FRAME_DATA_COL, QHeaderView.Stretch)
        frames_layout.addWidget(self.frame_table, 1)
        left.addWidget(frames_panel)

        signals_panel = QWidget()
        signals_layout = QVBoxLayout(signals_panel)
        signals_layout.setContentsMargins(0, 0, 0, 0)
        self.signal_filter = QLineEdit()
        self.signal_filter.setPlaceholderText("Filter signals by name…")
        self.signal_filter.textChanged.connect(self._filter_signals)
        signals_layout.addWidget(self.signal_filter)

        self.signal_table = QTableWidget(0, len(SIGNAL_HEADERS))
        self.signal_table.setHorizontalHeaderLabels(SIGNAL_HEADERS)
        self.signal_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.signal_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.signal_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.signal_table.horizontalHeader().setStretchLastSection(True)
        self.signal_table.setSortingEnabled(True)
        install_column_config(self.signal_table, "live.signals")
        signal_header = self.signal_table.horizontalHeader()
        signal_header.setSectionResizeMode(QHeaderView.Interactive)
        signal_header.setSectionResizeMode(SAMPLES_COL, QHeaderView.Stretch)
        signals_layout.addWidget(self.signal_table, 1)
        left.addWidget(signals_panel)
        left.setStretchFactor(0, 1)
        left.setStretchFactor(1, 2)
        splitter.addWidget(left)

        # --- right: plot options + embedded canvas -------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.overlay_checkbox = QCheckBox("Overlay on one plot")
        self.overlay_checkbox.setChecked(True)
        right_layout.addWidget(self.overlay_checkbox)

        self.figure = Figure(facecolor="white")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        right_layout.addWidget(self.toolbar)
        right_layout.addWidget(self.canvas, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        remember_splitter(left, "live.frames-signals")
        remember_splitter(splitter, "live.table-plot")

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

    def _library_key(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.library.entries())

    def _active_entries(self):
        """Databases for decode: the ones Connect started with, while it runs."""
        if self._worker is not None and self._capture_entries is not None:
            return self._capture_entries
        return self.library.entries()

    def _on_library_changed(self):
        key = self._library_key()
        self._update_dbc_label()
        if self._worker is not None:
            return
        if key == self._catalog_key:
            return
        self._catalog_key = key
        self._rebuild_signal_catalog()

    def _update_dbc_label(self):
        entries = self.library.entries()
        if entries:
            text = "DBCs from Home: " + ", ".join(entry.label for entry in entries)
        else:
            text = "No DBCs loaded on Home"
        if self._worker is not None and self._library_key() != self._capture_key:
            text += " — still using the DBCs from connect"
        self.dbc_label.setText(text)
        self.dbc_label.setToolTip(text)

    def _decode_databases(self, entries=None) -> list[DecodeDatabase]:
        if entries is None:
            entries = self._active_entries()
        return [
            DecodeDatabase(source_id=entry.path, label=entry.label, database=entry.database)
            for entry in entries
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

    def _connection_label(self) -> str:
        channel = self.channel_edit.text().strip() or "PCAN_USBBUS1"
        if self.mode_combo.currentIndex() == 1:
            nom = self.fd_nom_bitrate_spin.value() / 1000
            data = self.fd_data_bitrate_spin.value() / 1000
            return f"{channel}, CAN FD {nom:.0f}/{data:.0f} kbit/s"
        rate = self.classic_bitrate_combo.currentData() / 1000
        return f"{channel}, classic {rate:.0f} kbit/s"

    def _install_space_filter(self):
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        """Space starts and stops the connection, except while typing."""
        if (
            event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_Space
            and not event.isAutoRepeat()
            and self.isVisible()
            and not isinstance(obj, (QLineEdit, QAbstractSpinBox, QComboBox))
        ):
            self._toggle_connection()
            return True
        return super().eventFilter(obj, event)

    def _toggle_connection(self):
        if self._worker is None:
            self._on_connect_clicked()
        else:
            self._on_disconnect_clicked()

    def _on_log_clicked(self):
        if self._logging:
            self._stop_log()
            self._update_status(
                f"Connected: {self._connection_label()}" if self._session_active else "Disconnected"
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Record BLF log", "", "Vector BLF (*.blf)"
        )
        if not path:
            return
        if not path.lower().endswith(".blf"):
            path += ".blf"
        self._log_path = path
        self._logging = True
        self.log_button.setText("Stop log")
        if self._worker is None:
            self._on_connect_clicked()
        else:
            self._attach_log()

    def _attach_log(self):
        """Open the chosen .blf and hand it to the running worker."""
        if not self._logging or not self._log_path or self._worker is None or self._log_session is not None:
            return
        try:
            session = BlfLogSession(self._log_path)
        except OSError as exc:
            self._logging = False
            self._log_path = ""
            self.log_button.setText("Start log…")
            QMessageBox.critical(self, "Could not start log", str(exc))
            return
        self._log_session = session
        self._worker.set_log_session(session)

    def _stop_log(self):
        self._logging = False
        self._log_path = ""
        session = self._log_session
        self._log_session = None
        if self._worker is not None:
            previous = self._worker.set_log_session(None)
            if previous is not None and previous is not session:
                previous.close()
        if session is not None:
            session.close()
        if hasattr(self, "log_button"):
            self.log_button.setText("Start log…")

    def _on_log_error(self, message: str):
        """The worker already closed the session and kept the bus open."""
        self._log_session = None
        self._logging = False
        self._log_path = ""
        self.log_button.setText("Start log…")
        QMessageBox.warning(self, "Log stopped", message)

    def _on_connect_clicked(self):
        config = self._read_bus_config()
        bus_kwargs = {"interface": "pcan", **build_bus_kwargs(config)}

        self._clear_samples()
        self._clear_frames()
        self._frame_count = 0
        self._error_count = 0
        self._unmapped_count = 0
        self._session_active = True
        self.figure.clear()
        self.canvas.draw_idle()

        entries = list(self.library.entries())
        self._capture_entries = entries
        self._capture_key = tuple(entry.path for entry in entries)
        self._worker = LiveCaptureWorker(bus_kwargs, self._decode_databases(entries))
        self._worker.connected.connect(self._on_connected)
        self._worker.samples_ready.connect(self._on_samples_ready)
        self._worker.frames_ready.connect(self._on_frames_ready)
        self._worker.traffic_counts.connect(self._on_traffic_counts)
        self._worker.error.connect(self._on_worker_error)
        self._worker.log_error.connect(self._on_log_error)
        self._set_ui_connecting()
        self._worker.start()

    def _on_connected(self):
        self._connect_t0 = time.monotonic()
        self.disconnect_button.setEnabled(True)
        self.pause_button.setEnabled(True)
        self._attach_log()
        self._update_status(f"Connected: {self._connection_label()}")
        self._redraw_timer.start()

    def _on_traffic_counts(self, frames: int, errors: int, unmapped: int):
        self._frame_count += frames
        self._error_count += errors
        self._unmapped_count += unmapped
        prefix = "Paused" if self._paused else f"Connected: {self._connection_label()}"
        self._update_status(prefix)

    def _on_pause_toggled(self, checked: bool):
        self._paused = checked
        self.pause_button.setText("Resume" if checked else "Pause")
        if checked:
            self._paused_now = time.monotonic() - self._connect_t0
        prefix = "Paused" if checked else f"Connected: {self._connection_label()}"
        self._update_status(prefix)

    def _on_clear_clicked(self):
        """Drop captured frames and signal history. Stay connected, and leave Pause as it is."""
        self._clear_samples()
        self._clear_frames()
        self._frame_count = 0
        self._error_count = 0
        self._unmapped_count = 0
        if self._session_active:
            self._update_status(self._status_prefix())
        self._redraw()

    def _status_prefix(self) -> str:
        if self._worker is not None:
            return "Paused" if self._paused else f"Connected: {self._connection_label()}"
        text = self.status_label.text()
        if " — " in text:
            return text.split(" — ", 1)[0]
        return text or "Disconnected"

    def _update_status(self, prefix: str):
        if not self._session_active:
            self.status_label.setText(prefix)
            return
        log_note = ""
        if self._log_session is not None:
            log_note = f", logging {self._log_session.count} to {Path(self._log_path).name}"
        self.status_label.setText(
            f"{prefix} — {self._frame_count} frames, "
            f"{self._error_count} errors, {self._unmapped_count} unmapped"
            f"{log_note}"
        )

    def _on_disconnect_clicked(self):
        self._stop_worker()
        self._update_status("Disconnected")

    def _on_worker_error(self, message: str):
        self._stop_worker()
        self._update_status("Disconnected (error)")
        QMessageBox.critical(self, "Connection error", message)

    def _stop_worker(self):
        self._stop_log()
        self._redraw_timer.stop()
        worker = self._worker
        self._worker = None
        self._capture_entries = None
        if worker is not None:
            worker.requestInterruption()
            worker.wait()
            worker.deleteLater()
        self._set_ui_disconnected()
        self._clear_pause()
        self._on_library_changed()

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
        self.status_label.setText("Connecting…")

    def _set_ui_disconnected(self):
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.channel_edit.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.classic_bitrate_combo.setEnabled(True)
        self.fd_row.setEnabled(True)

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
        entries = self.library.entries()
        if not entries:
            self.signal_table.setSortingEnabled(True)
            return
        keys = []
        for entry in entries:
            for msg in entry.database.messages:
                for sig in msg.signals:
                    key = (entry.path, msg.name, sig.name)
                    if key in self._buffers:
                        continue
                    self._buffers[key] = LiveSignalBuffer(
                        message_name=msg.name,
                        signal_name=sig.name,
                        arbitration_id=msg.frame_id,
                        unit=sig.unit or "",
                        source_id=entry.path,
                        dbc_label=entry.label,
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

    def _clear_frames(self):
        self._frames.clear()
        self.frame_table.setRowCount(0)

    def _message_name_for(self, arbitration_id: int) -> str:
        for entry in self._active_entries():
            try:
                return entry.database.get_message_by_frame_id(arbitration_id).name
            except (KeyError, AttributeError):
                continue
        return ""

    def _on_frames_ready(self, snaps: list) -> None:
        """Accumulate one batch of per-ID snapshots. The table is painted
        from _redraw so a busy bus does not rebuild rows on every flush."""
        now = time.monotonic() - self._connect_t0
        new_ids = []
        for snap in snaps:
            frame = self._frames.get(snap.arbitration_id)
            if frame is None:
                frame = _LiveFrame(
                    arbitration_id=snap.arbitration_id,
                    is_extended=snap.is_extended,
                    is_fd=snap.is_fd,
                    bitrate_switch=snap.bitrate_switch,
                    dlc=snap.dlc,
                    data=snap.data,
                    count=0,
                    first_time=now,
                    last_time=now,
                    is_error=snap.is_error,
                    is_remote=snap.is_remote,
                    message_name=self._message_name_for(snap.arbitration_id),
                )
                self._frames[snap.arbitration_id] = frame
                new_ids.append(snap.arbitration_id)
            frame.count += snap.count
            frame.last_time = now
            frame.data = snap.data
            frame.dlc = snap.dlc
            frame.is_extended = snap.is_extended
            frame.is_fd = snap.is_fd
            frame.bitrate_switch = snap.bitrate_switch
            frame.is_error = snap.is_error
            frame.is_remote = snap.is_remote
        if new_ids:
            self._append_frame_rows(new_ids)

    def _append_frame_rows(self, arbitration_ids: list[int]) -> None:
        self.frame_table.setSortingEnabled(False)
        start_row = self.frame_table.rowCount()
        self.frame_table.setRowCount(start_row + len(arbitration_ids))
        for i, arbitration_id in enumerate(arbitration_ids):
            frame = self._frames[arbitration_id]
            for col, text, sort_key in self._frame_cells(frame):
                item = _SortItem(text)
                item.setData(Qt.UserRole, sort_key)
                item.setData(Qt.UserRole + 1, arbitration_id)
                if col == FRAME_DATA_COL:
                    item.setToolTip(text)
                self.frame_table.setItem(start_row + i, col, item)
        self.frame_table.setSortingEnabled(True)
        self._filter_frames(self.frame_filter.text())

    def _frame_cells(self, frame: _LiveFrame) -> list[tuple[int, str, object]]:
        rate = frame.hz()
        data = frame.data.hex(" ")
        return [
            (FRAME_ID_COL, format_id_hex(frame.arbitration_id, frame.is_extended), frame.arbitration_id),
            (FRAME_MESSAGE_COL, frame.message_name, frame.message_name.lower()),
            (FRAME_TYPE_COL, frame_type_label(frame), frame_type_label(frame)),
            (FRAME_LEN_COL, str(frame.dlc), frame.dlc),
            (FRAME_COUNT_COL, str(frame.count), frame.count),
            (FRAME_HZ_COL, "" if rate is None else f"{rate:.1f}", -1.0 if rate is None else rate),
            (FRAME_DATA_COL, data, data),
        ]

    def _refresh_frame_table(self):
        # Sorting stays off while cells change, otherwise a live Count column
        # reorders rows in the middle of this loop.
        self.frame_table.setSortingEnabled(False)
        try:
            for row in range(self.frame_table.rowCount()):
                id_item = self.frame_table.item(row, FRAME_ID_COL)
                if id_item is None:
                    continue
                frame = self._frames.get(id_item.data(Qt.UserRole + 1))
                if frame is None:
                    continue
                for col, text, sort_key in self._frame_cells(frame):
                    item = self.frame_table.item(row, col)
                    if item is None:
                        continue
                    if item.text() != text:
                        item.setText(text)
                    if item.data(Qt.UserRole) != sort_key:
                        item.setData(Qt.UserRole, sort_key)
                    if col == FRAME_DATA_COL and item.toolTip() != text:
                        item.setToolTip(text)
        finally:
            self.frame_table.setSortingEnabled(True)

    def _filter_frames(self, text: str):
        text = text.strip().lower()
        for row in range(self.frame_table.rowCount()):
            id_item = self.frame_table.item(row, FRAME_ID_COL)
            if id_item is None:
                continue
            frame = self._frames.get(id_item.data(Qt.UserRole + 1))
            if frame is None:
                continue
            haystack = (
                f"{format_id_hex(frame.arbitration_id, frame.is_extended)} "
                f"{frame.arbitration_id:x} {frame.message_name} {frame.data.hex()}"
            ).lower()
            self.frame_table.setRowHidden(row, bool(text) and text not in haystack)

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
        self._refresh_frame_table()

        try:
            self._render_plot(now, window_s)
        except Exception:
            # figure.clear() runs before the axes are drawn. A failure after
            # that used to return without draw_idle and leave a white canvas.
            traceback.print_exc()
            self.figure.clear()
            self._draw_placeholder("Could not draw plot", now, window_s)

    def _render_plot(self, now: float, window_s: float) -> None:
        selected = list(self._selected_series())
        plottable = []
        for buf in selected:
            xs, ys = _numeric_points(buf)
            if xs:
                plottable.append((buf, xs, ys))

        self.figure.clear()
        if not plottable:
            note = "No samples yet" if selected else "Select a signal"
            self._draw_placeholder(note, now, window_s)
            return

        overlay = self.overlay_checkbox.isChecked() or len(plottable) == 1

        if overlay:
            ax = self.figure.add_subplot(111)
            for buf, xs, ys in plottable:
                ax.plot(xs, ys, label=buf.display_label)
                _apply_enum_ticks(ax, buf.choices)
            ax.set_xlabel("Time (s, since connect)")
            ax.legend()
            ax.grid(True)
            primary = ax
        else:
            axes = self.figure.subplots(len(plottable), 1, sharex=True)
            if len(plottable) == 1:
                axes = [axes]
            for ax, (buf, xs, ys) in zip(axes, plottable):
                ax.plot(xs, ys)
                ax.set_ylabel(buf.display_label)
                _apply_enum_ticks(ax, buf.choices)
                ax.grid(True)
            axes[-1].set_xlabel("Time (s, since connect)")
            primary = axes[-1]

        self.figure.tight_layout()
        if self._paused:
            primary.set_xlim(self._paused_now - window_s, self._paused_now)
        self.canvas.draw_idle()

    def _draw_placeholder(self, note: str, now: float, window_s: float) -> None:
        ax = self.figure.add_subplot(111)
        ax.set_xlabel("Time (s, since connect)")
        ax.grid(True)
        if self._connect_t0 or self._paused:
            ax.set_xlim(now - window_s, now)
        ax.text(
            0.5,
            0.5,
            note,
            transform=ax.transAxes,
            ha="center",
            va="center",
            color=STEEL,
            fontsize=12,
        )
        self.figure.tight_layout()
        self.canvas.draw_idle()


def _numeric_points(buf) -> tuple[list, list]:
    """Times and numeric values. Non-numeric samples are skipped so one bad value cannot blank the canvas."""
    xs = []
    ys = []
    for t, value in zip(buf.times, buf.values):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            value = value.value if hasattr(value, "value") else value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        xs.append(t)
        ys.append(value)
    return xs, ys


def _apply_enum_ticks(ax, choices) -> None:
    positions, labels = enum_ticks(choices)
    if not positions:
        return
    if any(isinstance(pos, bool) or not isinstance(pos, (int, float)) for pos in positions):
        return
    ax.set_yticks(positions)
    ax.set_yticklabels(labels)

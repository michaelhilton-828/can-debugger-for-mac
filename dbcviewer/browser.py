"""The single-file browsing tab: message list, signal list, value tables,
and the bit-layout diagram for the selected message.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .bitlayout import BitLayoutWidget
from .dbc_model import DbcLoadError, load_database, message_rows, signal_rows

MESSAGE_HEADERS = ["Name", "ID (hex)", "ID (dec)", "DLC", "Node(s)"]
SIGNAL_HEADERS = [
    "Name", "Start", "Len", "Order", "Signed", "Factor", "Offset",
    "Min", "Max", "Unit", "Mux", "Values",
]


class BrowserTab(QWidget):
    database_loaded = Signal(object, str)  # (cantools Database, file path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.database = None
        self.message_rows = []
        self.current_signal_rows = []
        self._pending_signal_name: str | None = None
        self._preserving_signal_filter = False

        self._build_ui()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self.open_button = QPushButton("Open .dbc…")
        self.open_button.clicked.connect(self.open_file_dialog)
        self.path_label = QLabel("No file loaded")
        self.path_label.setStyleSheet("color: #555;")
        top_row.addWidget(self.open_button)
        top_row.addWidget(self.path_label, 1)
        root.addLayout(top_row)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # --- left: message list -----------------------------------------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.message_search = QLineEdit()
        self.message_search.setPlaceholderText("Filter messages by name or ID…")
        self.message_search.textChanged.connect(self._filter_messages)
        left_layout.addWidget(self.message_search)

        self.message_table = QTableWidget(0, len(MESSAGE_HEADERS))
        self.message_table.setHorizontalHeaderLabels(MESSAGE_HEADERS)
        self.message_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.message_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.message_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.message_table.horizontalHeader().setStretchLastSection(True)
        self.message_table.itemSelectionChanged.connect(self._on_message_selected)
        self.message_table.setSortingEnabled(True)
        left_layout.addWidget(self.message_table, 1)
        self.message_detail = QLabel("Cycle time, receivers, and comment appear here.")
        self.message_detail.setWordWrap(True)
        self.message_detail.setStyleSheet("color: #333;")
        left_layout.addWidget(self.message_detail)
        splitter.addWidget(left)

        # --- right: signals + values + bit layout ------------------------
        right_splitter = QSplitter(Qt.Vertical)

        signals_box = QWidget()
        signals_layout = QVBoxLayout(signals_box)
        signals_layout.setContentsMargins(0, 0, 0, 0)
        self.signal_search = QLineEdit()
        self.signal_search.setPlaceholderText("Find a signal in this DBC…")
        self.signal_search.textChanged.connect(self._on_signal_query_changed)
        self.signal_search.returnPressed.connect(self._activate_signal_query)
        signals_layout.addWidget(self.signal_search)
        self.signal_hits = QListWidget()
        self.signal_hits.setMaximumHeight(120)
        self.signal_hits.hide()
        self.signal_hits.itemClicked.connect(self._jump_to_signal_hit)
        signals_layout.addWidget(self.signal_hits)

        self.signal_table = QTableWidget(0, len(SIGNAL_HEADERS))
        self.signal_table.setHorizontalHeaderLabels(SIGNAL_HEADERS)
        self.signal_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.signal_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.signal_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.signal_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.signal_table.itemSelectionChanged.connect(self._on_signal_selected)
        signals_layout.addWidget(self.signal_table, 1)
        right_splitter.addWidget(signals_box)

        values_box = QWidget()
        values_layout = QVBoxLayout(values_box)
        values_layout.setContentsMargins(0, 0, 0, 0)
        self.values_label = QLabel("Value table: (select a signal)")
        self.values_label.setWordWrap(True)
        values_layout.addWidget(self.values_label)
        self.signal_meta = QLabel("")
        self.signal_meta.setWordWrap(True)
        self.signal_meta.setStyleSheet("color: #333;")
        values_layout.addWidget(self.signal_meta)
        self.values_table = QTableWidget(0, 2)
        self.values_table.setHorizontalHeaderLabels(["Raw value", "Label"])
        self.values_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.values_table.horizontalHeader().setStretchLastSection(True)
        self.values_table.setMaximumHeight(150)
        values_layout.addWidget(self.values_table)
        right_splitter.addWidget(values_box)

        bitlayout_box = QWidget()
        bitlayout_layout = QVBoxLayout(bitlayout_box)
        bitlayout_layout.setContentsMargins(0, 0, 0, 0)
        self.bitlayout_label = QLabel("Bit layout:")
        bitlayout_layout.addWidget(self.bitlayout_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.bit_layout = BitLayoutWidget()
        self.bit_layout.signal_clicked.connect(self._select_signal_by_name)
        scroll.setWidget(self.bit_layout)
        bitlayout_layout.addWidget(scroll, 1)
        right_splitter.addWidget(bitlayout_box)

        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setStretchFactor(2, 4)

        splitter.addWidget(right_splitter)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

    # ------------------------------------------------------------- loading

    def open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open .dbc file", "", "CAN database (*.dbc);;All files (*)")
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> bool:
        try:
            database = load_database(path)
        except DbcLoadError as exc:
            QMessageBox.critical(self, "Failed to load .dbc file", str(exc))
            return False

        self.database = database
        self.path_label.setText(path)
        self.message_rows = message_rows(database)
        self.message_search.clear()
        self.signal_search.clear()
        self._populate_messages()
        self.current_signal_rows = []
        self._populate_signals([])
        self.bit_layout.set_message(0, [])
        self.message_detail.setText("")
        self.message_table.clearSelection()
        if self.message_table.rowCount() > 0:
            self.message_table.selectRow(0)
        self.database_loaded.emit(database, path)
        return True

    def show_message(self, name: str, signal_name: str | None = None, *, keep_query: bool = False) -> bool:
        """Select `name` (and optionally one of its signals). Returns False if
        the message is not in the loaded database.

        keep_query leaves the DBC-wide search text in place (used when the
        user picks a hit from that search).
        """
        msg_idx = next((i for i, row in enumerate(self.message_rows) if row.name == name), None)
        if msg_idx is None:
            return False
        visual = self._visual_row_for_message(msg_idx)
        if visual is None:
            return False
        self.message_table.setRowHidden(visual, False)
        self._pending_signal_name = signal_name or None
        self._preserving_signal_filter = bool(signal_name) or keep_query
        if signal_name and not keep_query:
            self.signal_search.blockSignals(True)
            self.signal_search.setText(signal_name)
            self.signal_search.blockSignals(False)
        self.message_table.blockSignals(True)
        self.message_table.selectRow(visual)
        self.message_table.blockSignals(False)
        item = self.message_table.item(visual, 0)
        if item is not None:
            self.message_table.scrollToItem(item)
        self._on_message_selected()
        return True

    # ------------------------------------------------------------ messages

    def _populate_messages(self):
        self.message_table.setSortingEnabled(False)
        self.message_table.setRowCount(len(self.message_rows))
        for row_idx, row in enumerate(self.message_rows):
            values = [row.name, row.id_hex, str(row.frame_id), str(row.dlc), row.senders]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in (2, 3):
                    item.setData(Qt.UserRole, int(value))
                item.setData(Qt.UserRole + 1, row_idx)
                self.message_table.setItem(row_idx, col, item)
        self.message_table.setSortingEnabled(True)
        self.message_table.resizeColumnsToContents()

    def _filter_messages(self, text: str):
        text = text.strip().lower()
        for r in range(self.message_table.rowCount()):
            row_idx = self.message_table.item(r, 0).data(Qt.UserRole + 1)
            row = self.message_rows[row_idx]
            haystack = f"{row.name} {row.id_hex} {row.frame_id}".lower()
            self.message_table.setRowHidden(r, text not in haystack)

    def _visual_row_for_message(self, msg_idx: int) -> int | None:
        for r in range(self.message_table.rowCount()):
            item = self.message_table.item(r, 0)
            if item is not None and item.data(Qt.UserRole + 1) == msg_idx:
                return r
        return None

    def _on_message_selected(self):
        items = self.message_table.selectedItems()
        pending = self._pending_signal_name
        preserve = self._preserving_signal_filter
        self._pending_signal_name = None
        self._preserving_signal_filter = False
        if not items:
            self.current_signal_rows = []
            self._populate_signals([])
            self.bit_layout.set_message(0, [])
            self.message_detail.setText("")
            return
        row_idx = items[0].data(Qt.UserRole + 1)
        msg_row = self.message_rows[row_idx]
        self.current_signal_rows = signal_rows(msg_row.message)
        if not preserve:
            self.signal_search.blockSignals(True)
            self.signal_search.clear()
            self.signal_search.blockSignals(False)
            self.signal_hits.hide()
            self.signal_hits.clear()
        self._populate_signals(self.current_signal_rows)
        self._set_message_detail(msg_row)
        self.bit_layout.set_message(msg_row.dlc, self.current_signal_rows)
        overlap = self.bit_layout.overlap_count()
        suffix = f"  ⚠ {overlap} overlapping bit(s)" if overlap else ""
        self.bitlayout_label.setText(f"Bit layout ({msg_row.dlc} bytes){suffix}:")
        if preserve and self.signal_search.text():
            self._filter_signals(self.signal_search.text())
            self._refresh_signal_hits(self.signal_search.text())
        if pending:
            self._select_signal_by_name(pending)

    def _set_message_detail(self, msg_row):
        cycle = f"{msg_row.cycle_time} ms" if msg_row.cycle_time else "—"
        lines = [f"Cycle: {cycle}    Receivers: {msg_row.receivers}"]
        if msg_row.comment:
            lines.append(msg_row.comment)
        self.message_detail.setText("\n".join(lines))

    # ------------------------------------------------------------- signals

    def _populate_signals(self, rows):
        self.signal_table.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            values = [
                row.name,
                str(row.start),
                str(row.length),
                row.byte_order,
                "yes" if row.signed else "no",
                _fmt_num(row.factor),
                _fmt_num(row.offset),
                _fmt_num(row.minimum),
                _fmt_num(row.maximum),
                row.unit,
                row.mux,
                f"{len(row.choices)} value(s)" if row.choices else "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole + 1, row_idx)
                self.signal_table.setItem(row_idx, col, item)
        self.values_table.setRowCount(0)
        self.values_label.setText("Value table: (select a signal)")
        self.signal_meta.setText("")

    def _on_signal_query_changed(self, text: str):
        self._filter_signals(text)
        self._refresh_signal_hits(text)

    def _filter_signals(self, text: str):
        text = text.strip().lower()
        for r in range(self.signal_table.rowCount()):
            item = self.signal_table.item(r, 0)
            if item is None:
                continue
            self.signal_table.setRowHidden(r, text not in item.text().lower())

    def _refresh_signal_hits(self, text: str):
        self.signal_hits.clear()
        query = text.strip().lower()
        if not query:
            self.signal_hits.hide()
            return
        shown = 0
        truncated = False
        for msg_idx, msg_row in enumerate(self.message_rows):
            for signal in msg_row.message.signals:
                if query not in signal.name.lower() and query not in msg_row.name.lower():
                    continue
                item = QListWidgetItem(f"{msg_row.name}  —  {signal.name}")
                item.setData(Qt.UserRole, (msg_idx, signal.name))
                self.signal_hits.addItem(item)
                shown += 1
                if shown >= 40:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            more = QListWidgetItem("Keep typing to narrow the matches…")
            more.setFlags(Qt.NoItemFlags)
            self.signal_hits.addItem(more)
        self.signal_hits.setVisible(shown > 0)

    def _activate_signal_query(self):
        visible = [
            r
            for r in range(self.signal_table.rowCount())
            if not self.signal_table.isRowHidden(r)
        ]
        if len(visible) == 1:
            self.signal_table.selectRow(visible[0])
            return
        for i in range(self.signal_hits.count()):
            item = self.signal_hits.item(i)
            if item is not None and item.flags() & Qt.ItemIsEnabled:
                self._jump_to_signal_hit(item)
                return

    def _jump_to_signal_hit(self, item):
        data = item.data(Qt.UserRole)
        if not data:
            return
        msg_idx, sig_name = data
        if not (0 <= msg_idx < len(self.message_rows)):
            return
        name = self.message_rows[msg_idx].name
        # Defer so this click handler is not still using the hits list when
        # the jump rebuilds it.
        QTimer.singleShot(0, lambda: self.show_message(name, sig_name, keep_query=True))

    def _select_signal_by_name(self, name: str):
        for r in range(self.signal_table.rowCount()):
            item = self.signal_table.item(r, 0)
            if item is not None and item.text() == name:
                self.signal_table.setRowHidden(r, False)
                self.signal_table.selectRow(r)
                self.signal_table.scrollToItem(item)
                return

    def _on_signal_selected(self):
        items = self.signal_table.selectedItems()
        if not items:
            self.bit_layout.set_selected_signal(None)
            self.signal_meta.setText("")
            return
        row_idx = items[0].data(Qt.UserRole + 1)
        sig_row = self.current_signal_rows[row_idx]
        self.bit_layout.set_selected_signal(sig_row.name)
        meta = [f"Receivers: {sig_row.receivers}"]
        if sig_row.comment:
            meta.append(sig_row.comment)
        self.signal_meta.setText("\n".join(meta))

        if sig_row.choices:
            self.values_label.setText(f"Value table for '{sig_row.name}':")
            self.values_table.setRowCount(len(sig_row.choices))
            for i, (raw, label) in enumerate(sig_row.choices):
                self.values_table.setItem(i, 0, QTableWidgetItem(str(raw)))
                self.values_table.setItem(i, 1, QTableWidgetItem(label))
        else:
            self.values_label.setText(f"Value table for '{sig_row.name}': none defined")
            self.values_table.setRowCount(0)


def _fmt_num(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)

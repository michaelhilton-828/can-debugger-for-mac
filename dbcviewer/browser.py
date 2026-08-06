"""The single-file browsing tab: message list, signal list, value tables,
and the bit-layout diagram for the selected message.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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
        splitter.addWidget(left)

        # --- right: signals + values + bit layout ------------------------
        right_splitter = QSplitter(Qt.Vertical)

        signals_box = QWidget()
        signals_layout = QVBoxLayout(signals_box)
        signals_layout.setContentsMargins(0, 0, 0, 0)
        self.signal_search = QLineEdit()
        self.signal_search.setPlaceholderText("Filter signals by name…")
        self.signal_search.textChanged.connect(self._filter_signals)
        signals_layout.addWidget(self.signal_search)

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
        values_layout.addWidget(self.values_label)
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
        self._populate_messages()
        self.current_signal_rows = []
        self._populate_signals([])
        self.bit_layout.set_message(0, [])
        self.message_table.clearSelection()
        if self.message_table.rowCount() > 0:
            self.message_table.selectRow(0)
        self.database_loaded.emit(database, path)
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

    def _on_message_selected(self):
        items = self.message_table.selectedItems()
        if not items:
            self.current_signal_rows = []
            self._populate_signals([])
            self.bit_layout.set_message(0, [])
            return
        row_idx = items[0].data(Qt.UserRole + 1)
        msg_row = self.message_rows[row_idx]
        self.current_signal_rows = signal_rows(msg_row.message)
        self.signal_search.clear()
        self._populate_signals(self.current_signal_rows)
        self.bit_layout.set_message(msg_row.dlc, self.current_signal_rows)
        overlap = self.bit_layout.overlap_count()
        suffix = f"  ⚠ {overlap} overlapping bit(s)" if overlap else ""
        self.bitlayout_label.setText(f"Bit layout ({msg_row.dlc} bytes){suffix}:")

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

    def _filter_signals(self, text: str):
        text = text.strip().lower()
        for r in range(self.signal_table.rowCount()):
            name = self.signal_table.item(r, 0).text().lower()
            self.signal_table.setRowHidden(r, text not in name)

    def _on_signal_selected(self):
        items = self.signal_table.selectedItems()
        if not items:
            self.bit_layout.set_selected_signal(None)
            return
        row_idx = items[0].data(Qt.UserRole + 1)
        sig_row = self.current_signal_rows[row_idx]
        self.bit_layout.set_selected_signal(sig_row.name)

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

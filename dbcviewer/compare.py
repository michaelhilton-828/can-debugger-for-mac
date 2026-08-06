"""Compare tab: load two .dbc files and show a side-by-side structural diff."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dbc_model import DbcLoadError, load_database
from .diff import compare_databases

ADDED_COLOR = QColor(0xDC, 0xF5, 0xDC)
REMOVED_COLOR = QColor(0xF9, 0xDA, 0xDA)
CHANGED_COLOR = QColor(0xFC, 0xF1, 0xC7)


class CompareTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.database_a = None
        self.database_b = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        grid = QGridLayout()
        self.button_a = QPushButton("Open File A…")
        self.button_a.clicked.connect(lambda: self._open_file("a"))
        self.label_a = QLabel("No file loaded")
        self.button_b = QPushButton("Open File B…")
        self.button_b.clicked.connect(lambda: self._open_file("b"))
        self.label_b = QLabel("No file loaded")
        self.compare_button = QPushButton("Compare →")
        self.compare_button.clicked.connect(self._run_compare)
        self.compare_button.setEnabled(False)

        grid.addWidget(self.button_a, 0, 0)
        grid.addWidget(self.label_a, 0, 1)
        grid.addWidget(self.button_b, 1, 0)
        grid.addWidget(self.label_b, 1, 1)
        grid.addWidget(self.compare_button, 0, 2, 2, 1)
        root.addLayout(grid)

        self.summary_label = QLabel("Load two files and click Compare.")
        self.summary_label.setStyleSheet("font-weight: bold; padding: 4px;")
        root.addWidget(self.summary_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Item", "File A (old)", "File B (new)"])
        self.tree.setColumnWidth(0, 380)
        root.addWidget(self.tree, 1)

    def _open_file(self, which: str):
        path, _ = QFileDialog.getOpenFileName(self, "Open .dbc file", "", "CAN database (*.dbc);;All files (*)")
        if not path:
            return
        try:
            db = load_database(path)
        except DbcLoadError as exc:
            QMessageBox.critical(self, "Failed to load .dbc file", str(exc))
            return

        if which == "a":
            self.database_a = db
            self.label_a.setText(path)
        else:
            self.database_b = db
            self.label_b.setText(path)

        self.compare_button.setEnabled(self.database_a is not None and self.database_b is not None)

    def _run_compare(self):
        diff = compare_databases(self.database_a, self.database_b)
        self.summary_label.setText(diff.summary())
        self.tree.clear()

        if diff.messages_added:
            top = self._top_item(f"Messages added ({len(diff.messages_added)})", ADDED_COLOR)
            for name in diff.messages_added:
                msg = self._find_message(self.database_b, name)
                child = QTreeWidgetItem([name, "", self._id_text(msg)])
                top.addChild(child)

        if diff.messages_removed:
            top = self._top_item(f"Messages removed ({len(diff.messages_removed)})", REMOVED_COLOR)
            for name in diff.messages_removed:
                msg = self._find_message(self.database_a, name)
                child = QTreeWidgetItem([name, self._id_text(msg), ""])
                top.addChild(child)

        if diff.messages_changed:
            top = self._top_item(f"Messages changed ({len(diff.messages_changed)})", CHANGED_COLOR)
            for md in diff.messages_changed:
                self._add_message_diff(top, md)

        self.tree.expandAll()

    def _top_item(self, label, color) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label, "", ""])
        for col in range(3):
            item.setBackground(col, QBrush(color))
        self.tree.addTopLevelItem(item)
        return item

    def _add_message_diff(self, parent: QTreeWidgetItem, md):
        msg_item = QTreeWidgetItem([md.name, md.id_hex_a, md.id_hex_b])
        parent.addChild(msg_item)

        for label, old, new in md.changes:
            leaf = QTreeWidgetItem([f"  {label}", old, new])
            msg_item.addChild(leaf)

        if md.signals_added:
            grp = QTreeWidgetItem([f"Signals added ({len(md.signals_added)})", "", ""])
            for col in range(3):
                grp.setBackground(col, QBrush(ADDED_COLOR))
            msg_item.addChild(grp)
            for name in md.signals_added:
                grp.addChild(QTreeWidgetItem([name, "", ""]))

        if md.signals_removed:
            grp = QTreeWidgetItem([f"Signals removed ({len(md.signals_removed)})", "", ""])
            for col in range(3):
                grp.setBackground(col, QBrush(REMOVED_COLOR))
            msg_item.addChild(grp)
            for name in md.signals_removed:
                grp.addChild(QTreeWidgetItem([name, "", ""]))

        if md.signals_changed:
            grp = QTreeWidgetItem([f"Signals changed ({len(md.signals_changed)})", "", ""])
            for col in range(3):
                grp.setBackground(col, QBrush(CHANGED_COLOR))
            msg_item.addChild(grp)
            for sd in md.signals_changed:
                sig_item = QTreeWidgetItem([sd.name, "", ""])
                grp.addChild(sig_item)
                for label, old, new in sd.changes:
                    sig_item.addChild(QTreeWidgetItem([f"  {label}", old, new]))

    @staticmethod
    def _find_message(database, name):
        for m in database.messages:
            if m.name == name:
                return m
        return None

    @staticmethod
    def _id_text(msg) -> str:
        if msg is None:
            return ""
        return f"0x{msg.frame_id:X} (DLC {msg.length})"

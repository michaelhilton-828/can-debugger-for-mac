"""Compare tab: load two .dbc files and show a side-by-side structural diff."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
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
    # (message name, signal name or "") — double-click in the diff tree.
    open_in_viewer = Signal(str, str)
    dbc_loaded = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.database_a = None
        self.database_b = None
        self._browser_database = None
        self._browser_path = ""
        self._file_a_explicit = False
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

        self.use_viewer_button = QPushButton("Use viewer DBC")
        self.use_viewer_button.setEnabled(False)
        self.use_viewer_button.clicked.connect(self._use_viewer_dbc)

        grid.addWidget(self.button_a, 0, 0)
        grid.addWidget(self.label_a, 0, 1)
        grid.addWidget(self.use_viewer_button, 0, 2)
        grid.addWidget(self.button_b, 1, 0)
        grid.addWidget(self.label_b, 1, 1)
        grid.addWidget(self.compare_button, 0, 3, 2, 1)
        root.addLayout(grid)

        self.summary_label = QLabel("Load two files and click Compare.")
        self.summary_label.setStyleSheet("font-weight: bold; padding: 4px;")
        root.addWidget(self.summary_label)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter diff by message, signal, or ID…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        root.addWidget(self.filter_edit)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Item", "File A (old)", "File B (new)"])
        self.tree.setColumnWidth(0, 380)
        self.tree.setToolTip("Double-click a message or signal to open it in the DBC Viewer tab.")
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        root.addWidget(self.tree, 1)

    def set_browser_database(self, database, path: str) -> None:
        """Seed File A from the DBC Viewer tab until the user picks File A explicitly."""
        self._browser_database = database
        self._browser_path = path
        self.use_viewer_button.setEnabled(database is not None)
        if not self._file_a_explicit:
            self.database_a = database
            self.label_a.setText(f"{path}  (from DBC Viewer)")
            self._sync_compare_button()

    def load_path(self, which: str, path: str) -> bool:
        """Load `path` as File A or File B. Used by the file dialog and by drops."""
        try:
            db = load_database(path)
        except DbcLoadError as exc:
            QMessageBox.critical(self, "Failed to load .dbc file", str(exc))
            return False

        if which == "a":
            self._file_a_explicit = True
            self.database_a = db
            self.label_a.setText(path)
        else:
            self.database_b = db
            self.label_b.setText(path)
        self._sync_compare_button()
        self.dbc_loaded.emit(path)
        return True

    def _use_viewer_dbc(self):
        if self._browser_database is None:
            return
        self._file_a_explicit = False
        self.database_a = self._browser_database
        self.label_a.setText(f"{self._browser_path}  (from DBC Viewer)")
        self._sync_compare_button()

    def _sync_compare_button(self):
        self.compare_button.setEnabled(self.database_a is not None and self.database_b is not None)

    def _open_file(self, which: str):
        path, _ = QFileDialog.getOpenFileName(self, "Open .dbc file", "", "CAN database (*.dbc);;All files (*)")
        if path:
            self.load_path(which, path)

    def _run_compare(self):
        diff = compare_databases(self.database_a, self.database_b)
        self.summary_label.setText(diff.summary())
        self.tree.clear()

        if diff.messages_added:
            top = self._top_item(f"Messages added ({len(diff.messages_added)})", ADDED_COLOR)
            for name in diff.messages_added:
                msg = self._find_message(self.database_b, name)
                child = QTreeWidgetItem([name, "", self._id_text(msg)])
                self._tag_message(child, name)
                top.addChild(child)

        if diff.messages_removed:
            top = self._top_item(f"Messages removed ({len(diff.messages_removed)})", REMOVED_COLOR)
            for name in diff.messages_removed:
                msg = self._find_message(self.database_a, name)
                child = QTreeWidgetItem([name, self._id_text(msg), ""])
                self._tag_message(child, name)
                top.addChild(child)

        if diff.messages_changed:
            top = self._top_item(f"Messages changed ({len(diff.messages_changed)})", CHANGED_COLOR)
            for md in diff.messages_changed:
                self._add_message_diff(top, md)

        self._apply_filter()

    def _top_item(self, label, color) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label, "", ""])
        for col in range(3):
            item.setBackground(col, QBrush(color))
        self.tree.addTopLevelItem(item)
        return item

    def _add_message_diff(self, parent: QTreeWidgetItem, md):
        msg_item = QTreeWidgetItem([md.name, md.id_hex_a, md.id_hex_b])
        self._tag_message(msg_item, md.name)
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
                child = QTreeWidgetItem([name, "", ""])
                self._tag_signal(child, md.name, name)
                grp.addChild(child)

        if md.signals_removed:
            grp = QTreeWidgetItem([f"Signals removed ({len(md.signals_removed)})", "", ""])
            for col in range(3):
                grp.setBackground(col, QBrush(REMOVED_COLOR))
            msg_item.addChild(grp)
            for name in md.signals_removed:
                child = QTreeWidgetItem([name, "", ""])
                self._tag_signal(child, md.name, name)
                grp.addChild(child)

        if md.signals_changed:
            grp = QTreeWidgetItem([f"Signals changed ({len(md.signals_changed)})", "", ""])
            for col in range(3):
                grp.setBackground(col, QBrush(CHANGED_COLOR))
            msg_item.addChild(grp)
            for sd in md.signals_changed:
                sig_item = QTreeWidgetItem([sd.name, "", ""])
                self._tag_signal(sig_item, md.name, sd.name)
                grp.addChild(sig_item)
                for label, old, new in sd.changes:
                    sig_item.addChild(QTreeWidgetItem([f"  {label}", old, new]))

    @staticmethod
    def _tag_message(item: QTreeWidgetItem, message_name: str):
        item.setData(0, Qt.UserRole, (message_name, ""))

    @staticmethod
    def _tag_signal(item: QTreeWidgetItem, message_name: str, signal_name: str):
        item.setData(0, Qt.UserRole, (message_name, signal_name))

    def _on_item_double_clicked(self, item, _column):
        current = item
        while current is not None:
            data = current.data(0, Qt.UserRole)
            if data:
                self.open_in_viewer.emit(data[0], data[1])
                return
            current = current.parent()

    def _apply_filter(self, _text: str = ""):
        query = self.filter_edit.text().strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            any_visible = False
            for j in range(top.childCount()):
                child = top.child(j)
                visible = self._apply_item_filter(child, query)
                child.setHidden(not visible)
                any_visible = any_visible or visible
            top.setHidden(bool(query) and not any_visible)
            if query and any_visible:
                top.setExpanded(True)

    def _apply_item_filter(self, item: QTreeWidgetItem, query: str) -> bool:
        if not query:
            item.setHidden(False)
            for i in range(item.childCount()):
                self._apply_item_filter(item.child(i), "")
            return True
        self_match = query in self._item_text(item)
        child_visible = False
        for i in range(item.childCount()):
            child = item.child(i)
            if self_match:
                child.setHidden(False)
                self._show_subtree(child)
                child_visible = True
            else:
                visible = self._apply_item_filter(child, query)
                child.setHidden(not visible)
                child_visible = child_visible or visible
        if child_visible:
            item.setExpanded(True)
        return self_match or child_visible

    def _show_subtree(self, item: QTreeWidgetItem):
        for i in range(item.childCount()):
            child = item.child(i)
            child.setHidden(False)
            self._show_subtree(child)

    @staticmethod
    def _item_text(item: QTreeWidgetItem) -> str:
        return " ".join(item.text(col) for col in range(item.columnCount())).lower()

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

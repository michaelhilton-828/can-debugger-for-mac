"""Compare tab: pick two loaded DBC files and show a structural diff."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dbc_library import DbcLibrary
from .diff import compare_databases
from .table_columns import install_column_config

ADDED_COLOR = QColor(0xDC, 0xF5, 0xDC)
REMOVED_COLOR = QColor(0xF9, 0xDA, 0xDA)
CHANGED_COLOR = QColor(0xFC, 0xF1, 0xC7)

_NEED_TWO = "Fewer than two DBC files are loaded. Add them on the Home tab."
_NEED_DIFFERENT = "Choose two different files."


class CompareTab(QWidget):
    # (message name, signal name or "") — double-click in the diff tree.
    open_in_viewer = Signal(str, str)

    def __init__(self, library: DbcLibrary, parent=None):
        super().__init__(parent)
        self.library = library
        self.database_a = None
        self.database_b = None
        self._user_picked = False
        self._path_a = ""
        self._path_b = ""
        self._compared: tuple[str, str] | None = None
        self._build_ui()
        self.library.changed.connect(self._on_library_changed)
        self._on_library_changed()

    def _build_ui(self):
        root = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("File A"))
        self.combo_a = self._make_combo()
        row.addWidget(self.combo_a, 1)
        row.addWidget(QLabel("File B"))
        self.combo_b = self._make_combo()
        row.addWidget(self.combo_b, 1)
        root.addLayout(row)

        self.summary_label = QLabel(_NEED_TWO)
        self.summary_label.setWordWrap(True)
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
        install_column_config(self.tree, "compare.diff")
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        root.addWidget(self.tree, 1)

    def _make_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        combo.setMinimumContentsLength(16)
        combo.currentIndexChanged.connect(self._on_combo_changed)
        return combo

    def _on_library_changed(self):
        entries = self.library.entries()
        paths = [entry.path for entry in entries]
        self._fill_combos(entries)
        if self._user_picked and self._path_a in paths and self._path_b in paths:
            self._set_combo(self.combo_a, self._path_a)
            self._set_combo(self.combo_b, self._path_b)
        elif len(entries) >= 2:
            self._user_picked = False
            self._path_a = entries[0].path
            self._path_b = entries[1].path
            self._set_combo(self.combo_a, self._path_a)
            self._set_combo(self.combo_b, self._path_b)
        elif len(entries) == 1:
            self._user_picked = False
            self._path_a = entries[0].path
            self._path_b = ""
            self._set_combo(self.combo_a, self._path_a)
        else:
            self._user_picked = False
            self._path_a = ""
            self._path_b = ""
        self._maybe_compare()

    def _fill_combos(self, entries):
        for combo in (self.combo_a, self.combo_b):
            combo.blockSignals(True)
            combo.clear()
            for entry in entries:
                combo.addItem(entry.label, entry.path)
                combo.setItemData(combo.count() - 1, entry.path, Qt.ToolTipRole)
            combo.blockSignals(False)

    def _set_combo(self, combo: QComboBox, path: str) -> None:
        combo.blockSignals(True)
        index = combo.findData(path) if path else -1
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)
        combo.setToolTip(combo.currentData() or "")

    def _on_combo_changed(self, _index: int = 0):
        self._user_picked = True
        self._path_a = self.combo_a.currentData() or ""
        self._path_b = self.combo_b.currentData() or ""
        self.combo_a.setToolTip(self._path_a)
        self.combo_b.setToolTip(self._path_b)
        self._maybe_compare()

    def _maybe_compare(self):
        entries = self.library.entries()
        if len(entries) < 2:
            self.database_a = None
            self.database_b = None
            self._compared = None
            self.summary_label.setText(_NEED_TWO)
            self.tree.clear()
            return
        if not self._path_a or not self._path_b or self._path_a == self._path_b:
            self.database_a = None
            self.database_b = None
            self._compared = None
            self.summary_label.setText(_NEED_DIFFERENT)
            self.tree.clear()
            return
        pair = (self._path_a, self._path_b)
        if pair == self._compared:
            return
        by_path = {entry.path: entry.database for entry in entries}
        self.database_a = by_path.get(self._path_a)
        self.database_b = by_path.get(self._path_b)
        if self.database_a is None or self.database_b is None:
            return
        self._run_compare()
        self._compared = pair

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

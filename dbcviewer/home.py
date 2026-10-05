"""Home tab: the only place to add, remove, and reorder loaded DBC files."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .dbc_library import MAX_DBCS, DbcLibrary
from .theme import set_muted


def _dbc_paths(mime) -> list[str]:
    if mime is None or not mime.hasUrls():
        return []
    paths = []
    for url in mime.urls():
        path = url.toLocalFile()
        if path and path.lower().endswith(".dbc"):
            paths.append(path)
    return paths


def _single_move(old: list[str], new: list[str]):
    """Return (from_index, to_index) when `new` is `old` after one move."""
    if len(old) != len(new) or old == new:
        return None
    for from_index, path in enumerate(old):
        if from_index < len(new) and new[from_index] == path:
            continue
        for to_index, candidate in enumerate(new):
            if candidate != path:
                continue
            trial = list(old)
            item = trial.pop(from_index)
            trial.insert(to_index, item)
            if trial == new:
                return from_index, to_index
    return None


class _DbcList(QListWidget):
    """Internal drag reorders the library. Dropped .dbc files are added to it."""

    def __init__(self, host: "HomeTab"):
        super().__init__(host)
        self._host = host

    def dragEnterEvent(self, event):
        if _dbc_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if _dbc_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = _dbc_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self._host.library.add_paths(paths)
            return
        self._host.begin_reorder()
        try:
            super().dropEvent(event)
            order = [self.item(i).data(Qt.UserRole) for i in range(self.count())]
        finally:
            self._host.end_reorder()
        self._host.apply_reorder(order)


class HomeTab(QWidget):
    def __init__(self, library: DbcLibrary, parent=None):
        super().__init__(parent)
        self.library = library
        self._reordering = False
        self._syncing = False
        self.setAcceptDrops(True)
        self._build_ui()
        self.library.changed.connect(self._refresh)
        self._refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)

        row = QHBoxLayout()
        self.add_button = QPushButton("Add DBC…")
        self.add_button.setToolTip(f"Add DBC files. At most {MAX_DBCS} can be loaded.")
        self.add_button.clicked.connect(self._add_dialog)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        root.addLayout(row)

        hint = QLabel(
            "Up to 5 files. The top file wins when CAN IDs overlap. Other tabs use this list."
        )
        hint.setWordWrap(True)
        set_muted(hint)
        root.addWidget(hint)

        self.list = _DbcList(self)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setDropIndicatorShown(True)
        self.list.setToolTip("Drag a row to change priority. The top file wins when CAN IDs overlap.")
        self.list.currentRowChanged.connect(self._on_row_changed)
        root.addWidget(self.list, 1)

    def begin_reorder(self) -> None:
        self._reordering = True

    def end_reorder(self) -> None:
        self._reordering = False

    def apply_reorder(self, order: list[str]) -> None:
        current = [entry.path for entry in self.library.entries()]
        moved = _single_move(current, [path for path in order if path])
        if moved is not None:
            self.library.move(moved[0], moved[1])
            return
        row = self.list.currentRow()
        if row >= 0 and row != self.library.selected_index():
            self.library.set_selected_index(row)

    def dragEnterEvent(self, event):
        if _dbc_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if _dbc_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = _dbc_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.library.add_paths(paths)

    def _add_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add DBC files", "", "CAN database (*.dbc);;All files (*)"
        )
        if paths:
            self.library.add_paths(paths)

    def _remove(self):
        self.library.remove_at(self.list.currentRow())

    def _on_row_changed(self, row: int):
        if self._reordering or self._syncing:
            return
        if row != self.library.selected_index():
            self.library.set_selected_index(row)

    def _refresh(self):
        if self._reordering:
            return
        entries = self.library.entries()
        self._syncing = True
        self.list.blockSignals(True)
        try:
            self.list.clear()
            for entry in entries:
                count = entry.message_count
                noun = "message" if count == 1 else "messages"
                item = QListWidgetItem(f"{entry.label}  ({count} {noun})")
                item.setData(Qt.UserRole, entry.path)
                item.setToolTip(entry.path)
                flags = item.flags()
                flags |= Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled | Qt.ItemIsSelectable | Qt.ItemIsEnabled
                item.setFlags(flags)
                self.list.addItem(item)
            index = self.library.selected_index()
            if 0 <= index < self.list.count():
                self.list.setCurrentRow(index)
        finally:
            self.list.blockSignals(False)
            self._syncing = False
        self.remove_button.setEnabled(0 <= self.library.selected_index() < len(entries))

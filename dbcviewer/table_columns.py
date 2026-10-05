"""Show, hide, reorder, and resize columns on a table or tree header.

Logical column indexes stay put, so callers can keep using fixed column
numbers. Only the header's visual order, widths, and hidden set change. The
result is stored in QSettings and restored the next time the view is created.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QObject, QSettings, Qt
from PySide6.QtWidgets import QHeaderView, QMenu, QTableWidget, QTreeWidget

_SETTINGS_ORG = "dbc-viewer"
_SETTINGS_APP = "DBC Viewer"


def install_column_config(view, settings_key: str) -> "ColumnConfig":
    """Enable resize, drag-reorder, and a right-click show/hide menu on `view`'s header."""
    config = ColumnConfig(view, settings_key)
    view._column_config = config
    return config


class ColumnConfig(QObject):
    def __init__(self, view, settings_key: str):
        super().__init__(view)
        self._view = view
        self._key = f"columns/{settings_key}"
        self._loading = False
        header = self.header()
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.setToolTip(
            "Drag a column edge to resize. Drag the header to reorder. "
            "Right-click to show or hide columns."
        )
        header.customContextMenuRequested.connect(self._on_context_menu)
        header.sectionMoved.connect(self._save)
        header.sectionResized.connect(self._save)
        self._restore()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)

    def header(self):
        if isinstance(self._view, QTreeWidget):
            return self._view.header()
        return self._view.horizontalHeader()

    def column_name(self, logical: int) -> str:
        if isinstance(self._view, QTableWidget):
            item = self._view.horizontalHeaderItem(logical)
            text = item.text().strip() if item is not None else ""
        else:
            text = self._view.headerItem().text(logical).strip() if self._view.headerItem() else ""
        return text or f"Column {logical + 1}"

    def build_menu(self, logical: int) -> QMenu:
        header = self.header()
        menu = QMenu(header)
        menu.setToolTipsVisible(True)
        if logical >= 0 and not header.isSectionHidden(logical):
            hide = menu.addAction(f"Hide “{self.column_name(logical)}”")
            hide.setEnabled(self._visible_count() > 1)
            hide.triggered.connect(lambda _checked=False, col=logical: self.hide_column(col))
        hidden = [index for index in range(header.count()) if header.isSectionHidden(index)]
        add_menu = menu.addMenu("Add column")
        add_menu.setToolTip("Show a column that was removed")
        if not hidden:
            add_menu.setEnabled(False)
        for index in hidden:
            action = add_menu.addAction(self.column_name(index))
            action.triggered.connect(lambda _checked=False, col=index: self.show_column(col))
        menu.addSeparator()
        for index in range(header.count()):
            action = menu.addAction(self.column_name(index))
            action.setCheckable(True)
            visible = not header.isSectionHidden(index)
            action.setChecked(visible)
            if visible and self._visible_count() == 1:
                action.setEnabled(False)
            action.toggled.connect(lambda checked, col=index: self.set_column_visible(col, checked))
        return menu

    def hide_column(self, logical: int):
        if self._visible_count() <= 1:
            return
        self.header().setSectionHidden(logical, True)
        self._save()

    def show_column(self, logical: int):
        self.set_column_visible(logical, True)

    def set_column_visible(self, logical: int, visible: bool):
        header = self.header()
        if not visible and self._visible_count() <= 1 and not header.isSectionHidden(logical):
            return
        header.setSectionHidden(logical, not visible)
        self._save()

    def _visible_count(self) -> int:
        header = self.header()
        return sum(1 for index in range(header.count()) if not header.isSectionHidden(index))

    def _on_context_menu(self, pos):
        header = self.header()
        menu = self.build_menu(header.logicalIndexAt(pos))
        menu.exec(header.mapToGlobal(pos))

    def _restore(self):
        state = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(self._key)
        blob = _as_bytes(state)
        if not blob:
            return
        self._loading = True
        try:
            self.header().restoreState(QByteArray(blob))
        finally:
            self._loading = False

    def _save(self, *_args):
        if self._loading:
            return
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(self._key, self.header().saveState())


def _as_bytes(state) -> bytes:
    if state is None:
        return b""
    if isinstance(state, QByteArray):
        return bytes(state)
    if isinstance(state, (bytes, bytearray)):
        return bytes(state)
    return b""

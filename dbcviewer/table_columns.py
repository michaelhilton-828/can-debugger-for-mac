"""Show, hide, reorder, and resize columns on a table or tree header.

Logical column indexes stay put, so callers can keep using fixed column
numbers. Only the header's visual order, widths, and hidden set change. The
result is stored in QSettings and restored the next time the view is created.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QEvent, QObject, QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QTableWidget,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .theme import set_muted

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
            "Drag a column edge to resize. Drag a header to reorder; "
            "the view scrolls when the pointer reaches either edge. "
            "Right-click to show, hide, or drag columns into order."
        )
        header.customContextMenuRequested.connect(self._on_context_menu)
        header.sectionMoved.connect(self._save)
        header.sectionResized.connect(self._save)
        self._restore()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)
        self._reorder_drag = False
        self._scroll_dir = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._scroll_header)
        header.viewport().installEventFilter(self)

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
            hide.triggered.connect(lambda _checked=False, col=logical: self._hide_from_menu(col))
        order = _ColumnOrderList(self)
        self._order_list = order
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(4)
        hint = QLabel("Drag to reorder")
        set_muted(hint)
        layout.addWidget(hint)
        layout.addWidget(order)
        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        return menu

    def visual_order(self) -> list[int]:
        header = self.header()
        return [header.logicalIndex(visual) for visual in range(header.count())]

    def apply_visual_order(self, logical_indexes: list[int]):
        """Put columns in `logical_indexes` from left to right."""
        header = self.header()
        if len(logical_indexes) != header.count():
            return
        if sorted(logical_indexes) != list(range(header.count())):
            return
        # sectionMoved saves as it goes. Apply the whole order, then save once.
        self._loading = True
        try:
            for target, logical in enumerate(logical_indexes):
                current = header.visualIndex(logical)
                if current != target:
                    header.moveSection(current, target)
        finally:
            self._loading = False
        self._save()

    def _hide_from_menu(self, logical: int):
        self.hide_column(logical)
        order = getattr(self, "_order_list", None)
        if order is not None:
            order.sync_checks()

    def hide_column(self, logical: int):
        if self._visible_count() <= 1:
            return
        self.header().setSectionHidden(logical, True)
        self._save()

    def show_column(self, logical: int):
        self.set_column_visible(logical, True)

    def set_column_visible(self, logical: int, visible: bool) -> bool:
        header = self.header()
        if not visible and self._visible_count() <= 1 and not header.isSectionHidden(logical):
            return False
        header.setSectionHidden(logical, not visible)
        if visible:
            self._reveal(logical)
        self._save()
        return True

    def _reveal(self, logical: int):
        """Scroll a column that was off the right edge into view."""
        header = self.header()
        if logical < 0 or header.isSectionHidden(logical):
            return
        bar = self._view.horizontalScrollBar()
        start = header.sectionPosition(logical)
        end = start + header.sectionSize(logical)
        page = bar.pageStep() or header.viewport().width()
        value = bar.value()
        if start < value:
            bar.setValue(start)
        elif end > value + page:
            bar.setValue(max(0, end - page))

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

    def eventFilter(self, obj, event):
        header = self.header()
        if obj is not header.viewport():
            return super().eventFilter(obj, event)
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
            self._reorder_drag = self._press_is_reorder(event.pos())
            self._scroll_dir = 0
        elif (
            event.type() == QEvent.Type.MouseMove
            and event.buttons() & Qt.LeftButton
            and self._reorder_drag
        ):
            self._scroll_dir = self._edge_direction(event.pos())
            if self._scroll_dir:
                self._scroll_header()
                if not self._scroll_timer.isActive():
                    self._scroll_timer.start()
            else:
                self._scroll_timer.stop()
        elif event.type() == QEvent.Type.MouseButtonRelease:
            self._reorder_drag = False
            self._scroll_dir = 0
            self._scroll_timer.stop()
        return super().eventFilter(obj, event)

    def _press_is_reorder(self, pos) -> bool:
        """True when the press is on a header label, not its resize grip."""
        header = self.header()
        logical = header.logicalIndexAt(pos)
        if logical < 0 or header.isSectionHidden(logical):
            return False
        left = header.sectionViewportPosition(logical)
        width = header.sectionSize(logical)
        grip = 5
        if width <= grip * 2:
            return True
        x = pos.x()
        return left + grip < x < left + width - grip

    def _edge_direction(self, pos) -> int:
        width = self.header().viewport().width()
        margin = 28
        if pos.x() >= width - margin:
            return 1
        if pos.x() <= margin:
            return -1
        return 0

    def _scroll_header(self):
        if not self._scroll_dir:
            self._scroll_timer.stop()
            return
        bar = self._view.horizontalScrollBar()
        bar.setValue(bar.value() + 18 * self._scroll_dir)


class _ColumnOrderList(QListWidget):
    """Every column, in visual order, including ones scrolled off the table."""

    def __init__(self, config: ColumnConfig):
        super().__init__()
        self._config = config
        self._loading = False
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumWidth(168)
        self.itemChanged.connect(self._on_item_changed)
        self.model().rowsMoved.connect(self._on_rows_moved)
        self.reload()

    def reload(self):
        self._loading = True
        self.clear()
        header = self._config.header()
        try:
            for visual in range(header.count()):
                logical = header.logicalIndex(visual)
                item = QListWidgetItem(self._config.column_name(logical))
                item.setData(Qt.UserRole, logical)
                item.setFlags(self._flags(logical))
                item.setCheckState(Qt.Unchecked if header.isSectionHidden(logical) else Qt.Checked)
                self.addItem(item)
        finally:
            self._loading = False
        row_height = self.sizeHintForRow(0) if self.count() else 22
        self.setFixedHeight(self.count() * max(row_height, 22) + 6)

    def sync_checks(self):
        header = self._config.header()
        self._loading = True
        try:
            for row in range(self.count()):
                item = self.item(row)
                logical = item.data(Qt.UserRole)
                item.setFlags(self._flags(logical))
                item.setCheckState(Qt.Unchecked if header.isSectionHidden(logical) else Qt.Checked)
        finally:
            self._loading = False

    def _flags(self, logical: int):
        header = self._config.header()
        flags = (
            Qt.ItemIsEnabled
            | Qt.ItemIsSelectable
            | Qt.ItemIsDragEnabled
            | Qt.ItemIsDropEnabled
        )
        visible = not header.isSectionHidden(logical)
        if not (visible and self._config._visible_count() <= 1):
            flags |= Qt.ItemIsUserCheckable
        return flags

    def _on_item_changed(self, item):
        if self._loading:
            return
        logical = item.data(Qt.UserRole)
        want = item.checkState() == Qt.Checked
        hidden = self._config.header().isSectionHidden(logical)
        if want != hidden:
            return
        if not self._config.set_column_visible(logical, want):
            self._loading = True
            item.setCheckState(Qt.Checked if hidden else Qt.Unchecked)
            self._loading = False
            return
        self.sync_checks()

    def _on_rows_moved(self, _parent, start, end, _destination, row):
        if self._loading:
            return
        order = self.logical_order()
        self._config.apply_visual_order(order)
        count = end - start + 1
        landed = row - count if row > end else row
        item = self.item(landed) if 0 <= landed < self.count() else None
        if item is not None:
            self._config._reveal(item.data(Qt.UserRole))

    def logical_order(self) -> list[int]:
        return [self.item(row).data(Qt.UserRole) for row in range(self.count())]


def _as_bytes(state) -> bytes:
    if state is None:
        return b""
    if isinstance(state, QByteArray):
        return bytes(state)
    if isinstance(state, (bytes, bytearray)):
        return bytes(state)
    return b""

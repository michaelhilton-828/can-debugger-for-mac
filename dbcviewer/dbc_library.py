"""Shared list of loaded DBC files.

The Home tab is the only editor of this list. Other tabs read it. Order is
priority order: when two files define the same CAN ID, the earlier entry is
the one used to decode that ID.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from .dbc_model import DbcLoadError, load_database

MAX_DBCS = 5
_SETTINGS_ORG = "dbc-viewer"
_SETTINGS_APP = "DBC Viewer"
_SETTINGS_KEY = "home_dbcs"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _parse_paths(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(path) for path in raw if path]
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [path for path in data if isinstance(path, str) and path]


@dataclass
class LoadedDbc:
    path: str
    label: str
    database: object

    @property
    def message_count(self) -> int:
        messages = getattr(self.database, "messages", None)
        if messages is None:
            return 0
        return len(messages)


class DbcLibrary(QObject):
    """At most five loaded DBC files, persisted across launches."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[LoadedDbc] = []
        self._selected = -1
        self._load_settings()

    def entries(self) -> list[LoadedDbc]:
        return list(self._entries)

    def selected_index(self) -> int:
        return self._selected

    def set_selected_index(self, index: int) -> None:
        if not self._entries:
            index = -1
        elif not 0 <= index < len(self._entries):
            return
        if index == self._selected:
            return
        self._selected = index
        self.changed.emit()

    def add_paths(self, paths) -> None:
        """Load `paths`, skipping duplicates. Stops at five files."""
        incoming = [path for path in paths if path]
        if not incoming:
            return
        added = False
        hit_cap = False
        for path in incoming:
            key = _norm(path)
            if any(_norm(entry.path) == key for entry in self._entries):
                continue
            if len(self._entries) >= MAX_DBCS:
                hit_cap = True
                break
            try:
                database = load_database(path)
            except DbcLoadError as exc:
                QMessageBox.critical(self._dialog_parent(), "Failed to load .dbc file", str(exc))
                continue
            absolute = os.path.abspath(path)
            self._entries.append(
                LoadedDbc(path=absolute, label=os.path.basename(absolute), database=database)
            )
            added = True
        if hit_cap:
            QMessageBox.information(
                self._dialog_parent(),
                "DBC limit reached",
                f"A maximum of {MAX_DBCS} DBC files can be loaded. Extra files were not added.",
            )
        if not added:
            return
        if self._selected < 0:
            self._selected = 0
        self._save()
        self.changed.emit()

    def remove_at(self, index: int) -> None:
        if not 0 <= index < len(self._entries):
            return
        del self._entries[index]
        if not self._entries:
            self._selected = -1
        elif self._selected > index:
            self._selected -= 1
        elif self._selected >= len(self._entries):
            self._selected = len(self._entries) - 1
        self._save()
        self.changed.emit()

    def move(self, from_index: int, to_index: int) -> None:
        """Reorder so the entry at `from_index` ends up at `to_index`."""
        count = len(self._entries)
        if not 0 <= from_index < count or not 0 <= to_index < count:
            return
        if from_index == to_index:
            return
        entry = self._entries.pop(from_index)
        self._entries.insert(to_index, entry)
        if self._selected == from_index:
            self._selected = to_index
        elif from_index < self._selected <= to_index:
            self._selected -= 1
        elif to_index <= self._selected < from_index:
            self._selected += 1
        self._save()
        self.changed.emit()

    def _dialog_parent(self):
        parent = self.parent()
        if isinstance(parent, QWidget):
            return parent
        return None

    def _settings(self) -> QSettings:
        return QSettings(_SETTINGS_ORG, _SETTINGS_APP)

    def _save(self) -> None:
        payload = json.dumps([entry.path for entry in self._entries])
        self._settings().setValue(_SETTINGS_KEY, payload)

    def _load_settings(self) -> None:
        paths = _parse_paths(self._settings().value(_SETTINGS_KEY, "[]"))
        dropped = False
        for path in paths:
            if len(self._entries) >= MAX_DBCS:
                dropped = True
                break
            if not os.path.isfile(path):
                dropped = True
                continue
            key = _norm(path)
            if any(_norm(entry.path) == key for entry in self._entries):
                dropped = True
                continue
            try:
                database = load_database(path)
            except DbcLoadError:
                dropped = True
                continue
            absolute = os.path.abspath(path)
            self._entries.append(
                LoadedDbc(path=absolute, label=os.path.basename(absolute), database=database)
            )
        if self._entries:
            self._selected = 0
        if dropped or len(paths) != len(self._entries):
            self._save()

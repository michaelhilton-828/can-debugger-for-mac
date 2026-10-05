"""Persist small pieces of window layout in QSettings."""

from __future__ import annotations

import json

from PySide6.QtCore import QSettings

_SETTINGS_ORG = "dbc-viewer"
_SETTINGS_APP = "DBC Viewer"


def remember_splitter(splitter, settings_key: str) -> None:
    """Restore `splitter` sizes and save them whenever the user moves a handle.

    No-op when the splitter has no children yet. Call this after the panes
    have been added. A pane cannot be dragged all the way closed.
    """
    if splitter.count() == 0:
        return

    splitter.setChildrenCollapsible(False)
    key = f"splitters/{settings_key}"
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    state = {"restoring": False}

    def save(*_args):
        if state["restoring"]:
            return
        sizes = [int(size) for size in splitter.sizes()]
        settings.setValue(key, json.dumps(sizes))

    splitter.splitterMoved.connect(save)
    sizes = _sizes_from_settings(settings.value(key))
    if sizes is not None and len(sizes) == splitter.count():
        state["restoring"] = True
        try:
            splitter.setSizes(sizes)
        finally:
            state["restoring"] = False


def _sizes_from_settings(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, (list, tuple)):
        try:
            return [int(size) for size in value]
        except (TypeError, ValueError):
            return None
    return None

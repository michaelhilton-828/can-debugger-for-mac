#!/usr/bin/env python3
"""DBC Viewer: a small, read-only desktop GUI for browsing CAN .dbc files.

Run with:  .venv/bin/python main.py
"""

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QTabWidget

from dbcviewer.browser import BrowserTab
from dbcviewer.compare import CompareTab
from dbcviewer.live_signal import LiveSignalViewerTab
from dbcviewer.log_replay import LogReplayTab
from dbcviewer.log_replay_model import supported_log_extensions

MAX_RECENT = 8


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DBC Viewer")
        self.resize(1300, 850)
        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        self.browser_tab = BrowserTab()
        self.compare_tab = CompareTab()
        self.log_replay_tab = LogReplayTab()
        self.live_signal_tab = LiveSignalViewerTab()
        self.tabs.addTab(self.browser_tab, "DBC Viewer")
        self.tabs.addTab(self.compare_tab, "DBC Compare")
        self.tabs.addTab(self.log_replay_tab, "Log Replay")
        self.tabs.addTab(self.live_signal_tab, "Live Signal Viewer")
        self.setCentralWidget(self.tabs)

        self._settings = QSettings("dbc-viewer", "DBC Viewer")

        self.browser_tab.database_loaded.connect(self.log_replay_tab.set_browser_database)
        self.browser_tab.database_loaded.connect(self.live_signal_tab.set_browser_database)
        self.browser_tab.database_loaded.connect(self.compare_tab.set_browser_database)
        self.browser_tab.database_loaded.connect(lambda _db, path: self._remember("recent_dbc", path))
        self.compare_tab.dbc_loaded.connect(lambda path: self._remember("recent_dbc", path))
        self.compare_tab.open_in_viewer.connect(self._open_in_viewer)
        self.log_replay_tab.log_loaded.connect(lambda path: self._remember("recent_log", path))

        self._build_menu()

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open .dbc…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.browser_tab.open_file_dialog)
        file_menu.addAction(open_action)

        self.recent_menu = file_menu.addMenu("Open &Recent")
        self._rebuild_recent_menu()

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def _recent(self, key: str) -> list[str]:
        raw = self._settings.value(key, "[]")
        if isinstance(raw, list):
            return [str(path) for path in raw]
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [path for path in data if isinstance(path, str)]

    def _remember(self, key: str, path: str):
        items = [item for item in self._recent(key) if item != path]
        items.insert(0, path)
        self._settings.setValue(key, json.dumps(items[:MAX_RECENT]))
        self._rebuild_recent_menu()

    def _forget(self, key: str, path: str):
        items = [item for item in self._recent(key) if item != path]
        self._settings.setValue(key, json.dumps(items))
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self.recent_menu.clear()
        dbcs = self._recent("recent_dbc")
        logs = self._recent("recent_log")
        if not dbcs and not logs:
            empty = QAction("No recent files", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return
        if dbcs:
            header = QAction("DBC files", self)
            header.setEnabled(False)
            self.recent_menu.addAction(header)
            for path in dbcs:
                self.recent_menu.addAction(self._recent_action(path, "recent_dbc"))
        if logs:
            if dbcs:
                self.recent_menu.addSeparator()
            header = QAction("Logs", self)
            header.setEnabled(False)
            self.recent_menu.addAction(header)
            for path in logs:
                self.recent_menu.addAction(self._recent_action(path, "recent_log"))
        self.recent_menu.addSeparator()
        clear = QAction("Clear recent", self)
        clear.triggered.connect(self._clear_recent)
        self.recent_menu.addAction(clear)

    def _recent_action(self, path: str, key: str) -> QAction:
        action = QAction(Path(path).name, self)
        action.setToolTip(path)
        action.setData((key, path))
        action.triggered.connect(lambda _checked=False, k=key, p=path: self._open_recent(k, p))
        return action

    def _open_recent(self, key: str, path: str):
        if not os.path.isfile(path):
            QMessageBox.warning(self, "File not found", f"Could not find:\n{path}")
            self._forget(key, path)
            return
        if key == "recent_dbc":
            self.tabs.setCurrentWidget(self.browser_tab)
            self.browser_tab.load_file(path)
        else:
            self.tabs.setCurrentWidget(self.log_replay_tab)
            self.log_replay_tab.load_log_file(path)

    def _clear_recent(self):
        self._settings.setValue("recent_dbc", "[]")
        self._settings.setValue("recent_log", "[]")
        self._rebuild_recent_menu()

    def _open_in_viewer(self, message_name: str, signal_name: str):
        if self.browser_tab.show_message(message_name, signal_name or None):
            self.tabs.setCurrentWidget(self.browser_tab)
            return
        QMessageBox.information(
            self,
            "Not in the open DBC",
            f"'{message_name}' is not in the DBC currently loaded in the viewer.",
        )

    # --- drag-and-drop: .dbc into the viewer (or Compare, if that tab is
    # current); a log file into Log Replay.
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if not path:
            return
        lower = path.lower()
        if lower.endswith(".dbc"):
            if self.tabs.currentWidget() is self.compare_tab:
                which = "a" if self.compare_tab.database_a is None else "b"
                self.compare_tab.load_path(which, path)
            else:
                self.tabs.setCurrentWidget(self.browser_tab)
                self.browser_tab.load_file(path)
            return
        if any(lower.endswith(ext) for ext in supported_log_extensions()):
            self.tabs.setCurrentWidget(self.log_replay_tab)
            self.log_replay_tab.load_log_file(path)

    def closeEvent(self, event):
        self.live_signal_tab.shutdown()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""DBC Viewer: a small, read-only desktop GUI for browsing CAN .dbc files.

Run with:  .venv/bin/python main.py
"""

import sys

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget

from dbcviewer.browser import BrowserTab
from dbcviewer.compare import CompareTab
from dbcviewer.log_replay import LogReplayTab


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
        self.tabs.addTab(self.browser_tab, "DBC Viewer")
        self.tabs.addTab(self.compare_tab, "DBC Compare")
        self.tabs.addTab(self.log_replay_tab, "Log Replay")
        self.setCentralWidget(self.tabs)

        self.browser_tab.database_loaded.connect(self.log_replay_tab.set_browser_database)

        self._build_menu()

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open .dbc…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.browser_tab.open_file_dialog)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    # --- drag-and-drop: dropping a .dbc file loads it into the Browse tab
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if path.lower().endswith(".dbc"):
            self.tabs.setCurrentWidget(self.browser_tab)
            self.browser_tab.load_file(path)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

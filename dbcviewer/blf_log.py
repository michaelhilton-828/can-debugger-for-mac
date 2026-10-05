"""Thread-safe Vector BLF session for the live capture worker.

python-can only — no Qt. The GUI thread opens and closes the session;
the capture thread calls write().
"""

from __future__ import annotations

import threading

import can


class BlfLogSession:
    """Overwrite a path with classic CAN and CAN FD frames, including BRS."""

    def __init__(self, path: str) -> None:
        self._writer = can.BLFWriter(path, append=False)
        self._lock = threading.Lock()
        self._count = 0
        self._closed = False

    def write(self, msg: can.Message) -> None:
        with self._lock:
            if self._closed:
                return
            self._writer.on_message_received(msg)
            self._count += 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._writer.stop()

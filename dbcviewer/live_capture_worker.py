"""Background CAN receive worker.

QtCore + python-can only - no QtWidgets, no matplotlib. The can.Bus is
created AND destroyed entirely inside run(), on this thread; the GUI thread
never touches the Bus object directly. python-can's pcan backend has no
internal lock guarding recv()/send()/shutdown() across threads, so this
sidesteps that hazard by construction rather than by careful coordination.
"""

from __future__ import annotations

import time

import can
from PySide6.QtCore import QThread, Signal

from .live_capture_model import classify_frame, decode_frame

BATCH_INTERVAL_S = 0.075  # flush accumulated samples to the GUI thread every ~75ms
RECV_TIMEOUT_S = 0.2  # bus.recv() timeout per poll iteration


class LiveCaptureWorker(QThread):
    """Owns a can.Bus for the lifetime of one run() call.

    Never call connect/recv/shutdown from outside this thread - the bus
    object itself never leaves run()'s local scope.
    """

    connected = Signal()
    samples_ready = Signal(list)  # list[DecodedSample], batched
    traffic_counts = Signal(int, int, int)  # frames, error frames, unmapped IDs in this batch
    error = Signal(str)
    finished_clean = Signal()

    def __init__(self, bus_kwargs: dict, database, parent=None):
        super().__init__(parent)
        self._bus_kwargs = bus_kwargs  # interface="pcan" already included by caller
        self._database = database

    def run(self) -> None:
        try:
            bus = can.Bus(**self._bus_kwargs)
        except can.CanInitializationError as exc:
            self.error.emit(f"Could not open PCAN device:\n{exc}")
            return
        except OSError as exc:
            self.error.emit(
                f"Native PCAN library not found (is MacCAN's PCBUSB driver installed?):\n{exc}"
            )
            return

        self.connected.emit()
        t0 = time.monotonic()
        batch = []
        frames = 0
        errors = 0
        unmapped = 0
        last_flush = t0

        try:
            while not self.isInterruptionRequested():
                try:
                    msg = bus.recv(timeout=RECV_TIMEOUT_S)
                except can.CanOperationError as exc:
                    self.error.emit(f"Bus error, disconnected:\n{exc}")
                    return
                if msg is not None:
                    frames += 1
                    kind = classify_frame(msg, self._database)
                    if kind == "error":
                        errors += 1
                    elif kind == "unmapped":
                        unmapped += 1
                    elif kind == "data":
                        for sample in decode_frame(msg, self._database):
                            sample.time = time.monotonic() - t0
                            batch.append(sample)
                now = time.monotonic()
                if (batch or frames) and (now - last_flush) >= BATCH_INTERVAL_S:
                    self._flush(batch, frames, errors, unmapped)
                    batch = []
                    frames = 0
                    errors = 0
                    unmapped = 0
                    last_flush = now
            self._flush(batch, frames, errors, unmapped)
            self.finished_clean.emit()
        finally:
            bus.shutdown()

    def _flush(self, batch, frames: int, errors: int, unmapped: int) -> None:
        if batch:
            self.samples_ready.emit(batch)
        if frames or errors or unmapped:
            self.traffic_counts.emit(frames, errors, unmapped)

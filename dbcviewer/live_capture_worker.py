"""Background CAN receive worker.

QtCore + python-can only - no QtWidgets, no matplotlib. The can.Bus is
created AND destroyed entirely inside run(), on this thread; the GUI thread
never touches the Bus object directly. python-can's pcan backend has no
internal lock guarding recv()/send()/shutdown() across threads, so this
sidesteps that hazard by construction rather than by careful coordination.
"""

from __future__ import annotations

import threading
import time

import can
from PySide6.QtCore import QThread, Signal

from .blf_log import BlfLogSession
from .live_capture_model import classify_frame, decode_frame, merge_snapshot

BATCH_INTERVAL_S = 0.075  # flush accumulated samples to the GUI thread every ~75ms
RECV_TIMEOUT_S = 0.2  # bus.recv() timeout per poll iteration


class LiveCaptureWorker(QThread):
    """Owns a can.Bus for the lifetime of one run() call.

    Never call connect/recv/shutdown from outside this thread - the bus
    object itself never leaves run()'s local scope. A BLF session is the
    exception: the GUI may swap one in with set_log_session while run()
    is in progress, and the GUI closes it.
    """

    connected = Signal()
    samples_ready = Signal(list)  # list[DecodedSample], batched
    frames_ready = Signal(list)  # list[FrameSnapshot], one per ID in this batch
    traffic_counts = Signal(int, int, int)  # frames, error frames, unmapped IDs in this batch
    error = Signal(str)
    log_error = Signal(str)  # logging stopped; capture keeps running
    finished_clean = Signal()

    def __init__(self, bus_kwargs: dict, databases, parent=None):
        super().__init__(parent)
        self._bus_kwargs = bus_kwargs  # interface="pcan" already included by caller
        self._databases = databases
        self._log_lock = threading.Lock()
        self._log_session: BlfLogSession | None = None

    def set_log_session(self, session: BlfLogSession | None) -> BlfLogSession | None:
        """Point the receive loop at a BLF session, or None to stop writing.

        Safe to call from the GUI thread while run() is in progress. Returns
        the previous session and does not close either one.
        """
        with self._log_lock:
            previous = self._log_session
            self._log_session = session
            return previous

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
        pending_frames: dict = {}
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
                    self._write_log(msg)
                    frames += 1
                    merge_snapshot(pending_frames, msg)
                    kind = classify_frame(msg, self._databases)
                    if kind == "error":
                        errors += 1
                    elif kind == "unmapped":
                        unmapped += 1
                    elif kind == "data":
                        for sample in decode_frame(msg, self._databases):
                            sample.time = time.monotonic() - t0
                            batch.append(sample)
                now = time.monotonic()
                if (batch or frames) and (now - last_flush) >= BATCH_INTERVAL_S:
                    self._flush(batch, frames, errors, unmapped, pending_frames)
                    batch = []
                    pending_frames = {}
                    frames = 0
                    errors = 0
                    unmapped = 0
                    last_flush = now
            self._flush(batch, frames, errors, unmapped, pending_frames)
            self.finished_clean.emit()
        finally:
            # GUI owns the BLF session. Leaving run() only stops writing.
            bus.shutdown()

    def _write_log(self, msg: can.Message) -> None:
        with self._log_lock:
            session = self._log_session
        if session is None:
            return
        try:
            session.write(msg)
        except Exception as exc:
            self.log_error.emit(f"BLF log write failed; logging stopped:\n{exc}")
            with self._log_lock:
                if self._log_session is session:
                    self._log_session = None
            try:
                session.close()
            except Exception:
                pass

    def _flush(self, batch, frames: int, errors: int, unmapped: int, pending_frames: dict) -> None:
        if batch:
            self.samples_ready.emit(batch)
        if pending_frames:
            self.frames_ready.emit(list(pending_frames.values()))
        if frames or errors or unmapped:
            self.traffic_counts.emit(frames, errors, unmapped)

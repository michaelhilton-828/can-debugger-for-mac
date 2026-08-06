"""Pure data/logic for live CAN capture: bus-connection-kwargs construction,
per-frame decoding, and rolling-window buffering.

Qt-free and matplotlib.pyplot-free, same contract as log_replay_model.py.
Building/tearing down a real can.Bus and running a receive loop are thread
lifecycle concerns and belong in live_capture_worker.py instead, so this
module stays trivially unit-testable without real hardware.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import can


class LiveCaptureError(Exception):
    """Raised for bus-config problems, with a human-readable message."""


@dataclass
class BusConfig:
    """User-configurable connection parameters for a PEAK PCAN adapter."""

    channel: str = "PCAN_USBBUS1"
    fd: bool = False
    classic_bitrate: int = 500_000
    fd_f_clock: int = 80_000_000
    fd_nom_bitrate: int = 500_000
    fd_nom_sample_point: float = 80.0
    fd_data_bitrate: int = 2_000_000
    fd_data_sample_point: float = 80.0
    receive_own_messages: bool = False


def build_bus_kwargs(config: BusConfig) -> dict:
    """Translate a BusConfig into kwargs for can.Bus(interface="pcan", **kwargs).

    Classic CAN: {"channel", "bitrate", "receive_own_messages"}.
    CAN FD: {"channel", "fd": True, "timing": BitTimingFd.from_sample_point(...),
    "receive_own_messages"} - this backend has no simple bitrate=/data_bitrate=
    shortcut for FD, see can/bit_timing.py's BitTimingFd.from_sample_point.

    interface="pcan" is deliberately NOT included here so this function
    stays a pure translation, testable without importing the pcan backend
    (which requires the native PCBUSB library to be installed) at all.
    """
    kwargs = {"channel": config.channel, "receive_own_messages": config.receive_own_messages}
    if config.fd:
        kwargs["fd"] = True
        kwargs["timing"] = can.BitTimingFd.from_sample_point(
            f_clock=config.fd_f_clock,
            nom_bitrate=config.fd_nom_bitrate,
            nom_sample_point=config.fd_nom_sample_point,
            data_bitrate=config.fd_data_bitrate,
            data_sample_point=config.fd_data_sample_point,
        )
    else:
        kwargs["bitrate"] = config.classic_bitrate
    return kwargs


@dataclass
class DecodedSample:
    """One decoded signal value from one received CAN frame."""

    message_name: str
    signal_name: str
    arbitration_id: int
    unit: str
    choices: dict
    time: float
    value: object

    @property
    def key(self) -> tuple[str, str]:
        return (self.message_name, self.signal_name)


def decode_frame(msg, database) -> list[DecodedSample]:
    """Decode one received can.Message against `database`.

    Same tolerant, best-effort philosophy as log_replay_model.load_log()'s
    inner loop: error/remote frames are skipped, an unmapped arbitration_id
    or any decode failure yields [] rather than raising - only setup-level
    problems (bad bus config, missing driver) are ever exceptional for live
    capture, not individual malformed/partial frames.
    """
    if msg.is_error_frame or msg.is_remote_frame:
        return []
    try:
        dbc_msg = database.get_message_by_frame_id(msg.arbitration_id)
    except KeyError:
        return []
    try:
        decoded = dbc_msg.decode(msg.data, decode_choices=True, allow_truncated=True)
    except Exception:
        return []

    samples = []
    for sig_name, value in decoded.items():
        sig_def = dbc_msg.get_signal_by_name(sig_name)
        numeric_value = value.value if hasattr(value, "value") else value
        samples.append(
            DecodedSample(
                message_name=dbc_msg.name,
                signal_name=sig_name,
                arbitration_id=msg.arbitration_id,
                unit=sig_def.unit or "",
                choices=sig_def.choices or {},
                time=msg.timestamp,
                value=numeric_value,
            )
        )
    return samples


@dataclass
class LiveSignalBuffer:
    """Rolling-window time-series for one (message, signal) pair."""

    message_name: str
    signal_name: str
    arbitration_id: int
    unit: str
    choices: dict = field(default_factory=dict)
    times: deque = field(default_factory=deque)
    values: deque = field(default_factory=deque)

    @property
    def key(self) -> tuple[str, str]:
        return (self.message_name, self.signal_name)

    @property
    def display_label(self) -> str:
        return f"{self.signal_name} [{self.unit}]" if self.unit else self.signal_name

    def append(self, t: float, value) -> None:
        self.times.append(t)
        self.values.append(value)

    def trim(self, now: float, window_seconds: float) -> None:
        """Drop samples older than (now - window_seconds) from the left.

        deque.popleft() is O(1); this runs continuously (~10 Hz) for the
        life of a connection, so a plain list's O(n) front-trim is the
        wrong complexity here - samples always arrive in increasing-time
        order, so trimming is always from the left end only.
        """
        cutoff = now - window_seconds
        while self.times and self.times[0] < cutoff:
            self.times.popleft()
            self.values.popleft()

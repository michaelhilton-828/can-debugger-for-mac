"""Helpers for decoding CAN log files against a loaded .dbc database.

Qt-free *and* matplotlib.pyplot-free: this module only extracts and shapes
data. Any Axes/Figure manipulation belongs in log_replay.py, so this stays
trivially testable without a display or a Qt/matplotlib backend.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import can
from can.io import MESSAGE_READERS


class LogLoadError(Exception):
    """Raised when a log file cannot be opened/parsed, with a human-readable message."""


@dataclass
class SignalSeries:
    """Decoded time-series for one (message, signal) pair extracted from a log."""

    message_name: str
    signal_name: str
    arbitration_id: int
    unit: str
    choices: dict = field(default_factory=dict)
    times: list = field(default_factory=list)
    values: list = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.message_name, self.signal_name)

    @property
    def display_label(self) -> str:
        if self.signal_name == "Unknown":
            return f"Unknown (0x{self.arbitration_id:X})"
        return f"{self.signal_name} [{self.unit}]" if self.unit else self.signal_name


def load_log(path: str, database) -> tuple[dict[tuple[str, str], SignalSeries], set[int]]:
    """Decode every frame in a CAN log file against `database`.

    Uses can.LogReader, which dispatches to the right reader by file
    extension (see supported_log_extensions()) - so any format python-can
    supports works here, not just Vector .blf. Frames whose arbitration ID
    has no matching message get one "Unknown" entry per ID instead of being
    dropped, so unmapped traffic is still visible in the signal list (with
    message/signal/unit shown as "Unknown"/"unknown" and the raw payload,
    interpreted as a big-endian integer, standing in for a decoded value).
    Frames that match a message but individually fail to decode are still
    skipped (the DBC is often a partial match for a log); only
    file-open/parse failures raise.

    Returns (signals, unmapped_ids), where unmapped_ids is the set of
    arbitration IDs seen in the log with no matching message in `database`.
    """
    try:
        reader = can.LogReader(path)
    except (ValueError, OSError) as exc:
        raise LogLoadError(f"Could not read log file:\n{exc}") from exc

    signals: dict[tuple[str, str], SignalSeries] = {}
    unmapped_ids: set[int] = set()
    try:
        with reader:
            for msg in reader:
                if msg.is_error_frame or msg.is_remote_frame:
                    continue
                try:
                    dbc_msg = database.get_message_by_frame_id(msg.arbitration_id)
                except KeyError:
                    unmapped_ids.add(msg.arbitration_id)
                    key = ("Unknown", f"id_{msg.arbitration_id}")
                    entry = signals.get(key)
                    if entry is None:
                        entry = SignalSeries(
                            message_name="Unknown",
                            signal_name="Unknown",
                            arbitration_id=msg.arbitration_id,
                            unit="unknown",
                        )
                        signals[key] = entry
                    entry.times.append(msg.timestamp)
                    entry.values.append(int.from_bytes(bytes(msg.data), "big") if msg.data else 0)
                    continue
                try:
                    decoded = dbc_msg.decode(msg.data, decode_choices=True, allow_truncated=True)
                except Exception:
                    continue
                for sig_name, value in decoded.items():
                    key = (dbc_msg.name, sig_name)
                    entry = signals.get(key)
                    if entry is None:
                        sig_def = dbc_msg.get_signal_by_name(sig_name)
                        entry = SignalSeries(
                            message_name=dbc_msg.name,
                            signal_name=sig_name,
                            arbitration_id=msg.arbitration_id,
                            unit=sig_def.unit or "",
                            choices=sig_def.choices or {},
                        )
                        signals[key] = entry
                    # NamedSignalValue (enum) isn't directly plottable; plot its
                    # underlying int and rely on choices for axis tick labels.
                    numeric_value = value.value if hasattr(value, "value") else value
                    entry.times.append(msg.timestamp)
                    entry.values.append(numeric_value)
    except LogLoadError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface reader-internal errors as LogLoadError
        raise LogLoadError(f"Failed while parsing log file:\n{exc}") from exc

    return signals, unmapped_ids


# Draw point markers only while this many samples (or fewer) are in view.
# A full log is a line; zooming in far enough brings the markers back.
MARKER_VISIBLE_LIMIT = 400


def marker_for_count(visible_count: int) -> str:
    """Matplotlib marker for a line with `visible_count` points in the x window.

    ``"None"`` is the marker style that draws the line with no point markers.
    A bare ``None`` is rejected by matplotlib's MarkerStyle.
    """
    if visible_count <= 0 or visible_count > MARKER_VISIBLE_LIMIT:
        return "None"
    return "."


def padded_range(lo: float, hi: float, fraction: float = 0.05) -> tuple[float, float]:
    """Expand [lo, hi] by `fraction` of its span so points are not clipped by the spine."""
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        pad = 1.0 if lo == 0 else abs(lo) * fraction
    else:
        pad = (hi - lo) * fraction
    return lo - pad, hi + pad


def zoom_about(lo: float, hi: float, center: float, scale: float) -> tuple[float, float]:
    """Scale the distance from `center` to each end. scale < 1 zooms in."""
    return center + (lo - center) * scale, center + (hi - center) * scale


def nearest_index(times, t: float):
    """Index of the sample closest to `t`, or None if `times` is empty.

    `times` must be sorted ascending.
    """
    if not times:
        return None
    i = bisect.bisect_left(times, t)
    if i >= len(times):
        return len(times) - 1
    if i == 0:
        return 0
    if abs(times[i] - t) < abs(times[i - 1] - t):
        return i
    return i - 1


def format_signal_value(value, choices) -> str:
    """Physical value, or the value-table label when one matches exactly."""
    if choices:
        if value in choices:
            return str(choices[value])
        if isinstance(value, float) and value.is_integer():
            iv = int(value)
            if iv in choices:
                return str(choices[iv])
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def numeric_minmax(times, values, t_min: float, t_max: float):
    """Min/max of numeric samples whose time falls in [t_min, t_max], or None."""
    lo = min(t_min, t_max)
    hi = max(t_min, t_max)
    nums = [
        v
        for t, v in zip(times, values)
        if lo <= t <= hi and isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if not nums:
        return None
    return min(nums), max(nums)


def enum_ticks(choices: dict) -> tuple[list, list[str]]:
    """Sorted (positions, labels) for relabeling a Y-axis from a signal's
    value table, or ([], []) if `choices` is empty/falsy."""
    if not choices:
        return [], []
    positions = sorted(choices.keys())
    return positions, [str(choices[p]) for p in positions]


def supported_log_extensions() -> list[str]:
    """File extensions (e.g. ['.asc', '.blf', ...]) that can.LogReader can
    open, read live from python-can rather than hardcoded."""
    return sorted(MESSAGE_READERS.keys())

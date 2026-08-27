"""Helpers for decoding CAN log files against loaded .dbc databases.

Qt-free *and* matplotlib.pyplot-free: this module only extracts and shapes
data. Any Axes/Figure manipulation belongs in log_replay.py, so this stays
trivially testable without a display or a Qt/matplotlib backend.

A log is decoded against an ordered list of DBCs rather than a single one,
because a bus is often described by one .dbc per sending node. The databases
are deliberately kept as separate objects instead of being merged with
cantools' Database.add_dbc_file(): that helper resolves a frame-id clash by
silently overwriting the earlier message in its lookup dict, which would drop
signals with nothing but a logging warning to show for it.
"""

from __future__ import annotations

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
    slot: int = 0  # 1-based DBC slot that decoded this; 0 for unmapped traffic
    source: str = ""  # display label of that DBC ("" for unmapped traffic)
    ambiguous: bool = False  # this frame id is defined differently by another slot

    @property
    def key(self) -> tuple[int, str, str]:
        """Identity within a decode run. Includes the slot so two DBCs that
        define the same message/signal name never collapse into one series."""
        return (self.slot, self.message_name, self.signal_name)

    @property
    def display_label(self) -> str:
        if self.signal_name == "Unknown":
            return f"Unknown (0x{self.arbitration_id:X})"
        base = f"{self.signal_name} [{self.unit}]" if self.unit else self.signal_name
        # Only name the DBC when it actually disambiguates, so the common case
        # keeps short plot legends.
        return f"{base} (DBC {self.slot})" if self.ambiguous else base


def _message_fingerprint(message) -> tuple:
    """Definition-identity of a message, used to tell a message that two DBCs
    merely share from one they genuinely disagree about."""
    return (
        message.name,
        message.length,
        bool(message.is_extended_frame),
        bool(getattr(message, "is_fd", False)),
        tuple(
            sorted(
                (s.name, s.start, s.length, s.byte_order, bool(s.is_signed), s.scale, s.offset)
                for s in message.signals
            )
        ),
    )


def build_message_index(databases) -> tuple[dict[int, list], set[int]]:
    """Index the messages of an ordered list of (label, database) pairs.

    `databases` is positional: a None database keeps its place so the 1-based
    slot numbers stay stable (slot 2 is slot 2 even when slot 1 is empty).

    Returns (index, conflicts), where index maps a frame id to the list of
    (slot, label, message) that define it. A frame id defined *identically*
    by several DBCs is indexed once, from the earliest slot. One defined
    *differently* is indexed from every slot that defines it and its id lands
    in `conflicts`, so the caller decodes it under both interpretations and
    can flag the ambiguity rather than silently picking a winner.

    Building this up front also keeps the per-frame decode path to a single
    dict lookup - logs routinely run to hundreds of thousands of frames, so a
    per-frame, per-database exception-driven lookup is worth avoiding.
    """
    index: dict[int, list] = {}
    fingerprints: dict[int, list] = {}
    conflicts: set[int] = set()

    for position, (label, database) in enumerate(databases):
        if database is None:
            continue
        slot = position + 1
        for message in database.messages:
            frame_id = message.frame_id
            fingerprint = _message_fingerprint(message)
            seen = fingerprints.setdefault(frame_id, [])
            if fingerprint in seen:
                continue
            if seen:
                conflicts.add(frame_id)
            seen.append(fingerprint)
            index.setdefault(frame_id, []).append((slot, label, message))

    return index, conflicts


def load_log(path: str, databases) -> tuple[dict[tuple[int, str, str], SignalSeries], set[int], set[int]]:
    """Decode every frame in a CAN log file against `databases`.

    `databases` is an ordered list of (label, database) pairs - see
    build_message_index() for how frame ids present in more than one are
    resolved.

    Uses can.LogReader, which dispatches to the right reader by file
    extension (see supported_log_extensions()) - so any format python-can
    supports works here, not just Vector .blf. Frames whose arbitration ID
    matches no message in any DBC get one "Unknown" entry per ID instead of
    being dropped, so unmapped traffic is still visible in the signal list
    (with message/signal/unit shown as "Unknown"/"unknown" and the raw
    payload, interpreted as a big-endian integer, standing in for a decoded
    value). Frames that match a message but individually fail to decode are
    still skipped (a DBC is often a partial match for a log); only
    file-open/parse failures raise.

    Returns (signals, unmapped_ids, conflicts), where unmapped_ids is the set
    of arbitration IDs seen in the log that no loaded DBC describes, and
    conflicts is the set of IDs described differently by two DBCs.
    """
    index, conflicts = build_message_index(databases)

    try:
        reader = can.LogReader(path)
    except (ValueError, OSError) as exc:
        raise LogLoadError(f"Could not read log file:\n{exc}") from exc

    signals: dict[tuple[int, str, str], SignalSeries] = {}
    unmapped_ids: set[int] = set()
    try:
        with reader:
            for msg in reader:
                if msg.is_error_frame or msg.is_remote_frame:
                    continue

                entries = index.get(msg.arbitration_id)
                if not entries:
                    unmapped_ids.add(msg.arbitration_id)
                    key = (0, "Unknown", f"id_{msg.arbitration_id}")
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

                ambiguous = msg.arbitration_id in conflicts
                for slot, label, dbc_msg in entries:
                    try:
                        decoded = dbc_msg.decode(msg.data, decode_choices=True, allow_truncated=True)
                    except Exception:
                        continue
                    for sig_name, value in decoded.items():
                        key = (slot, dbc_msg.name, sig_name)
                        entry = signals.get(key)
                        if entry is None:
                            sig_def = dbc_msg.get_signal_by_name(sig_name)
                            entry = SignalSeries(
                                message_name=dbc_msg.name,
                                signal_name=sig_name,
                                arbitration_id=msg.arbitration_id,
                                unit=sig_def.unit or "",
                                choices=sig_def.choices or {},
                                slot=slot,
                                source=label,
                                ambiguous=ambiguous,
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

    return signals, unmapped_ids, conflicts


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

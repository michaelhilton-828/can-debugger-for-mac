"""Helpers for loading .dbc files and extracting display-friendly data.

Wraps cantools so the rest of the app never has to know about its object
model directly. All functions here are pure / Qt-free so they can be tested
and reused from both the browser and compare views.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cantools
from cantools.database import UnsupportedDatabaseFormatError


class DbcLoadError(Exception):
    """Raised when a .dbc file cannot be parsed, with a human-readable message."""


def load_database(path: str):
    """Load a .dbc file, tolerating minor spec violations.

    strict=False so that files with small inconsistencies (e.g. a signal
    that slightly overruns its message's declared length) still load —
    real-world DBCs in the wild are not always perfectly strict-compliant,
    and this is a read-only viewer, not a codec that needs to encode/decode
    safely. Genuine parse failures (bad syntax, unsupported format) still
    raise, but as a clear DbcLoadError instead of leaking cantools' internals.
    """
    try:
        return cantools.database.load_file(path, strict=False)
    except UnsupportedDatabaseFormatError as exc:
        raise DbcLoadError(f"Not a valid .dbc file:\n{exc}") from exc
    except (OSError, IOError) as exc:
        raise DbcLoadError(f"Could not read file:\n{exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface any parser error as a friendly message
        raise DbcLoadError(f"Failed to parse .dbc file:\n{exc}") from exc


def format_id_hex(frame_id: int, is_extended: bool) -> str:
    width = 8 if is_extended else 3
    return f"0x{frame_id:0{width}X}"


def format_senders(senders) -> str:
    return ", ".join(senders) if senders else "—"


def signal_bit_cells(signal) -> list[tuple[int, int]]:
    """Return the list of (byte_index, bit_weight) cells a signal occupies.

    bit_weight follows normal binary weight within the byte: 0 = LSB, 7 = MSB.

    DBC "start bit" is expressed differently per byte order:
      - little_endian (Intel): `start` is already the flat/LSB-first bit
        index (byte*8 + weight), and the signal's bits simply increase from
        there.
      - big_endian (Motorola): `start` is the *sawtooth*-numbered position of
        the signal's MSB (see cantools.database.utils.sawtooth_to_network_bitnum
        for the canonical description of that numbering). Converting the
        first bit to flat/network numbering and then walking forward by 1
        for each subsequent bit correctly follows the MSB-first, byte0-first
        significance order and lands on the right (byte, weight) cell even
        across byte boundaries.
    """
    cells = []
    if signal.byte_order == "big_endian":
        net0 = 8 * (signal.start // 8) + (7 - signal.start % 8)
        for i in range(signal.length):
            n = net0 + i
            cells.append((n // 8, 7 - (n % 8)))
    else:
        for i in range(signal.length):
            n = signal.start + i
            cells.append((n // 8, n % 8))
    return cells


def choices_list(signal) -> list[tuple[int, str]]:
    """Return the signal's value table as a sorted list of (raw_value, label)."""
    if not signal.choices:
        return []
    items = []
    for raw, label in signal.choices.items():
        items.append((raw, str(label)))
    items.sort(key=lambda item: item[0])
    return items


def mux_label(signal) -> str:
    """Short label describing a signal's multiplexing role, or '' if none."""
    if getattr(signal, "is_multiplexer", False):
        return "M"
    ids = getattr(signal, "multiplexer_ids", None)
    if ids:
        return "m" + ",".join(str(i) for i in ids)
    return ""


@dataclass
class SignalRow:
    """Flat, display-ready view of a cantools Signal."""

    name: str
    start: int
    length: int
    byte_order: str
    signed: bool
    factor: float
    offset: float
    minimum: float | None
    maximum: float | None
    unit: str
    mux: str
    choices: list[tuple[int, str]] = field(default_factory=list)
    signal: object = None  # original cantools Signal, for the bit-layout widget

    @classmethod
    def from_signal(cls, signal) -> "SignalRow":
        return cls(
            name=signal.name,
            start=signal.start,
            length=signal.length,
            byte_order="little" if signal.byte_order == "little_endian" else "big",
            signed=bool(signal.is_signed),
            factor=signal.scale,
            offset=signal.offset,
            minimum=signal.minimum,
            maximum=signal.maximum,
            unit=signal.unit or "",
            mux=mux_label(signal),
            choices=choices_list(signal),
            signal=signal,
        )


@dataclass
class MessageRow:
    """Flat, display-ready view of a cantools Message."""

    name: str
    frame_id: int
    is_extended: bool
    dlc: int
    senders: str
    message: object = None  # original cantools Message

    @classmethod
    def from_message(cls, message) -> "MessageRow":
        return cls(
            name=message.name,
            frame_id=message.frame_id,
            is_extended=bool(message.is_extended_frame),
            dlc=message.length,
            senders=format_senders(message.senders),
            message=message,
        )

    @property
    def id_hex(self) -> str:
        return format_id_hex(self.frame_id, self.is_extended)


def message_rows(database) -> list[MessageRow]:
    return [MessageRow.from_message(m) for m in database.messages]


def signal_rows(message) -> list[SignalRow]:
    return [SignalRow.from_signal(s) for s in message.signals]

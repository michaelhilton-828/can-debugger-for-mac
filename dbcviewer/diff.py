"""Pure diff engine comparing two loaded .dbc databases.

No Qt dependency here — this is plain data-crunching so it can be exercised
and tested on its own. Messages and signals are matched by *name* (not CAN
ID), since a common real-world edit is renumbering a message's ID while
keeping its name — that should surface as a "changed" message, not a
remove+add pair. Renaming a message with no other trace is indistinguishable
from add+remove and is treated as such; this is a reasonable, simple
tradeoff for an internal tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dbc_model import choices_list, format_senders, mux_label

# (field label, old value, new value)
FieldChange = tuple[str, str, str]


@dataclass
class SignalDiff:
    name: str
    changes: list[FieldChange] = field(default_factory=list)


@dataclass
class MessageDiff:
    name: str
    id_hex_a: str = ""
    id_hex_b: str = ""
    changes: list[FieldChange] = field(default_factory=list)
    signals_added: list[str] = field(default_factory=list)
    signals_removed: list[str] = field(default_factory=list)
    signals_changed: list[SignalDiff] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.changes
            or self.signals_added
            or self.signals_removed
            or self.signals_changed
        )


@dataclass
class DatabaseDiff:
    messages_added: list[str] = field(default_factory=list)
    messages_removed: list[str] = field(default_factory=list)
    messages_changed: list[MessageDiff] = field(default_factory=list)

    @property
    def signals_changed_count(self) -> int:
        return sum(len(m.signals_changed) for m in self.messages_changed)

    @property
    def signals_added_count(self) -> int:
        return sum(len(m.signals_added) for m in self.messages_changed)

    @property
    def signals_removed_count(self) -> int:
        return sum(len(m.signals_removed) for m in self.messages_changed)

    def summary(self) -> str:
        parts = []
        if self.messages_added:
            parts.append(f"{len(self.messages_added)} message(s) added")
        if self.messages_removed:
            parts.append(f"{len(self.messages_removed)} message(s) removed")
        changed_headers = sum(1 for m in self.messages_changed if m.changes)
        if changed_headers:
            parts.append(f"{changed_headers} message(s) with changed ID/DLC/node")
        if self.signals_added_count:
            parts.append(f"{self.signals_added_count} signal(s) added")
        if self.signals_removed_count:
            parts.append(f"{self.signals_removed_count} signal(s) removed")
        if self.signals_changed_count:
            parts.append(f"{self.signals_changed_count} signal(s) changed")
        if not parts:
            return "No differences found."
        return ", ".join(parts) + "."


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        # Trim floating noise but keep meaningful precision.
        return f"{value:g}"
    return str(value)


def _choices_repr(sig) -> str:
    items = choices_list(sig)
    if not items:
        return "—"
    return "; ".join(f"{raw}={label}" for raw, label in items)


def _diff_signal(sig_a, sig_b) -> SignalDiff:
    changes: list[FieldChange] = []

    def check(label, val_a, val_b):
        if val_a != val_b:
            changes.append((label, _fmt(val_a), _fmt(val_b)))

    check("start bit", sig_a.start, sig_b.start)
    check("length (bits)", sig_a.length, sig_b.length)
    check("byte order", sig_a.byte_order, sig_b.byte_order)
    check("signed", sig_a.is_signed, sig_b.is_signed)
    check("factor", sig_a.scale, sig_b.scale)
    check("offset", sig_a.offset, sig_b.offset)
    check("minimum", sig_a.minimum, sig_b.minimum)
    check("maximum", sig_a.maximum, sig_b.maximum)
    check("unit", sig_a.unit, sig_b.unit)
    check("mux", mux_label(sig_a), mux_label(sig_b))

    choices_a, choices_b = choices_list(sig_a), choices_list(sig_b)
    if choices_a != choices_b:
        changes.append(("value table", _choices_repr(sig_a), _choices_repr(sig_b)))

    return SignalDiff(name=sig_a.name, changes=changes)


def _diff_message(msg_a, msg_b) -> MessageDiff:
    md = MessageDiff(
        name=msg_a.name,
        id_hex_a=f"0x{msg_a.frame_id:X}",
        id_hex_b=f"0x{msg_b.frame_id:X}",
    )

    def check(label, val_a, val_b):
        if val_a != val_b:
            md.changes.append((label, _fmt(val_a), _fmt(val_b)))

    check("CAN ID", f"0x{msg_a.frame_id:X}", f"0x{msg_b.frame_id:X}")
    check("extended", msg_a.is_extended_frame, msg_b.is_extended_frame)
    check("DLC", msg_a.length, msg_b.length)
    check("transmitter node(s)", format_senders(msg_a.senders), format_senders(msg_b.senders))

    signals_a = {s.name: s for s in msg_a.signals}
    signals_b = {s.name: s for s in msg_b.signals}

    md.signals_added = sorted(set(signals_b) - set(signals_a))
    md.signals_removed = sorted(set(signals_a) - set(signals_b))

    for name in sorted(set(signals_a) & set(signals_b)):
        sd = _diff_signal(signals_a[name], signals_b[name])
        if sd.changes:
            md.signals_changed.append(sd)

    return md


def compare_databases(db_a, db_b) -> DatabaseDiff:
    messages_a = {m.name: m for m in db_a.messages}
    messages_b = {m.name: m for m in db_b.messages}

    result = DatabaseDiff()
    result.messages_added = sorted(set(messages_b) - set(messages_a))
    result.messages_removed = sorted(set(messages_a) - set(messages_b))

    for name in sorted(set(messages_a) & set(messages_b)):
        md = _diff_message(messages_a[name], messages_b[name])
        if md.has_changes:
            result.messages_changed.append(md)

    return result

# DBC Viewer

A simple desktop GUI for browsing CAN `.dbc` files. Read-only — it parses and
displays message/signal structure, it doesn't decode live traffic or log files.

## Features

- **Open a `.dbc` file** via a file picker, or drag-and-drop onto the window
- **Message list** — name, CAN ID (hex/dec), DLC, transmitting node(s)
- **Signal list** — start bit, length, byte order, sign, factor, offset,
  min/max, unit
- **Value tables** — enum labels (e.g. `0 = Off`, `1 = On`) for signals that
  define them
- **Bit-layout diagram** — a per-message grid showing which bits each signal
  occupies, so gaps and overlaps are visible at a glance
- **Search/filter** — filter the message list by name or ID, and the signal
  list by name
- **Compare mode** — load a second `.dbc` and see a side-by-side diff:
  messages/signals added, removed, or changed (ID, DLC, node, bit layout,
  scaling, value tables, etc.)

## Requirements

- Python 3.9+
- [PySide6](https://pypi.org/project/PySide6/) and
  [cantools](https://pypi.org/project/cantools/) (see `requirements.txt`)

## Setup

```bash
git clone git@github.com:michaelhilton-828/dbc-viewer.git
cd dbc-viewer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running

```bash
./run.sh
```

(or `.venv/bin/python main.py` directly)

## Building a standalone app (macOS)

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller "DBC Viewer.spec"
```

This produces `dist/DBC Viewer.app`, which runs standalone without needing
Python installed.

## Project layout

```
main.py             entry point / main window
dbcviewer/
  dbc_model.py       cantools wrapper, bit-position math
  browser.py         message/signal browser tab
  bitlayout.py       bit-layout diagram widget
  compare.py         two-file compare tab
  diff.py            diff engine (message/signal comparison)
samples/             small sample .dbc files for testing
```

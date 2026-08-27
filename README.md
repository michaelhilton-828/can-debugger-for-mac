# DBC Viewer

A desktop GUI for working with CAN `.dbc` files: browse message/signal
structure, compare two `.dbc` files, replay a logged CAN capture against a
`.dbc`, and view live CAN/CAN-FD traffic from a PEAK PCAN-USB adapter.

## Features

- **DBC Viewer tab** — open a `.dbc` file via a file picker or drag-and-drop:
  - **Message list** — name, CAN ID (hex/dec), DLC, transmitting node(s)
  - **Signal list** — start bit, length, byte order, sign, factor, offset,
    min/max, unit
  - **Value tables** — enum labels (e.g. `0 = Off`, `1 = On`) for signals that
    define them
  - **Bit-layout diagram** — a per-message grid showing which bits each
    signal occupies, so gaps and overlaps are visible at a glance
  - **Search/filter** — filter the message list by name or ID, and the
    signal list by name
- **DBC Compare tab** — load a second `.dbc` and see a side-by-side diff:
  messages/signals added, removed, or changed (ID, DLC, node, bit layout,
  scaling, value tables, etc.)
- **Log Replay tab** — decode a CAN log file (any format `python-can`
  supports: `.asc`, `.blf`, `.csv`, `.db`, `.log`, `.mf4`, `.trc`) and plot
  selected signals, with pan/zoom/reset on the plot:
  - **Two DBC slots** — a bus is often described by one `.dbc` per sending
    node, so the tab decodes against up to two of them at once. Slot 1
    defaults to the DBC Viewer tab's file; slot 2 is picked here. Either slot
    can be left empty, and a `DBC` column shows which file each signal came
    from (so you can also filter the signal list by source).
  - **Frame-ID overlap handling** — a CAN ID that both files define
    *identically* is decoded once. One they define *differently* is decoded
    under **both** interpretations, giving two labelled series plus a warning
    on the status line, so you can see which reading is plausible rather
    than having one silently win.
  - IDs that no loaded `.dbc` describes are still listed as `Unknown` with
    their raw payload, so unmapped traffic stays visible
- **Live Signal Viewer tab** — connect to a PEAK PCAN-USB (FD) adapter over
  USB, decode live classic-CAN or CAN-FD traffic against a `.dbc`, and plot
  selected signals in real time over a scrolling time window

The DBC Compare, Log Replay, and Live Signal Viewer tabs all default to
sharing whatever `.dbc` is currently loaded in the DBC Viewer tab, but can
each load an independent one instead (for Log Replay this applies to DBC
slot 1; slot 2 is always chosen in the tab).

## Requirements

- Python 3.9+
- [PySide6](https://pypi.org/project/PySide6/), [cantools](https://pypi.org/project/cantools/),
  [python-can](https://pypi.org/project/python-can/), [matplotlib](https://pypi.org/project/matplotlib/),
  and [uptime](https://pypi.org/project/uptime/) (see `requirements.txt`)
- For the Live Signal Viewer tab on macOS: [MacCAN's PCBUSB library](https://github.com/mac-can/PCBUSB-Library),
  a separate system-level driver install for PEAK PCAN adapters (PEAK ships
  no native macOS driver). Not needed for any other tab.

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

## Updating the installed app (macOS)

After pulling or making code changes, rebuild and reinstall the standalone
app in one step:

```bash
./update.sh
```

This syncs dependencies, quits the running "DBC Viewer" app (if open),
rebuilds it with PyInstaller, replaces `/Applications/DBC Viewer.app` with
the new build, and relaunches it.

## Project layout

```
main.py                    entry point / main window
dbcviewer/
  dbc_model.py             cantools wrapper, bit-position math
  browser.py               DBC Viewer tab (message/signal browser)
  bitlayout.py             bit-layout diagram widget
  compare.py               DBC Compare tab (two-file diff)
  diff.py                  diff engine (message/signal comparison)
  log_replay_model.py      log-file decode logic, multi-DBC (Qt-free)
  log_replay.py            Log Replay tab
  live_capture_model.py    live-capture decode/buffering logic (Qt-free)
  live_capture_worker.py   background CAN receive thread
  live_signal.py           Live Signal Viewer tab
samples/                    small sample .dbc files for testing
```

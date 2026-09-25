# DBC Viewer

A desktop GUI for working with CAN `.dbc` files: browse message/signal
structure, compare two `.dbc` files, replay a logged CAN capture against a
`.dbc`, and view live CAN/CAN-FD traffic from a PEAK PCAN-USB adapter.

## Features

- **DBC Viewer tab** — open a `.dbc` file via a file picker, File → Open
  Recent, or drag-and-drop:
  - **Message list** — name, CAN ID (hex/dec), DLC, transmitting node(s),
    plus cycle time, receivers, and the message comment
  - **Signal list** — start bit, length, byte order, sign, factor, offset,
    min/max, unit, receivers, and comment
  - **Value tables** — enum labels (e.g. `0 = Off`, `1 = On`) for signals that
    define them
  - **Bit-layout diagram** — a per-message grid showing which bits each
    signal occupies, so gaps and overlaps are visible at a glance. Click a
    cell to select that signal
  - **Search** — filter messages by name or ID, and find a signal anywhere
    in the file (the hit list jumps to its message)
- **DBC Compare tab** — File A starts as the DBC Viewer file. Load a second
  `.dbc` (or drop it on this tab) and see a side-by-side diff: messages and
  signals added, removed, or changed (ID, DLC, node, bit layout, scaling,
  value tables, etc.). Groups stay collapsed, a filter narrows the tree, and
  double-clicking a message or signal opens it in the viewer
- **Log Replay tab** — decode a CAN log file (any format `python-can`
  supports: `.asc`, `.blf`, `.csv`, `.db`, `.log`, `.mf4`, `.trc`) against a
  `.dbc` and plot selected signals. Plots are lines, with point markers only
  when zoomed in; From/To sets the time window and the label shows min/max
  in that window. Re-decode reruns the log after a DBC change. Drop a log
  file on the window to open it here
- **Live Signal Viewer tab** — connect to a PEAK PCAN-USB (FD) adapter over
  USB, decode live classic-CAN or CAN-FD traffic against a `.dbc`, and plot
  selected signals in real time over a scrolling time window. Every DBC
  signal is listed before traffic arrives. Pause freezes the window. The
  status line counts frames, error frames, and unmapped IDs. CAN-FD bitrate
  settings sit on their own row

The DBC Compare, Log Replay, and Live Signal Viewer tabs all default to
sharing whatever `.dbc` is currently loaded in the DBC Viewer tab, but can
each load an independent one instead.

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
  log_replay_model.py      log-file decode logic (Qt-free)
  log_replay.py            Log Replay tab
  live_capture_model.py    live-capture decode/buffering logic (Qt-free)
  live_capture_worker.py   background CAN receive thread
  live_signal.py           Live Signal Viewer tab
samples/                    small sample .dbc files for testing
```

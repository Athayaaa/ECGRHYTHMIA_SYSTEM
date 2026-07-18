# ECGRHYTHMIA PyQt6 ECG Viewer

Standalone desktop viewer for calibrated ECGRHYTHMIA ECG frames. It reads the
existing `frame_xxxxxx_mv.npy` files without opening the serial port or changing
the stored samples. No filtering, normalization, baseline removal, clipping,
resampling, or AI inference is performed.

## Installation

Use a dedicated Python 3.10 environment so the GUI dependencies remain separate
from the receiver and AI environments:

```bash
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
python3.10 -m venv venv_gui
source venv_gui/bin/activate
python -m pip install --upgrade pip
python -m pip install -r ecg_qt_viewer/requirements.txt
```

## Launch

From the `raspberry_pi` directory:

```bash
source venv_gui/bin/activate
python -m ecg_qt_viewer.main
```

On first launch (or when a saved folder is unavailable), the default input
directory is `data/calibrated`. Later launches reopen the last selected data
folder. Select the ProSim exports explicitly with:

```bash
python -m ecg_qt_viewer.main \
  --data-dir data/calibrated_prosim_test
```

Available command-line options are:

```text
--data-dir DIRECTORY
--initial-frame FRAME_ID_OR_NAME
--follow-live
--speed-mm-s {25,50}
--gain-mm-mv {5,10,20}
--y-range-mv {0.5,1,2,5}
--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}
```

## Frames and Live Follow

The canonical input is a calibrated `float32` NPY array with shape `(2500, 3)`,
250 Hz sampling, 10 seconds of data, and channel order Lead I, Lead II, Lead III.
The matching JSON file supplies metadata when present. An NPY-only frame can be
viewed, but is marked `WARNING` because metadata is missing.

`Follow Live` watches the selected output directory and moves to the newest
stable, completed 10-second frame. It waits for file size stability and prefers
the matching JSON file before loading. This is completed-frame following—not
sample-by-sample serial streaming—so a new display normally appears once each
10-second frame has been exported. Selecting an older frame disables live
following automatically.

Use the toolbar, frame combo box, or keyboard to move through stored frames.
Replay progressively reveals the selected stored frame at 0.5×, 1×, or 2× and
does not alter its source data.

## Clinical scale controls

- Paper speed selects 25 or 50 mm/s and controls horizontal medical-grid spacing.
- Gain selects 5, 10, or 20 mm/mV and controls vertical grid spacing.
- Time Window and Y Range control the visible data-coordinate ranges.
- Fixed clinical scaling is the default; Auto Y is available for inspection.
- Grid, crosshair, visible lead mode, and view locking can be changed independently.

The Display Calibration dialog provides a nominal 50 mm on-screen ruler. Adjust
the display factor against a physical ruler if desired. The factor changes only
rendered physical scale; it never changes timestamps or mV samples.

> Physical millimeter accuracy depends on monitor DPI and OS scaling.

## Crosshair and metadata

The crosshair snaps to the nearest source sample and reports its exact time and
the stored mV value for all three leads. The information panel shows source and
calibration metadata, per-lead statistics, maximum absolute amplitude, and all
validation results. Invalid shape, NaN/Inf, or amplitude above 10 mV blocks the
plot; metadata inconsistencies remain visible warnings and do not block it.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+O` | Open data folder |
| `Left` / `Right` | Previous / next frame |
| `Home` / `End` | First / latest frame |
| `Space` | Replay or pause |
| `R` | Reset view |

## Export and source safety

`Export Current View as PNG` captures the visible plots and medical grid. The
save dialog initially uses the Pictures folder and then remembers the last
export folder. `Copy Current Frame Path` and `Open Containing Folder` operate on
the selected source file. The viewer never modifies or overwrites an NPY/JSON
source frame; a PNG is written only to the path explicitly accepted in the save
dialog.

## Tests

```bash
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_gui/bin/activate
python -m py_compile ecg_qt_viewer/*.py
QT_QPA_PLATFORM=offscreen \
  python -m unittest discover -s ecg_qt_viewer/tests -v
QT_QPA_PLATFORM=offscreen \
  python -m ecg_qt_viewer.main --data-dir data/calibrated_prosim_test
```

The offscreen launch loads a frame, processes Qt events, prints the frame used,
and closes automatically after a short delay.

## Troubleshooting

Ubuntu:

- If Qt reports an `xcb` platform-plugin error, install the distribution's Qt/XCB
  runtime packages (commonly `libxcb-cursor0`, `libxkbcommon-x11-0`, and related
  XCB libraries), then reactivate `venv_gui`.
- Under Wayland, try `QT_QPA_PLATFORM=xcb` if the native Wayland plugin is not
  available.
- Verify that the selected directory contains files matching
  `frame_*_mv.npy`; temporary and hidden files are ignored.

Raspberry Pi:

- Use the 64-bit Raspberry Pi OS desktop when possible and install PyQt6 wheels
  inside `venv_gui`. If a wheel is unavailable, install the OS Qt6 runtime and
  build prerequisites before retrying pip.
- OpenGL/remote-desktop issues can often be diagnosed with
  `QT_OPENGL=software python -m ecg_qt_viewer.main`.
- A missing JSON sidecar is a warning. A corrupt NPY, invalid shape, NaN/Inf, or
  signal above the 10 mV safety limit is intentionally not plotted.
- Physical millimeter accuracy varies with the monitor, desktop scaling, and
  remote session; use Display Calibration on the actual display being viewed.

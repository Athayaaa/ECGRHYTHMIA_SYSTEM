# ECGRHYTHMIA Qt ECG Viewer

Standalone desktop viewer for ECGRHYTHMIA ECG frames. Supports both **Raw ADC** and 
**Calibrated mV** display modes. It reads existing `frame_*.npy` (raw) or 
`frame_*_mv.npy` (calibrated) files without opening the serial port or changing
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
python -m pip install PyQt6
```

### Ubuntu 22.04 (Jammy) on Raspberry Pi

Jammy does not provide the `python3-pyqt6` package. The viewer automatically
uses the supported PyQt5 system package instead:

```bash
sudo apt update
sudo apt install -y python3-pyqt5

cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
sed -i 's/include-system-site-packages = false/include-system-site-packages = true/' venv/pyvenv.cfg
source venv/bin/activate
python -m pip install -r ecg_qt_viewer/requirements.txt
python -c "from PyQt5.QtWidgets import QApplication; print('PyQt5: OK')"
```

The receiver uses `venv`, so do not create `venv_gui` for this Jammy setup.

## Launch

The receiver opens this viewer automatically in Raw ADC + Follow Live mode,
using the receiver's output directory:

```bash
python receiver.py
```

The automatically opened window is owned by that receiver process and closes
when the receiver stops. Use `python receiver.py --no-gui` for a headless
receiver session. This does not change the standalone launch below.

From the `raspberry_pi` directory:

```bash
source venv_gui/bin/activate
python -m ecg_qt_viewer.main
```

### Launch Options

View raw ADC data:

```bash
python -m ecg_qt_viewer.main \
  --data-dir data/corrected_sessions/SESSION_NAME \
  --source-mode raw
```

View calibrated mV data:

```bash
python -m ecg_qt_viewer.main \
  --data-dir data/corrected_sessions/SESSION_NAME \
  --source-mode mv
```

On first launch (or when a saved folder is unavailable), the default input
directory is `data/calibrated`. Later launches reopen the last selected data
folder and data mode.

Available command-line options:

```text
--data-dir DIRECTORY          Directory containing frame files
--source-mode {raw,mv}        'raw' for raw ADC, 'mv' for calibrated mV
--initial-frame FRAME_ID      Initial frame to display
--follow-live                 Auto-follow newest completed frames
--speed-mm-s {25,50}         Paper speed (mV mode only)
--gain-mm-mv {5,10,20}       Gain (mV mode only)
--y-range-mv {0.5,1,2,5}     Y range (mV mode only)
--log-level LEVEL            Logging level
```

## Data Modes

### Raw ADC Mode

Displays raw ADC count data from `frame_*.npy` files.

**View Options:**
- **Absolute Code**: Display raw ADC values as stored
- **Zero-Centered**: Subtract `zero_code` from Lead I and Lead II (Lead III unchanged). Requires `zero_code` in metadata.
- **Median-Centered**: Subtract median from each lead independently for display

**Y Scale Options:**
- ±1,000 count
- ±2,500 count  
- ±5,000 count
- ±10,000 count
- ±25,000 count
- ±50,000 count
- Auto

**Grid Options:**
- 100 count
- 250 count
- 500 count
- 1,000 count
- 2,500 count
- 5,000 count

Raw mode displays a **RAW ADC — NOT mV** badge and shows:
- ADC count values (not millivolts)
- ADCMAX, zero_code (if available in metadata)
- Protocol version, native sample rate, resampling info
- Packet loss and resampler reset counts

**Important:** Gain (mm/mV) controls are disabled in raw mode since data is not calibrated.

### Calibrated mV Mode

Displays calibrated millivolt data from `frame_*_mv.npy` files.

**Paper Speed:**
- 25 mm/s: minor grid 0.04 s, major grid 0.20 s
- 50 mm/s: minor grid 0.02 s, major grid 0.10 s

**Gain (mm/mV):**
- 5 mm/mV: minor grid 0.20 mV, major grid 1.00 mV
- 10 mm/mV: minor grid 0.10 mV, major grid 0.50 mV
- 20 mm/mV: minor grid 0.05 mV, major grid 0.25 mV

**Y Range:**
- ±0.5 mV to ±5 mV

Calibrated mode displays a **CALIBRATED mV** badge and shows:
- Millivolt values
- Calibration method, scale, and status
- Clinical ECG grid with standard paper spacing

## Frames and Live Follow

The canonical input for raw mode is a `float32` or integer NPY array with shape `(2500, 3)`,
250 Hz sampling, 10 seconds of data, and channel order Lead I, Lead II, Lead III.

The canonical input for mV mode is a calibrated `float32` NPY array with the same shape.

The matching JSON file supplies metadata when present. An NPY-only frame can be
viewed, but is marked `WARNING` because metadata is missing.

`Follow Live` watches the selected output directory and moves to the newest
stable, completed 10-second frame by file update time, not merely the highest
numerical frame ID. This also follows a restarted receiver that begins writing
again at `frame_000001.npy`. It waits for file size stability and prefers
the matching JSON file before loading. This is completed-frame following—not
sample-by-sample serial streaming—so a new display normally appears once each
10-second frame has been exported. Selecting an older frame disables live
following automatically.

Use the toolbar, frame combo box, or keyboard to move through stored frames.
Replay progressively reveals the selected stored frame at 0.5×, 1×, or 2× and
does not alter its source data.

## Mode Switching

You can switch between Raw ADC and Calibrated mV modes using the **Data Mode** 
combo box in the toolbar. The viewer will:
- Refresh the frame list for the new mode
- Attempt to display the same frame ID in the new mode (if available)
- Update all controls and displays appropriately

When switching modes, the viewer looks for:
- Raw mode: `frame_000001.npy`, `frame_000002.npy`, etc.
- mV mode: `frame_000001_mv.npy`, `frame_000002_mv.npy`, etc.

## Crosshair and Metadata

The crosshair snaps to the nearest source sample and reports:
- Exact time in seconds
- Sample index
- Values for all three leads with appropriate units (count or mV)

In Zero-Centered raw mode, the crosshair shows both the displayed (zero-centered)
value and the original raw value.

The information panel shows:
- Source and metadata information
- Per-lead statistics with appropriate units
- Mode-specific metadata (ADCMAX, zero_code for raw; calibration info for mV)
- Maximum absolute amplitude
- Lead III consistency validation
- All validation results

Invalid shape, unsupported data, and NaN/Inf block the plot. A finite signal
above the clinical amplitude limit remains visible for engineering diagnosis,
but retains its red `FAIL` status and validation reason.
Metadata inconsistencies remain visible warnings and do not block display.

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+O` | Open data folder |
| `Left` / `Right` | Previous / next frame |
| `Home` / `End` | First / latest frame |
| `Space` | Replay or pause |
| `R` | Reset view |

## Export and Source Safety

`Export Current View as PNG` captures the visible plots and grid. The
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

Test with raw data:

```bash
QT_QPA_PLATFORM=offscreen \
  python -m ecg_qt_viewer.main \
  --data-dir data/corrected_sessions/SESSION_NAME \
  --source-mode raw
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
- Verify that the selected directory contains files matching the selected mode:
  - Raw mode: `frame_*.npy` (without `_mv` suffix)
  - mV mode: `frame_*_mv.npy`
- Temporary and hidden files are ignored.

Raspberry Pi:

- On Ubuntu 22.04 (Jammy), install `python3-pyqt5` with `apt`; do not run
  `pip install PyQt6`, which would attempt a slow source build.
- OpenGL/remote-desktop issues can often be diagnosed with
  `QT_OPENGL=software python -m ecg_qt_viewer.main`.
- A missing JSON sidecar is a warning. A corrupt NPY, invalid shape, or NaN/Inf
  is not plotted. Finite mV data above 10 mV remains visible for diagnosis but
  is explicitly marked `FAIL`.
- Physical millimeter accuracy varies with the monitor, desktop scaling, and
  remote session; use Display Calibration on the actual display being viewed.

## Raw vs Calibrated Data

**Use Raw ADC mode to:**
- Inspect raw sensor output before calibration
- Debug sensor issues, ADC ranges, and zero_code values
- Verify data integrity with packet loss/resampler information
- Compare zero-centered vs absolute code views

**Use Calibrated mV mode to:**
- View clinically meaningful ECG waveforms
- Apply standard ECG paper speed and gain settings
- Export publication-ready ECG strips
- Analyze signal amplitudes in millivolts

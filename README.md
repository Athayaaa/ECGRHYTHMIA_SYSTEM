# ECGRHYTHMIA System

> **LEGACY DOCUMENTATION NOTICE:** The Phase 1/2/3D/3E instructions below
> describe the historical dummy or ADS1293 640-SPS/software-gated pipeline.
> Its `calibration_config.yaml`, Streamlit rail checks, and ProSim results must
> not be used with corrected protocol V2 acquisition. Start corrected work at
> `raspberry_pi/README.md`, `raspberry_pi/ADS1293_CORRECTED_PIPELINE.md`, and
> `raspberry_pi/CALIBRATION_MIGRATION.md`. The corrected datasheet conversion is
> preliminary until new external ProSim calibration is completed.

Phase 1 ESP32 firmware supports two ECG binary output transports.

- `esp32dev_dummy`: UART2 on GPIO17/GPIO16
- `esp32dev_dummy_usb`: USB Serial, enabled with `ECG_OUTPUT_USB=1`
- USB Serial baudrate: 115200
- UART2 baudrate: 115200
- Raspberry Pi GPIO UART port: `/dev/ttyAMA0`
- Raspberry Pi USB Serial port: `/dev/ttyUSB0`

When USB output mode is enabled, the same binary ECG packets are written to `Serial`.
Avoid continuous debug output on that stream while the Raspberry Pi parser is reading binary data.

The ECG stream remains dummy ECG at 250 Hz. Raspberry Pi frame exports stay at 2500 samples per 10-second frame with shape `(2500, 3)` and channel order `Lead I`, `Lead II`, `Lead III`.

Phase 2.5 USB dummy ECG pipeline test berhasil.

ESP32 DevKit V1 mengirim dummy ECG melalui USB Serial ke Raspberry Pi pada baudrate 115200.
Raspberry Pi menerima data melalui /dev/ttyUSB0, melakukan parsing packet, CRC check, buffering, dan export frame.
Hasil validasi latest frame menunjukkan CSV dan NPY valid.
CSV berisi time,ch1,ch2,ch3.
NPY memiliki format frame ECG siap input 1D-CNN.

## ESP32-C3 SuperMini ADS1293 USB Target

ESP32-C3 SuperMini uses native USB CDC serial for commands and binary ECG packet
output at 115200 baud. The PlatformIO environment is `esp32c3_ads1293_usb`.

ADS1293 wiring:

- `VCC` -> ESP32-C3 `3V3`
- `GND` -> ESP32-C3 `GND`
- `SCLK` -> GPIO4
- `MISO / SDO` -> GPIO5
- `MOSI / SDI` -> GPIO6
- `CS` -> GPIO7
- `DRDY` -> GPIO3

Build/upload:

```bash
cd esp32_firmware
pio run -e esp32c3_ads1293_usb -t upload
```

On Linux, the ESP32-C3 USB CDC port may appear as `/dev/ttyACM0`; USB-UART
adapters often appear as `/dev/ttyUSB0`. Use the detected port with the existing
receiver:

```bash
cd raspberry_pi
python3 receiver.py --port /dev/ttyACM0 --baudrate 115200
```

## Phase 3D ADS1293 Signal Quality Validation

Use an ECG simulator before testing on human subjects. Recommended first test:

1. Connect ProSim or another ECG simulator to the ADS1293 electrodes.
2. Set the simulator to 60 BPM, 1 mV normal sinus rhythm.
3. Run the Raspberry Pi receiver:

   ```bash
   cd raspberry_pi
   python3 receiver.py --port /dev/ttyUSB0 --baudrate 115200
   ```

4. After at least one complete frame is exported, stop the receiver and validate
   the latest frame:

   ```bash
   python3 validate_latest_frame.py
   ```

5. Run the ADS1293 signal quality analyzer:

   ```bash
   python3 analyze_latest_ads1293_frame.py
   ```

The analyzer checks the latest complete `.csv`, `.bin`, `.npy`, and `.json`
frame set in `raspberry_pi/data`, using frame metadata/file time so a new run
that starts again at `frame_000001` is still selected correctly. It verifies the
`(2500, 3)` float32 model-ready array, checks `ch3 = ch2 - ch1`, prints per-lead
metrics, and writes plots to `raspberry_pi/data/analysis/`.

## Phase 3E Local Calibration GUI

> **LEGACY ONLY:** `calibration_gui.py` retains the historical signed-rail and
> `calibration_config.yaml` workflow. Do not open corrected V2 frames in this
> Streamlit tool and do not use its saved scale with corrected firmware. Use
> the corrected command-line calibration/validation flow documented under
> `raspberry_pi/` instead.

Detailed calibration guide: `docs/PHASE_3E_STREAMLIT_CALIBRATION_GUIDE.md`.

The calibration GUI does not open the serial port. It only reads the latest
complete frame files from `raspberry_pi/data`. Keep `receiver.py` running in one
terminal so it can export frames, then run Streamlit in a second terminal.
Install dependencies once with `pip install -r requirements.txt` from the
`raspberry_pi` directory.

Terminal 1:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate
python3 receiver.py --port /dev/ttyUSB0 --baudrate 115200
```

Terminal 2:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate
streamlit run calibration_gui.py
```

The GUI has three modes:

- Quick View: inspect the latest converted ECG on a medical grid and export a
  PNG.
- Calibration: adjust raw-count to mV conversion, save
  `raspberry_pi/calibration_config.yaml`, and export calibrated CSV/NPY files.
- Advanced Debug: inspect raw plots, metrics, ch3 consistency, and file paths.

### ProSim Auto-Calibration

Use ProSim auto-calibration to estimate `scale_mV_per_count` from saved ADS1293
frame files. The calibration script reads only `raspberry_pi/data`; it does not
open the serial port and does not change receiver packet, CRC, or frame export
formats.

1. Connect ProSim `RA`, `LA`, `LL`, and `RL/RLD` to the ADS1293 electrodes.
2. Set ProSim to a `1 mV` calibration pulse if available. Otherwise use Normal
   Sinus Rhythm, `60 BPM`, `1 mV`.
3. Run the corrected receiver for about 1 minute into a new session directory:

   ```bash
   cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
   source venv/bin/activate
   python3 receiver.py \
     --port /dev/ttyUSB0 \
     --output-dir data/sessions/prosim_60bpm_1mV
   ```

4. Validate the corrected frame and its persisted acquisition status/hash:

   ```bash
   python3 analyze_latest_ads1293_frame.py \
     --data-dir data/sessions/prosim_60bpm_1mV
   ```

5. Run auto-calibration using the verified session and write a new config:

   ```bash
   python3 auto_calibrate_prosim.py \
     --input-dir data/sessions/prosim_60bpm_1mV \
     --session-metadata data/sessions/prosim_60bpm_1mV/session_<timestamp>.json \
     --expected-mv 1.0 \
     --expected-bpm 60 \
     --lead lead_ii \
     --frames 6 \
     --method rpeak_baseline \
     --pipeline-profile ads1293_native_266p667_resampled_250_v2 \
     --output calibration_config_ads1293_266p667_prosim_60bpm_1mV.yaml
   ```

   The output path must not already exist. The preserved
   `calibration_config.yaml` and `calibration_config_legacy_640_to_250.yaml`
   cannot be overwritten by this command. Repeat with multiple amplitudes for
   the final multipoint procedure documented in `raspberry_pi/CALIBRATION_MIGRATION.md`.

Use the Human Quick Test preset only with battery or power-bank operation. Do
not connect charger/PLN while electrodes are attached.

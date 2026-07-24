# OLED I2C 128x64 Status Display

Read-only status display for the ECGRHYTHMIA system on Raspberry Pi.

**Read-only guarantee:** `oled_status.py` only reads existing files and the
process list. It never opens serial ports, never touches `/dev/tty*`, never
imports or launches `receiver.py`, and never modifies firmware, the GUI, the
AI engine, or any ECG data. If it fails or is killed, acquisition is
unaffected.

---

## 1. Hardware wiring (SSD1306 128x64 I2C)

| OLED pin | Raspberry Pi pin        |
|----------|-------------------------|
| VCC      | 3.3V (pin 1)            |
| GND      | GND (pin 6)             |
| SDA      | GPIO2 / SDA (pin 3)     |
| SCL      | GPIO3 / SCL (pin 5)     |

Do **not** use 5V; the panel is 3.3V.

## 2. Enable I2C

```bash
sudo raspi-config      # Interface Options -> I2C -> Enable
sudo reboot
```

Verify the bus and detect the panel address (usually `0x3C`, sometimes `0x3D`):

```bash
sudo apt-get install -y i2c-tools
i2cdetect -y 1
```

## 3. Create the virtual environment (isolated from the receiver)

```bash
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
python3 -m venv venv_oled
source venv_oled/bin/activate
pip install --upgrade pip
pip install luma.oled Pillow smbus2 psutil
```

Keep these dependencies out of the receiver's requirements. `venv_oled/` is
git-ignored.

## 4. Console test (no hardware, works on a laptop)

```bash
python3 oled_status.py --console --once
python3 oled_status.py --console --data-root data/corrected_sessions
```

`data/corrected_sessions` does not exist yet, so the app auto-falls back to
`raspberry_pi/data/` and shows the real newest frame index, packet loss,
`UNKNOWN` config, and `WAIT` AI status.

Run the built-in tests:

```bash
python3 oled_status.py --self-test
```

## 5. Run on the OLED

```bash
source venv_oled/bin/activate
python3 oled_status.py --i2c-bus 1 --address 0x3C --driver ssd1306
```

If your panel is at `0x3D`, pass `--address 0x3D`.

## 6. Install as a systemd service (optional)

```bash
sudo cp ecgrhythmia-oled.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ecgrhythmia-oled.service
journalctl -u ecgrhythmia-oled -f
```

The unit starts `After=multi-user.target`, runs as `athaya`, and only restarts
on its own failure. It is not a receiver dependency.

---

## Pages (auto-rotate ~4s, `--page-seconds`)

### Page 1 — MAIN
```
ECGRHYTHMIA
<STATE>     MM:SS      <- REC duration since receiver start
FRAME         NNN      <- newest frame_*.json index
SIGNAL   <val|N/A>     <- from metadata if present, else N/A (not computed)
```
State: `BOOTING`, `READY`, `WAITING`, `RECORDING`, `ERROR`.

### Page 2 — ACQUISITION
```
ACQUISITION
ADS1293   <OK|FAIL|UNKNOWN>   <- config_verified (no producer yet -> UNKNOWN)
IN        <native|N/A>        <- native rate (no producer yet -> N/A)
OUT       250 Hz              <- sample_rate_hz from frame metadata
LOSS      <packet_loss_count> <- parser_stats
```

### Page 3 — RASPBERRY PI
```
RASPBERRY PI
TEMP      <NN C|N/A>
DISK      <NN%>
AI        <WAIT|OK|UNKNOWN>   <- OK only if AI engine process detected
NET       <OK|-->             <- non-loopback interface up with IPv4
```

## Error codes (overlay)

| Code | Meaning                                              |
|------|------------------------------------------------------|
| E01  | OLED/I2C init failed                                 |
| E02  | ADS1293 config explicitly not verified (metadata)    |
| E03  | Receiver running but no new frame within timeout     |
| E04  | Packet loss / out-of-order increase                  |
| E05  | CRC errors increasing                                |
| E06  | Disk almost full (>90%)                              |
| E07  | CPU temperature too high (>75 C)                     |

E04/E05 are detected by comparing `parser_stats` against the previous cycle.

## CLI

```
python3 oled_status.py \
  --data-root data/corrected_sessions \  # falls back to data/ if absent
  --session-dir <folder> \               # explicit override
  --i2c-bus 1 --address 0x3C --driver ssd1306 \
  --refresh 0.5 \                        # clamped to >= 0.5 (<= 2 reads/s)
  --page-seconds 4 \
  --console \                            # text render, no hardware
  --once \                               # one cycle then exit
  --self-test \                          # run inline tests
  --log-level INFO
```

## v1 limitations

- Signal quality is **not** computed; only surfaced if metadata provides it.
- Native input sampling rate has no producer yet -> `N/A`.
- `config_verified` has no producer yet -> `ADS1293` shows `UNKNOWN` (hook in
  place for when the pipeline emits it).
- AI live status has no interface yet -> defaults to `WAIT` unless an AI
  engine process is detected.
- `data/corrected_sessions` does not exist yet; the app uses `data/`.

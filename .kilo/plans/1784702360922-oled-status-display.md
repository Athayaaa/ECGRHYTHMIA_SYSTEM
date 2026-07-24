# Plan: OLED I2C 128x64 Status Display (ECGRHYTHMIA v1)

## Goal
Standalone, read-only OLED status app on Raspberry Pi. Reads existing system
files/process state only. Must not touch receiver, firmware, GUI, AI engine,
serial, or ECG data.

## Scope / Files (create only)
- `raspberry_pi/oled_status.py` — single-file app + inline tests
- `raspberry_pi/ecgrhythmia-oled.service` — systemd unit (documented, not enabled)
- `raspberry_pi/OLED_STATUS_GUIDE.md` — install + usage docs
- `.gitignore` — add `venv_oled/` (existing `venv/` pattern does NOT match `venv_oled`)

Do NOT create packages or extra Python modules. Do NOT modify receiver.py,
firmware, streaming_resampler, AI engine, or any data.

## Key findings from codebase (constraints that override literal spec)
Spec references sources that DO NOT exist on disk. Real state:
- `data/corrected_sessions/` — **absent**. Real output dir is `raspberry_pi/data/`.
- `session_*.json`, `acquisition_events.jsonl` — **absent**.
- `config_verified`, native sampling rate — **absent** in metadata.
- Real per-frame metadata: `raspberry_pi/data/frame_XXXXXX.json` with fields:
  `frame_index`, `sample_rate_hz` (250), `created_at_utc`, `device_id`,
  `duration_seconds`, and `parser_stats` = `{packets_ok, crc_errors,
  packet_loss_count, out_of_order_count, header_errors, bytes_discarded,
  last_sequence}`.
- Calibrated mV metadata: `raspberry_pi/data/calibrated/frame_XXXXXX_mv.json`
  with `calibration.calibration_source`, `unit`, `ch3_max_error_mV`,
  `source_metadata` (nested raw frame metadata).
- `raspberry_pi/data/ecg_metadata.sqlite` — frame metadata store.
- Receiver process: `receiver.py` (cmdline match), spawns ECG viewer child.
- AI engine: `third_party/ecg-edge-ai-engine/` (no live status file currently).
- Python 3.10.12; `psutil` already installed system-wide.
- `.gitignore` already ignores `venv/`, `venv_gui/`, `raspberry_pi/data/...`.

### Decision: Graceful degradation (user-approved)
Read what exists; treat missing spec sources as not-available.
- `config_verified` missing -> `UNKNOWN`
- no recent frame -> `WAITING`
- signal quality metadata missing -> `N/A`
- AI status unavailable -> `WAIT`
- Never fabricate `ADS OK` / `SIGNAL GOOD` without metadata evidence.

### Decision: Status inference (user-approved)
- Detect receiver via `psutil` process scan: cmdline contains `receiver.py`.
- REC duration = now - receiver process `create_time()` (if detectable).
- Frame "recent" if newest `frame_*.json` mtime age < `stale_threshold` (~15s).
- State machine:
  - receiver process not running -> `READY`
  - receiver running, no recent frame -> `WAITING`
  - receiver running, recent frame -> `RECORDING`
  - important acquisition error -> `ERROR`
  - startup before first read -> `BOOTING`

## Data-root resolution
- `--data-root` default `data/corrected_sessions` (per spec) BUT if it does not
  exist or is empty, auto-fallback to `raspberry_pi/data` (relative to script dir).
- `--session-dir` overrides everything (explicit folder to scan for `frame_*.json`).
- Resolve paths relative to script dir when not absolute.
- Scan newest frame by filename numeric order + mtime; tolerate folder moving
  between reads (re-resolve each cycle).

## Status reader module (functions inside oled_status.py)
Pure helpers, each returns typed value + availability flag; all wrapped so a
failure yields UNKNOWN/N/A, never a crash:
1. `find_data_root(args)` -> Path (with fallback logic).
2. `find_latest_frame_meta(root)` -> dict|None. Read only the newest
   `frame_*.json`; handle partial/incomplete JSON (json.JSONDecodeError -> None).
3. `find_latest_calibrated_meta(root)` -> dict|None from `calibrated/*_mv.json`.
4. `get_receiver_status()` -> {running: bool, start_time: float|None} via psutil.
5. `get_ai_status(root)` -> `OK`/`WAIT`/`UNKNOWN` (process scan for AI engine
   cmdline OR presence of AI output; default `WAIT`).
6. `get_cpu_temp()` -> float|None (read `/proc/thermal` / `psutil.sensors_temperatures`;
   on failure None).
7. `get_disk_usage(root)` -> percent via `psutil.disk_usage(data partition)`.
8. `get_net_status()` -> `OK`/`--` : interface up / has non-loopback IPv4
   (`psutil.net_if_addrs`/`net_if_stats`). NO internet ping.
9. `get_config_verified(cal_meta)` -> `OK` only if metadata explicitly says
   verified true; `FAIL` if explicitly false; else `UNKNOWN`. (No such field
   today -> practically `UNKNOWN`; keep hook for future.)
10. `derive_signal_quality(meta)` -> value from metadata if present else `N/A`
    (do NOT compute in v1).

Aggregate into a single `StatusSnapshot` dataclass consumed by renderers.

## Pages (auto-rotate ~4s, `--page-seconds`; refresh <=2/s, `--refresh` default 0.5)
### Page 1 MAIN
```
ECGRHYTHMIA
REC       MM:SS
FRAME       NNN
SIGNAL   <val|N/A>
```
State line reflects BOOTING/READY/WAITING/RECORDING/ERROR.

### Page 2 ACQUISITION
```
ACQUISITION
ADS1293   <OK|FAIL|UNKNOWN>
IN        <native|N/A>
OUT       250 Hz
LOSS      <packet_loss_count>
```
`IN` native rate absent -> `N/A`. `OUT` from `sample_rate_hz`. `LOSS`/CRC from
`parser_stats`.

### Page 3 RASPBERRY PI
```
RASPBERRY PI
TEMP      <NN C|N/A>
DISK      <NN%>
AI        <WAIT|OK|UNKNOWN>
NET       <OK|-->
```

### ERROR overlay (takes over temporarily)
```
ERROR ENN
<short cause>

<hint line 1>
<hint line 2>
```
Codes:
- E01 OLED/I2C init failed
- E02 ADS1293 config not verified (only if metadata explicitly FAIL)
- E03 receiver running but no new frame within timeout
- E04 packet loss / sequence gap increase (packet_loss_count/out_of_order_count)
- E05 CRC errors increasing (track delta between cycles)
- E06 disk almost full (threshold, e.g. >90%)
- E07 CPU temperature too high (threshold, e.g. >75C)
Deltas (E04/E05) computed by comparing parser_stats vs previous snapshot.

## Rendering backends
- OLED: `luma.oled` device (`ssd1306` default, `--driver`), `luma.core` I2C
  serial (`--i2c-bus` default 1, `--address` default 0x3C, support 0x3D),
  Pillow for text. Small builtin/default font.
- `--console`: render page text to stdout, no OLED needed (laptop testing).
- OLED init failure -> print E01 to stderr/journal; if `--console` requested run
  console mode; otherwise exit with clear message. Never kill other programs.

## CLI (argparse)
```
python3 oled_status.py
  --data-root data/corrected_sessions
  --session-dir <folder>
  --i2c-bus 1
  --address 0x3C
  --driver ssd1306
  --refresh 0.5
  --page-seconds 4
  --console
  --once
  --log-level INFO
```
- `--once`: read + render one cycle, exit.
- `--console`: text render, no OLED.
- `--log-level`: standard logging to stderr/journal.

## Safety requirements (enforce in code)
- No `import receiver`; no launching receiver; no `serial` import; no opening
  `/dev/tty*`.
- Main loop uses `time.sleep`; poll interval floor 500ms (reject `--refresh` < 0.5
  or clamp). Refresh <= 2/s.
- Never read entire `acquisition_events.jsonl` repeatedly (file absent now; if it
  appears, read only tail via seek from end).
- Handle files being written (partial JSON), missing files, moved session folder.
- No high-priority threads; no busy CPU loop.
- All OLED/read errors logged, program-local only.

## Tests (inline in oled_status.py, runnable via `python3 oled_status.py --self-test` or a `test_*` block)
Keep minimal, use tmp dirs / monkeypatch, no new test files:
- metadata present -> parsed correctly
- metadata missing -> returns None / UNKNOWN
- partial/incomplete JSON -> no crash, None
- latest corrected/frame session discovered (numeric ordering)
- latest frame index found
- config_verified true/false/unknown mapping
- packet loss surfaced from parser_stats
- CPU temp read failure -> N/A, no crash
- disk usage returns percent
- console mode renders without OLED
- assert app never imports serial / opens tty (static check on source or guard)

## systemd unit (ecgrhythmia-oled.service)
- `ExecStart=/home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi/venv_oled/bin/python /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi/oled_status.py`
- `WorkingDirectory=/home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi`
- `User=athaya`
- `Restart=on-failure` (only OLED app failures), `RestartSec=5`
- `After=multi-user.target` (start after base system ready)
- NOT a receiver dependency; receiver unaffected if this fails.
- `StandardOutput/StandardError=journal`
- Document install (`cp` to `/etc/systemd/system/`, `daemon-reload`, `enable`)
  but DO NOT run sudo / enable during implementation.

## Dependencies (venv_oled only; keep out of receiver requirements)
`luma.oled`, `Pillow`, `smbus2`, `psutil`. Document creating `venv_oled` and
`pip install`. Add `venv_oled/` to `.gitignore`.

## Documentation (OLED_STATUS_GUIDE.md)
Purpose; wiring (VCC 3.3V, GND, SDA GPIO2 pin3, SCL GPIO3 pin5); enable I2C via
`raspi-config`; verify bus with `i2cdetect -y 1`; create `venv_oled`; install
deps; console test; run OLED; systemd install + `journalctl -u ecgrhythmia-oled`;
meaning of 3 pages; error codes E01-E07; explicit statement that OLED is
read-only and does not handle serial or ECG acquisition.

## Validation commands (run during implementation; do NOT run receiver, change data, or commit)
```
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
python3 -m py_compile oled_status.py
python3 oled_status.py --console --once --data-root data/corrected_sessions
timeout 10 python3 oled_status.py --console --data-root data/corrected_sessions
git diff --check
git status --short
```
Note: `data/corrected_sessions` absent -> app must fall back to `data/` and show
real frame index/loss, UNKNOWN for config, WAIT for AI. Confirm no crash.

## Final report must cover
files created/changed; status sources used; how OLED avoids disturbing receiver
(read-only, no serial, poll >=500ms); console test result; py_compile result;
dependencies; run command; service install command; v1 limitations
(no signal-quality computation, no native rate, config_verified always UNKNOWN
until pipeline emits it, corrected_sessions not yet present); git status.
Do NOT commit or push.

## Open items / risks
- `config_verified` and native sampling rate have no producer yet; Page 2 will
  show UNKNOWN/N/A until upstream metadata adds them (hooks left in code).
- AI live status has no file/interface yet; default `WAIT` unless AI process
  detected.
- Requires switching to an implementation-capable agent (source + docs + unit +
  gitignore edits are outside plan-mode write permissions).

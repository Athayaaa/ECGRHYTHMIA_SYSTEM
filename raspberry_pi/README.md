# ECGRHYTHMIA Raspberry Pi Pipeline

Folder ini menerima ECG dari ESP32 melalui USB Serial untuk testing saat ini.
Target antarmuka akhir tetap UART. Pipeline ADS1293 corrected menghasilkan
frame 10 detik pada 250 Hz dengan shape `(2500, 3)` dan channel order:

```text
[Lead I, Lead II, Lead III]
Lead III = Lead II - Lead I
```

Tidak ada Lead V1. RLD/RL hanya reference opsional dan bukan channel data.

## Baca sebelum merekam

- `ADS1293_ACQUISITION_AUDIT.md`: audit pipeline 640-SPS lama.
- `ADS1293_CORRECTED_PIPELINE.md`: handshake, protocol V2, resampling, output,
  dan validation commands.
- `CALIBRATION_MIGRATION.md`: pemisahan kalibrasi legacy dan corrected.
- `MODEL_INPUT_SPEC.md`: kontrak file mV untuk integrasi model berikutnya.

Peringatan utama:

- kalibrasi ProSim legacy tidak valid untuk firmware corrected;
- konversi datasheet V2 saat ini masih preliminary;
- external ProSim recalibration corrected masih diperlukan;
- internal ADS1293 self-test bukan external gain calibration;
- baseline correction adalah preprocessing, bukan bagian dari ADC scale;
- validasi ADS1293/ProSim/manusia secara live masih pending.

## Ringkasan pipeline

```text
ADS1293 800/3 SPS, CH1/CH2 unsigned offset-code
  -> protocol V2 + CRC16 + native sequence
  -> strict firmware/register handshake
  -> stateful anti-aliased polyphase FIR 15/16
  -> 250 Hz Lead I/II
  -> derive Lead III
  -> raw float32 frame (2500, 3), domain kode
  -> compatible calibration
  -> mV float32 frame (2500, 3)
  -> optional model-ready validation/export
```

Sequence loss tidak diisi dengan interpolasi. Resampler di-reset dan partial
frame yang terdampak dibuang.

## Setup

```bash
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate
python3 -m pip install -r requirements.txt
```

Konfigurasi default ada di `config.yaml`: ADS1293 V2, 115200 baud, output 250 Hz,
frame 10 detik, dan nominal calibration corrected. Model-ready export disabled
secara default.

## Validasi offline

Tidak memerlukan ESP32, ADS1293, atau ProSim:

```bash
python3 validate_corrected_sampling_pipeline.py --synthetic --duration 30
python3 -m unittest -v \
  test_ads1293_conversion.py \
  test_ads1293_metadata.py \
  test_ads1293_selftest.py \
  test_ads1293_serial.py \
  test_ads1293_session.py \
  test_packet_parser_v2.py \
  test_receiver_v2_integration.py \
  test_streaming_resampler.py \
  test_validate_corrected_sampling_pipeline.py \
  test_model_input.py \
  test_phase3h_pipeline.py \
  test_auto_calibrate_output_safety.py \
  test_prosim_session_provenance.py
```

Test Qt memakai environment GUI terpisah karena `venv` akuisisi tidak memuat
PyQt6:

```bash
deactivate
source venv_gui/bin/activate
QT_QPA_PLATFORM=offscreen python3 -m unittest discover \
  -s ecg_qt_viewer/tests -p 'test_*.py' -v
```

Expected synthetic regression: 8000 native sample, 7500 output sample, tiga
frame lengkap, tidak ada loss/reset/NaN/Inf, dan PASS.

Capture native yang dibuat receiver dapat divalidasi dengan session JSON yang
memuat write/readback register terverifikasi:

```bash
python3 validate_corrected_sampling_pipeline.py \
  --input data/sessions/20260718_bench01/native/native_samples.csv \
  --session-metadata data/sessions/20260718_bench01/session_<timestamp>.json
```

## Menjalankan receiver ADS1293

Gunakan directory baru untuk setiap sesi agar output sebelumnya tidak tertimpa:

```bash
python3 receiver.py \
  --config config.yaml \
  --port /dev/ttyUSB0 \
  --output-dir data/sessions/20260718_bench01 \
  --max-frames 3
```

Tambahkan `--native-capture` bila perlu menyimpan CH1/CH2 native sebelum
resampling. Receiver menghentikan binary stream, meminta satu `ads config dump`,
memverifikasi rate/register/protocol/fingerprint, baru memulai stream V2.
Mismatch menghentikan startup.

`--allow-unverified-firmware` hanya melewati provenance/readback yang hilang
atau tidak tepercaya untuk diagnosis. Mismatch protocol, rate, packet,
encoding, ADC scale, CRC, self-test, device, atau nilai register yang sudah
diketahui tetap fatal. Saat override yang sah dipakai, session ditandai
`FAIL_UNVERIFIED`; raw acquisition boleh disimpan, tetapi calibrated/model-ready
export tetap tidak dibuat.

## Internal self-test

Jalankan ketika perangkat ADS1293 tersedia dan receiver lain tidak memakai port:

```bash
python3 ads1293_selftest.py \
  --port /dev/ttyUSB0 \
  --baudrate 115200 \
  --samples 100
```

Report ditulis ke `data/selftest_reports/`. Tool menguji zero, positive, dan
negative internal test signal, mengembalikan mode off, lalu memverifikasi status
off; kegagalan pemulihan membuat hasil gagal. PASS hanya berarti signal
path/configuration internal memenuhi batas tool; bukan bukti gain mV eksternal.

## Output default

```text
data/
  session_<timestamp>.json       firmware/register/session provenance
  raw_ecg.csv                    continuous 250-Hz resampled code stream
  frame_xxxxxx.{csv,bin,npy,json} raw canonical code frame
  ecg_metadata.sqlite            metadata index
  acquisition_events.jsonl       loss/reset events
  native/native_samples.csv      optional native diagnostic capture
  calibrated/frame_xxxxxx_mv.*   mV output when compatible
  model_ready/                    optional, disabled by default
  selftest_reports/*.json        internal self-test reports
```

`frame_xxxxxx.npy` di root output belum berunit mV. Ia berisi Lead I/II hasil
resampling dalam domain offset-code dan Lead III sebagai selisihnya.
`calibrated/frame_xxxxxx_mv.npy` adalah file mV. Jangan memasukkan raw code frame
langsung ke model yang mengharapkan mV.

FIR bekerja pada deviasi terhadap `zero_code`, lalu menambahkan `zero_code`
kembali. Ini menangani bias representasi offset-binary dan bukan baseline
correction ECG; baseline preprocessing tetap merupakan tahap terpisah.

Setiap frame JSON mengacu ke `session_id`, native sequence/timestamp range,
firmware/register fingerprint, packet loss, resampler reset/filter delay,
acquisition status, calibration status, dan signal-quality status. Periksa
metadata tersebut; keberadaan file saja bukan tanda bahwa frame PASS.

## Konversi offline

Corrected dataset selalu membutuhkan session JSON eksternal dengan
register/readback terverifikasi penuh. `session_id`, firmware commit, protocol,
dan fingerprint frame wajib cocok dengan session; session lain atau metadata
scalar buatan tidak dapat dipakai sebagai pengganti:

```bash
python3 convert_dataset_to_mv.py \
  --input-dir data/sessions/20260718_bench01 \
  --session-metadata data/sessions/20260718_bench01/session_<timestamp>.json \
  --config calibration_config_ads1293_266p667_nominal.yaml \
  --output-dir data/converted/20260718_bench01
```

Untuk dataset V1 lama, pilih file legacy secara eksplisit dan tulis ke directory
lain. Detail dan prosedur ProSim corrected ada di `CALIBRATION_MIGRATION.md`.
Output converter/replay harus berupa path baru. Raw, calibrated, dan model-ready
NPY diikat ke metadata dengan SHA-256; mismatch hash atau local loss/reset
membuat frame fail-closed.

## Build firmware yang terkait

```bash
cd ../esp32_firmware
pio run -e esp32c3_ads1293_usb
pio run -e esp32dev_ads1293_usb
pio run -e esp32dev_dummy_usb
pio run -e esp32dev_dummy
```

Dummy build mempertahankan protocol V1 250 Hz untuk testing sintetis. Protocol
V1 tidak boleh dilabeli sebagai akuisisi ADS1293 corrected.

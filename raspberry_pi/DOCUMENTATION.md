# 📖 ECGRHYTHMIA System — Dokumentasi Lengkap

> **Wearable 3-Lead ECG berbasis Edge AI untuk Deteksi Dini Aritmia**

---

## Daftar Isi

1. [Ringkasan Sistem](#1-ringkasan-sistem)
2. [Arsitektur & Alur Data](#2-arsitektur--alur-data)
3. [Hardware yang Digunakan](#3-hardware-yang-digunakan)
4. [Instalasi & Setup Awal](#4-instalasi--setup-awal)
5. [ESP32 Firmware — Build & Upload](#5-esp32-firmware--build--upload)
6. [Perintah Serial ESP32](#6-perintah-serial-esp32)
7. [Raspberry Pi — Receiver & Pipeline](#7-raspberry-pi--receiver--pipeline)
8. [Kalibrasi (ProSim Auto-Calibration)](#8-kalibrasi-prosim-auto-calibration)
9. [ECG Qt Viewer (Desktop GUI)](#9-ecg-qt-viewer-desktop-gui)
10. [OLED Status Display](#10-oled-status-display)
11. [Validasi & Analisis Data](#11-validasi--analisis-data)
12. [Persiapan Data untuk Model AI](#12-persiapan-data-untuk-model-ai)
13. [Replay Dataset Offline](#13-replay-dataset-offline)
14. [MQTT Publisher (Dashboard / HiveMQ Cloud)](#14-mqtt-publisher-dashboard--hivemq-cloud)
15. [Referensi Struktur Folder](#15-referensi-struktur-folder)
16. [Referensi Semua File](#16-referensi-semua-file)
17. [Format Data & Protokol](#17-format-data--protokol)
18. [Troubleshooting](#18-troubleshooting)
19. [Catatan Penting](#19-catatan-penting)

---

## 1. Ringkasan Sistem

ECGRHYTHMIA adalah sistem akuisisi ECG 3-lead (Einthoven limb leads) yang terdiri dari:

| Komponen | Fungsi |
|---|---|
| **ESP32** | Membaca sinyal ECG dari sensor ADS1293 atau menghasilkan dummy ECG, lalu mengirim data melalui binary serial packet |
| **Raspberry Pi** | Menerima packet serial, mem-parsing, meng-buffer, meng-export frame 10 detik, melakukan kalibrasi, dan menyiapkan input untuk model AI |
| **Model 1D-CNN** | Model inferensi aritmia (TFLite placeholder — belum di-training) |
| **ECG Qt Viewer** | Desktop GUI untuk melihat sinyal ECG realtime |
| **OLED Status** | Display status sistem pada layar OLED 128×64 |

### Lead ECG yang Digunakan

| Channel | Lead | Rumus |
|---|---|---|
| `ch1` | Lead I | LA − RA |
| `ch2` | Lead II | LL − RA |
| `ch3` | Lead III | Lead II − Lead I *(derived)* |

> **Penting:** ch3 adalah Lead III Einthoven, **bukan** ECG Lead V1. RLD/RL hanya digunakan sebagai reference electrode opsional.

### Spesifikasi Data

| Parameter | Nilai |
|---|---|
| Sampling rate | 250 Hz |
| Durasi frame | 10 detik |
| Sample per frame | 2500 |
| Shape model input | `(2500, 3)` |
| Dtype | `float32` |
| Channel order | `["Lead I", "Lead II", "Lead III"]` |
| Format CSV | `time,ch1,ch2,ch3` |

---

## 2. Arsitektur & Alur Data

```
┌──────────────┐     Binary Serial      ┌──────────────────┐
│   ESP32      │ ──────────────────────► │   Raspberry Pi   │
│  (ADS1293    │   32-byte packets       │                  │
│   atau Dummy)│   + CRC16-CCITT        │  ┌─── receiver.py │
└──────────────┘   USB Serial 115200     │  │                │
                                         │  ▼                │
                                         │  packet_parser    │
                                         │  │                │
                                         │  ▼                │
                                         │  ecg_buffer       │
                                         │  (ring buffer     │
                                         │   2500 samples)   │
                                         │  │                │
                                         │  ▼                │
                                         │  window_exporter  │
                                         │  │                │
                                         │  ├─► .csv         │
                                         │  ├─► .bin         │
                                         │  ├─► .npy         │
                                         │  ├─► .json        │
                                         │  │                │
                                         │  ▼                │
                                         │  calibration      │
                                         │  (raw → mV)       │
                                         │  │                │
                                         │  ├─► _mv.csv      │
                                         │  ├─► _mv.npy      │
                                         │  ├─► _mv.json     │
                                         │  │                │
                                         │  ▼                │
                                         │  model_input      │
                                         │  (mV → model-     │
                                         │   ready)          │
                                         └──────────────────┘
```

---

## 3. Hardware yang Digunakan

### Board ESP32

| Board | Penggunaan | PlatformIO Board ID |
|---|---|---|
| ESP32 Dev Module | Testing awal & saat ini | `esp32dev` |
| ESP32-C3 SuperMini | Testing ADS1293 + USB CDC | `esp32-c3-devkitm-1` |
| Seeed XIAO ESP32-S3 | Target final *(belum diimplementasi)* | — |

### Sensor ECG

| Sensor | Status |
|---|---|
| ProtoCentral ADS1293 3-ch breakout | Sensor final, sudah terhubung |
| Dummy ECG Generator | Untuk testing tanpa sensor fisik |

### Wiring ADS1293 ke ESP32

**ESP32 Dev Module:**

| ADS1293 Pin | ESP32 GPIO |
|---|---|
| SCLK | GPIO18 |
| MISO (SDO) | GPIO19 |
| MOSI (SDI) | GPIO23 |
| CS | GPIO4 |
| DRDY | GPIO2 |
| VCC | 3V3 |
| GND | GND |

**ESP32-C3 SuperMini:**

| ADS1293 Pin | ESP32-C3 GPIO |
|---|---|
| SCLK | GPIO4 |
| MISO (SDO) | GPIO5 |
| MOSI (SDI) | GPIO6 |
| CS | GPIO7 |
| DRDY | GPIO3 |
| VCC | 3V3 |
| GND | GND |

### Koneksi ESP32 ↔ Raspberry Pi

| Mode | Port ESP32 | Port RPi | Baudrate |
|---|---|---|---|
| USB Serial *(saat ini)* | USB | `/dev/ttyUSB0` | 115200 |
| USB CDC (ESP32-C3) | USB | `/dev/ttyACM0` | 115200 |
| UART *(target final)* | GPIO17 TX / GPIO16 RX | `/dev/ttyAMA0` | 115200 |

---

## 4. Instalasi & Setup Awal

### Prasyarat

- **PlatformIO** (VS Code extension atau CLI) untuk ESP32 firmware
- **Python 3.10+** di Raspberry Pi
- **Git** untuk clone repository

### Clone Repository

```bash
git clone <repo-url> ~/Projects/ECGRHYTHMIA_SYSTEM
cd ~/Projects/ECGRHYTHMIA_SYSTEM
```

### Setup Python Environment di Raspberry Pi

Proyek ini menggunakan **3 virtual environment terpisah** untuk masing-masing kebutuhan:

```bash
cd raspberry_pi

# 1. venv utama (receiver + calibration + validation)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. venv_gui (ECG Qt Viewer - PyQt6)
python3.10 -m venv venv_gui
source venv_gui/bin/activate
pip install -r ecg_qt_viewer/requirements.txt

# 3. venv_ai (AI engine - TFLite)
python3 -m venv venv_ai
source venv_ai/bin/activate
pip install -r ../third_party/ecg-edge-ai-engine/requirements.txt
```

---

## 5. ESP32 Firmware — Build & Upload

### PlatformIO Environments

Firmware memiliki 4 build environment yang bisa dipilih:

| Environment | Board | Mode | Output |
|---|---|---|---|
| `esp32dev_dummy` | ESP32 Dev | Dummy ECG | UART2 (GPIO17/16) |
| `esp32dev_dummy_usb` | ESP32 Dev | Dummy ECG | USB Serial *(default)* |
| `esp32dev_ads1293_usb` | ESP32 Dev | ADS1293 real | USB Serial |
| `esp32c3_ads1293_usb` | ESP32-C3 | ADS1293 real | USB CDC |

### Perintah Build & Upload

```bash
cd esp32_firmware

# Build saja (tanpa upload):
pio run -e esp32dev_dummy_usb

# Build + upload ke board:
pio run -e esp32dev_dummy_usb -t upload

# Upload firmware ADS1293 (ESP32 Dev):
pio run -e esp32dev_ads1293_usb -t upload

# Upload firmware ADS1293 (ESP32-C3):
pio run -e esp32c3_ads1293_usb -t upload

# Monitor serial output:
pio device monitor -b 115200
```

### Build Flags Penting

| Flag | Fungsi |
|---|---|
| `-D BOARD_ESP32_DEV` | Target board ESP32 Dev Module |
| `-D BOARD_ESP32C3_SUPERMINI` | Target board ESP32-C3 SuperMini |
| `-D ENABLE_DUMMY_DEFAULT=1` | Aktifkan dummy ECG generator |
| `-D ENABLE_ADS1293=1` | Aktifkan driver ADS1293 |
| `-D ECG_OUTPUT_USB=1` | Output ke USB Serial (bukan UART2) |

---

## 6. Perintah Serial ESP32

Kirim perintah melalui Serial Monitor (PlatformIO / `screen` / `minicom`). Perintah bersifat case-insensitive.

### Mode Dummy ECG

| Perintah | Fungsi |
|---|---|
| `start` | Mulai streaming binary ECG packet |
| `stop` | Hentikan streaming |
| `status` | Tampilkan info firmware (board, phase, output, sample rate, dll) |
| `sim on` | Aktifkan simulasi sinyal ECG (P-QRS-T) |
| `sim off` | Matikan simulasi (stream mengirim nilai nol) |

### Mode ADS1293

| Perintah | Fungsi |
|---|---|
| `ads status` | Tampilkan status ADS1293 (device ID, register, DRDY, dll) |
| `ads start` | Mulai akuisisi ADS1293 (tanpa binary stream) |
| `ads stop` | Hentikan semua: akuisisi, debug, dan binary stream |
| `ads read` | Baca satu sampel raw sekali saja (debug) |
| `ads debug on` | Tampilkan sampel raw di serial @ 8 Hz (untuk debugging) |
| `ads debug off` | Matikan debug output |
| `ads stream start` | Mulai binary stream dari ADS1293 @ 250 Hz |
| `ads stream stop` | Hentikan binary stream saja |
| `status` | Sama dengan `ads status` |

### Urutan Start Otomatis (Receiver)

Ketika `receiver.py` membuka koneksi ke ADS1293 build, ia mengirim perintah-perintah ini secara berurutan:

```
ads debug off  →  ads stop  →  ads start  →  ads stream start
```

---

## 7. Raspberry Pi — Receiver & Pipeline

### Memulai Sesi Pengukuran Baru (Clean Up)

Sebelum Anda melakukan pengukuran baru, sangat disarankan untuk menghapus data lama agar hasil pengukuran dari sesi sebelumnya tidak tercampur dengan yang baru (seperti file `frame_*.npy`, hasil deteksi AI lama, dan file database metadatanya).

Jalankan script bash berikut yang sudah disediakan di folder `raspberry_pi`:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
./clean_session.sh
```
*Script ini akan dengan aman menghapus semua data `csv`, `npy`, `bin`, `json`, serta hasil prediksi model lama yang ada di dalam direktori `data/`.*

### Menjalankan Receiver

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate

# Jalankan receiver (default: baca config.yaml)
python3 receiver.py

# Override port dan baudrate:
python3 receiver.py --port /dev/ttyACM0 --baudrate 115200

# Tanpa GUI viewer:
python3 receiver.py --no-gui

# Batasi jumlah frame:
python3 receiver.py --max-frames 10

# Custom output directory:
python3 receiver.py --output-dir data/my_session

# Lihat semua opsi:
python3 receiver.py --help
```

### Argumen CLI Receiver

| Argumen | Default | Fungsi |
|---|---|---|
| `--config` | `config.yaml` | Path ke file konfigurasi |
| `--port` | dari config | Serial device path override |
| `--baudrate` | dari config | Baudrate override |
| `--output-dir` | `data/` | Direktori output frame |
| `--max-frames` | `0` (tak terbatas) | Berhenti setelah N frame |
| `--no-gui` | *(flag)* | Jalankan tanpa membuka ECG Viewer |

### Konfigurasi Receiver (`config.yaml`)

```yaml
serial:
  port: /dev/ttyUSB0         # Port serial
  baudrate: 115200            # Baudrate
  timeout_seconds: 0.1        # Timeout baca serial
  read_size_bytes: 1024       # Ukuran baca per iterasi
  startup_delay_sec: 3.0      # Delay sebelum mulai
  flush_on_start: true        # Flush buffer serial saat start

source: ads1293               # 'ads1293' atau 'dummy'
send_start_commands: true     # Kirim start commands otomatis

sampling:
  sample_rate_hz: 250         # Sampling rate (Hz)
  frame_seconds: 10           # Durasi frame (detik)

output:
  directory: data             # Folder output
  raw_csv: raw_ecg.csv        # Nama file CSV raw
  sqlite_db: ecg_metadata.sqlite  # Database metadata

runtime:
  status_interval_seconds: 5  # Interval print status
  launch_ecg_viewer: true     # Auto-launch ECG Viewer

model_ready_export:
  enabled: false              # Auto-export model-ready NPY
  output_dir: data/model_ready
  max_abs_mv: 10.0            # Threshold clipping mV
```

### Output yang Dihasilkan Receiver

Setiap kali buffer ring penuh (2500 sampel = 10 detik), receiver meng-export **satu set file per frame**:

| File | Isi | Contoh |
|---|---|---|
| `frame_NNNNNN.csv` | CSV: `time,ch1,ch2,ch3` | `frame_000001.csv` |
| `frame_NNNNNN.bin` | Binary dump array mentah | `frame_000001.bin` |
| `frame_NNNNNN.npy` | NumPy array `float32 (2500,3)` | `frame_000001.npy` |
| `frame_NNNNNN.json` | Metadata (device, checksum, stats, dll) | `frame_000001.json` |

Jika kalibrasi aktif, juga menghasilkan file **calibrated (mV)**:

| File | Isi | Contoh |
|---|---|---|
| `frame_NNNNNN_mv.csv` | CSV: `time,lead_i_mV,lead_ii_mV,lead_iii_mV` | `frame_000001_mv.csv` |
| `frame_NNNNNN_mv.npy` | NumPy array mV `float32 (2500,3)` | `frame_000001_mv.npy` |
| `frame_NNNNNN_mv.json` | Metadata kalibrasi | `frame_000001_mv.json` |

---

## 8. Kalibrasi (ProSim Auto-Calibration)

Kalibrasi mengubah data ADC raw count menjadi milivolt (mV) menggunakan ECG simulator ProSim.

### Langkah Kalibrasi

**Langkah 1: Rekam data dari ProSim**

```bash
# Hubungkan ProSim ke ADS1293 (RA, LA, LL, RL)
# Set ProSim: 60 BPM, 1 mV Normal Sinus Rhythm

cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate

# Rekam selama minimal 1 menit
python3 receiver.py --port /dev/ttyUSB0 --baudrate 115200
# Ctrl+C untuk berhenti
```

**Langkah 2: Validasi frame terakhir**

```bash
python3 validate_latest_frame.py
```

**Langkah 3: Jalankan auto-kalibrasi**

```bash
# Kalibrasi single-point
python3 auto_calibrate_prosim.py --expected-mv 1.0 --lead lead_ii --frames 6

# Kalibrasi multi-point (lebih akurat)
python3 auto_calibrate_prosim.py --method multipoint_rpeak \
    --input-dir ../data_kalibrasi/60BPM_0p5mV --expected-mv 0.5 \
    --input-dir ../data_kalibrasi/60BPM_1mV --expected-mv 1.0 \
    --input-dir ../data_kalibrasi/60BPM_2mV --expected-mv 2.0
```

**Langkah 4: Gunakan Calibration GUI (opsional)**

```bash
# Terminal 1: Jalankan receiver
python3 receiver.py --port /dev/ttyUSB0

# Terminal 2: Buka GUI kalibrasi (Streamlit)
streamlit run calibration_gui.py
```

### Argumen `auto_calibrate_prosim.py`

| Argumen | Default | Fungsi |
|---|---|---|
| `--expected-mv` | *(wajib)* | Amplitudo yang diharapkan (mV) |
| `--lead` | `lead_ii` | Lead referensi untuk kalibrasi |
| `--frames` | `6` | Jumlah frame terakhir yang digunakan |
| `--method` | `rpeak_baseline` | Metode: `rpeak_baseline`, `multipoint_rpeak`, `robust_pp` |
| `--input-dir` | `data/` | Direktori frame sumber |
| `--output` | `calibration_config.yaml` | Path output konfigurasi |

### Hasil Kalibrasi

File `calibration_config.yaml` berisi:

```yaml
scale_mV_per_count: 0.00011691   # Faktor konversi ADC → mV
offset_count:
  lead_i: 6399663.0              # Offset baseline Lead I
  lead_ii: 6399426.0             # Offset baseline Lead II
  lead_iii: -237.0               # Offset baseline Lead III
```

### Mode Calibration GUI

| Mode | Fungsi |
|---|---|
| **Quick View** | Inspeksi sinyal ECG terkalibrasi pada medical grid, export PNG |
| **Calibration** | Atur konversi raw → mV, simpan config, export file kalibrasi |
| **Advanced Debug** | Inspeksi raw plots, metrik, konsistensi ch3, dan path file |

---

## 9. ECG Qt Viewer (Desktop GUI)

GUI desktop berbasis PyQt6 untuk visualisasi sinyal ECG secara real-time.

### Instalasi

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
python3.10 -m venv venv_gui
source venv_gui/bin/activate
pip install -r ecg_qt_viewer/requirements.txt
```

### Menjalankan

```bash
# Auto-launch dari receiver (default):
python3 receiver.py

# Standalone — raw ADC mode:
source venv_gui/bin/activate
python -m ecg_qt_viewer.main --data-dir data/ --source-mode raw

# Standalone — calibrated mV mode:
python -m ecg_qt_viewer.main --data-dir data/calibrated --source-mode mv

# Dengan follow-live (ikuti frame terbaru):
python -m ecg_qt_viewer.main --data-dir data/ --follow-live
```

### Argumen CLI

| Argumen | Default | Fungsi |
|---|---|---|
| `--data-dir` | `data/calibrated` | Direktori frame |
| `--source-mode` | `raw` | `raw` = ADC count, `mv` = milivolt |
| `--initial-frame` | *(otomatis)* | Frame ID awal |
| `--follow-live` | *(flag)* | Auto-ikuti frame terbaru |
| `--speed-mm-s` | `25` | Paper speed: `25` atau `50` (mv mode) |
| `--gain-mm-mv` | `10` | Gain: `5`, `10`, `20` (mv mode) |
| `--y-range-mv` | `2` | Y range: `0.5`, `1`, `2`, `5` (mv mode) |

### Keyboard Shortcuts

| Shortcut | Aksi |
|---|---|
| `Ctrl+O` | Buka folder data |
| `←` / `→` | Frame sebelum / sesudah |
| `Home` / `End` | Frame pertama / terakhir |
| `Space` | Replay / pause |
| `R` | Reset tampilan |

### Mode Tampilan

| Mode | Data Source | Tampilan |
|---|---|---|
| **Raw ADC** | `frame_*.npy` | Nilai ADC count mentah |
| **Calibrated mV** | `frame_*_mv.npy` | Nilai milivolt, clinical ECG grid |

---

## 10. OLED Status Display

Display status sistem dan menu interaktif pada layar OLED I2C 128×64 di
Raspberry Pi. Input menu memakai rotary encoder BCM GPIO13/GPIO19/GPIO26.
OLED tidak membuka serial ECG; start/stop recording dilakukan lewat systemd
user services.

### Apa yang Ditampilkan

- Status receiver (running / stopped)
- Jumlah frame yang ter-export
- Suhu CPU Raspberry Pi
- Penggunaan disk
- Status kalibrasi
- Status service AI dan MQTT (RUNNING / STOPPED)

OLED tidak menampilkan label, confidence, atau probabilities hasil prediksi AI.

### Menjalankan

```bash
# Manual:
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_oled/bin/activate
python3 oled_status.py --i2c-bus 1 --address 0x3C --driver ssd1306

# Sebagai systemd user service:
mkdir -p ~/.config/systemd/user
cp ecgrhythmia-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ecgrhythmia-oled.service

# Cek status service:
systemctl --user status ecgrhythmia-oled.service
```

> **Catatan:** OLED status tetap independen dari transport ECG. Ia mengontrol
> service receiver/AI/MQTT, tetapi tidak pernah membuka port serial sendiri.

---

## 11. Validasi & Analisis Data

### Validasi Frame Terakhir (Raw)

```bash
python3 validate_latest_frame.py
```

Memeriksa: shape `(2500,3)`, dtype `float32`, header CSV `time,ch1,ch2,ch3`, dan konsistensi `ch3 = ch2 - ch1`.

### Validasi Frame Terakhir (Calibrated mV)

```bash
python3 validate_latest_mv_frame.py
```

Memeriksa: shape, dtype, header `time,lead_i_mV,lead_ii_mV,lead_iii_mV`, dan konsistensi Lead III.

### Analisis Sinyal ADS1293

```bash
python3 analyze_latest_ads1293_frame.py
```

Menganalisis frame `.csv`, `.bin`, `.npy`, `.json` terakhir. Memeriksa `ch3 = ch2 - ch1`, mencetak metrik per-lead, dan menulis plot ke `data/analysis/`.

### Validasi Kalibrasi ProSim

```bash
python3 validate_prosim_calibration.py \
    --calibration calibration_config.yaml \
    --input-dir ../data_kalibrasi/60BPM_1mV \
    --expected-mv 1.0
```

Membandingkan hasil kalibrasi dengan nilai amplitudo yang diketahui.

### Validasi Semua Dataset

```bash
python3 validate_all_recorded_datasets.py
```

Replay dan validasi semua dataset ProSim yang terekam secara batch.

### Validasi Model Input

```bash
python3 validate_model_input.py --npy data/model_ready/frame_000001_model.npy
```

Memeriksa NPY model-ready: shape, dtype, rentang mV, NaN/Inf, dan konsistensi Lead III.

---

## 12. Persiapan Data untuk Model AI

### Pipeline Data: Raw → Calibrated → Model-Ready

```
frame_000001.npy (raw ADC count)
        │
        ▼  convert_dataset_to_mv.py  atau  calibration via receiver
        │
frame_000001_mv.npy (calibrated mV)
        │
        ▼  prepare_model_input.py  atau  prepare_model_dataset.py
        │
frame_000001_model.npy (model-ready, clipped, validated)
```

### Konversi Raw → mV (Batch)

```bash
python3 convert_dataset_to_mv.py \
    --input-dir ../data_kalibrasi/60BPM_1mV \
    --calibration calibration_config.yaml \
    --output-dir ../data_kalibrasi_mv/60BPM_1mV
```

### Persiapan Model Input (Satu File)

```bash
python3 prepare_model_input.py \
    --input data/calibrated/frame_000001_mv.npy \
    --output-dir data/model_ready
```

### Persiapan Model Input (Batch)

```bash
python3 prepare_model_dataset.py \
    --input-dir ../data_kalibrasi_mv/60BPM_1mV \
    --output-dir data/model_ready
```

### Spesifikasi Model Input

| Parameter | Nilai |
|---|---|
| Shape | `(2500, 3)` |
| Dtype | `float32` |
| Unit | mV (milivolt) |
| Sample rate | 250 Hz |
| Durasi | 10 detik |
| Channel order | `["Lead I", "Lead II", "Lead III"]` |
| Clipping | Default ±10.0 mV |

---

## 13. Replay Dataset Offline

Replay frame dataset yang sudah direkam tanpa membutuhkan hardware. Menggunakan pipeline kalibrasi dan model input yang sama dengan receiver live.

```bash
python3 replay_dataset.py \
    --input-dir ../data_kalibrasi/60BPM_1mV \
    --calibration calibration_config.yaml \
    --output-dir data/replay_output
```

> **Catatan:** Replay tidak memodifikasi dataset sumber.

---

## 14. MQTT Publisher (Dashboard / HiveMQ Cloud)

`mqtt_publisher.py` mengirim satu payload JSON untuk setiap pasangan frame ECG mV
dan hasil AI yang sudah lengkap. Publisher ini **tidak membuka serial port**,
tidak menjalankan model, dan tidak mengubah file sumber.

### Alur MQTT

```
data/calibrated/frame_000001_mv.npy
        +
data/predictions/frame_000001_prediction.json
        │
        ▼
mqtt_publisher.py
        │  MQTT QoS 1 + ACK
        ▼
Dashboard / HiveMQ Cloud
```

Publisher selalu memasangkan frame berdasarkan `frame_id`:

| ECG mV | Prediction |
|---|---|
| `frame_000001_mv.npy` | `frame_000001_prediction.json` |
| `frame_000002_mv.npy` | `frame_000002_prediction.json` |

`latest_prediction.json` tidak digunakan untuk pairing.

### Topic MQTT

Topic utama dashboard:

```text
ecgrhythmia/device01/frame
```

Untuk HiveMQ Web Client, subscribe dengan salah satu:

```text
ecgrhythmia/device01/frame
```

atau wildcard project:

```text
ecgrhythmia/#
```

Jangan menulis `#ecgrhythmia/device01/frame`; wildcard `#` hanya valid sebagai
`#` atau setelah slash seperti `ecgrhythmia/#`.

### Instalasi Dependensi

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_ai/bin/activate
pip install -r requirements.txt
```

### Dry Run Tanpa Broker

Dry run membaca ECG mV dan prediction, membangun payload, lalu mencetak ringkasan
tanpa koneksi broker dan tanpa menulis state.

```bash
python3 mqtt_publisher.py \
  --ecg-dir data/calibrated_prosim_test \
  --prediction-dir data/predictions_prosim_test \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01/frame \
  --device-id device01 \
  --tls \
  --once \
  --dry-run
```

Output normal:

```text
Frame 000001 ready
Prediction: Normal 98.75%
Payload size: 164613 bytes
Dry run: not published
```

### Credential HiveMQ Cloud

Host HiveMQ Cloud:

```text
93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud
```

Port TLS:

```text
8883
```

Username project:

```text
ecg-undip
```

Password **jangan** ditulis di source code, command history, payload, atau state
JSON. Simpan sebagai environment variable lokal:

```bash
mkdir -p ~/.config/ecgrhythmia
nano ~/.config/ecgrhythmia/hivemq.env
chmod 600 ~/.config/ecgrhythmia/hivemq.env
```

Isi file `~/.config/ecgrhythmia/hivemq.env`:

```bash
export HIVEMQ_PASSWORD='isi_password_hivemq'
```

Sebelum publish:

```bash
source ~/.config/ecgrhythmia/hivemq.env
```

### Publish ke HiveMQ Cloud

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_ai/bin/activate
source ~/.config/ecgrhythmia/hivemq.env

python3 mqtt_publisher.py \
  --ecg-dir data/calibrated_prosim_test \
  --prediction-dir data/predictions_prosim_test \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01/frame \
  --device-id device01 \
  --username ecg-undip \
  --password-env HIVEMQ_PASSWORD \
  --client-id ecgrhythmia-device01-publisher \
  --state-file data/predictions_prosim_test/mqtt_publish_state_hivemq.json \
  --tls \
  --once
```

Log sukses:

```text
MQTT publisher started
Connecting to HiveMQ Cloud
Broker: 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud:8883
TLS: enabled
Connected to broker
Frame 000001 published, QoS 1
```

### Mode Follow Realtime

Gunakan mode ini saat `receiver.py` dan `ai_worker.py` berjalan kontinu.
Publisher akan menunggu pasangan frame ECG mV dan prediction dengan ID yang sama.

```bash
python3 mqtt_publisher.py \
  --ecg-dir data/calibrated \
  --prediction-dir data/predictions \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01/frame \
  --device-id device01 \
  --username ecg-undip \
  --password-env HIVEMQ_PASSWORD \
  --client-id ecgrhythmia-device01-publisher \
  --state-file data/predictions/mqtt_publish_state_hivemq.json \
  --tls \
  --follow
```

### State Anti-Duplikat

Frame hanya ditandai terkirim setelah MQTT publish mendapat ACK. State HiveMQ
direkomendasikan terpisah dari state broker lokal:

```text
data/predictions/mqtt_publish_state_hivemq.json
data/predictions_prosim_test/mqtt_publish_state_hivemq.json
```

State membedakan `broker`, `topic`, `message_id`, dan hash file ECG. Frame yang
pernah dikirim ke `localhost` tetap dapat dikirim satu kali ke HiveMQ Cloud,
tetapi tidak dikirim berulang ke broker dan topic yang sama.

Untuk mengulang test ProSim dari nol:

```bash
rm data/predictions_prosim_test/mqtt_publish_state_hivemq.json
```

### Format Payload MQTT

Payload dibuat di memori dan langsung dikirim ke MQTT; default tidak disimpan ke
file. Struktur ringkas:

```json
{
  "schema_version": 1,
  "message_id": "device01-default-frame_000001",
  "device_id": "device01",
  "session_id": "default",
  "frame_id": "000001",
  "created_at": "2026-07-23T16:00:00+07:00",
  "sampling_rate_hz": 250.0,
  "duration_s": 10.0,
  "unit": "mV",
  "shape": [2500, 3],
  "channel_order": ["Lead I", "Lead II", "Lead III"],
  "validation": {
    "status": "PASS",
    "warnings": []
  },
  "ecg": {
    "format": "samples_by_time",
    "samples": [[0.012, 0.018, 0.006]]
  },
  "prediction": {
    "status": "PASS",
    "label": "Normal",
    "confidence_percent": 98.7,
    "probabilities": {
      "Normal": 98.7,
      "AF": 0.2,
      "Takikardia": 0.1,
      "Bradikardia": 1.0
    },
    "threshold": 0.5,
    "latency_ms": 12.0,
    "runtime": "ai-edge-litert"
  }
}
```

Pada payload aktual, `ecg.samples` berisi 2500 baris x 3 channel dalam satuan
mV. Payload tidak memuat raw ADC count, local absolute path, username, atau
password.

### Validasi MQTT

```bash
python3 -m py_compile mqtt_publisher.py
python3 -m unittest discover -s tests -v
```

---

## 15. Referensi Struktur Folder

```
ECGRHYTHMIA_SYSTEM/
│
├── esp32_firmware/                 # Firmware ESP32 (PlatformIO)
│   ├── platformio.ini              # Konfigurasi build environments
│   ├── src/                        # Source code firmware
│   │   ├── main.cpp                # Entry point firmware
│   │   ├── config.h                # Konfigurasi pin & konstanta
│   │   ├── ADS1293Reader.cpp/.h    # Driver sensor ADS1293
│   │   ├── DummyECGGenerator.cpp/.h # Generator sinyal dummy
│   │   ├── ECGPacket.cpp/.h        # Encoder binary packet
│   │   ├── CRC16.cpp/.h            # Implementasi CRC16-CCITT
│   │   └── ECGOutputTransport.cpp/.h # Abstraksi output (USB/UART)
│   ├── include/                    # Header tambahan
│   ├── lib/                        # Library tambahan
│   └── test/                       # Test firmware
│
├── raspberry_pi/                   # Software Raspberry Pi
│   ├── config.yaml                 # Konfigurasi utama receiver
│   ├── calibration_config.yaml     # Hasil kalibrasi aktif
│   ├── requirements.txt            # Dependensi Python
│   ├── receiver.py                 # ⭐ Receiver utama
│   ├── packet_parser.py            # Parser binary packet
│   ├── ecg_buffer.py               # Ring buffer 10 detik
│   ├── crc16.py                    # CRC16-CCITT Python
│   ├── window_exporter.py          # Export frame CSV/BIN/NPY/JSON
│   ├── data_preparer.py            # Validasi & prep array frame
│   ├── calibration_utils.py        # Utilitas kalibrasi bersama
│   ├── calibration_gui.py          # GUI kalibrasi (Streamlit)
│   ├── auto_calibrate_prosim.py    # Auto-kalibrasi ProSim
│   ├── model_interface.py          # Placeholder interface model
│   ├── model_input_utils.py        # Utilitas persiapan input model
│   ├── metadata_store.py           # Penyimpanan metadata SQLite
│   ├── oled_status.py              # Display OLED status
│   ├── ecgrhythmia-oled.service    # Systemd service OLED
│   │
│   ├── validate_latest_frame.py    # Validasi frame raw terakhir
│   ├── validate_latest_mv_frame.py # Validasi frame mV terakhir
│   ├── validate_model_input.py     # Validasi model input
│   ├── validate_prosim_calibration.py # Validasi kalibrasi ProSim
│   ├── validate_all_recorded_datasets.py # Validasi semua dataset
│   ├── analyze_latest_ads1293_frame.py   # Analisis sinyal ADS1293
│   │
│   ├── convert_dataset_to_mv.py    # Konversi batch raw → mV
│   ├── prepare_model_input.py      # Persiapan satu model input
│   ├── prepare_model_dataset.py    # Persiapan batch model input
│   ├── ai_worker.py                # Worker inferensi AI per frame model-ready
│   ├── mqtt_publisher.py           # Publish ECG mV + prediction ke MQTT
│   ├── replay_dataset.py           # Replay dataset offline
│   │
│   ├── test_model_input.py         # Unit test model input
│   ├── test_phase3h_pipeline.py    # Test pipeline Phase 3H
│   ├── tests/
│   │   └── test_mqtt_publisher.py  # Unit test MQTT publisher
│   │
│   ├── ecg_qt_viewer/              # Desktop GUI ECG Viewer
│   │   ├── main.py                 # Entry point viewer
│   │   ├── main_window.py          # Window utama (PyQt6)
│   │   ├── ecg_plot_widget.py      # Widget plot ECG
│   │   ├── ecg_grid_item.py        # Grid item medical ECG
│   │   ├── frame_repository.py     # Repository frame data
│   │   ├── frame_validator.py      # Validator frame data
│   │   ├── frame_watcher.py        # File watcher frame baru
│   │   ├── viewer_settings.py      # Pengaturan viewer
│   │   ├── requirements.txt        # Dependensi PyQt6
│   │   └── tests/                  # Test viewer
│   │
│   ├── data/                       # Output frame (runtime)
│   ├── venv/                       # Python venv (receiver)
│   ├── venv_gui/                   # Python venv (PyQt6 GUI)
│   └── venv_ai/                    # Python venv (AI engine)
│
├── data_kalibrasi/                 # Dataset kalibrasi raw
│   ├── 60BPM_0p5mV/               # ProSim 60 BPM 0.5 mV
│   ├── 60BPM_1mV/                  # ProSim 60 BPM 1.0 mV
│   ├── 60BPM_2mV/                  # ProSim 60 BPM 2.0 mV
│   ├── 80BPM_1mV/                  # ProSim 80 BPM 1.0 mV
│   ├── 100BPM_1mV/                 # ProSim 100 BPM 1.0 mV
│   ├── 120BPM_1mV/                 # ProSim 120 BPM 1.0 mV
│   ├── SVTArr/                     # SVT Arrhythmia dataset
│   └── datatestFikri/              # Dataset test khusus
│
├── data_kalibrasi_mv/              # Dataset kalibrasi terkonversi (mV)
│   ├── 60BPM_1mV/
│   ├── 100BPM_1mV/
│   ├── 120BPM_1mV/
│   ├── SVTArr/
│   └── datatestFikri/
│
├── third_party/
│   └── ecg-edge-ai-engine/         # Engine AI inferensi ECG
│       ├── src/                    # Source code engine
│       ├── models/                 # Model TFLite
│       ├── examples/               # Contoh penggunaan
│       ├── tests/                  # Test engine
│       ├── batch_test_ecgrhythmia.py # Batch test inferensi
│       ├── setup.py                # Package setup
│       └── requirements.txt        # Dependensi AI
│
├── docs/                           # Dokumentasi
│   ├── PKM_ECG_Project_Briefcase.pdf    # Briefcase PKM-KC
│   ├── DocumentationADS1293ProtoCentral.pdf # Dokumentasi ADS1293
│   ├── ads1293.pdf                      # Datasheet ADS1293
│   └── PHASE_3E_STREAMLIT_CALIBRATION_GUIDE.md
│
├── prompts/                        # Prompt konteks per-phase
│   ├── project_context.md
│   ├── phase1_esp32_dummy.md
│   ├── phase2_raspberry_receiver.md
│   ├── phase3_ads1293.md
│   └── phase4_websocket_tflite.md
│
├── AGENTS.md                       # Aturan pengembangan (untuk AI)
├── README.md                       # README utama
└── DOCUMENTATION.md                # 📖 File ini
```

---

## 16. Referensi Semua File

### ESP32 Firmware (`esp32_firmware/src/`)

| File | Fungsi |
|---|---|
| `main.cpp` | Entry point. Setup serial, ADS1293, dan dummy generator. Polling command, debug, sample, dan LED blink di `loop()`. |
| `config.h` | Semua konstanta konfigurasi: pin GPIO, baudrate, sampling rate, build flags. **Tidak ada pin yang di-hardcode di `main.cpp`**. |
| `ADS1293Reader.cpp/.h` | Driver SPI untuk sensor ProtoCentral ADS1293. Membaca 2 channel raw (Lead I & II), Lead III derived. Menggunakan library `protocentral-ads1293-arduino`. |
| `DummyECGGenerator.cpp/.h` | Generator sinyal ECG sintetis (P-QRS-T wave) menggunakan fungsi Gaussian. Digunakan saat sensor belum tersedia. |
| `ECGPacket.cpp/.h` | Encoder binary serial packet 32-byte. Format: `SYNC[2] + VERSION[1] + PAYLOAD_LEN[2] + PAYLOAD[25] + CRC16[2]`. |
| `CRC16.cpp/.h` | Implementasi CRC16-CCITT-FALSE (polynomial 0x1021, initial 0xFFFF). |
| `ECGOutputTransport.cpp/.h` | Abstraksi output stream (USB Serial atau UART2). |
| `platformio.ini` | Konfigurasi PlatformIO: 4 build environment, library dependencies. |

### Raspberry Pi — Core Pipeline (`raspberry_pi/`)

| File | Fungsi |
|---|---|
| `receiver.py` | **Program utama receiver.** Membuka serial port, mengirim start commands ke ESP32, mem-parsing packet, meng-buffer, meng-export frame, menjalankan kalibrasi inline, dan meluncurkan Qt Viewer. |
| `packet_parser.py` | Parser incremental untuk binary serial packet. Menangani sync recovery, validasi header, CRC check, dan statistik packet loss. |
| `ecg_buffer.py` | Ring buffer ukuran tetap (2500 sampel) untuk mengakumulasi data ECG selama 10 detik sebelum di-export sebagai satu frame. |
| `crc16.py` | Implementasi CRC16-CCITT-FALSE di Python (harus identik dengan ESP32). |
| `window_exporter.py` | Export frame yang sudah lengkap ke `.csv`, `.bin`, `.npy`, dan `.json` dengan metadata (device ID, checksum SHA256, statistik parser). |
| `data_preparer.py` | Validasi shape array dan konversi ke `float32`. Menyediakan `frame_time_axis()` untuk sumbu waktu. |
| `config.yaml` | Konfigurasi utama receiver: serial port, sampling, output, device, dan model export settings. |

### Raspberry Pi — Kalibrasi (`raspberry_pi/`)

| File | Fungsi |
|---|---|
| `calibration_utils.py` | Utilitas bersama: load frame, cari frame set, konversi raw→mV, tulis frame terkalibrasi, deteksi R-peak, dan analisis baseline-to-peak. |
| `auto_calibrate_prosim.py` | Script auto-kalibrasi: menghitung `scale_mV_per_count` dan `offset_count` dari data ProSim. Mendukung metode `rpeak_baseline`, `multipoint_rpeak`, dan `robust_pp`. |
| `calibration_gui.py` | GUI Streamlit untuk kalibrasi interaktif (Quick View, Calibration, Advanced Debug). |
| `calibration_config.yaml` | Hasil kalibrasi aktif yang digunakan oleh receiver dan tools lainnya. |
| `convert_dataset_to_mv.py` | Konversi batch seluruh direktori frame raw → frame mV. |

### Raspberry Pi — Model AI (`raspberry_pi/`)

| File | Fungsi |
|---|---|
| `model_interface.py` | Placeholder interface untuk model TFLite. Menyediakan `get_latest_window()` dan `get_model_input_shape()`. **Tidak menjalankan inferensi.** |
| `model_input_utils.py` | Utilitas lengkap untuk persiapan input model: validasi, clipping, signal quality check, dan export NPY model-ready. |
| `prepare_model_input.py` | Script CLI: persiapan satu frame mV → model-ready NPY. |
| `prepare_model_dataset.py` | Script CLI: persiapan batch frame mV → model-ready NPY + manifest CSV. |
| `ai_worker.py` | Worker inferensi AI. Membaca frame model-ready, menulis `frame_NNNNNN_prediction.json`, dan tidak membuka serial port. |

### Raspberry Pi — MQTT (`raspberry_pi/`)

| File | Fungsi |
|---|---|
| `mqtt_publisher.py` | Publisher MQTT untuk mengirim pasangan ECG mV + prediction JSON ke dashboard/HiveMQ Cloud. Mendukung TLS, QoS 1, ACK, dry-run, follow, dan state anti-duplikat. |
| `tests/test_mqtt_publisher.py` | Unit test MQTT publisher dengan mock MQTT client tanpa koneksi internet. |

### Raspberry Pi — Validasi & Analisis (`raspberry_pi/`)

| File | Fungsi |
|---|---|
| `validate_latest_frame.py` | Validasi frame raw terakhir: shape, dtype, header CSV, konsistensi ch3. |
| `validate_latest_mv_frame.py` | Validasi frame mV terakhir: shape, dtype, header mV CSV, konsistensi Lead III. |
| `validate_model_input.py` | Validasi NPY model-ready: shape, dtype, rentang mV, NaN/Inf. |
| `validate_prosim_calibration.py` | Validasi konfigurasi kalibrasi terhadap dataset raw yang diketahui. |
| `validate_all_recorded_datasets.py` | Replay dan validasi batch semua dataset ProSim. |
| `analyze_latest_ads1293_frame.py` | Analisis mendalam frame ADS1293: metrik per-lead, plot, konsistensi ch3. |

### Raspberry Pi — Tools Lainnya (`raspberry_pi/`)

| File | Fungsi |
|---|---|
| `replay_dataset.py` | Replay offline dataset yang sudah direkam. Menggunakan pipeline kalibrasi dan model input yang sama. |
| `metadata_store.py` | Penyimpanan metadata frame ke SQLite database. |
| `oled_status.py` | Standalone app untuk display OLED I2C 128×64. Read-only, tidak menyentuh serial. |
| `ecgrhythmia-oled.service` | Systemd unit file untuk menjalankan OLED status sebagai service. |

### Raspberry Pi — Test (`raspberry_pi/`)

| File | Fungsi |
|---|---|
| `test_model_input.py` | Unit test untuk persiapan model input. |
| `test_phase3h_pipeline.py` | Test end-to-end pipeline Phase 3H. |

### ECG Qt Viewer (`raspberry_pi/ecg_qt_viewer/`)

| File | Fungsi |
|---|---|
| `main.py` | Entry point & argument parsing. |
| `main_window.py` | Window utama PyQt6: toolbar, mode switching, frame navigation, live follow. |
| `ecg_plot_widget.py` | Widget plot ECG dengan crosshair, pan/zoom. |
| `ecg_grid_item.py` | Grid item untuk medical ECG paper (25mm/s, 10mm/mV). |
| `frame_repository.py` | Repository untuk load dan mengelola frame data. |
| `frame_validator.py` | Validator frame: shape, dtype, NaN/Inf, amplitude check. |
| `frame_watcher.py` | File system watcher untuk frame baru (live follow). |
| `viewer_settings.py` | Pengaturan persisten viewer (folder terakhir, mode, dll). |

### Third Party (`third_party/ecg-edge-ai-engine/`)

| File | Fungsi |
|---|---|
| `src/` | Source code engine AI inferensi ECG. |
| `models/` | Model TFLite untuk inferensi. |
| `batch_test_ecgrhythmia.py` | Script batch testing inferensi AI. |
| `setup.py` | Package setup untuk instalasi sebagai modul Python. |

---

## 17. Format Data & Protokol

### Binary Serial Packet (ESP32 → RPi)

Setiap packet 32 byte, little-endian:

```
Offset  Size  Field
──────  ────  ─────────────────────────
  0      2    SYNC magic: 0xAA 0x55
  2      1    VERSION: 0x01
  3      2    PAYLOAD_SIZE: 25 (little-endian)
  5      4    sequence (uint32)
  9      4    timestamp_us (uint32)
 13      2    sample_count (uint16) = 1
 15      2    sampling_rate_hz (uint16) = 250
 17      1    flags (uint8)
 18      4    lead_i (int32)
 22      4    lead_ii (int32)
 26      4    lead_iii (int32)
 30      2    CRC16-CCITT-FALSE
```

- **CRC16** dihitung atas byte 0–29 (30 byte sebelum field CRC).
- **Polynomial:** 0x1021, **Initial:** 0xFFFF.
- **TIDAK menggunakan JSON** untuk komunikasi realtime.

### Frame Export Files

| Format | Konten |
|---|---|
| `.csv` | Text CSV, header `time,ch1,ch2,ch3`, 2501 baris (1 header + 2500 data) |
| `.bin` | Binary dump numpy array |
| `.npy` | NumPy array, dtype `float32`, shape `(2500, 3)` |
| `.json` | Metadata: measurement_id, device_id, timestamps, checksum SHA256, parser stats |

---

## 18. Troubleshooting

### ESP32 Tidak Mengirim Data

1. Pastikan environment yang benar sudah di-upload:
   ```bash
   pio run -e esp32dev_dummy_usb -t upload
   ```
2. Buka Serial Monitor dan ketik `status` untuk cek konfigurasi.
3. Ketik `start` (dummy mode) atau `ads stream start` (ADS1293 mode).

### Receiver Tidak Menerima Packet

1. Periksa port serial:
   ```bash
   ls -la /dev/ttyUSB* /dev/ttyACM*
   ```
2. Pastikan baudrate sesuai (115200).
3. Cek permission:
   ```bash
   sudo usermod -aG dialout $USER
   # Logout dan login kembali
   ```
4. Jika menggunakan ADS1293, cek `config.yaml` → `source: ads1293`.

### CRC Error Tinggi

- Kemungkinan noise pada koneksi serial. Periksa kabel USB.
- Pastikan tidak ada program lain yang membuka port serial yang sama.

### Frame Export Gagal

1. Periksa apakah direktori output ada: `raspberry_pi/data/`
2. Pastikan disk tidak penuh.
3. Periksa log receiver untuk error detail.

### Kalibrasi Gagal

1. Pastikan data ProSim sudah terekam dengan benar.
2. Jalankan `validate_latest_frame.py` terlebih dahulu.
3. Pastikan amplitudo sinyal cukup (minimum ~100 ADC count).

### Qt Viewer Tidak Muncul

1. Pastikan `venv_gui` terinstall:
   ```bash
   source venv_gui/bin/activate
   python -c "import PyQt6; print('OK')"
   ```
2. Ubuntu: install paket XCB:
   ```bash
   sudo apt install libxcb-cursor0 libxkbcommon-x11-0
   ```
3. Coba mode offscreen:
   ```bash
   QT_QPA_PLATFORM=offscreen python -m ecg_qt_viewer.main
   ```

### MQTT HiveMQ Tidak Connect

1. Pastikan password sudah tersedia sebagai environment variable:
   ```bash
   echo "$HIVEMQ_PASSWORD" | wc -c
   ```
   Output harus lebih dari `1`. Jangan print password langsung ke terminal.
2. Pastikan command memakai TLS dan port `8883`.
3. Pastikan username benar: `ecg-undip`.
4. Jika muncul `authentication failed` atau `not authorized`, cek credential di
   HiveMQ Cloud Access Management.
5. Jika muncul TLS certificate error, jangan matikan certificate verification.
   Periksa waktu sistem dan CA trust store Ubuntu/Python.

### HiveMQ Web Client Gagal Subscribe Topic

- Gunakan topic spesifik:
  ```text
  ecgrhythmia/device01/frame
  ```
- Atau wildcard:
  ```text
  ecgrhythmia/#
  ```
- Jangan gunakan:
  ```text
  #ecgrhythmia/device01/frame
  ```

### MQTT Tidak Mengirim Ulang Frame Saat Test

Jika frame sudah pernah mendapat ACK, publisher akan skip frame yang sama. Untuk
ulang test HiveMQ ProSim dari nol:

```bash
rm data/predictions_prosim_test/mqtt_publish_state_hivemq.json
```

---

## 19. Catatan Penting

> [!CAUTION]
> **Keselamatan Listrik**
> Gunakan preset Human Quick Test hanya dengan operasi baterai atau power bank.
> **JANGAN hubungkan charger / PLN saat elektroda terpasang pada tubuh.**

> [!IMPORTANT]
> **Aturan Pengembangan**
> - Jangan gunakan JSON untuk komunikasi realtime ESP32 ↔ Raspberry Pi.
> - Gunakan binary frame serial + CRC16.
> - Jangan hardcode pin di `main.cpp` — semua dari `config.h`.
> - Jangan buat model 1D-CNN dari nol atau training model.
> - TFLite hanya interface/placeholder sampai model tersedia.
> - Kode harus modular, bukan monolitik.

> [!NOTE]
> **Status Saat Ini**
> - Sensor ADS1293 sudah terhubung dan berfungsi.
> - Kalibrasi multipoint sudah selesai (R² = 0.9999).
> - Pipeline raw → calibrated → model-ready sudah teruji.
> - Testing saat ini melalui USB Serial; UART tetap target final.
> - Model AI belum di-training — menggunakan TFLite placeholder.

---

*Dokumentasi ini di-generate berdasarkan state kode per Juli 2026.*

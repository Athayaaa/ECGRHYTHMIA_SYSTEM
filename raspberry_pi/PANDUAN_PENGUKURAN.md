# Panduan Melakukan Pengukuran Langsung (Real-time AI + MQTT)

Panduan ini menjelaskan langkah-langkah untuk melakukan sesi perekaman dan
pemantauan sinyal ECG secara *real-time* lengkap dengan deteksi aritmia berbasis
Edge AI dan pengiriman data ke dashboard web melalui MQTT.

Pastikan **ESP32 sudah menyala dan terhubung ke Raspberry Pi melalui USB** sebelum memulai pengukuran.

---

## Ringkasan Alur

```text
ESP32
  -> receiver.py
  -> data/sessions/session_DDMMYYYY_HHMMSS/calibrated/frame_000001_mv.npy
  -> ai_worker.py
  -> data/sessions/session_DDMMYYYY_HHMMSS/predictions/frame_000001_prediction.json
  -> mqtt_publisher.py
  -> HiveMQ Cloud / dashboard web
```

## Session Perekaman

Untuk perekaman baru, gunakan folder sesi otomatis. Nama sesi memakai format
`session_DDMMYYYY_HHMMSS`; apabila waktunya bertabrakan, sistem menambahkan
akhiran `_01`, `_02`, dan seterusnya tanpa mengubah sesi yang sudah ada.

```text
data/sessions/session_DDMMYYYY_HHMMSS/
├── raw_adc/
├── calibrated/
├── model_ready/
├── predictions/
├── logs/
└── session.json
```

`session.json` memuat ID, waktu mulai/selesai, status, sampling rate, durasi
frame, channel `Lead I`, `Lead II`, `Lead III`, unit `mV`, dan jumlah frame.
File ini ditulis atomik saat sesi dibuat dan saat sesi dihentikan.

Sebelum memakai perangkat, periksa pembuatan sesi tanpa membuka serial:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate
python3 receiver.py --new-session --sessions-root data/sessions --dry-run-session
```

Data yang dikirim ke MQTT adalah **full ECG mV per frame**:

| Parameter | Nilai |
|---|---|
| Durasi frame | 10 detik |
| Sampling rate | 250 Hz |
| Jumlah sample | 2500 |
| Shape | `[2500, 3]` |
| Unit | `mV` |
| Channel order | `Lead I`, `Lead II`, `Lead III` |
| Base topic | `ecgrhythmia/device01` |
| Topic JSON | `ecgrhythmia/device01/frame/json` |
| Topic NPY ECG | `ecgrhythmia/device01/frame/npy` |
| Topic NPY prediction | `ecgrhythmia/device01/prediction` |

Publisher MQTT tidak membuka serial port, tidak menjalankan AI, dan tidak
mengubah file ECG atau prediction. Publisher hanya menunggu pasangan:

```text
frame_000001_mv.npy
frame_000001_prediction.json
```

`latest_prediction.json` tidak digunakan untuk publish.

---

## Tahap 1: Session Baru dan Output Legacy
Sesi baru tidak perlu menghapus sesi sebelumnya: setiap perekaman memakai folder
baru di `data/sessions/`. Bila masih ada output flat dari versi lama dan ingin
membersihkannya, jalankan perintah berikut. Perintah ini tidak menyentuh folder
`data/sessions/` atau dataset ProSim.

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate
python3 receiver.py --clean-legacy-output
```

---

## Tahap 2: Setup MQTT HiveMQ Cloud

Tahap ini hanya perlu dilakukan sekali pada Raspberry Pi/PC yang menjalankan
publisher.

### Broker dan Topic

| Item | Nilai |
|---|---|
| Broker host | `93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud` |
| Port | `8883` |
| TLS | Aktif / wajib |
| Username | `ecg-undip` |
| Base topic publish | `ecgrhythmia/device01` |
| Topic JSON | `ecgrhythmia/device01/frame/json` |
| Topic NPY ECG | `ecgrhythmia/device01/frame/npy` |
| Topic NPY prediction | `ecgrhythmia/device01/prediction` |
| QoS | `1` |
| Retain | `false` |
| Client ID publisher | `ecgrhythmia-device01-publisher` |

Dashboard web harus memakai client ID berbeda, misalnya:

```text
ecgrhythmia-dashboard-web
```

### Simpan Password sebagai Environment Variable

Jangan menulis password HiveMQ di `mqtt_publisher.py`, dokumentasi, git, atau
command history. Simpan di file lokal yang permission-nya dibatasi:

```bash
mkdir -p ~/.config/ecgrhythmia
nano ~/.config/ecgrhythmia/hivemq.env
chmod 600 ~/.config/ecgrhythmia/hivemq.env
```

Isi file `~/.config/ecgrhythmia/hivemq.env`:

```bash
export HIVEMQ_PASSWORD='isi_password_hivemq'
```

Setiap terminal yang akan menjalankan MQTT publisher perlu memuat file ini:

```bash
source ~/.config/ecgrhythmia/hivemq.env
```

### Test Dry Run MQTT

Dry run memastikan file ECG mV dan prediction dapat dipasangkan tanpa koneksi ke
broker dan tanpa menulis state.

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_ai/bin/activate

python3 mqtt_publisher.py \
  --ecg-dir data/calibrated_prosim_test \
  --prediction-dir data/predictions_prosim_test \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01 \
  --device-id device01 \
  --payload-format json \
  --stress-test \
  --tls \
  --once \
  --dry-run
```

Output normal mode JSON:

```text
Frame 000001 ready
Format: JSON
Frame 000001 telemetry: CPU 41.2%, RAM 36.7%, TEMP 56.9 C
Frame 000001 network: MQTT disconnected, LAT N/A ms, RSSI -59 dBm
Frame 000001 stress test: enabled
Topic: ecgrhythmia/device01/frame/json
ECG shape: (2500, 3)
ECG dtype: float32
Payload size: 164613 bytes
Prediction: Normal 98.75%
Dry run: not connected to broker
```

Untuk mengecek mode NPY tanpa koneksi broker:

```bash
python3 mqtt_publisher.py \
  --ecg-dir data/calibrated_prosim_test \
  --prediction-dir data/predictions_prosim_test \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01 \
  --device-id device01 \
  --payload-format npy \
  --stress-test \
  --tls \
  --once \
  --dry-run
```

Output normal mode NPY:

```text
Frame 000001 ready
Format: NPY
ECG topic: ecgrhythmia/device01/frame/npy
Prediction topic: ecgrhythmia/device01/prediction
ECG shape: (2500, 3)
ECG dtype: float32
NPY size: 30128 bytes
Prediction payload size: sekitar 800 bytes
Prediction: Normal 98.75%
Dry run: not connected to broker
```

### Test Subscribe di HiveMQ Web Client

Di HiveMQ Web Client, subscribe ke topic:

```text
ecgrhythmia/device01/#
```

Atau subscribe spesifik sesuai format:

```text
ecgrhythmia/device01/frame/json
ecgrhythmia/device01/frame/npy
ecgrhythmia/device01/prediction
```

Untuk menangkap semua topic ECGRHYTHMIA:

```text
ecgrhythmia/#
```

Jangan memakai:

```text
#ecgrhythmia/device01/frame
```

Wildcard `#` hanya valid sebagai `#` atau setelah slash seperti
`ecgrhythmia/#`.

---

## Tahap 3: Menjalankan Sistem Realtime

Untuk menjalankan pengukuran secara keseluruhan, Anda membutuhkan 3 terminal
utama:

1. Receiver dan GUI ECG Viewer
2. AI Worker
3. MQTT Publisher

Terminal OLED bersifat opsional.

### Terminal 1: Menjalankan Receiver & GUI
Proses ini akan membaca data sensor dari ESP32, menyimpannya, dan secara otomatis membuka aplikasi GUI ECG Viewer.

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate

# Jalankan receiver dalam satu session baru (ini otomatis meluncurkan GUI ECG Viewer)
python3 receiver.py --new-session --sessions-root data/sessions \
  --port /dev/ttyACM0 --baudrate 115200
```

Catat nilai `Output` yang dicetak receiver, lalu pakai path tersebut sebagai
`SESSION_DIR` pada terminal AI dan MQTT di bawah. Contoh berikut memakai satu
sesi bernama `session_DDMMYYYY_HHMMSS`.

> **Tips:** Di aplikasi GUI yang terbuka, pastikan Anda menekan tombol **"Follow Live"** agar grafik terus bergeser mengikuti detak jantung yang paling baru.

### Terminal 2: Menjalankan AI Worker
Proses ini akan memantau data baru yang dihasilkan oleh receiver secara terus-menerus, menjalankan model AI, dan mengirimkan hasil prediksinya kembali ke GUI.

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_ai/bin/activate

# Jalankan AI worker dalam mode continuous (--follow)
SESSION_DIR=data/sessions/session_DDMMYYYY_HHMMSS
python3 ai_worker.py \
  --input-dir "$SESSION_DIR/model_ready" \
  --output-dir "$SESSION_DIR/predictions" \
  --follow
```

*(Begitu worker mendeteksi data baru, hasil prediksi deteksi aritmianya akan langsung muncul dan berkedip pada badge di kanan atas aplikasi GUI Anda).*

### Terminal 3: Menjalankan MQTT Publisher ke HiveMQ Cloud

Proses ini menunggu frame ECG mV dari receiver dan prediction JSON dari
`ai_worker.py`. Jika pasangan dengan frame ID yang sama sudah lengkap, publisher
mengirim payload MQTT ke dashboard web. Default format adalah JSON, tetapi Anda
dapat memilih `--payload-format json` atau `--payload-format npy`.

Mode JSON mengirim satu pesan ke:

```text
ecgrhythmia/device01/frame/json
```

Mode NPY mengirim dua pesan dengan `message_id` dan `frame_id` yang sama:

```text
ecgrhythmia/device01/frame/npy
ecgrhythmia/device01/prediction
```

Gunakan mode JSON jika dashboard ingin menerima ECG dan prediction dalam satu
payload JSON. Gunakan mode NPY jika dashboard ingin menerima ECG sebagai binary
NumPy `.npy` yang lebih kecil, lalu memasangkannya dengan prediction JSON.

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_ai/bin/activate
source ~/.config/ecgrhythmia/hivemq.env

SESSION_DIR=data/sessions/session_DDMMYYYY_HHMMSS

python3 mqtt_publisher.py \
  --ecg-dir "$SESSION_DIR/calibrated" \
  --prediction-dir "$SESSION_DIR/predictions" \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01 \
  --device-id device01 \
  --payload-format json \
  --stress-test \
  --username ecg-undip \
  --password-env HIVEMQ_PASSWORD \
  --client-id ecgrhythmia-device01-publisher \
  --state-file "$SESSION_DIR/predictions/mqtt_publish_state_hivemq.json" \
  --tls \
  --follow
```

Log sukses:

```text
MQTT publisher started
Watching ECG: .../data/sessions/session_DDMMYYYY_HHMMSS/calibrated
Watching predictions: .../data/sessions/session_DDMMYYYY_HHMMSS/predictions
Connecting to HiveMQ Cloud
Broker: 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud:8883
TLS: enabled
Connected to broker
Frame 000001 ready
Format: JSON
Frame 000001 telemetry: CPU 41.2%, RAM 36.7%, TEMP 56.9 C
Frame 000001 network: MQTT connected, LAT N/A ms, RSSI -59 dBm
Frame 000001 stress test: enabled
Topic: ecgrhythmia/device01/frame/json
Payload size: 164613 bytes
Frame 000001 published, QoS 1
```

Normalnya satu frame dipublish sekitar setiap 10 detik setelah prediction untuk
frame yang sama tersedia.

Jika ingin menjalankan mode NPY secara live, ganti hanya argumen format:

```bash
python3 mqtt_publisher.py \
  --ecg-dir data/calibrated \
  --prediction-dir data/predictions \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01 \
  --device-id device01 \
  --payload-format npy \
  --stress-test \
  --username ecg-undip \
  --password-env HIVEMQ_PASSWORD \
  --client-id ecgrhythmia-device01-publisher \
  --state-file data/predictions/mqtt_publish_state_hivemq.json \
  --tls \
  --follow
```

Log sukses mode NPY:

```text
Frame 000001 ready
Format: NPY
ECG topic: ecgrhythmia/device01/frame/npy
Prediction topic: ecgrhythmia/device01/prediction
ECG shape: (2500,3)
NPY size: 30128 bytes
Frame 000001 published, QoS 1
```

### Terminal 4 (Opsional): Menjalankan Layar OLED
Jika perangkat Anda dilengkapi layar OLED kecil yang terhubung, Anda bisa
membukanya di terminal keempat untuk melihat rangkuman status sistem secara
*real-time* tanpa harus selalu memantau GUI di monitor utama:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_oled/bin/activate

# B default GPIO19; gunakan GPIO20 bila OUT B encoder dipindahkan ke pin 38.
python3 oled_status.py --i2c-bus 1 --address 0x3C --driver ssd1306 --encoder-b-gpio 20
```

OLED tidak membuka serial, tidak menjalankan AI, dan tidak membaca prediction
JSON. OLED hanya menampilkan status service AI/MQTT dan dapat mengontrol service
receiver, AI, serta MQTT dari rotary menu.

### Mode Service OLED

Untuk penggunaan mandiri tanpa empat terminal, install service user sekali saja:

```bash
mkdir -p ~/.config/systemd/user
cp ecgrhythmia-receiver.service ecgrhythmia-ai.service ecgrhythmia-mqtt.service ecgrhythmia-oled.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ecgrhythmia-oled.service
sudo loginctl enable-linger "$USER"
```

Aktifkan hanya service OLED. Dari menu `Recording`, OLED membuat session baru
`data/sessions/session_DDMMYYYY_HHMMSS/`, menulis
`runtime/current_session.env`, lalu menjalankan receiver → AI → MQTT. Output
receiver masuk ke `raw_adc/`, output mV ke `calibrated/`, input AI ke
`model_ready/`, dan hasil AI/MQTT ke `predictions/`. Saat stop, OLED
menghentikan MQTT → AI → receiver dan memfinalkan `session.json`; sesi lama
tidak dihapus. Untuk wiring dan pengaturan service dengan GPIO20, lihat
`OLED_STATUS_GUIDE.md`.

---

## Tahap 4: Verifikasi Dashboard

Pastikan tim dashboard web menggunakan konfigurasi berikut:

| Item | Nilai |
|---|---|
| Broker | `93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud` |
| Port | `8883` |
| TLS | Aktif |
| Username | `ecg-undip` |
| Subscribe semua device01 | `ecgrhythmia/device01/#` |
| JSON ECG + prediction | `ecgrhythmia/device01/frame/json` |
| NPY ECG binary | `ecgrhythmia/device01/frame/npy` |
| NPY prediction JSON | `ecgrhythmia/device01/prediction` |
| QoS subscribe | `1` |

Dashboard harus melakukan deduplikasi berdasarkan:

```text
message_id
```

### Format JSON

Jika publisher dijalankan dengan `--payload-format json`, dashboard menerima satu
payload JSON compact di topic:

```text
ecgrhythmia/device01/frame/json
```

Payload berisi:

```json
{
  "schema_version": 1,
  "message_id": "device01-default-frame_000001",
  "device_id": "device01",
  "session_id": "default",
  "frame_id": "000001",
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
  },
  "system": {
    "cpu_usage_percent": 41.2,
    "memory_usage_percent": 36.7,
    "memory_usage_mb": 812,
    "cpu_temperature_c": 56.9,
    "uptime_s": 19352
  },
  "stress_test": {
    "enabled": true,
    "frame_counter": 1
  },
  "network": {
    "mqtt_publish_latency_ms": 8.7,
    "wifi_rssi_dbm": -59,
    "mqtt_connected": true
  }
}
```

Pada payload aktual, `ecg.samples` berisi 2500 baris x 3 channel. Contoh di
atas hanya menampilkan satu baris agar mudah dibaca.

`system` dikoleksi satu kali per frame setelah pasangan ECG/prediction siap:
CPU, RAM, uptime berasal dari `psutil`, sedangkan temperatur CPU diprioritaskan
dari `/sys/class/thermal/thermal_zone0/temp`. `network.mqtt_publish_latency_ms`
adalah latency publish yang telah mendapat ACK sebelumnya; pada frame pertama
nilainya dapat `null`. RSSI juga `null` bila driver WiFi tidak memberi dBm.

### Format NPY

Jika publisher dijalankan dengan `--payload-format npy`, dashboard menerima ECG
binary NumPy `.npy` di topic:

```text
ecgrhythmia/device01/frame/npy
```

Ketentuan ECG NPY:

| Parameter | Nilai |
|---|---|
| dtype | `float32` |
| Unit | `mV` |
| Shape saat ini | `[2500, 3]` |
| Channel order | `Lead I`, `Lead II`, `Lead III` |
| Pickle | Tidak digunakan |

Prediction tetap JSON dan dikirim ke:

```text
ecgrhythmia/device01/prediction
```

Struktur prediction mode NPY:

```json
{
  "schema_version": 1,
  "message_id": "device01-default-frame_000001",
  "device_id": "device01",
  "session_id": "default",
  "frame_id": "000001",
  "ecg_format": "npy",
  "ecg_topic": "ecgrhythmia/device01/frame/npy",
  "sampling_rate_hz": 250.0,
  "unit": "mV",
  "shape": [2500, 3],
  "channel_order": ["Lead I", "Lead II", "Lead III"],
  "validation": {
    "status": "PASS",
    "warnings": []
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
  },
  "system": {
    "cpu_usage_percent": 41.2,
    "memory_usage_percent": 36.7,
    "memory_usage_mb": 812,
    "cpu_temperature_c": 56.9,
    "uptime_s": 19352
  },
  "stress_test": {
    "enabled": true,
    "frame_counter": 1
  },
  "network": {
    "mqtt_publish_latency_ms": 8.7,
    "wifi_rssi_dbm": -59,
    "mqtt_connected": true
  }
}
```

Dashboard harus memasangkan ECG NPY dan prediction JSON berdasarkan
`message_id` dan `frame_id`. Jika ECG NPY sudah diterima tetapi prediction retry,
dashboard tetap melakukan deduplikasi berdasarkan `message_id`.
Binary ECG NPY tidak mengandung telemetry dan tidak pernah dimodifikasi;
`system`, `stress_test`, dan `network` berada pada JSON companion ini.

---

## Tahap 5: Test Publish ProSim Sekali Kirim

Gunakan mode ini untuk demonstrasi atau validasi dashboard tanpa menjalankan
receiver live. Publisher memakai data ProSim yang sudah tersedia.

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_ai/bin/activate
source ~/.config/ecgrhythmia/hivemq.env

python3 mqtt_publisher.py \
  --ecg-dir data/calibrated_prosim_test \
  --prediction-dir data/predictions_prosim_test \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01 \
  --device-id device01 \
  --payload-format json \
  --stress-test \
  --username ecg-undip \
  --password-env HIVEMQ_PASSWORD \
  --client-id ecgrhythmia-device01-publisher \
  --state-file data/predictions_prosim_test/mqtt_publish_state_hivemq.json \
  --tls \
  --once
```

Untuk test ProSim sekali kirim dalam mode NPY:

```bash
python3 mqtt_publisher.py \
  --ecg-dir data/calibrated_prosim_test \
  --prediction-dir data/predictions_prosim_test \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01 \
  --device-id device01 \
  --payload-format npy \
  --stress-test \
  --username ecg-undip \
  --password-env HIVEMQ_PASSWORD \
  --client-id ecgrhythmia-device01-publisher \
  --state-file data/predictions_prosim_test/mqtt_publish_state_hivemq.json \
  --tls \
  --once
```

Jika ingin mengulang test ProSim dari awal, hapus state HiveMQ ProSim:

```bash
rm data/predictions_prosim_test/mqtt_publish_state_hivemq.json
```

---

## Troubleshooting MQTT

### Publisher Berhenti: Password Env Belum Ada

Jika muncul error seperti:

```text
environment variable 'HIVEMQ_PASSWORD' is not set
```

jalankan:

```bash
source ~/.config/ecgrhythmia/hivemq.env
```

Lalu ulangi command publisher.

### Authentication Failed / Not Authorized

Periksa:

- username harus `ecg-undip`;
- password HiveMQ benar;
- user tersebut punya izin publish ke `ecgrhythmia/device01/#`;
- password tidak memiliki spasi tambahan di file env.

### TLS Certificate Failed

Jangan menonaktifkan TLS verification. Periksa:

- port harus `8883`;
- `--tls` harus dipakai;
- waktu sistem Ubuntu/Raspberry Pi benar;
- CA trust store tersedia.

### Frame Tidak Dikirim Ulang

Publisher menyimpan state setelah publish mendapat ACK. Jika frame sudah pernah
terkirim ke broker, base topic, session, frame ID, dan format payload yang sama,
log akan menunjukkan:

```text
Frame 000001 skipped: already sent
```

Frame yang sudah terkirim sebagai JSON masih bisa dikirim lagi sebagai NPY karena
state membedakan format payload. Untuk test live, biasanya tidak perlu menghapus
state karena frame baru punya ID baru. Untuk test ProSim yang sama, hapus file
state ProSim:

```bash
rm data/predictions_prosim_test/mqtt_publish_state_hivemq.json
```

### Dashboard Tidak Menerima Pesan

Periksa:

- dashboard subscribe ke `ecgrhythmia/device01/#` atau `ecgrhythmia/#`;
- untuk JSON, dashboard membaca `ecgrhythmia/device01/frame/json`;
- untuk NPY, dashboard membaca `ecgrhythmia/device01/frame/npy` dan
  `ecgrhythmia/device01/prediction`;
- dashboard memakai client ID berbeda dari publisher;
- MQTT Web Client tidak memakai topic salah seperti `#ecgrhythmia/device01/frame`;
- publisher menampilkan `Frame NNNNNN published, QoS 1`;
- dashboard siap menerima payload JSON sekitar 160 KB per frame, atau NPY sekitar
  30 KB untuk ECG plus prediction JSON kecil.

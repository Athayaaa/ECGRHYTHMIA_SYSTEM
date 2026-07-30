# Panduan Melakukan Pengukuran Langsung (Real-time AI + MQTT)

Panduan ini menjelaskan langkah-langkah untuk melakukan sesi perekaman dan
pemantauan sinyal ECG secara *real-time* lengkap dengan deteksi aritmia berbasis
Edge AI dan pengiriman data ke dashboard web melalui MQTT.

> [!CAUTION]
> Prototipe ini adalah alat pengembangan/riset, bukan perangkat diagnosis atau
> pengganti penilaian tenaga kesehatan. Jangan gunakan pada orang yang sedang
> mengalami nyeri dada, sesak, pingsan, atau gejala gawat; cari pertolongan medis
> segera. Gunakan hanya rangkaian yang beroperasi dengan baterai/power bank dan
> jangan hubungkan charger atau sumber PLN ketika elektroda menempel pada tubuh.

Pastikan **ESP32 sudah menyala dan terhubung ke Raspberry Pi melalui USB**
sebelum memulai pengukuran. Untuk pengukuran manusia, kabel dan elektroda harus
utuh, kering, serta terpasang pada sistem berdaya baterai yang terisolasi.

---

## Ringkasan Alur

```text
ESP32
  -> receiver.py
  -> data/sessions/session_DDMMYYYY_HHMMSS/calibrated/frame_000001_mv.npy
  -> data/sessions/session_DDMMYYYY_HHMMSS/filtered/frame_000001_mv.npy
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
├── calibrated/    # mV hasil kalibrasi asli
├── filtered/      # salinan mV dengan notch 50 Hz bila aktif
├── model_ready/
├── predictions/
├── logs/
└── session.json
```

`session.json` memuat ID, waktu mulai/selesai, status, sampling rate, durasi
frame, channel `Lead I`, `Lead II`, `Lead III`, unit `mV`, dan jumlah frame.
File ini ditulis atomik saat sesi dibuat dan saat sesi dihentikan.

## Protokol Pengukuran Manusia

Gunakan posisi elektroda yang sama pada setiap sesi untuk subjek yang sama.
Posisi tubuh, persiapan kulit, dan lokasi elektroda memengaruhi bentuk serta
amplitudo ECG; perpindahan lokasi dapat mengubah hasil walaupun orangnya sama.
Panduan ini memakai konfigurasi monitoring 3-lead pada proyek ini, bukan
pengganti ECG diagnostik 12-lead.

### 1. Persiapan Aman

1. Gunakan ESP32/Raspberry Pi yang dipasok baterai atau power bank. Lepaskan
   charger laptop/Raspberry Pi/ESP32 sebelum elektroda dipasang ke tubuh.
2. Periksa kabel lead, konektor, dan elektroda. Jangan gunakan kabel terkelupas,
   elektroda rusak, atau elektroda yang sudah tidak melekat baik.
3. Minta subjek duduk atau berbaring santai, diam, dan bernapas normal selama
   sekitar 5 menit. Jangan berbicara, menyentuh kabel, atau menggerakkan lengan
   selama perekaman.
4. Pilih kulit yang bersih, kering, bebas krim/minyak. Bersihkan dengan alcohol
   swab, tunggu hingga kering, dan rapikan rambut bila menghambat perekat.
   Gunakan elektroda baru untuk setiap sesi atau bila perekat mulai longgar.

Persiapan kulit seperti pembersihan dan abrasi ringan yang sesuai dapat
mengurangi noise; perubahan posisi pasien juga dapat mengubah amplitudo ECG.
Rujukan: [AHA/ACC/HRS ECG technology statement](https://www.jacc.org/doi/10.1016/j.jacc.2007.01.024)
dan [AHA cardiac event-recorder guidance](https://www.heart.org/en/health-topics/arrhythmia/symptoms-diagnosis--monitoring-of-arrhythmia/cardiac-event-recorder).

### 2. Pemasangan Lead Proyek

Selalu ikuti label **RA, LA, LL, RLD/RL** pada kabel/breakout, bukan warna kabel,
karena warna dapat berbeda antar perangkat.

| Label | Lokasi torso untuk monitoring yang konsisten | Fungsi di proyek |
|---|---|---|
| `RA` | Dada kanan atas, sedikit di bawah klavikula kanan | Referensi negatif Lead I dan II |
| `LA` | Dada kiri atas, sedikit di bawah klavikula kiri | Positif Lead I |
| `LL` | Perut kiri bawah, di atas area tulang panggul | Positif Lead II |
| `RLD` / `RL` | Perut kanan bawah, simetris dengan LL | Right-leg drive/referensi; bukan channel data |

Firmware memetakan **Lead I = LA - RA**, **Lead II = LL - RA**, dan menghitung
**Lead III = Lead II - Lead I**. Tempelkan keempat elektroda pada kulit, tekan
keliling perekatnya, buat kabel tidak tertarik, dan jangan menyilangkan atau
membiarkan kabel bergerak bebas. Penempatan torso membantu mengurangi artefak
gerakan untuk monitoring, tetapi bentuknya tidak boleh dibandingkan langsung
dengan ECG diagnostik 12-lead yang memakai penempatan ekstremitas standar.

### 3. Mulai dan Periksa Sinyal

1. Pastikan [calibration_config.yaml](calibration_config.yaml) memuat:

   ```yaml
   filters:
     notch_50hz: true
     notch_quality_factor: 30.0
   ```

2. Jalankan receiver dalam sesi baru seperti pada Tahap 3. Tunggu sedikitnya
   10 detik agar frame pertama selesai diekspor.
3. Buka tampilan mV hasil notch pada terminal lain bila diperlukan:

   ```bash
   cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
   source venv_gui/bin/activate
   python -m ecg_qt_viewer.main \
     --data-dir "$SESSION_DIR/filtered" --source-mode mv --follow-live
   ```

4. Periksa Lead I dan Lead II lebih dahulu. Puncak R harus konsisten antar detak,
   baseline tidak meloncat besar, dan status kualitas tidak boleh `FAIL`.
5. Jika tampak bergerigi, jangan hanya mengandalkan notch. Diamkan subjek,
   tekan kembali RA/LA/RLD, ganti elektroda yang longgar, dan pastikan kabel
   tidak bergerak. Gerigi yang dominan di Lead I biasanya perlu memeriksa
   pasangan RA-LA lebih dahulu.
6. Bandingkan bila perlu: `calibrated/` adalah rekaman mV asli, sedangkan
   `filtered/` adalah salinan dengan notch 50 Hz. Simpan keduanya untuk audit;
   hilangnya noise setelah notch tidak membuktikan pemasangan elektroda sudah
   baik.

### 4. Akhiri Sesi

1. Hentikan receiver dengan `Ctrl+C` atau menu **Stop Recording** pada OLED.
2. Pastikan `session.json` berstatus `STOPPED` dan catat ID sesi, waktu,
   posisi tubuh, serta setiap penggantian elektroda. Jangan simpan identitas
   pribadi subjek di nama folder.
3. Lepaskan elektroda perlahan dan periksa iritasi kulit. Bila ada keluhan,
   hentikan penggunaan dan ikuti prosedur keselamatan setempat.

### Diagnosa Cepat Kualitas Sinyal

| Gejala | Periksa lebih dahulu |
|---|---|
| Gerigi 50 Hz, terutama Lead I | Kontak RA dan LA, kulit berminyak/berkeringat, kabel bergerak, serta kontak RLD/RL |
| Baseline naik-turun lambat | Gerakan, bicara, napas dalam, elektroda longgar |
| Sinyal sangat kecil atau datar | Label lead tertukar, konektor lepas, elektroda kering, atau mode sensor belum aktif |
| Puncak terpotong/angka mV tidak wajar | Lepas elektroda dari subjek, periksa konektor/suplai, lalu uji ulang memakai simulator ECG |

Jika noise tetap besar setelah pemasangan ulang, hentikan pengukuran manusia dan
uji rangkaian lebih dahulu dengan simulator ECG. Filter notch hanya menekan
komponen sekitar 50 Hz; ia tidak memperbaiki kontak elektroda, artefak gerak,
atau masalah keselamatan listrik.

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
| Topic JSON default | `ecgrhythmia/device01/frame/json` |
| Topic JSON exact | `ecgrhythmia/device01` saat `MQTT_JSON_TOPIC_EXACT=1` |
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
| Topic JSON default | `ecgrhythmia/device01/frame/json` |
| Topic JSON exact | `ecgrhythmia/device01` saat `MQTT_JSON_TOPIC_EXACT=1` |
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

### Mode Laptop Tanpa OLED

Jika ESP32 terhubung langsung ke laptop, gunakan tiga terminal di bawah dengan
port serial laptop, biasanya `/dev/ttyACM0`. OLED dan `systemctl --user` tidak
diperlukan. Setelah receiver membuat sesi, salin nilai `Output` yang dicetak
ke variabel `SESSION_DIR` pada terminal AI dan MQTT.

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
  --ecg-dir "$SESSION_DIR/filtered" \
  --prediction-dir "$SESSION_DIR/predictions" \
  --broker 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  --port 8883 \
  --topic ecgrhythmia/device01 \
  --device-id device01 \
  --payload-format json \
  --json-topic-exact \
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
Watching ECG: .../data/sessions/session_DDMMYYYY_HHMMSS/filtered
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
Topic: ecgrhythmia/device01
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

### Perbaikan Environment AI

Jalankan perintah berikut setelah clone baru, setelah `git pull` yang mengubah
dependency AI, atau bila log AI menyebut LiteRT/SciPy tidak dapat dimuat:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
systemctl --user stop ecgrhythmia-ai.service
bash setup_ai_env.sh
systemctl --user start ecgrhythmia-ai.service
```

Script ini memasang ulang dependency yang dipin tanpa cache dan memverifikasi
NumPy, SciPy, LiteRT, serta `model.tflite` sebelum worker dijalankan.

### Konfigurasi Lokal Raspi

Jangan mengubah `config.yaml` untuk port serial atau parameter khusus perangkat.
Gunakan override yang diabaikan Git agar `git pull` tidak menimbulkan konflik:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
[ -e config.local.yaml ] || cp config.local.example.yaml config.local.yaml
```

`config.local.yaml` dapat memuat `serial.port`, `device`, dan
`model_ready_export`. Nilainya menimpa `config.yaml` hanya pada Raspi tersebut.

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
`filtered/` menjadi salinan notch otomatis dan sumber MQTT ketika notch aktif,
input AI ke `model_ready/`, dan hasil AI/MQTT ke `predictions/`. Saat stop, OLED
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
| JSON exact-topic opsional | `ecgrhythmia/device01` |
| NPY ECG binary | `ecgrhythmia/device01/frame/npy` |
| NPY prediction JSON | `ecgrhythmia/device01/prediction` |
| QoS subscribe | `1` |

Dashboard harus melakukan deduplikasi berdasarkan:

```text
message_id
```

### Format JSON

Jika publisher dijalankan dengan `--payload-format json`, dashboard menerima satu
payload JSON compact. Default topic adalah:

```text
ecgrhythmia/device01/frame/json
```

Untuk kompatibilitas website yang hanya mendengarkan topic root perangkat,
atur `MQTT_JSON_TOPIC_EXACT=1` pada `runtime/mqtt_credentials.env`. Publisher
kemudian mengirim JSON tepat ke:

```text
ecgrhythmia/device01
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
- untuk JSON, dashboard membaca `ecgrhythmia/device01/frame/json` secara
  default, atau `ecgrhythmia/device01` bila exact-topic diaktifkan;
- untuk NPY, dashboard membaca `ecgrhythmia/device01/frame/npy` dan
  `ecgrhythmia/device01/prediction`;
- dashboard memakai client ID berbeda dari publisher;
- MQTT Web Client tidak memakai topic salah seperti `#ecgrhythmia/device01/frame`;
- publisher menampilkan `Frame NNNNNN published, QoS 1`;
- dashboard siap menerima payload JSON sekitar 160 KB per frame, atau NPY sekitar
  30 KB untuk ECG plus prediction JSON kecil.

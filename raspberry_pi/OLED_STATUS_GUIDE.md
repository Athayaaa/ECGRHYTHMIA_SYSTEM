# OLED I2C 128x64 Status Display + Rotary Menu

OLED ECGRHYTHMIA menampilkan status sistem dan menyediakan menu interaktif
melalui rotary encoder. Aplikasi OLED tidak membuka jalur transport ECG dan
tidak membaca data prediksi AI; status AI hanya ditampilkan sebagai service
`RUNNING` atau `STOPPED`.

## Wiring

### OLED SSD1306/SH1106 I2C

| OLED pin | Raspberry Pi pin |
|---|---|
| VCC | 3.3V, physical pin 1 |
| GND | GND, physical pin 6 |
| SDA | GPIO2 / SDA1, physical pin 3 |
| SCL | GPIO3 / SCL1, physical pin 5 |

Gunakan 3.3V, jangan 5V.

### Rotary Encoder

Penomoran GPIO memakai BCM.

| Encoder pin | Raspberry Pi pin |
|---|---|
| OUT A | GPIO13, physical pin 33 |
| OUT B | GPIO19, physical pin 35 |
| SWITCH | GPIO26, physical pin 37 |
| GND | physical pin 39 |

Input encoder aktif-low dengan internal pull-up. Pin 34 dapat menjadi GND
alternatif, tetapi tidak diperlukan. Pin 36/GPIO16, pin 38/GPIO20, dan
pin 40/GPIO21 tidak digunakan.

## Enable I2C

Jika `raspi-config` tersedia:

```bash
sudo raspi-config
# Interface Options -> I2C -> Enable
sudo reboot
```

Jika `raspi-config` tidak tersedia, tambahkan ini ke `/boot/firmware/config.txt`
atau `/boot/config.txt`:

```text
dtparam=i2c_arm=on
```

Lalu reboot dan cek alamat OLED:

```bash
sudo apt-get install -y i2c-tools
i2cdetect -y 1
```

Alamat umum adalah `0x3C`.

## Environment OLED

```bash
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
python3 -m venv venv_oled
source venv_oled/bin/activate
pip install --upgrade pip
pip install luma.oled Pillow smbus2 psutil gpiozero
```

Jika arah putar terbalik, jalankan dengan `--reverse-encoder` atau ubah setting
melalui file `runtime/oled_settings.json`.

Jika salah satu GPIO encoder bermasalah, pin dapat dioverride tanpa mengubah
wiring default. Contoh memindahkan OUT B dari GPIO19 ke GPIO20:

```bash
python3 oled_status.py --i2c-bus 1 --address 0x3C --driver ssd1306 --encoder-b-gpio 20
```

## Console Test

```bash
python3 oled_status.py --console --once
python3 oled_status.py --self-test
```

Console mode tidak memakai OLED dan tidak memakai GPIO.

## Run OLED

```bash
source venv_oled/bin/activate
python3 oled_status.py --i2c-bus 1 --address 0x3C --driver ssd1306
```

Jika panel SH1106:

```bash
python3 oled_status.py --i2c-bus 1 --address 0x3C --driver sh1106
```

Saat program baru menyala, OLED menampilkan greeting sekitar 3 detik. Durasi
dapat diubah:

```bash
python3 oled_status.py --i2c-bus 1 --address 0x3C --driver ssd1306 --greeting-seconds 5
```

## Rotary Interaction

| Input | Fungsi |
|---|---|
| Putar kanan | pilihan berikutnya |
| Putar kiri | pilihan sebelumnya |
| Tekan singkat | pilih / konfirmasi |
| Tekan lama 1.5 detik | kembali |
| Tekan singkat di halaman utama | buka menu |

Menu kembali otomatis ke halaman utama setelah timeout tidak aktif.

## Menu

Menu utama:

1. Recording
2. Services
3. Status
4. Display
5. System
6. Back

Submenu Recording:

- Start New Session
- Stop Recording
- Session Info
- Back

Submenu Services:

- Receiver Status
- Restart Receiver
- AI Worker Status
- MQTT Status
- Restart MQTT
- Back

Submenu Status:

- Acquisition
- Network
- Raspberry Pi
- Storage
- Back

Submenu Display:

- Brightness: 25%, 50%, 75%, 100%
- Screen Timeout: Off, 30 seconds, 60 seconds, 5 minutes
- Invert Display
- Back

Submenu System:

- Reboot
- Shutdown
- Back

Reboot dan Shutdown membutuhkan konfirmasi `YES` lalu long press.

## Session Runtime

Start New Session membuat:

```text
raspberry_pi/data/sessions/session_DDMMYYYY_HHMMSS/
├── raw_adc/
├── calibrated/
├── filtered/
├── model_ready/
├── predictions/
├── logs/
└── session.json
```

Apabila dua sesi dibuat pada detik yang sama, sesi kedua memakai akhiran
`_01`, lalu `_02`, tanpa menghapus atau menimpa sesi sebelumnya. `session.json`
ditulis atomik dengan status `RECORDING` saat mulai; saat Stop Recording,
status menjadi `STOPPED` dan memuat waktu selesai serta jumlah frame.

File runtime ditulis atomic ke:

```text
raspberry_pi/runtime/current_session.env
```

Isi file:

```text
SESSION_ID=...
SESSION_DIR=...
RAW_ADC_DIR=...
CALIBRATED_DIR=...
FILTERED_DIR=...
MODEL_READY_DIR=...
PREDICTION_DIR=...
LOG_DIR=...
```

Password MQTT tidak disimpan di file ini. Jika MQTT butuh kredensial, buat file
terpisah:

```bash
mkdir -p runtime
nano runtime/mqtt_credentials.env
chmod 600 runtime/mqtt_credentials.env
```

Contoh:

```text
HIVEMQ_USERNAME=ecg-undip
HIVEMQ_PASSWORD=isi_password_di_sini
```

Service sudah menetapkan host HiveMQ, port TLS, topic output, dan nama variable
kredensial. Service memvalidasi semua variabel session dan MQTT saat mulai,
sehingga error konfigurasi tampil jelas di journal. Setelah mengubah file ini
atau unit MQTT, jalankan
`systemctl --user daemon-reload` lalu restart service MQTT.

## User Services

OLED mengontrol service ini memakai `systemctl --user`:

- `ecgrhythmia-receiver.service`
- `ecgrhythmia-ai.service`
- `ecgrhythmia-mqtt.service`
- `ecgrhythmia-mqtt-controller.service`
- `ecgrhythmia-oled.service`

Install sebagai user services:

```bash
mkdir -p ~/.config/systemd/user
cp ecgrhythmia-receiver.service ~/.config/systemd/user/
cp ecgrhythmia-ai.service ~/.config/systemd/user/
cp ecgrhythmia-mqtt.service ~/.config/systemd/user/
cp ecgrhythmia-mqtt-controller.service ~/.config/systemd/user/
cp ecgrhythmia-oled.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ecgrhythmia-oled.service ecgrhythmia-mqtt-controller.service
journalctl --user -u ecgrhythmia-oled -f
```

Service receiver, AI, dan publisher sesi MQTT tidak perlu di-enable; OLED akan
start/stop sesuai menu Recording. Controller remote MQTT menyala sendiri saat
boot dan menunggu command web `START`/`STOP` pada
`ecgrhythmia/device01/command`. Jika user service manager belum aktif saat boot:

```bash
loginctl enable-linger athaya
```

Saat menu OLED memulai recording, service MQTT membaca
`runtime/current_session.env` dan langsung mengikuti prediction pada session
tersebut. Ia tidak menunggu command MQTT `START`; dashboard cukup subscribe ke
`ecgrhythmia/device01` sebelum frame baru selesai diproses.

Jangan memulai dua session bersamaan. Saat recording sedang dijalankan dari
OLED, controller remote akan mengabaikan `START`; gunakan menu OLED untuk
stop. Sebaliknya, bila session dimulai dari web, kirim `STOP` dari web sebelum
memulai session baru melalui OLED.

## Status Pages

Halaman utama:

```text
ECGRHYTHMIA
READY
Last: F0015
Press: Menu
```

Saat recording:

```text
ECGRHYTHMIA
RECORDING
00:02  F0015
Press: Menu
```

Saat recording dimulai dari command MQTT website:

```text
ECGRHYTHMIA
REMOTE MQTT
REC 00:02 F0015
Press: Menu
```

Saat controller remote sedang memulai receiver dan AI, OLED menampilkan
`REMOTE START`. Detail menu **Services -> MQTT Status** juga menampilkan state
remote dan ID sesi. Status ini dibaca dari
`runtime/mqtt_controller_state.json` yang ditulis atomik oleh controller.

Saat receiver aktif tetapi frame tidak baru:

```text
ECGRHYTHMIA
WAITING FRAME
Receiver: ON
Press: Menu
```

Error overlay:

```text
ECGRHYTHMIA
ERROR E03
NO NEW FRAME
Press: Menu
```

## Error Codes

| Code | Meaning |
|---|---|
| E01 | OLED/I2C init failed |
| E02 | ADS1293 config explicitly failed in metadata |
| E03 | Receiver running but no new frame within timeout |
| E04 | Packet loss / out-of-order increased |
| E05 | CRC errors increased |
| E06 | Disk usage above 90% |
| E07 | CPU temperature above 75 C |

## Manual Hardware Validation

1. Jalankan OLED.
2. Putar encoder kanan dan kiri.
3. Pastikan satu detent menghasilkan satu perpindahan.
4. Tekan singkat untuk masuk menu.
5. Tekan lama untuk kembali.
6. Jalankan Start New Session.
7. Periksa receiver, AI worker, dan MQTT aktif satu kali.
8. Jalankan Stop Recording.
9. Pastikan file session tetap tersimpan.
10. Reboot Raspberry Pi dan uji OLED kembali.

## Test Commands

```bash
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv_oled/bin/activate
python3 -m py_compile oled_status.py
python3 -m unittest discover -s tests -v
git diff --check
git status --short
```

# Panduan Monitoring ECGRHYTHMIA di Raspberry Pi

Panduan ini dipakai saat Raspberry Pi menjalankan pipeline:

```text
ESP32 -> receiver -> AI worker -> MQTT -> HiveMQ/dashboard
```

Jalankan perintah dari terminal Raspberry Pi atau melalui SSH. Hentikan mode
pantau realtime dengan `Ctrl+C`.

## 1. Log Pipeline Utama

Buka terminal pertama untuk memantau receiver, AI, dan MQTT sekaligus:

```bash
journalctl --user -f \
  -u ecgrhythmia-receiver.service \
  -u ecgrhythmia-ai.service \
  -u ecgrhythmia-mqtt.service
```

Hal yang perlu diperhatikan:

- Receiver: `status ok` bertambah dan `crc_errors=0`, `loss=0`,
  `out_of_order=0`.
- Receiver: muncul `exported frame ...`.
- AI: inference/prediction berhasil tanpa model error.
- MQTT: `Connected to broker` lalu `Frame ... published`.

Untuk memantau satu service saja:

```bash
journalctl --user -u ecgrhythmia-oled.service -f
journalctl --user -u ecgrhythmia-receiver.service -f
journalctl --user -u ecgrhythmia-ai.service -f
journalctl --user -u ecgrhythmia-mqtt.service -f
journalctl --user -u ecgrhythmia-mqtt-controller.service -f
```

## 2. Status Service

```bash
systemctl --user status ecgrhythmia-oled.service --no-pager -l
systemctl --user status ecgrhythmia-receiver.service --no-pager -l
systemctl --user status ecgrhythmia-ai.service --no-pager -l
systemctl --user status ecgrhythmia-mqtt.service --no-pager -l
systemctl --user status ecgrhythmia-mqtt-controller.service --no-pager -l

systemctl --user --failed
```

Untuk ringkasan yang terus diperbarui setiap dua detik:

```bash
watch -n 2 '
systemctl --user is-active ecgrhythmia-receiver.service
systemctl --user is-active ecgrhythmia-ai.service
systemctl --user is-active ecgrhythmia-mqtt.service
systemctl --user is-active ecgrhythmia-mqtt-controller.service
systemctl --user is-active ecgrhythmia-oled.service
'
```

Output `active` berarti service sedang berjalan.

## 3. Kontrol Service

Ganti `<service>` dengan `oled`, `receiver`, `ai`, `mqtt`, atau
`mqtt-controller`.

```bash
systemctl --user start ecgrhythmia-<service>.service
systemctl --user stop ecgrhythmia-<service>.service
systemctl --user restart ecgrhythmia-<service>.service
```

Contoh restart AI worker:

```bash
systemctl --user restart ecgrhythmia-ai.service
systemctl --user status ecgrhythmia-ai.service --no-pager -l
```

## 4. Start dan Stop Recording dari SSH

Gunakan controller MQTT untuk membuat atau menghentikan session dari SSH.
Controller membuat `current_session.env`, lalu menjalankan receiver dan AI
dengan urutan yang benar. Jangan menjalankan receiver, AI, dan publisher satu
per satu dengan `systemctl` untuk membuat recording baru.

Pastikan controller sudah aktif:

```bash
systemctl --user is-active ecgrhythmia-mqtt-controller.service
```

Output harus `active`. Bila `mosquitto_pub` belum ada, pasang satu kali:

```bash
sudo apt update
sudo apt install -y mosquitto-clients
```

### Start Session

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
set -a
source runtime/mqtt_credentials.env
set +a

mosquitto_pub \
  -h 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  -p 8883 \
  --cafile /etc/ssl/certs/ca-certificates.crt \
  -u "$HIVEMQ_USERNAME" \
  -P "$HIVEMQ_PASSWORD" \
  -i ecgrhythmia-device01-ssh-control \
  -t ecgrhythmia/device01/command \
  -m START \
  -q 1
```

Command ini non-retained secara default. OLED akan menampilkan `REMOTE START`,
kemudian `REMOTE MQTT` saat recording sudah aktif.

Verifikasi session dan service setelah beberapa detik:

```bash
systemctl --user status ecgrhythmia-receiver.service --no-pager
systemctl --user status ecgrhythmia-ai.service --no-pager

source runtime/current_session.env
echo "Session: $SESSION_ID"
echo "Folder:  $SESSION_DIR"
```

### Stop Session

Sesi yang dimulai dari SSH/remote harus dihentikan dengan command `STOP` yang
sama, bukan dengan menghentikan receiver secara paksa:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
set -a
source runtime/mqtt_credentials.env
set +a

mosquitto_pub \
  -h 93d81a02c1f743b6ab4ea22d7ad9c3e0.s1.eu.hivemq.cloud \
  -p 8883 \
  --cafile /etc/ssl/certs/ca-certificates.crt \
  -u "$HIVEMQ_USERNAME" \
  -P "$HIVEMQ_PASSWORD" \
  -i ecgrhythmia-device01-ssh-control \
  -t ecgrhythmia/device01/command \
  -m STOP \
  -q 1
```

Controller menghentikan AI lalu receiver, memfinalkan `session.json` menjadi
`STOPPED`, dan kembali menunggu command berikutnya. Jika sesi dimulai dari
rotary/OLED, hentikan dari menu OLED; controller remote tidak mengambil alih
sesi tersebut.

## 5. Jumlah Frame di Setiap Tahap Pipeline

Terminal kedua dapat dipakai untuk memastikan setiap tahap mengikuti tahap
sebelumnya:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi

while true; do
  clear
  source runtime/current_session.env

  echo "Session: $SESSION_ID"
  echo
  echo "Raw ADC:     $(find "$SESSION_DIR/raw_adc" -name 'frame_*.npy' | wc -l)"
  echo "Calibrated:  $(find "$SESSION_DIR/calibrated" -name 'frame_*_mv.npy' | wc -l)"
  echo "Model ready: $(find "$SESSION_DIR/model_ready" -name 'frame_*_input.npy' | wc -l)"
  echo "Prediction:  $(find "$SESSION_DIR/predictions" -name 'frame_*_prediction.json' | wc -l)"

  sleep 2
done
```

Urutan ideal jumlah file:

```text
Raw ADC -> Calibrated -> Model ready -> Prediction
```

Lihat file terbaru setiap tahap:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source runtime/current_session.env

ls -lt "$SESSION_DIR/raw_adc" | head
ls -lt "$SESSION_DIR/calibrated" | head
ls -lt "$SESSION_DIR/model_ready" | head
ls -lt "$SESSION_DIR/predictions" | head
```

## 6. Cek Serial ESP32

```bash
# Port serial yang tersedia.
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null

# Proses yang sedang menggunakan port serial.
lsof /dev/ttyACM0

# User harus memiliki grup dialout.
groups
```

Jika receiver aktif, `lsof /dev/ttyACM0` biasanya menampilkan proses Python.

## 7. Cek MQTT dan Dashboard

Pantau publisher:

```bash
journalctl --user -u ecgrhythmia-mqtt.service -f
```

Log normal yang diharapkan:

```text
MQTT publisher started
Connected to broker
Frame 000001 published, QoS 1
```

Service controller remote seharusnya selalu `active` setelah boot dan menulis:

```text
MQTT controller connected
Subscribed: ecgrhythmia/device01/command
Waiting for START command
```

Untuk meninjau command web yang diterima:

```bash
journalctl --user -u ecgrhythmia-mqtt-controller.service -f
```

Kirim `START` atau `STOP` dengan retained off ke
`ecgrhythmia/device01/command`. Saat sesi sedang dimulai dari OLED, controller
akan mengabaikan `START` agar session aktif tidak tertimpa.

Di HiveMQ Web Client, subscribe topic berikut sebelum frame baru diproses:

```text
ecgrhythmia/device01
```

## 8. Suhu, RAM, dan Disk

Terminal ketiga untuk memantau kesehatan Raspberry Pi:

```bash
watch -n 2 '
echo "Temperature (millidegree C):"
cat /sys/class/thermal/thermal_zone0/temp
echo
echo "Memory:"
free -h
echo
echo "Disk root:"
df -h /
'
```

Nilai suhu dibagi `1000`; contoh `55000` berarti `55 C`.

## 9. Cek Error SD Card atau Filesystem

Gunakan terutama bila export frame berhenti, muncul `Input/output error`, atau
filesystem menjadi read-only:

```bash
findmnt -no SOURCE,OPTIONS /
dmesg -wT | rg --line-buffered -i 'I/O error|ext4|mmc|read-only|remount'
```

Tidak boleh muncul `I/O error` atau `Read-only file system`. Jangan menjalankan
`fsck` pada root filesystem yang masih mounted.

## 10. Cek Jaringan

```bash
ip addr show
iw dev wlan0 link
ping -c 4 1.1.1.1
```

## 11. Log Lama Sejak Boot Terakhir

```bash
journalctl --user -u ecgrhythmia-receiver.service -b -n 100 --no-pager
journalctl --user -u ecgrhythmia-ai.service -b -n 100 --no-pager
journalctl --user -u ecgrhythmia-mqtt.service -b -n 100 --no-pager
journalctl --user -u ecgrhythmia-mqtt-controller.service -b -n 100 --no-pager
journalctl --user -u ecgrhythmia-oled.service -b -n 100 --no-pager
```

`-b` berarti hanya menampilkan log sejak boot Raspberry Pi terakhir.

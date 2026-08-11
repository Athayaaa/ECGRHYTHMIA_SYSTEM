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
```

## 2. Status Service

```bash
systemctl --user status ecgrhythmia-oled.service --no-pager -l
systemctl --user status ecgrhythmia-receiver.service --no-pager -l
systemctl --user status ecgrhythmia-ai.service --no-pager -l
systemctl --user status ecgrhythmia-mqtt.service --no-pager -l

systemctl --user --failed
```

Untuk ringkasan yang terus diperbarui setiap dua detik:

```bash
watch -n 2 '
systemctl --user is-active ecgrhythmia-receiver.service
systemctl --user is-active ecgrhythmia-ai.service
systemctl --user is-active ecgrhythmia-mqtt.service
systemctl --user is-active ecgrhythmia-oled.service
'
```

Output `active` berarti service sedang berjalan.

## 3. Kontrol Service

Ganti `<service>` dengan `oled`, `receiver`, `ai`, atau `mqtt`.

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

## 4. Jumlah Frame di Setiap Tahap Pipeline

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

## 5. Cek Serial ESP32

```bash
# Port serial yang tersedia.
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null

# Proses yang sedang menggunakan port serial.
lsof /dev/ttyACM0

# User harus memiliki grup dialout.
groups
```

Jika receiver aktif, `lsof /dev/ttyACM0` biasanya menampilkan proses Python.

## 6. Cek MQTT dan Dashboard

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

Di HiveMQ Web Client, subscribe topic berikut sebelum frame baru diproses:

```text
ecgrhythmia/device01
```

## 7. Suhu, RAM, dan Disk

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

## 8. Cek Error SD Card atau Filesystem

Gunakan terutama bila export frame berhenti, muncul `Input/output error`, atau
filesystem menjadi read-only:

```bash
findmnt -no SOURCE,OPTIONS /
dmesg -wT | rg --line-buffered -i 'I/O error|ext4|mmc|read-only|remount'
```

Tidak boleh muncul `I/O error` atau `Read-only file system`. Jangan menjalankan
`fsck` pada root filesystem yang masih mounted.

## 9. Cek Jaringan

```bash
ip addr show
iw dev wlan0 link
ping -c 4 1.1.1.1
```

## 10. Log Lama Sejak Boot Terakhir

```bash
journalctl --user -u ecgrhythmia-receiver.service -b -n 100 --no-pager
journalctl --user -u ecgrhythmia-ai.service -b -n 100 --no-pager
journalctl --user -u ecgrhythmia-mqtt.service -b -n 100 --no-pager
journalctl --user -u ecgrhythmia-oled.service -b -n 100 --no-pager
```

`-b` berarti hanya menampilkan log sejak boot Raspberry Pi terakhir.

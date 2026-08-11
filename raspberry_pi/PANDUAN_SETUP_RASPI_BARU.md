# Setup ECGRHYTHMIA pada MicroSD Raspberry Pi Baru

Panduan ini untuk Raspberry Pi dengan Ubuntu 22.04 baru. Perintah mengasumsikan
username `athaya` dan lokasi project `~/Projects/ECGRHYTHMIA_SYSTEM`.

## 1. Siapkan Ubuntu, SSH, dan Paket Sistem

Pastikan Raspberry Pi tersambung ke internet, lalu jalankan:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git openssh-server python3-venv python3-pip python3-dev build-essential i2c-tools python3-pyqt5
sudo systemctl enable --now ssh

for group in dialout i2c gpio; do
  if getent group "$group" >/dev/null; then
    sudo usermod -aG "$group" "$USER"
  fi
done
```

`dialout` dibutuhkan untuk ESP32 USB serial. Grup `i2c` dan `gpio` memberikan
akses OLED serta rotary encoder bila tersedia pada image Ubuntu.

## 2. Aktifkan I2C dan Reboot

Pada Ubuntu Raspberry Pi, buka konfigurasi boot:

```bash
sudo nano /boot/firmware/config.txt
```

Tambahkan baris berikut bila belum ada:

```text
dtparam=i2c_arm=on
```

Simpan, lalu reboot agar pembaruan sistem, grup user, dan I2C diterapkan:

```bash
sudo reboot
```

Setelah Raspberry Pi hidup, masuk kembali dan cek OLED:

```bash
i2cdetect -y 1
```

OLED biasanya muncul pada alamat `3c`.

## 3. Ambil Repository

Untuk repository private, buat SSH key baru dan tambahkan isi public key ke
GitHub pada **Settings -> SSH and GPG keys**:

```bash
ssh-keygen -t ed25519 -C "athayaraspi"
cat ~/.ssh/id_ed25519.pub
```

Setelah key ditambahkan ke GitHub:

```bash
mkdir -p ~/Projects
cd ~/Projects
git clone --branch aftertestingdirse git@github.com:Athayaaa/ECGRHYTHMIA_SYSTEM.git
cd ECGRHYTHMIA_SYSTEM
git status
```

Jika repository sudah diclone tetapi belum pada branch yang benar:

```bash
git switch aftertestingdirse
git pull origin aftertestingdirse
```

## 4. Konfigurasi Lokal Raspberry Pi

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
cp config.local.example.yaml config.local.yaml
nano config.local.yaml
```

Pastikan bagian ini sesuai perangkat:

```yaml
serial:
  port: /dev/ttyACM0

device:
  device_id: device01
  state: testing
```

`max_abs_mv: 1000.0` pada contoh hanya untuk pengujian transmisi. Sebelum
pengukuran ECG nyata, gunakan ambang tervalidasi sesuai prosedur proyek.

## 5. Buat Environment Receiver, MQTT, dan GUI

Ubuntu 22.04 menyediakan PyQt5 dari `apt`, sehingga environment utama perlu
mengakses package sistem tersebut:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
python3 -m venv --system-site-packages venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r ecg_qt_viewer/requirements.txt

python -c "import serial, paho.mqtt.client; from PyQt5.QtWidgets import QApplication; print('venv: OK')"
deactivate
```

Jangan menjalankan `pip install PyQt6` pada Ubuntu 22.04 Raspberry Pi.

## 6. Buat Environment AI

Script ini memasang dependency yang dipin, LiteRT, engine AI, lalu menjalankan
smoke test model. Proses dapat memerlukan beberapa menit.

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
bash setup_ai_env.sh
```

Hasil yang diharapkan di akhir:

```text
LiteRT, SciPy, dan inferensi model: OK
Environment AI siap: .../venv_ai
```

## 7. Buat Environment OLED dan Uji Hardware

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
python3 -m venv venv_oled
source venv_oled/bin/activate
python -m pip install --upgrade pip
python -m pip install luma.oled Pillow smbus2 psutil gpiozero lgpio

python3 oled_status.py --self-test
python3 oled_status.py --i2c-bus 1 --address 0x3C --driver ssd1306 --encoder-b-gpio 20
```

Gunakan `--encoder-b-gpio 20` bila OUT B rotary memakai GPIO20. Hentikan test
manual dengan `Ctrl+C` setelah OLED dan rotary berfungsi.

## 8. Simpan Kredensial MQTT

File ini lokal dan tidak masuk Git:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
mkdir -p runtime
nano runtime/mqtt_credentials.env
chmod 600 runtime/mqtt_credentials.env
```

Isi dengan kredensial HiveMQ Cloud. Jangan memakai `export` di file ini:

```text
HIVEMQ_USERNAME=ecg-undip
HIVEMQ_PASSWORD=isi_password_hivemq
```

Service sudah menyimpan host, port TLS, dan topic output
`ecgrhythmia/device01` pada unit file.

## 9. Install User Services dan Autostart OLED

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
mkdir -p ~/.config/systemd/user
cp ecgrhythmia-receiver.service ~/.config/systemd/user/
cp ecgrhythmia-ai.service ~/.config/systemd/user/
cp ecgrhythmia-mqtt.service ~/.config/systemd/user/
cp ecgrhythmia-oled.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now ecgrhythmia-oled.service
sudo loginctl enable-linger "$USER"

systemctl --user status ecgrhythmia-oled.service --no-pager
```

Hanya OLED yang di-enable saat boot. Dari menu OLED, pilih **Recording -> Start
New Session**; OLED akan membuat session dan menjalankan receiver -> AI -> MQTT.

## 10. Verifikasi Pipeline

Sambungkan ESP32 melalui USB, lalu cek port:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

Mulai session dari OLED dan pantau:

```bash
journalctl --user -f \
  -u ecgrhythmia-receiver.service \
  -u ecgrhythmia-ai.service \
  -u ecgrhythmia-mqtt.service
```

Urutan log yang diharapkan:

```text
exported frame ...
AI worker ... prediction ...
Connected to broker
Frame ... published
```

Di HiveMQ Web Client, subscribe ke:

```text
ecgrhythmia/device01
```

Untuk command monitoring lengkap, buka `PANDUAN_MONITORING_RASPI.md`.

## 11. Akses SSH dari Laptop

Pada Raspi, lihat alamat IP:

```bash
hostname -I
```

Pada laptop:

```bash
ssh athaya@ALAMAT_IP_RASPI
```

Untuk menampilkan ECG GUI di laptop Linux melalui X forwarding:

```bash
ssh -X athaya@ALAMAT_IP_RASPI
```

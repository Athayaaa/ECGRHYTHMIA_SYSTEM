# Phase 1: ESP32 Dev Module Dummy ECG

Implementasikan hanya Phase 1.

Target:
- ESP32 Dev Module menghasilkan dummy ECG.
- Dummy ECG 250 Hz.
- Channel:
  - ch1 = Lead I dummy
  - ch2 = Lead II dummy
  - ch3 = Lead III = Lead II - Lead I
- channel_order = ["Lead I", "Lead II", "Lead III"].
- ch3 adalah Lead III, bukan ECG Lead V1.
- Kirim data melalui USB Serial binary frame untuk testing saat ini.
- UART binary frame tetap disiapkan untuk target/fase final.
- Gunakan CRC16.
- Jangan implementasikan ADS1293 dulu.
- Jangan implementasikan Raspberry Pi full pipeline dulu selain parser minimal jika diperlukan.

PlatformIO env:
- esp32dev_dummy

Buat file:
- platformio.ini
- src/main.cpp
- src/config.h
- src/DummyECGGenerator.h
- src/DummyECGGenerator.cpp
- src/ECGPacket.h
- src/ECGPacket.cpp
- src/CRC16.h
- src/CRC16.cpp

UART testing later/final target:
- TX GPIO17
- RX GPIO16
- baudrate 921600

Current verified USB Serial testing:
- ESP32 Dev Module / DevKit board
- port Raspberry Pi: /dev/ttyUSB0
- baudrate 115200
- CSV output 2501 rows
- NPY output shape (2500, 3), dtype float32
- CRC errors 0, packet loss 0

USB Serial command:
- start
- stop
- status
- sim on
- sim off

Acceptance:
- Project bisa build.
- ESP32 bisa upload.
- Serial monitor menampilkan status.
- Dummy ECG dikirim 250 Hz.
- Tidak perlu sensor ADS1293.

# Project Context

Nama sistem: ECGRHYTHMIA

Sistem mengacu pada briefcase PKM-KC terbaru:
- Wearable 3-Lead ECG berbasis Edge AI dan Rule-Based Explanation.
- ESP32-S3 membaca ADS1293 via SPI.
- Raspberry Pi menerima data via USB Serial untuk testing saat ini.
- Final target komunikasi ESP32 ke Raspberry Pi tetap UART.
- Raspberry Pi membuat ring buffer 10 detik.
- Frame ECG berisi 2500 sample x 3 channel.
- Model 1D-CNN berjalan di Raspberry Pi menggunakan TFLite.
- Payload hasil akhir dikirim ke PWA via WebSocket.
- Store-first protocol: simpan .bin dan SQLite sebelum transmisi.

Keputusan hardware:
- Testing sekarang: ESP32 Dev Module biasa.
- Final: Seeed Studio XIAO ESP32-S3.
- Sensor final: ProtoCentral ADS1293.
- Saat ini sensor belum ada, jadi gunakan dummy ECG mode.

Keputusan ECG:
- Sistem menggunakan basic 3-lead Einthoven limb leads.
- Elektroda: RA, LA, LL.
- ch1 = Lead I = LA - RA.
- ch2 = Lead II = LL - RA.
- ch3 = Lead III = Lead II - Lead I.
- channel_order = ["Lead I", "Lead II", "Lead III"].
- ch3 adalah Lead III, bukan ECG Lead V1.
- Jangan gunakan ECG Lead V1.
- RLD/RL hanya referensi opsional, bukan channel data.

Target data:
- Sampling target: 250 Hz.
- Durasi frame: 10 detik.
- Sample per frame: 2500.
- Shape model: (2500, 3).
- CSV: time,ch1,ch2,ch3.
- NPY: hanya ch1,ch2,ch3, dtype float32, shape (2500,3).
- Current verified USB Serial test: /dev/ttyUSB0, 115200 baud, CRC errors 0, packet loss 0.

ADS1293:
- Gunakan library resmi ProtoCentral ADS1293.
- Untuk ESP32 Dev Module pin dokumentasi ProtoCentral:
  - MISO GPIO19
  - MOSI GPIO23
  - SCLK GPIO18
  - CS GPIO4
  - DRDY GPIO2
- 3-lead mode memakai CON1 saja.
- Channel 1 Lead I, Channel 2 Lead II, Lead III derived.
- RLD/RL dapat dipakai sebagai reference/drive electrode opsional, bukan output channel.
- ADS1293 sampling terdekat adalah SPS_256, sehingga Raspberry Pi harus siap resampling 256 ke 250 untuk model.

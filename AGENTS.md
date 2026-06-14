# ECGRHYTHMIA Project Instructions

Project ini adalah sistem wearable ECG berbasis Edge AI untuk deteksi dini aritmia.

Gunakan briefcase dan dokumentasi di folder docs/ sebagai acuan utama.

Target sistem:
- ESP32 Dev Module digunakan untuk testing awal.
- Final board adalah Seeed Studio XIAO ESP32-S3.
- Sensor final adalah ProtoCentral ADS1293 3-channel ECG breakout board.
- Saat ini sensor belum tersedia, jadi ESP32 harus mendukung dummy ECG mode.
- Testing saat ini: Raspberry Pi menerima data dari ESP32 Dev Module melalui USB Serial.
- Final target: Raspberry Pi menerima data dari ESP32 melalui UART.
- Data ECG diproses realtime berbasis frame 10 detik.
- Sampling target model: 250 Hz.
- Jumlah sample per frame: 2500.
- Input model 1D-CNN: shape (2500, 3).
- Channel order: ["Lead I", "Lead II", "Lead III"].
- Jangan gunakan ECG Lead V1.
- Lead III dihitung dari Lead II - Lead I.
- RLD/RL hanya referensi opsional, bukan channel data.
- Format CSV frame: time,ch1,ch2,ch3.
- Output model-ready: .npy float32 shape (2500, 3).

Aturan penting:
- Jangan gunakan JSON untuk komunikasi realtime ESP32 ke Raspberry Pi.
- Gunakan binary frame serial + CRC16.
- Untuk testing sekarang gunakan USB Serial; UART tetap target final.
- Jangan membuat register ADS1293 sembarangan.
- Gunakan library/dokumentasi ProtoCentral untuk ADS1293.
- Jangan membuat model 1D-CNN dari nol.
- Jangan training model.
- TFLite hanya interface/placeholder sampai model tersedia.
- Kode harus modular, bukan monolitik.
- Jangan hardcode pin di main.cpp.
- Semua pin harus dari config.h.
- Fokus implementasi bertahap per phase.

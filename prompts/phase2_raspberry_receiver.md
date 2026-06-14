# Phase 2: Raspberry Pi Receiver

Implementasikan Raspberry Pi receiver untuk membaca data binary dari ESP32.

Target:
- Baca USB Serial dari /dev/ttyUSB0 untuk testing saat ini.
- Baudrate 115200.
- Startup serial: disable DTR/RTS jika tersedia, tunggu 3 detik, flush input buffer.
- Mulai sequence tracking dari packet valid pertama; packet valid pertama tidak dihitung out_of_order.
- UART /dev/serial0 baudrate 921600 tetap target/fase final.
- Parse binary frame ESP32.
- Validasi CRC16.
- Hitung packet loss dari sequence number.
- Buat ring buffer 10 detik.
- Output frame 2500 sample x 3 channel.
- Channel order wajib ["Lead I", "Lead II", "Lead III"].
- ch1 = Lead I, ch2 = Lead II, ch3 = Lead III = Lead II - Lead I.
- Jangan gunakan ECG Lead V1; RLD/RL hanya reference opsional, bukan data channel.
- Export:
  - raw_ecg.csv
  - frame_000001.csv
  - frame_000001.npy
  - frame_000001.json

CSV frame:
time,ch1,ch2,ch3
0.000,...
0.004,...
...
9.996,...

NPY:
- shape (2500,3)
- dtype float32
- tanpa kolom time

Buat file:
- receiver.py
- packet_parser.py
- crc16.py
- ecg_buffer.py
- data_preparer.py
- window_exporter.py
- config.yaml
- requirements.txt
- validate_latest_frame.py

Jangan implementasikan TFLite, SQLite, WebSocket dulu.

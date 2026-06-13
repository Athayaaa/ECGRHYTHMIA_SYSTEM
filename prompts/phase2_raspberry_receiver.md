# Phase 2: Raspberry Pi Receiver

Implementasikan Raspberry Pi receiver untuk membaca data binary dari ESP32.

Target:
- Baca UART dari /dev/serial0.
- Baudrate 921600.
- Parse binary frame ESP32.
- Validasi CRC16.
- Hitung packet loss dari sequence number.
- Buat ring buffer 10 detik.
- Output frame 2500 sample x 3 channel.
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

Jangan implementasikan TFLite, SQLite, WebSocket dulu.
# ECGRHYTHMIA System

Phase 1 ESP32 firmware supports two ECG binary output transports.

- `esp32dev_dummy`: UART2 on GPIO17/GPIO16
- `esp32dev_dummy_usb`: USB Serial, enabled with `ECG_OUTPUT_USB=1`
- USB Serial baudrate: 115200
- UART2 baudrate: 115200
- Raspberry Pi GPIO UART port: `/dev/ttyAMA0`
- Raspberry Pi USB Serial port: `/dev/ttyUSB0`

When USB output mode is enabled, the same binary ECG packets are written to `Serial`.
Avoid continuous debug output on that stream while the Raspberry Pi parser is reading binary data.

The ECG stream remains dummy ECG at 250 Hz. Raspberry Pi frame exports stay at 2500 samples per 10-second frame with shape `(2500, 3)` and channel order `Lead I`, `Lead II`, `Lead III`.

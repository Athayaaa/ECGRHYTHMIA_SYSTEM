# Phase 4: Store-First, SQLite, TFLite Interface, WebSocket

Tambahkan:
- store_first.py
- sqlite_store.py
- tflite_interface.py
- payload_builder.py
- websocket_server.py
- state_machine.py

Jangan training model.
Jangan membuat model 1D-CNN dari nol.

TFLite interface:
- jika model belum ada, return:
  - classification_result = "UNCLASSIFIED"
  - anomaly_indices = []

Payload JSON:
- measurement_id
- device_id
- timestamp
- sha256_checksum
- data_payload.raw.time
- data_payload.raw.ch1
- data_payload.raw.ch2
- data_payload.raw.ch3
- classification_result
- anomaly_indices

State:
- NEW
- LOCAL_SAVED
- PENDING
- SENDING
- SYNCED
- FAILED
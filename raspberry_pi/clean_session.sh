#!/bin/bash

# Script untuk menghapus data sesi sebelumnya dari direktori data/
# Agar siap digunakan untuk sesi pengukuran baru yang bersih.

DATA_DIR="data"

echo "Membersihkan data sesi sebelumnya di direktori: ${DATA_DIR}"

# 1. Hapus frame mentah (raw frames)
rm -f ${DATA_DIR}/frame_*.csv
rm -f ${DATA_DIR}/frame_*.bin
rm -f ${DATA_DIR}/frame_*.npy
rm -f ${DATA_DIR}/frame_*.json

# 2. Hapus file raw CSV & database SQLite
rm -f ${DATA_DIR}/raw_ecg.csv
rm -f ${DATA_DIR}/ecg_metadata.sqlite
rm -f ${DATA_DIR}/ecg_metadata.sqlite-journal

# 3. Hapus data terkalibrasi (calibrated frames)
if [ -d "${DATA_DIR}/calibrated" ]; then
    rm -f ${DATA_DIR}/calibrated/frame_*.npy
    rm -f ${DATA_DIR}/calibrated/frame_*.json
    rm -f ${DATA_DIR}/calibrated/frame_*.csv
fi

# 4. Hapus data AI model-ready
if [ -d "${DATA_DIR}/model_ready" ]; then
    rm -f ${DATA_DIR}/model_ready/frame_*.npy
    rm -f ${DATA_DIR}/model_ready/frame_*.json
fi

# 5. Hapus prediksi AI (termasuk latest_prediction.json)
if [ -d "${DATA_DIR}/predictions" ]; then
    rm -f ${DATA_DIR}/predictions/frame_*.json
    rm -f ${DATA_DIR}/predictions/latest_prediction.json
fi

echo "Selesai! Direktori data sudah bersih dan siap untuk pengukuran baru."

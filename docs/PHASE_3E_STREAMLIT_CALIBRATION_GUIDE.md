# Phase 3E Streamlit Calibration Guide

> **LEGACY 640-TO-250 PIPELINE ONLY:** The Streamlit implementation retains
> historical signed-24-bit rail checks and writes `calibration_config.yaml`.
> It must not be used to validate or calibrate corrected protocol V2 unsigned
> offset-code frames. Follow `raspberry_pi/ADS1293_CORRECTED_PIPELINE.md` and
> `raspberry_pi/CALIBRATION_MIGRATION.md` for corrected acquisition. The new
> datasheet-nominal conversion remains preliminary and still needs ProSim.

Panduan ini dipakai untuk membuka GUI kalibrasi ADS1293, melihat frame ECG terbaru, mengatur konversi raw count ke mV, dan mengekspor data terkalibrasi.

GUI ini hanya membaca file dari `raspberry_pi/data`. GUI tidak membuka serial port dan tidak mengubah format packet, CRC, frame shape, atau format export receiver.

Formula kalibrasi tetap:

```text
lead_mV = (raw_count - offset_count) * scale_mV_per_count
```

## 1. Keselamatan

Untuk uji manusia:

- Gunakan baterai atau power bank saja.
- Jangan gunakan charger/PLN saat elektroda menempel ke tubuh.
- Pastikan RA, LA, LL, dan RLD/electrode reference terpasang dengan benar.
- Mulai dari ECG simulator/ProSim sebelum uji manusia.

## 2. Jalankan Sistem

Terminal 1, jalankan receiver:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate
python3 receiver.py --port /dev/ttyUSB0 --baudrate 115200
```

Jika memakai ESP32-C3 native USB, port bisa menjadi:

```bash
python3 receiver.py --port /dev/ttyACM0 --baudrate 115200
```

Tunggu sampai receiver mengekspor frame seperti:

```text
exported frame 000001: ... frame_000001.csv ... frame_000001.npy ...
```

Terminal 2, jalankan GUI:

```bash
cd ~/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate
streamlit run calibration_gui.py
```

Buka URL yang ditampilkan Streamlit, biasanya:

```text
http://localhost:8501
```

## 3. Pilih Preset

Di sidebar, pilih `preset` sesuai kondisi test.

- `Floating/Input Test`: untuk input belum tersambung. Ini bukan ECG valid.
- `Human Quick Test`: untuk uji manusia singkat dengan baterai/power bank.
- `ProSim 1mV 60BPM`: untuk simulator ECG 60 BPM, 1 mV.
- `Custom`: untuk mengatur semua nilai sendiri.

Rekomendasi awal:

- Untuk ProSim: pilih `ProSim 1mV 60BPM`.
- Untuk manusia: pilih `Human Quick Test`.
- Untuk debugging kabel: pilih `Floating/Input Test`.

## 4. Quick View

Mode `Quick View` dipakai untuk melihat sinyal tanpa banyak kontrol.

Yang perlu dicek:

- `Pipeline result` harus `PASS`.
- `Signal quality result` idealnya `PASS`, tetapi bisa `WARNING` jika input floating.
- `Shape` harus `(2500, 3)`.
- `Sample rate` harus `250 Hz`.
- Medical ECG grid tampil dengan Lead I, Lead II, Lead III.

Kontrol penting:

- `paper speed`: gunakan `25 mm/s` untuk standar awal.
- `gain`: gunakan `10 mm/mV` untuk standar awal.
- `display mode`: pilih `stacked_3_leads` atau `single_lead`.
- `selected lead`: dipakai saat `single_lead`.
- `y range`: default manual `-2 mV` sampai `+2 mV`.
- `Clip display to y-range`: hanya memotong tampilan plot, bukan data asli.

Jika plot terlihat penuh/gelap saat input floating, itu normal. Sambungkan elektroda atau simulator sebelum menilai morfologi ECG.

## 5. Calibration Mode

Mode `Calibration` dipakai untuk mengatur konversi raw count ke mV.

Langkah yang disarankan:

1. Pilih preset yang sesuai.
2. Pastikan frame terbaru sudah muncul.
3. Klik `Auto baseline from current frame`.
4. Aktifkan `Remove mean / baseline correction` jika baseline masih bergeser.
5. Cek plot `Raw plot` dan `Converted mV plot`.
6. Atur `scale_mV_per_count` sampai amplitudo mV masuk akal.
7. Pakai `Invert Lead` jika polaritas lead terbalik.
8. Klik `Save calibration config`.
9. Klik `Export latest converted frame` jika hasil sudah siap.

Arti kontrol:

- `offset_count`: nilai baseline raw count per lead.
- `scale_mV_per_count`: faktor konversi semua lead dari count ke mV.
- `Invert Lead`: membalik polaritas lead tertentu.
- `Remove mean / baseline correction`: mengurangi rata-rata sinyal per frame.
- `Auto baseline from current frame`: mengisi offset dari rata-rata frame terbaru.

Untuk ProSim 1 mV 60 BPM:

- Sambungkan RA, LA, LL, dan RLD sesuai simulator.
- Pilih preset `ProSim 1mV 60BPM`.
- Jalankan receiver sampai beberapa frame stabil.
- Gunakan `Auto baseline from current frame`.
- Atur `scale_mV_per_count` sampai amplitudo lead referensi mendekati setting simulator.
- Simpan konfigurasi.

## 6. Advanced Debug

Mode `Advanced Debug` dipakai saat sinyal belum benar.

Cek bagian ini:

- `ch3 consistency`: harus `PASS`.
- `ch3 max error`: harus sangat kecil.
- `NaN/Inf count`: harus `0`.
- `saturation count`: idealnya `0`.
- `sudden jump count`: idealnya `0` atau kecil.
- `max absolute jump`: makin kecil makin stabil.

Lead mapping:

```text
Lead I   = ch1 = LA - RA
Lead II  = ch2 = LL - RA
Lead III = ch3 = Lead II - Lead I
```

Jika `ch3 consistency` gagal, jangan lanjut kalibrasi. Cek firmware, receiver, dan file frame terbaru.

## 7. Export

Export yang tersedia:

- `Export medical ECG PNG`: menyimpan gambar medical ECG ke `raspberry_pi/data/calibrated/`.
- `Save calibration config`: menyimpan `raspberry_pi/calibration_config.yaml`.
- `Export latest converted frame`: menyimpan:
  - `raspberry_pi/data/calibrated/frame_xxxxxx_mv.csv`
  - `raspberry_pi/data/calibrated/frame_xxxxxx_mv.npy`

Clipping display tidak memengaruhi data export. Export CSV/NPY tetap memakai nilai converted mV asli.

## 8. Interpretasi Result

Pipeline:

- `PASS`: frame lengkap, shape benar, dtype benar, tidak ada NaN/Inf, ch3 konsisten.
- `FAIL`: ada masalah format, data, atau konsistensi.
- `NO DATA`: belum ada frame lengkap di `raspberry_pi/data`.

Signal quality:

- `PASS`: tidak ada warning besar.
- `WARNING`: ada saturasi, peak-to-peak terlalu besar, atau sudden jump tinggi.
- `FAIL`: pipeline gagal sehingga kualitas sinyal belum bisa dinilai.
- `NO DATA`: belum ada frame.

## 9. Troubleshooting

No ECG frame found:

- Pastikan Terminal 1 menjalankan `receiver.py`.
- Pastikan port benar: `/dev/ttyUSB0` atau `/dev/ttyACM0`.
- Tunggu sampai frame CSV/BIN/NPY/JSON diekspor.

Plot terlalu tebal/gelap:

- Biasanya input floating atau elektroda belum benar.
- Aktifkan `Clip display to y-range`.
- Gunakan y range manual `-2` sampai `+2 mV`.

Input saturated/floating:

- Cek RA, LA, LL, dan RLD.
- Cek power dan ground ADS1293.
- Gunakan ECG simulator dulu.

Signal unstable/sudden jumps tinggi:

- Cek kabel elektroda.
- Cek kontak elektroda ke kulit/simulator.
- Hindari charger/PLN untuk human test.
- Coba power bank.

Lead terlihat terbalik:

- Gunakan checkbox `Invert Lead`.
- Cek ulang wiring RA, LA, LL.

Frame tidak berubah:

- Pastikan receiver masih berjalan.
- Aktifkan auto-refresh di GUI.
- Cek folder `raspberry_pi/data`.

## 10. Urutan Kalibrasi Singkat

1. Jalankan receiver.
2. Jalankan Streamlit GUI.
3. Pilih preset `ProSim 1mV 60BPM`.
4. Pastikan `Pipeline result = PASS`.
5. Klik `Auto baseline from current frame`.
6. Atur `scale_mV_per_count`.
7. Cek medical ECG grid.
8. Simpan `calibration_config.yaml`.
9. Export converted CSV/NPY.
10. Baru lanjut ke uji manusia dengan baterai/power bank.

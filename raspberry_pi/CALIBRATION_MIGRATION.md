# ADS1293 Calibration Migration

## Keputusan migrasi

Kalibrasi ProSim yang direkam melalui pipeline lama tidak valid untuk firmware
ADS1293 corrected. Jangan menyalin scale atau offset lama ke konfigurasi V2 dan
jangan memilih konfigurasi legacy secara otomatis.

Alasannya bukan sekadar perubahan nama protocol. Seluruh acquisition fingerprint
berubah:

| Parameter | Pipeline legacy | Pipeline corrected |
|---|---:|---:|
| Protocol | V1 | V2 |
| Native ODR | 640 SPS | `800/3 SPS` |
| Decimation | R1=4, R2=5, R3=8 | R1=4, R2=6, R3=16 |
| ADCMAX | `0xC35000` (12800000) | `0xF30000` (15925248) |
| Zero code | 6400000 | 7962624 |
| Downsampling | polling latest sample 250 Hz | stateful FIR 15/16 |
| Loss behavior | update dilewati tanpa resampler | sequence loss eksplisit/reset |
| ADC transport | signed fields V1 | unsigned offset-code V2 |
| Kalibrasi saat ini | ProSim legacy | datasheet nominal preliminary |

Polling 250 Hz pada sumber native 640 SPS membuang sekitar 61% update dan bukan
rational anti-alias resampling. Kalibrasi empiris lama dapat ikut menyerap bias
sampling-phase tersebut. Hasil seperti `60BPM_1mV`, `120BPM_1mV`, atau rekaman
subjek yang dibuat oleh pipeline lama tetap berguna sebagai arsip pipeline lama,
tetapi tidak membuktikan scale corrected.

## File konfigurasi

| File | Status | Penggunaan |
|---|---|---|
| `calibration_config.yaml` | legacy preserved | arsip konfigurasi ProSim lama |
| `calibration_config_v1.yaml` | legacy preserved | compatibility/arsip V1 |
| `calibration_config_legacy_640_to_250.yaml` | legacy explicit-only | replay/conversion data lama |
| `calibration_config_ads1293_266p667_nominal.yaml` | corrected preliminary | default sementara untuk V2 |
| `calibration_config_preliminary.yaml` | historical | jangan diasumsikan cocok tanpa fingerprint |

File legacy tidak dihapus atau ditimpa. Loader corrected menolak legacy secara
default. Untuk dataset legacy, caller harus menyebut
`calibration_config_legacy_640_to_250.yaml` secara eksplisit. Ini adalah
pemisahan provenance, bukan endorsement bahwa hasil lama dapat digabungkan
dengan hasil V2.

## Konversi nominal corrected

ADS1293 ECG output adalah unsigned offset binary. Persamaan forward datasheet:

```text
ADCOUT = [3.5 * (VINP - VINM) / (2 * VREF) + 1/2] * ADCMAX
```

Inverse conversion:

```text
VIN_mV = (ADCOUT - zero_code) * scale_mV_per_count
scale_mV_per_count = (2 * VREF * 1000) / (3.5 * ADCMAX)
```

Untuk corrected configuration:

```text
ADCMAX    = 15925248
zero_code = 7962624
VREF      = 2.4 V nominal
gain      = 3.5
scale     = 0.0000861166225749559 mV/count
```

`calibration_config_ads1293_266p667_nominal.yaml` mengikat nilai tersebut ke
fingerprint berikut: protocol V2, AFE 102400 Hz, R1/R2/R3 `4/6/16`, native rate
`800/3`, output 250 Hz, resampling `15/16`, encoding offset binary, ADCMAX
15925248, zero code, VREF nominal, dan analog gain. Missing field atau satu
mismatch saja membuat kalibrasi incompatible dalam strict mode.

Konversi ini adalah **preliminary datasheet conversion**. Toleransi VREF,
analog front-end, elektroda, dan signal chain nyata belum diukur oleh konstanta
nominal. External multipoint ProSim recalibration pada pipeline corrected tetap
wajib sebelum scale disebut final.

## Aturan konversi dan saturation

Lead I/II harus memenuhi:

```text
0 <= offset_code <= ADCMAX
```

Jangan gunakan `abs(code)` dan jangan sign-extend uint24. Setelah resampling,
konversi dilakukan sebagai:

```text
Lead_I_mV  = (Lead_I_code  - Lead_I_offset)  * scale
Lead_II_mV = (Lead_II_code - Lead_II_offset) * scale
Lead_III_mV = Lead_II_mV - Lead_I_mV
```

Lead III tidak mempunyai rail/ADCMAX offset-code sendiri karena ia adalah
selisih signed dari dua channel terukur. Near-rail dan saturation diperiksa
pada Lead I/II sebelum memperlakukan frame sebagai data kalibrasi yang valid.

Baseline wander atau DC offset fisiologis bukan alasan untuk mengubah ADC scale.
Baseline correction, high-pass, detrending, dan mean removal adalah preprocessing
terpisah. Jika diterapkan, langkah tersebut harus dicatat di metadata model-ready
dan tidak boleh dicampur ke `zero_code`, `offset_count`, atau
`scale_mV_per_count` tanpa prosedur kalibrasi yang didefinisikan.

## Mengonversi dataset

Corrected frame dengan metadata fingerprint lengkap:

```bash
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate

python3 convert_dataset_to_mv.py \
  --input-dir data/sessions/20260718_bench01 \
  --session-metadata data/sessions/20260718_bench01/session_<timestamp>.json \
  --config calibration_config_ads1293_266p667_nominal.yaml \
  --output-dir data/converted/20260718_bench01
```

Output directory wajib berada di luar input directory agar raw acquisition tidak
berubah dan harus berupa path baru. Untuk corrected pipeline,
`--session-metadata` wajib diberikan. Session harus memuat readback register
lengkap dan berstatus PASS; `session_id`, firmware commit, protocol, serta
seluruh fingerprint frame harus sama persis. Session dari rekaman lain tidak
boleh dipakai untuk meloloskan compatibility frame. Hash BIN/NPY raw juga
diverifikasi sebelum output ditulis.

Dataset legacy hanya dengan pilihan eksplisit:

```bash
python3 convert_dataset_to_mv.py \
  --input-dir ../data_kalibrasi/60BPM_1mV \
  --config calibration_config_legacy_640_to_250.yaml \
  --output-dir ../data_kalibrasi_mv/60BPM_1mV_legacy
```

Nama suffix `_legacy` direkomendasikan agar hasil tidak tertukar dengan output
corrected. Jangan memberi session metadata V2 kepada data V1 untuk memaksa
compatibility.

## Prosedur ProSim corrected di masa depan

Lakukan hanya setelah hardware dan streaming V2 lolos verifikasi live:

1. Flash firmware corrected dan simpan firmware commit/build environment.
2. Jalankan `ads config dump`; pastikan semua write/readback match.
3. Jalankan internal self-test sebagai pemeriksaan signal path, bukan kalibrasi.
4. Rekam setiap titik ProSim ke output directory baru dengan receiver V2; jangan
   mencampur frame legacy dan corrected.
5. Pastikan session/frame metadata PASS, tidak ada packet loss/reset, kode tidak
   near rail, dan channel mapping benar.
6. Gunakan beberapa amplitude dan metode R-peak baseline/multipoint yang sesuai.
7. Tulis hasil ke file baru; jangan overwrite konfigurasi legacy.
8. Validasi amplitude, BPM, repeatability, dan Lead III pada dataset terpisah.
9. Baru setelah acceptance live, ubah status dari preliminary menjadi calibrated.

Contoh bentuk perintah multipoint; ganti directory dan nilai sesuai pengukuran
yang benar-benar dilakukan. Implementasi strict saat ini mengharuskan seluruh
point merupakan subset dari satu sesi akuisisi kontinu yang sama; semua frame
harus membawa `session_id` dan firmware fingerprint yang cocok dengan session
JSON berikut:

```bash
python3 auto_calibrate_prosim.py \
  --point data/prosim_v2/0p5mV:0.5 \
  --point data/prosim_v2/1mV:1.0 \
  --point data/prosim_v2/2mV:2.0 \
  --session-metadata data/prosim_v2/session_<timestamp>.json \
  --expected-bpm 60 \
  --method multipoint_rpeak \
  --pipeline-profile ads1293_native_266p667_resampled_250_v2 \
  --output calibration_config_ads1293_266p667_prosim.yaml
```

Jangan menjalankan contoh ini pada gabungan dataset lama. Hasil internal ADS1293
self-test juga tidak boleh digunakan untuk menentukan scale eksternal karena
test source dan ADC reference berbagi VREF.

## Acceptance yang masih pending

Sebelum corrected calibration dinyatakan final, masih diperlukan:

- verifikasi register dan continuous DRDY pada ADS1293 nyata;
- konfirmasi tidak ada serial overrun pada sesi panjang;
- internal zero/positive/negative self-test live;
- external ProSim multipoint calibration dan hold-out validation;
- inspeksi waveform serta amplitude pada sinyal manusia;
- dokumentasi board, electrode placement, dan kondisi pengukuran.

Synthetic regression memverifikasi matematika dan implementasi software, tetapi
tidak memenuhi daftar acceptance live di atas.

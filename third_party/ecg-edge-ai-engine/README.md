# ECG Edge AI Engine

ECG Edge AI Engine adalah library inferensi **black-box** berbasis Python yang dirancang untuk dijalankan pada perangkat edge (seperti Raspberry Pi). Library ini dirancang khusus untuk memproses sinyal elektrodiagram (ECG) mentah (3-lead, 250 Hz), menjalankan penyaringan dan pembersihan sinyal (preprocessing), memanggil model kecerdasan buatan berbasis TensorFlow Lite (TFLite), dan mengembalikan hasil prediksi yang siap dibaca oleh aplikasi.

Tujuan utama dari engine ini adalah menyembunyikan seluruh detail internal dsp (Digital Signal Processing) dan TFLite interpreter di balik satu interface sederhana:
`engine.predict(raw_signal)`

---

## Fitur Utama

- **Black-box API**: Cukup panggil `predict()` dengan numpy array mentah.
- **DSP Preprocessing Modular**: Pipeline penyaringan berurutan (Sanitize -> Validate -> Wavelet Denoising -> Median Filter Baseline Wander -> Butterworth Bandpass -> Z-score Normalization -> Crop/Pad Length).
- **Thread-safe Model Caching**: Singleton loader memuat model TFLite sekali saja ke dalam memori untuk menghindari overhead IO berulang.
- **Dynamic Input Resizing**: Otomatis menyesuaikan panjang sinyal input dengan panjang input model menggunakan center cropping (jika terlalu panjang) atau zero padding (jika terlalu pendek).
- **Format Output Terstruktur**: Mengembalikan data hasil klasifikasi berupa dictionary Python lengkap beserta probabilitas masing-masing kelas.

---

## Struktur Proyek

```
ecg-edge-ai-engine/
├── src/                      # Source code modul engine
│   ├── __init__.py           # Package initialization & exports
│   ├── engine.py             # Interface utama (ECGEngine)
│   ├── config.py             # Konfigurasi parameter DSP dan Logging
│   ├── preprocessing.py      # Implementasi pembersihan/DSP sinyal
│   ├── inference.py          # Wrapper TFLite interpreter execution
│   ├── postprocessing.py     # Pemetaan probabilitas ke prediksi label
│   ├── model_loader.py       # Caching singleton loader model TFLite
│   ├── utils.py              # Custom exceptions & logger builder
│   └── version.py            # Informasi versi package
│
├── models/                   # Tempat penyimpanan model dan metadata
│   ├── model.tflite          # File biner TensorFlow Lite
│   └── metadata.json         # Konfigurasi meta-parameter model & kelas
│
├── examples/                 # Contoh kode penggunaan engine
│   ├── example_predict.py    # Contoh penggunaan array numpy sintetis
│   └── example_csv.py        # Contoh memuat data dari file CSV
│
├── tests/                    # Unit tests seluruh komponen
│
├── requirements.txt          # Daftar dependencies eksternal
├── setup.py                  # Script instalasi modul (pip install .)
└── README.md                 # Dokumentasi proyek
```

---

## Persyaratan (Requirements)

- **Python**: Versi minimum `3.10`
- **Platform**: Ubuntu laptop `x86_64` atau Raspberry Pi 4 dengan OS Ubuntu 64-bit (`aarch64`)
- **Dependencies utama**:
  - `numpy==1.26.4`
  - `scipy==1.15.3`
  - `pandas` (>= 2.3.3)
  - `PyWavelets==1.8.0`
  - `matplotlib==3.10.9`
  - `ai-edge-litert==2.1.6`

`tflite-runtime` 2.14.0 terlalu lama untuk model saat ini dan gagal memuat operator
`FULLY_CONNECTED` builtin opcode version 12. Gunakan `ai-edge-litert==2.1.6`
sebagai runtime utama.

```bash
python -m pip install ai-edge-litert==2.1.6
```

Sebelum instalasi, periksa versi Python, arsitektur, dan glibc:

```bash
python --version
uname -m
ldd --version | head -n 1
```

Output yang diharapkan:

- Laptop Ubuntu: Python `3.10.x`, arsitektur `x86_64`
- Raspberry Pi 4: Python `3.10.x`, OS 64-bit dengan arsitektur `aarch64`

---

## Instalasi

### 1. Instalasi Lokal dari Source
Pastikan virtual environment telah diaktifkan, kemudian jalankan perintah berikut di root folder project:

```bash
pip install .
```

Jika Anda sedang dalam mode pengembangan (development), gunakan mode editable agar setiap perubahan file di repository langsung aktif:

```bash
pip install -e .
```

---

## Contoh Penggunaan Sederhana

```python
import numpy as np
from ecg_edge_ai_engine import ECGEngine

# 1. Inisialisasi engine (model akan dimuat sekali saja secara otomatis)
engine = ECGEngine()

# 2. Siapkan data ECG 3-lead mentah dengan sampling rate 250 Hz
# Bentuk data harus berupa numpy array dengan dimensi (N, 3)
raw_signal = np.random.normal(size=(2500, 3))

# 3. Jalankan prediksi
result = engine.predict(raw_signal, sampling_rate=250.0)

# 4. Tampilkan hasil
print("Prediksi:", result["prediction"])
print("Confidence:", result["confidence"], "%")
print("Probabilitas tiap kelas:", result["probabilities"])
```

---

## Format Input & Output

### Format Input Sinyal
Engine menerima input berupa:
- `raw_signal`: `numpy.ndarray` 2D dengan shape `(N, 3)` di mana `N` adalah jumlah sampel data, dan `3` melambangkan 3 lead ECG (Lead I, Lead II, Lead III).
- `sampling_rate` (opsional): default `250.0` Hz. Jika hardware atau data Anda memiliki sampling rate yang berbeda, engine akan melakukan resampling secara internal sebelum memproses.

### Format Output Prediction
Output yang dihasilkan selalu bertipe `dict` dengan format terstruktur sebagai berikut:

```python
{
    "prediction": "AF",  # Label kelas terpilih (atau "Unclassified" jika di bawah threshold)
    "confidence": 98.1,  # Nilai kepercayaan dalam persentase (%)
    "probabilities": {   # Detail persentase probabilitas untuk tiap kelas
        "Normal": 1.25,
        "AF": 98.1,
        "Takikardia": 0.45,
        "Bradikardia": 0.2
    },
    "metadata": {        # Informasi metadata model yang aktif
        "model_name": "Pure CNN Multi Class 500Hz to 250Hz",
        "version": "1.0.0",
        "preprocessing_version": "v5.0_research_grade",
        "input_length": 2500,
        "sampling_rate": 250.0
    }
}
```

---

## Menjalankan Contoh (Examples)

Modul contoh berada dalam folder `examples/`. Anda dapat menjalankannya dengan mengaktifkan venv terlebih dahulu:

### 1. Inferensi dari Numpy Array Sintetis
```bash
python examples/example_predict.py
```

### 2. Inferensi dari File CSV (`raw_ecg.csv`)
Jika file `raw_ecg.csv` belum ada, script akan otomatis membuat data simulasi untuk pengetesan awal:
```bash
python examples/example_csv.py
```

---

## Mengganti Model AI (TFLite)

Untuk menggunakan model TFLite baru:
1. Ganti file `models/model.tflite` dengan model Anda yang baru.
2. Perbarui konfigurasi di `models/metadata.json` agar sesuai dengan parameter model yang baru:
   ```json
   {
       "model_name": "Nama Model Baru Anda",
       "version": "2.0.0",
       "input_length": 2500,
       "sampling_rate": 250.0,
       "labels": [
           "Normal",
           "AF",
           "Takikardia",
           "Bradikardia"
       ],
       "threshold": 0.5,
       "preprocessing_version": "v5.0_research_grade"
   }
   ```
   *Catatan: Engine membaca konfigurasi di atas secara dinamis (seperti label kelas, panjang input `input_length`, dan `threshold` batas penentuan kelas).*

---

## Mengubah Konfigurasi Preprocessing

Seluruh konstanta parameter preprocessing terpusat pada file `src/config.py`. Anda dapat mengubah konfigurasi ini tanpa menyentuh core logic program:

- `TARGET_FS`: Sampling rate target yang diinginkan oleh model (Default: `250.0` Hz)
- `MODEL_INPUT_LENGTH`: Panjang window sinyal masukan model (Default: `2500` sampel)
- `DEFAULT_WAVELET`: Jenis wavelet dasar untuk denoising (Default: `"db4"`)
- `DEFAULT_WAVELET_LEVEL`: Level wavelet decomposition (Default: `4`)
- `DEFAULT_MEDIAN_KERNEL`: Lebar kernel filter median baseline correction (Default: `51`)
- `DEFAULT_LOWCUT` / `DEFAULT_HIGHCUT`: Batas frekuensi bandpass filter Butterworth (Default: `0.5` Hz - `45.0` Hz)
- `DEFAULT_CLIP_MIN` / `DEFAULT_CLIP_MAX`: Batas pembatasan (clipping) nilai z-score normalisasi (Default: `-5.0` s/d `5.0`)
- `LOGGING_ENABLED`: Mengaktifkan/menonaktifkan logging runtime program secara global (Default: `True`)

---

## Menjalankan Unit Test

Untuk memverifikasi bahwa perubahan kode tidak mengganggu fungsionalitas engine, jalankan test suite menggunakan perintah:

```bash
python -m unittest discover -s tests
```

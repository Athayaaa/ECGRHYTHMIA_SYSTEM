# Corrected ADS1293 Acquisition Pipeline

## Status dan batas validasi

Dokumen ini mendefinisikan pipeline ADS1293 protocol V2 yang digunakan oleh
Raspberry Pi. Implementasi telah menyediakan pemeriksaan offline dan kontrak
metadata, tetapi **validasi dengan perangkat ADS1293 nyata, ProSim, dan subjek
manusia masih pending**. Hasil synthetic regression atau internal self-test
tidak boleh dilaporkan sebagai validasi live hardware.

Pipeline lama dan pipeline baru tidak ekuivalen:

```text
Lama
ADS1293 640 SPS -> polling software 250 Hz -> update terlewati
-> protocol V1 -> kalibrasi ProSim legacy

Corrected
ADS1293 800/3 SPS -> satu latest-register read per atomic DRDY claim
-> protocol V2 uint24 offset-code -> polyphase FIR stateful 15/16
-> tepat 250 Hz -> Lead III diturunkan -> frame (2500, 3)
-> konversi datasheet preliminary -> kalibrasi ProSim baru pada masa depan
```

Kalibrasi ProSim legacy tidak valid untuk pipeline corrected. Lihat
`CALIBRATION_MIGRATION.md` sebelum mengonversi dataset lama atau membuat
kalibrasi baru.

## Konfigurasi akuisisi ADS1293

Firmware menulis lalu membaca balik register yang membentuk konfigurasi berikut:

| Parameter | Nilai | Register efektif |
|---|---:|---|
| AFE resolution clock | 102400 Hz | `AFE_RES=0x00` |
| R1 | 4 | `R1_RATE=0x00` |
| R2 | 6 | `R2_RATE=0x04` |
| R3 CH1 | 16 | `R3_RATE_CH1=0x10` |
| R3 CH2 | 16 | `R3_RATE_CH2=0x10` |
| native ODR | `800/3 Hz` | `102400 / (4*6*16)` |
| ADC encoding | unsigned offset binary | ECG CH1/CH2 uint24 |
| ADCMAX | `0xF30000 = 15925248` | TI Table 8 |
| zero code | `0x798000 = 7962624` | `ADCMAX/2` |

Selain register rate dan routing utama, firmware secara eksplisit menulis dan
membaca balik `FLEX_VBAT_CN=0x00`, `LOD_CN=0x08`, `LOD_EN=0x00`,
`LOD_CURRENT=0x00`, `LOD_AC_CN=0x00`, `REF_CN=0x00`,
`SYNCB_CN=0x40`, dan `MASK_DRDYB=0x00`. `LOD_CN=0x08` mematikan blok
lead-off; itu bukan nilai threshold comparator. `REVID` harus `0x01`.
Ini mencegah state battery/lead-off/reference lama bertahan ketika hanya MCU
yang reset tanpa power-cycle ADS1293.

Startup oscillator juga deterministik: firmware menghentikan `STRTCLK`, memilih
internal crystal, menunggu 20 ms, lalu mengaktifkan clock. Setiap nilai target
wajib cocok saat readback sebelum akuisisi dapat dimulai.

Lead I adalah CH1 (`LA - RA`) dan Lead II adalah CH2 (`LL - RA`). ADS1293 tidak
mengirim Lead III dalam protocol V2. Raspberry Pi menghitungnya setelah kedua
lead terukur melewati resampler yang sama:

```text
Lead III = Lead II - Lead I
```

`DRDYB` ditangani oleh ISR falling-edge yang hanya memperbarui counter; SPI
tidak dijalankan di ISR. Register ECG ADS1293 menyimpan konversi terbaru, bukan
FIFO. Karena itu main loop mengklaim seluruh backlog secara atomik dan hanya
membaca enam byte CH1/CH2 terbaru. Tanpa backlog hasilnya tetap satu read per
DRDY. Dengan backlog, sequence packet ditempatkan pada nomor event terbaru dan
seluruh konversi yang tertimpa menjadi sequence gap yang tepat. Read gagal,
overrun, error ADS, kegagalan kirim sebelumnya, dan kode di luar rentang tetap
eksplisit melalui flags/diagnostics serta sequence gap.
Jika DRDY baru tiba di antara atomic claim dan selesainya SPI read, hasil read
dibuang karena identitas event-nya ambigu; edge baru tetap pending, counter
`ambiguous_reads_discarded`/overrun bertambah, dan packet berikutnya membawa
gap sequence yang sesuai. Data register non-FIFO tidak pernah diklaim seolah
backlog dapat dipulihkan dengan read berulang.

## Handshake sebelum streaming

Receiver memisahkan text-command mode dari binary mode. Urutan ADS1293 adalah:

1. Buka serial, tunggu boot, lalu hentikan stream dan debug text.
2. Kirim `ads config dump` ketika binary streaming berhenti.
3. Ambil tepat satu object JSON konfigurasi.
4. Cocokkan protocol, rate fraction, ADCMAX, zero code, VREF/gain nominal,
   encoding, CRC, packet size, dan setiap pasangan register
   `written`/`readback`.
5. Kirim `ads start`, kemudian `ads stream start`, dan tunggu sedikitnya satu
   packet V2 lengkap yang CRC dan semantik startup-nya valid.
6. Baru tulis `session_<timestamp>.json` yang memuat firmware fingerprint,
   register, serial utilization, resampler, dan kompatibilitas kalibrasi.

Kontrak yang diterima secara default adalah:

```text
protocol_version=2
afe_res_hz=102400
r1=4, r2=6, r3_ch1=16, r3_ch2=16
native_sample_rate_numerator=800
native_sample_rate_denominator=3
native_sample_rate_hz=266.6666666667
adc_encoding=offset_binary_uint24
adcmax=15925248
zero_code=7962624
packet_size=32
crc_type=CRC16-CCITT-FALSE
config_verified=true
```

Mismatch membuat startup fail-closed. Opsi `--allow-unverified-firmware` hanya
boleh melewati bukti provenance/readback yang hilang atau tidak tepercaya untuk
diagnosis raw. Mismatch yang sudah diketahui pada protocol, packet, rate,
encoding, ADCMAX/zero, VREF/gain, CRC, self-test, device, atau nilai register
tetap fatal dan tidak dapat dioverride. Session diagnosis tetap berstatus FAIL;
calibrated dan model-ready export dinonaktifkan.

## Protocol V2

Semua integer multibyte little-endian. CRC16-CCITT-FALSE dihitung pada byte
0--29. Ukuran packet tetap 32 byte.

| Offset | Ukuran | Tipe | Isi |
|---:|---:|---|---|
| 0 | 2 | bytes | sync `AA 55` |
| 2 | 1 | `uint8` | version `2` |
| 3 | 2 | `uint16` | payload length `25` |
| 5 | 4 | `uint32` | native DRDY sequence |
| 9 | 4 | `uint32` | timestamp read SPI, mikrodetik |
| 13 | 2 | `uint16` | sample count `1` |
| 15 | 2 | `uint16` | native-rate numerator `800` |
| 17 | 1 | `uint8` | flags |
| 18 | 4 | `uint32` | Lead I uint24 offset-code |
| 22 | 4 | `uint32` | Lead II uint24 offset-code |
| 26 | 4 | `uint32` | diagnostics |
| 30 | 2 | `uint16` | CRC16 |

Denominator rate (`3`) berasal dari config handshake. Field Lead I/II disimpan
dalam `uint32`, tetapi hanya rentang ADS1293 `0..ADCMAX` yang valid. Jangan
sign-extend bit 23 dan jangan menafsirkan kode tersebut sebagai two's complement.

Flags V2:

| Bit | Arti |
|---:|---|
| 0 | register configuration verified |
| 1 | ADS error status nonzero |
| 2 | DRDY backlog/overrun |
| 3 | kode CH1/CH2 di luar `0..ADCMAX` |
| 4 | pengiriman packet sebelumnya gagal |
| 5 | internal self-test aktif |

Empat byte diagnostics, dari least significant, adalah cached `ERR_STATUS`,
pending DRDY sesudah claim, maksimum pending DRDY, dan counter overrun. Tiga
counter satu-byte tersebut dijepit pada `255`, tidak pernah wrap; pending
sesudah atomic collapse biasanya nol. Sequence gap tetap menjadi sumber utama
jumlah loss yang tepat. Protocol V1 tetap tersedia untuk dummy build lama dan
tidak diperlakukan sebagai ADS1293 V2.

Pada 32 byte, `800/3 SPS`, dan framing serial 8N1, utilisasi nominal 115200 baud
sekitar 74,1%. Karena marginnya terbatas, status overrun dan sequence continuity
harus selalu diperiksa.

## Resampling kontinu 15/16

Rasio yang benar adalah:

```text
250 / (800/3) = 15/16
```

`streaming_resampler.py` menggunakan FIR polyphase anti-alias yang stateful.
State filter dan phase dipertahankan melintasi batas serial read dan batas frame;
data tidak di-resample per frame secara independen. Filter memiliki delay yang
dideklarasikan sebesar 10 output sample atau 40 ms. Chunking acak dan satu chunk
menghasilkan output ekuivalen setelah delay tersebut diperhitungkan.

Sebelum FIR, `zero_code=0x798000` dikurangkan dari Lead I/II dan ditambahkan
kembali sesudah FIR. Ini hanya menghilangkan bias representasi offset-binary,
bukan melakukan baseline correction fisiologis. Langkah tersebut mencegah bias
sekitar delapan juta count menimbulkan transient awal besar atau ripple DC
bergantung fase; referensi offline yang setara adalah
`resample_poly(x-zero_code, 15, 16)+zero_code`.

Jika sequence meloncat atau out-of-order:

- sample yang hilang tidak diinterpolasi;
- state resampler di-reset;
- partial output frame yang terdampak dibuang;
- event dicatat ke `acquisition_events.jsonl`;
- frame terdampak tidak boleh diberi status PASS.

Packet dengan ADS error, backlog/overrun, code-out-of-range, previous-TX
failure, self-test aktif, atau measured code near rail dikarantina **sebelum**
masuk FIR. Partial frame dibuang, resampler di-reset, dan alasan/status ditulis
ke `acquisition_events.jsonl`. Ini juga berlaku tepat di batas frame: delay FIR
tidak boleh membuat kontribusi packet abnormal bocor ke frame PASS berikutnya.

Setelah resampling, Lead III dihitung dan accumulator hanya mengekspor frame
lengkap `(2500, 3)` `float32`, 250 Hz, berdurasi 10 detik. Untuk synthetic input
kontinu 30 detik, 8000 native sample menghasilkan 7500 output sample atau tepat
tiga frame setelah stream difinalisasi.

## Konversi kode ADC menjadi mV

Persamaan inverse ADS1293 untuk lead terukur adalah:

```text
VIN_mV = (ADCOUT - ADCMAX/2) * (2 * VREF * 1000)
         / (3.5 * ADCMAX)
```

Dengan `VREF=2.4 V` dan konfigurasi corrected:

```text
nominal scale = 0.0000861166225749559 mV/count
```

Nilai ini adalah konversi input-referred berdasarkan parameter datasheet, bukan
hasil kalibrasi gain eksternal. Konfigurasi
`calibration_config_ads1293_266p667_nominal.yaml` berstatus `preliminary` dan
`prosim_recalibration_required: true`.

Lead I dan Lead II dikonversi terhadap offset masing-masing. Lead III dalam mV
selalu dihitung ulang sebagai Lead II mV dikurangi Lead I mV. Range/saturation
untuk channel terukur dinilai dalam domain kode offset, bukan dengan nilai
absolut signed. Baseline correction, high-pass, detrending, atau mean removal
adalah preprocessing sinyal yang terpisah dan tidak boleh dimasukkan ke ADC
scale atau zero code.

## File dan directory default

Dengan `config.yaml`, output utama berada di `raspberry_pi/data/`:

| Path | Semantik |
|---|---|
| `session_<timestamp>.json` | fingerprint firmware/register dan status sesi |
| `raw_ecg.csv` | stream kontinu 250 Hz dalam domain kode sesudah resampling |
| `frame_xxxxxx.csv/.bin/.npy/.json` | raw canonical frame `(2500,3)`, bukan mV |
| `ecg_metadata.sqlite` | indeks metadata frame, termasuk JSON provenance |
| `acquisition_events.jsonl` | loss, reset, dan discontinuity events |
| `native/native_samples.csv` | capture opsional CH1/CH2 native sebelum resampling |
| `calibrated/frame_xxxxxx_mv.*` | frame mV bila fingerprint/status kompatibel |
| `model_ready/` | output opsional; disabled pada konfigurasi default |
| `selftest_reports/selftest_<timestamp>.json` | laporan internal self-test |

Kolom ketiga raw canonical frame adalah selisih kode hasil resampling, bukan
channel offset-code ADS1293 ketiga. File calibrated memiliki unit mV. Native
capture hanya untuk diagnosis dan menyimpan dua channel yang benar-benar
diukur, sequence, timestamp, flags, dan diagnostics.

JSON raw menyimpan SHA-256 terpisah untuk BIN dan NPY. Loader corrected
memverifikasi kedua hash dan membuktikan BIN sama dengan byte canonical `<f4`
dari NPY. JSON calibrated dan model-ready juga mengikat NPY masing-masing;
hash mismatch menghentikan pipeline.

Receiver melakukan preflight sebelum membuka serial dan menolak directory yang
sudah berisi artefak sesi ADS1293 yang dapat tertimpa atau tercampur. Gunakan
`--output-dir` yang unik untuk setiap run, misalnya
`data/sessions/20260718_bench01`.

Converter dan replay corrected selalu memerlukan session JSON eksternal yang
terverifikasi lengkap. `session_id`, firmware commit, protocol, dan fingerprint
rate/register/ADCMAX pada frame harus cocok dengan session. Output offline wajib
memakai path baru. Model-ready corrected hanya dibuat bila acquisition serta
signal quality PASS, local loss/gap/reset nol, fingerprint kalibrasi cocok, dan
hash calibrated NPY valid.

## Perintah

Siapkan lingkungan Raspberry Pi:

```bash
cd /home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi
source venv/bin/activate
python3 -m pip install -r requirements.txt
```

Regression offline tanpa hardware atau ProSim:

```bash
python3 validate_corrected_sampling_pipeline.py --synthetic --duration 30
python3 -m unittest -v \
  test_ads1293_conversion.py \
  test_ads1293_metadata.py \
  test_ads1293_selftest.py \
  test_ads1293_serial.py \
  test_ads1293_session.py \
  test_packet_parser_v2.py \
  test_receiver_v2_integration.py \
  test_streaming_resampler.py \
  test_validate_corrected_sampling_pipeline.py \
  test_model_input.py \
  test_phase3h_pipeline.py \
  test_auto_calibrate_output_safety.py \
  test_prosim_session_provenance.py
```

Receiver hardware V2:

```bash
python3 receiver.py \
  --config config.yaml \
  --port /dev/ttyUSB0 \
  --output-dir data/sessions/20260718_bench01 \
  --native-capture \
  --max-frames 3
```

`--native-capture` bersifat opsional. Jangan gunakan
`--allow-unverified-firmware` pada data kalibrasi atau data yang akan masuk ke
model.

Internal ADS1293 self-test:

```bash
python3 ads1293_selftest.py \
  --port /dev/ttyUSB0 \
  --baudrate 115200 \
  --samples 100
```

Self-test menjalankan mode zero/positive/negative, lalu mengirim
`ads selftest off` dan memverifikasi lewat status bahwa mode benar-benar off,
termasuk pada jalur pemulihan setelah exception. Kegagalan pemulihan membuat
hasil gagal. Tool ini menguji signal path dan konfigurasi internal ADS1293;
sumber uji dan reference berbagi VREF. Karena itu, internal self-test **bukan
external gain calibration** dan tidak menggantikan ProSim.

# Phase 3: ProtoCentral ADS1293 Integration

Implementasikan ADS1293 sensor mode.

Gunakan library resmi ProtoCentral ADS1293.
Jangan membuat register manual jika library tersedia.

ESP32 Dev Module pin sesuai dokumentasi ProtoCentral:
- MISO GPIO19
- MOSI GPIO23
- SCLK GPIO18
- CS GPIO4
- DRDY GPIO2

Konfigurasi ECG:
- Basic 3-lead CON1 only.
- RA = IN1.
- LA = IN2.
- LL = IN3.
- ch1 = Lead I = LA - RA.
- ch2 = Lead II = LL - RA.
- ch3 = Lead III = Lead II - Lead I.
- Jangan gunakan V1.

Sampling:
- ADS1293 native gunakan SPS_256.
- Raspberry Pi resampling ke 250 Hz untuk model.
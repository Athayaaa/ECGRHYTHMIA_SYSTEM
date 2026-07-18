# ADS1293 Acquisition Audit

## Scope

This audit records the acquisition behavior that existed before protocol V2 and
the corrected native-sampling pipeline. It is based on the project firmware,
the bundled ProtoCentral ADS1293 library API, and `docs/ads1293.pdf` (TI
SNAS602C).

## Legacy pipeline

The legacy firmware called the library preset `SPS_256`, but that preset wrote
the following effective ADS1293 configuration:

| Item | Register | Value | Decoded value |
|---|---:|---:|---:|
| AFE resolution | `AFE_RES` (`0x13`) | reset `0x00` | 102400 Hz SDM rate |
| Stage 1 decimation | `R1_RATE` (`0x25`) | reset `0x00` | R1 = 4 |
| Stage 2 decimation | `R2_RATE` (`0x21`) | `0x02` | R2 = 5 |
| CH1 stage 3 decimation | `R3_RATE_CH1` (`0x22`) | `0x04` | R3 = 8 |
| CH2 stage 3 decimation | `R3_RATE_CH2` (`0x23`) | `0x04` | R3 = 8 |

The real native ECG output data rate was therefore:

```text
102400 / (4 * 5 * 8) = 640 samples/second
```

TI Table 8 gives `ADCMAX = 0xC35000 = 12800000` for this filter
configuration. A zero differential input produces `ADCMAX / 2 = 6400000`.

`main.cpp` did not read every DRDY event. It polled the latest ADS1293 value on
a 4000-us software cadence and transmitted approximately 250 packets/second.
This discarded roughly 61 percent of native updates. It was not a rational
resampler and did not provide a defined anti-alias filter. Narrow ECG features,
including R peaks, could be sampled with a systematic phase-dependent amplitude
error.

Protocol V1 used a 32-byte CRC-protected packet:

```text
magic[2], version[1], payload_len[2], sequence[4], timestamp_us[4],
sample_count[2], sampling_rate_hz[2], flags[1], ch1_i32[4], ch2_i32[4],
ch3_i32[4], crc16_ccitt_false[2]
```

Its sequence counted the software-cadence output samples, not every native
ADS1293 conversion. The Raspberry Pi parser treated all three channel fields as
signed `int32`, appended them directly to a 250-Hz ring buffer, and performed no
streaming resampling.

## Offset-code correction

ADS1293 ECG output follows TI Equation 13:

```text
ADCOUT = [3.5 * (VINP - VINM) / (2 * VREF) + 1/2] * ADCMAX
```

Consequently the acquired Lead I and Lead II registers are unsigned 24-bit
offset-code values. Sign-extending bit 23 as two's complement is invalid. The
inverse conversion is:

```text
VIN_mV = (ADCOUT - ADCMAX/2) * (2 * VREF * 1000) / (3.5 * ADCMAX)
```

Range and saturation checks for acquired channels must use `0 <= code <=
ADCMAX`. Lead III is not a third ADS1293 offset-code channel in this project; it
is the signed derived difference `Lead II - Lead I` after resampling.

## Corrected target

The corrected pipeline explicitly requests and verifies:

| Item | Register | Value | Decoded value |
|---|---:|---:|---:|
| AFE resolution | `AFE_RES` (`0x13`) | `0x00` | 102400 Hz SDM rate |
| Stage 1 decimation | `R1_RATE` (`0x25`) | `0x00` | R1 = 4 |
| Stage 2 decimation | `R2_RATE` (`0x21`) | `0x04` | R2 = 6 |
| CH1 stage 3 decimation | `R3_RATE_CH1` (`0x22`) | `0x10` | R3 = 16 |
| CH2 stage 3 decimation | `R3_RATE_CH2` (`0x23`) | `0x10` | R3 = 16 |

This yields:

```text
native ODR = 102400 / (4 * 6 * 16) = 800/3 Hz
ADCMAX = 0xF30000 = 15925248
zero code = 0x798000 = 7962624
resampling ratio = 250 / (800/3) = 15/16
nominal scale = (2 * 2.4 * 1000) / (3.5 * 15925248)
              = 0.0000861166225749559 mV/count
```

Every native DRDY event is counted in the ISR and accounted for by the native
sequence outside the ISR. Because the ADS1293 ECG data registers expose only
the latest conversion rather than a FIFO, an accumulated backlog is collapsed
to one read of the newest conversion; overwritten events become an exact
sequence gap. Protocol V2 carries measured Lead I and Lead II unsigned
offset-code values. Raspberry Pi applies a continuous stateful anti-aliased
15/16 polyphase resampler, derives Lead III, and only then creates canonical
`(2500, 3)` float32 frames at 250 Hz.

## Calibration migration

The existing `calibration_config.yaml` was produced from data captured through
the legacy 640-to-software-250 sample-skipping path. Its empirical scale may
include sampling-phase bias and must never be selected automatically for
protocol V2.

The legacy configuration must be preserved byte-for-byte and used only when a
caller explicitly requests legacy conversion. The corrected pipeline starts
with a `datasheet_nominal` / `preliminary` configuration tied to the verified
register and protocol fingerprint. External multipoint ProSim recalibration is
still required before the corrected conversion can be labelled final.

Internal ADS1293 zero/positive/negative test modes verify the ADC signal path
and configuration but are not external gain calibration because their test
voltage and ADC reference share VREF.

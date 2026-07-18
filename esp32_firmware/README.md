# ECGRHYTHMIA ESP32 firmware

The ADS1293 builds use a project-maintained register/SPI wrapper instead of
the upstream library's misleading `SPS_256` preset and signed 24-bit ECG
decode. The ProtoCentral library remains a hardware reference/dependency, but
no generated file under `.pio/libdeps` is modified.

## ADS1293 native configuration

The real-sensor environments (`esp32dev_ads1293_usb` and
`esp32c3_ads1293_usb`) program and read back these datasheet values before
acquisition can start:

| Register | Address | Value | Meaning |
|---|---:|---:|---|
| `CONFIG` | `0x00` | `0x00`/`0x01` | conversion stop/start |
| `FLEX_CH1_CN` | `0x01` | `0x11` | Lead I = LA - RA |
| `FLEX_CH2_CN` | `0x02` | `0x19` | Lead II = LL - RA |
| `FLEX_VBAT_CN` | `0x05` | `0x00` | battery monitor disconnected |
| `LOD_CN` | `0x06` | `0x08` | lead-off detection block shut down |
| `LOD_EN` | `0x07` | `0x00` | lead-off current injection disabled |
| `LOD_CURRENT` | `0x08` | `0x00` | DC lead-off current disabled |
| `LOD_AC_CN` | `0x09` | `0x00` | AC lead-off excitation disabled |
| `CMDET_EN` | `0x0A` | `0x07` | common mode on RA/LA/LL |
| `RLD_CN` | `0x0C` | `0x04` | RLD routed to IN4 |
| `REF_CN` | `0x11` | `0x00` | internal references enabled |
| `OSC_CN` | `0x12` | `0x04` | internal oscillator with external crystal, clock propagated |
| `AFE_RES` | `0x13` | `0x00` | 102.4 kHz SDM, low-resolution mode |
| `AFE_SHDN_CN` | `0x14` | `0x24` | CH1/CH2 active, CH3 shut down |
| `R2_RATE` | `0x21` | `0x04` | R2 = 6 |
| `R3_RATE_CH1` | `0x22` | `0x10` | R3 CH1 = 16 |
| `R3_RATE_CH2` | `0x23` | `0x10` | R3 CH2 = 16 |
| `R1_RATE` | `0x25` | `0x00` | R1 CH1/CH2 = 4 |
| `DIS_EFILTER` | `0x26` | `0x00` | ECG filters enabled |
| `DRDYB_SRC` | `0x27` | `0x08` | CH1 ECG drives DRDYB |
| `SYNCB_CN` | `0x28` | `0x40` | SYNCB output disabled, no synchronization source |
| `MASK_DRDYB` | `0x29` | `0x00` | datasheet-default startup/out-of-sync masking |
| `CH_CNFG` | `0x2F` | `0x30` | read back CH1/CH2 ECG |

`REVID` at `0x40` must read `0x01`. Explicitly restoring the battery,
lead-off, reference, synchronization, and DRDY-mask registers prevents a reset
of the ESP32 alone from inheriting behavior-affecting ADS1293 state left by an
earlier diagnostic session. Clock setup first clears `STRTCLK`, selects the
internal oscillator/external-crystal source with propagation stopped, waits
20 ms (datasheet `TSTART` is 15 ms), and only then writes `OSC_CN=0x04`.

The native output rate is exact:

```text
102400 / (4 * 6 * 16) = 800/3 = 266.6666667 SPS
```

For this filter configuration, datasheet Table 8 gives `ADCMAX=0xF30000`
(`15925248`) and zero differential input is `0x798000` (`7962624`). CH1 and
CH2 are transmitted as unsigned offset-binary codes. Lead III is not sampled
or transmitted by protocol V2; Raspberry Pi derives it after stateful 15/16
resampling.

DRDYB uses a falling-edge ISR that only updates counters. The ADS1293 ECG data
registers retain the latest conversion rather than a FIFO, so the main loop
atomically collapses all pending edges into one six-byte CH1/CH2 SPI read. With
no backlog this remains one read per DRDY. With backlog, the emitted packet is
assigned the newest event's native sequence and all replaced conversions form
an exact sequence gap. Reads occur only outside the ISR, and failures also
remain visible as gaps. After each SPI read, the main loop atomically checks
whether another DRDY arrived after the claim. If so, the just-read value is
conservatively discarded because its event identity is ambiguous; the new
event stays pending, the discarded conversion increments the overrun counter,
and the next emitted packet exposes the exact sequence gap. After the text
start message is flushed, counters and sequence are reset before binary output
is enabled, giving the stream a clean boundary without replaying stale
pre-stream edges.

## Protocol V2 (ADS1293)

All integer fields are little endian. CRC is CRC16-CCITT-FALSE over bytes
0 through 29.

| Byte offset | Size | Type | Field |
|---:|---:|---|---|
| 0 | 2 | `uint8[2]` | sync `AA 55` |
| 2 | 1 | `uint8` | version `2` |
| 3 | 2 | `uint16` | payload length `25` |
| 5 | 4 | `uint32` | native DRDY sequence |
| 9 | 4 | `uint32` | SPI-read timestamp, microseconds |
| 13 | 2 | `uint16` | sample count `1` |
| 15 | 2 | `uint16` | native-rate numerator `800` |
| 17 | 1 | `uint8` | flags |
| 18 | 4 | `uint32` | Lead I offset-binary uint24 code |
| 22 | 4 | `uint32` | Lead II offset-binary uint24 code |
| 26 | 4 | `uint32` | diagnostics |
| 30 | 2 | `uint16` | CRC16 |

The exact native-rate denominator (`3`) is verified during `ads config dump`.
Diagnostic bytes, from least to most significant, are cached ADS `ERR_STATUS`,
pending DRDY after claim, maximum pending DRDY, and cumulative DRDY overrun
count. Each of the last three values clamps at `255` and never wraps; pending
after the atomic collapse is normally zero. Exact loss is represented by
native sequence gaps, not reconstructed from these bounded diagnostics.

Flag bits are:

- bit 0: register configuration verified;
- bit 1: ADS error status is nonzero;
- bit 2: DRDY backlog or newly observed overrun;
- bit 3: CH1 or CH2 code is above `ADCMAX`;
- bit 4: previous packet transmission failed;
- bit 5: internal self-test is active.

At 32 bytes, 800/3 SPS, and 10 serial bits per byte, USB serial at 115200 baud
has about 74.1% nominal utilization. Full overrun and transmission statistics
are available through `ads status`.

Dummy builds intentionally keep protocol V1, 32-byte signed synthetic fields,
250 Hz timer behavior, and existing start/stop commands.

## Text commands

Binary streaming is stopped before any text response, preventing mixed text
and packet bytes.

```text
ads config dump
ads start
ads stream start
ads stream stop
ads stop
ads status
ads debug on
ads debug off
ads selftest zero
ads selftest positive
ads selftest negative
ads selftest off
ads selftest status
```

`ads config dump` produces one JSON line with the register write/readback
values, decoded rates, ADC encoding, packet contract, and firmware fingerprint.
Streaming is refused if required readback does not match.

Internal test codes are computed from the verified `ADCMAX`: zero `7962624`,
positive `10285056`, and negative `5640192`. Internal self-test verifies the
ADS signal path/configuration; it is not an external gain calibration and does
not replace a future ProSim recalibration. Self-test routing cannot be changed
while conversion is active: the command handler stops acquisition, writes and
reads back both routes, clears stale DRDY state, and restarts only if it was
active before the command.

## Builds

```bash
pio run -e esp32dev_ads1293_usb
pio run -e esp32c3_ads1293_usb
pio run -e esp32dev_dummy_usb
pio run -e esp32dev_dummy
pio test -e native_packet_test
```

The PlatformIO pre-build script injects Git commit, dirty status, UTC build
time, and environment into the firmware without hardcoding fake values.
Untracked, non-ignored files count as dirty because they can participate in a
build. The native test executes the production V2 encoder and CRC implementation
against an independent, byte-exact 32-byte fixture; it requires no ESP32 or
ADS1293 hardware.

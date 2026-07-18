#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

#include "DummyECGGenerator.h"

class ECGPacket {
 public:
  // Protocol V1 wire format (dummy builds), little endian:
  // magic[2], version[1], payload_len[2], sequence[4], timestamp_us[4],
  // sample_count[2], sampling_rate_hz[2], flags[1],
  // lead_i_uv[4], lead_ii_uv[4], lead_iii_uv[4], crc16_ccitt_false[2].
  //
  // Protocol V2 wire format (ADS1293 builds), also exactly 32 bytes:
  // magic[2], version[1], payload_len[2], native_sequence[4],
  // read_timestamp_us[4], sample_count[2], native_rate_numerator[2], flags[1],
  // lead_i_offset_code[4], lead_ii_offset_code[4], diagnostic[4], crc16[2].
  // The exact native-rate denominator (=3) is supplied by `ads config dump`.
  // V2 contains no Lead III; the Raspberry Pi derives it after resampling.
  // CRC is computed over every byte before the CRC field.
  static constexpr uint8_t SYNC_0 = 0xAA;
  static constexpr uint8_t SYNC_1 = 0x55;
  static constexpr uint8_t VERSION_V1 = 0x01;
  static constexpr uint8_t VERSION_V2 = 0x02;
  static constexpr uint8_t VERSION = VERSION_V1;  // Legacy source compatibility.
  static constexpr uint16_t SAMPLE_COUNT = 1;
  static constexpr uint16_t SAMPLING_RATE_HZ = 250;
  static constexpr uint16_t NATIVE_RATE_NUMERATOR = 800;
  static constexpr uint16_t PAYLOAD_SIZE = 25;
  static constexpr size_t PACKET_SIZE = 32;

  enum V2Flag : uint8_t {
    V2_CONFIG_VERIFIED = 1U << 0,
    V2_ADS_ERROR = 1U << 1,
    V2_DRDY_BACKLOG_OR_OVERRUN = 1U << 2,
    V2_CODE_OUT_OF_RANGE = 1U << 3,
    V2_PREVIOUS_TX_FAILURE = 1U << 4,
    V2_SELF_TEST_ACTIVE = 1U << 5,
  };

  static size_t encodeSample(uint8_t* buffer,
                             size_t bufferLength,
                             uint32_t sequence,
                             uint32_t timestampUs,
                             const ECGSample& sample,
                             uint8_t flags);
  static bool writeSample(Stream& stream,
                          uint32_t sequence,
                          uint32_t timestampUs,
                          const ECGSample& sample,
                          uint8_t flags);

  static size_t encodeNativeSampleV2(uint8_t* buffer,
                                     size_t bufferLength,
                                     uint32_t nativeSequence,
                                     uint32_t timestampUs,
                                     uint32_t leadIOffsetCode,
                                     uint32_t leadIIOffsetCode,
                                     uint8_t flags,
                                     uint32_t diagnostic);
  static bool writeNativeSampleV2(Stream& stream,
                                  uint32_t nativeSequence,
                                  uint32_t timestampUs,
                                  uint32_t leadIOffsetCode,
                                  uint32_t leadIIOffsetCode,
                                  uint8_t flags,
                                  uint32_t diagnostic);

 private:
  static void writeU16LE(uint8_t* buffer, size_t& offset, uint16_t value);
  static void writeU32LE(uint8_t* buffer, size_t& offset, uint32_t value);
  static void writeI32LE(uint8_t* buffer, size_t& offset, int32_t value);
};

static_assert(ECGPacket::PACKET_SIZE == 32,
              "V1 and V2 packets must remain exactly 32 bytes");
static_assert(ECGPacket::PAYLOAD_SIZE == 25,
              "32-byte packet requires a 25-byte payload");

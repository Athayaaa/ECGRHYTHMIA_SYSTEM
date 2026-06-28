#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

#include "DummyECGGenerator.h"

class ECGPacket {
 public:
  // Wire format, little endian:
  // magic[2], version[1], payload_len[2], sequence[4], timestamp_us[4],
  // sample_count[2], sampling_rate_hz[2], flags[1],
  // lead_i_uv[4], lead_ii_uv[4], lead_iii_uv[4], crc16_ccitt_false[2].
  // CRC is computed over every byte before the CRC field.
  static constexpr uint8_t SYNC_0 = 0xAA;
  static constexpr uint8_t SYNC_1 = 0x55;
  static constexpr uint8_t VERSION = 0x01;
  static constexpr uint16_t SAMPLE_COUNT = 1;
  static constexpr uint16_t SAMPLING_RATE_HZ = 250;
  static constexpr uint16_t PAYLOAD_SIZE = 25;
  static constexpr size_t PACKET_SIZE = 32;

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

 private:
  static void writeU16LE(uint8_t* buffer, size_t& offset, uint16_t value);
  static void writeU32LE(uint8_t* buffer, size_t& offset, uint32_t value);
  static void writeI32LE(uint8_t* buffer, size_t& offset, int32_t value);
};

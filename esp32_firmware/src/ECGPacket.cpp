#include "ECGPacket.h"

#include "CRC16.h"

size_t ECGPacket::encodeSample(uint8_t* buffer,
                               size_t bufferLength,
                               uint32_t sequence,
                               uint32_t timestampUs,
                               const ECGSample& sample,
                               uint8_t flags) {
  if (buffer == nullptr || bufferLength < PACKET_SIZE) {
    return 0;
  }

  size_t offset = 0;
  buffer[offset++] = SYNC_0;
  buffer[offset++] = SYNC_1;
  buffer[offset++] = VERSION;
  writeU16LE(buffer, offset, PAYLOAD_SIZE);

  writeU32LE(buffer, offset, sequence);
  writeU32LE(buffer, offset, timestampUs);
  writeU16LE(buffer, offset, SAMPLE_COUNT);
  writeU16LE(buffer, offset, SAMPLING_RATE_HZ);
  buffer[offset++] = flags;
  writeI32LE(buffer, offset, sample.leadIuV);
  writeI32LE(buffer, offset, sample.leadIIuV);
  writeI32LE(buffer, offset, sample.leadIIIuV);

  const uint16_t crc = CRC16::compute(buffer, offset);
  writeU16LE(buffer, offset, crc);

  return offset;
}

bool ECGPacket::writeSample(Stream& stream,
                            uint32_t sequence,
                            uint32_t timestampUs,
                            const ECGSample& sample,
                            uint8_t flags) {
  uint8_t packet[PACKET_SIZE];
  const size_t packetLength =
      encodeSample(packet, sizeof(packet), sequence, timestampUs, sample, flags);

  if (packetLength == 0) {
    return false;
  }

  return stream.write(packet, packetLength) == packetLength;
}

void ECGPacket::writeU16LE(uint8_t* buffer, size_t& offset, uint16_t value) {
  buffer[offset++] = static_cast<uint8_t>(value & 0xFF);
  buffer[offset++] = static_cast<uint8_t>((value >> 8) & 0xFF);
}

void ECGPacket::writeU32LE(uint8_t* buffer, size_t& offset, uint32_t value) {
  buffer[offset++] = static_cast<uint8_t>(value & 0xFF);
  buffer[offset++] = static_cast<uint8_t>((value >> 8) & 0xFF);
  buffer[offset++] = static_cast<uint8_t>((value >> 16) & 0xFF);
  buffer[offset++] = static_cast<uint8_t>((value >> 24) & 0xFF);
}

void ECGPacket::writeI32LE(uint8_t* buffer, size_t& offset, int32_t value) {
  writeU32LE(buffer, offset, static_cast<uint32_t>(value));
}

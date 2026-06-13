#include "CRC16.h"

uint16_t CRC16::compute(const uint8_t* data, size_t length) {
  uint16_t crc = INITIAL_VALUE;

  for (size_t i = 0; i < length; ++i) {
    crc = update(crc, data[i]);
  }

  return crc;
}

uint16_t CRC16::update(uint16_t crc, uint8_t data) {
  crc ^= static_cast<uint16_t>(data) << 8;

  for (uint8_t bit = 0; bit < 8; ++bit) {
    if ((crc & 0x8000) != 0) {
      crc = static_cast<uint16_t>((crc << 1) ^ POLYNOMIAL);
    } else {
      crc = static_cast<uint16_t>(crc << 1);
    }
  }

  return crc;
}

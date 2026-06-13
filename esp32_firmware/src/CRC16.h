#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

class CRC16 {
 public:
  static constexpr uint16_t INITIAL_VALUE = 0xFFFF;
  static constexpr uint16_t POLYNOMIAL = 0x1021;

  static uint16_t compute(const uint8_t* data, size_t length);
  static uint16_t update(uint16_t crc, uint8_t data);
};

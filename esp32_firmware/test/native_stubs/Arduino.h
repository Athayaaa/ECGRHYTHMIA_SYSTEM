#pragma once

#include <stddef.h>
#include <stdint.h>

// Minimal Arduino Stream surface required by ECGPacket for the native fixture.
// Production firmware continues to compile against the real Arduino core.
class Stream {
 public:
  virtual ~Stream() = default;
  virtual size_t write(const uint8_t* buffer, size_t length) = 0;
};

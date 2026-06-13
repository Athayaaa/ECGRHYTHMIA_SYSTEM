#pragma once

#include <Arduino.h>
#include <stdint.h>

struct ECGSample {
  int16_t leadIuV;
  int16_t leadIIuV;
  int16_t leadIIIuV;
};

class DummyECGGenerator {
 public:
  explicit DummyECGGenerator(uint16_t sampleRateHz);

  ECGSample nextSample(uint32_t sampleIndex, bool simulationEnabled) const;

 private:
  uint16_t sampleRateHz_;

  static float gaussian(float x, float center, float width);
  static float wrapUnit(float value);
  static int16_t clampToInt16(float value);
};

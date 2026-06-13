#include "DummyECGGenerator.h"

#include <math.h>

namespace {
constexpr float ECG_TWO_PI = 6.28318530718f;
constexpr float HEART_RATE_HZ = 1.15f;
}

DummyECGGenerator::DummyECGGenerator(uint16_t sampleRateHz)
    : sampleRateHz_(sampleRateHz) {}

ECGSample DummyECGGenerator::nextSample(uint32_t sampleIndex,
                                        bool simulationEnabled) const {
  if (!simulationEnabled) {
    return {0, 0, 0};
  }

  const float t = static_cast<float>(sampleIndex) / sampleRateHz_;
  const float phase = wrapUnit(t * HEART_RATE_HZ);

  const float pWave = 90.0f * gaussian(phase, 0.18f, 0.035f);
  const float qWave = -120.0f * gaussian(phase, 0.375f, 0.014f);
  const float rWave = 950.0f * gaussian(phase, 0.400f, 0.010f);
  const float sWave = -260.0f * gaussian(phase, 0.430f, 0.016f);
  const float tWave = 280.0f * gaussian(phase, 0.650f, 0.070f);
  const float baseline = 45.0f * sinf(ECG_TWO_PI * 0.33f * t);
  const float mainsLikeRipple = 8.0f * sinf(ECG_TWO_PI * 50.0f * t);

  const float commonBeat = pWave + qWave + rWave + sWave + tWave + baseline;
  const int16_t leadI =
      clampToInt16(0.74f * commonBeat + mainsLikeRipple);
  const int16_t leadII =
      clampToInt16(1.04f * commonBeat - 0.5f * mainsLikeRipple);
  const int16_t leadIII = static_cast<int16_t>(leadII - leadI);

  return {leadI, leadII, leadIII};
}

float DummyECGGenerator::gaussian(float x, float center, float width) {
  const float distance = x - center;
  return expf(-(distance * distance) / (2.0f * width * width));
}

float DummyECGGenerator::wrapUnit(float value) {
  const float wrapped = fmodf(value, 1.0f);
  return wrapped < 0.0f ? wrapped + 1.0f : wrapped;
}

int16_t DummyECGGenerator::clampToInt16(float value) {
  if (value > 32767.0f) {
    return 32767;
  }
  if (value < -32768.0f) {
    return -32768;
  }
  return static_cast<int16_t>(lroundf(value));
}

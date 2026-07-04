#pragma once

#include <Arduino.h>
#include <stdint.h>

#include "config.h"

#if ENABLE_ADS1293
#include <protocentral_ads1293.h>
#endif

class ADS1293Reader {
 public:
  struct RawSample {
    int32_t leadIRaw = 0;
    int32_t leadIIRaw = 0;
    int32_t leadIIIRaw = 0;
    bool dataReady = false;
    uint8_t errorStatus = 0;
    bool ok = false;
  };

  ADS1293Reader();

  bool begin();
  bool startAcquisition();
  bool stopAcquisition();
  bool refreshStatus();
  bool readRawSample(RawSample& sample);
  void printStatus(Stream& stream) const;
  void printRawSample(Stream& stream, const RawSample& sample) const;

  bool isConfigured() const;
  bool isAcquisitionEnabled() const;
  const char* lastMessage() const;

 private:
#if ENABLE_ADS1293
  ADS1293 ads_;
#endif
  bool spiStarted_ = false;
  bool configured_ = false;
  bool acquisitionEnabled_ = false;
  bool dataReady_ = false;
  bool registerDataReady_ = false;
  bool drdyPinAsserted_ = false;
  uint8_t deviceId_ = 0;
  uint8_t errorStatus_ = 0;
  const char* lastMessage_ = "ADS1293 support is not enabled in this build";

  bool refreshDataReadyStatus();
  static void printHexByte(Stream& stream, uint8_t value);
};

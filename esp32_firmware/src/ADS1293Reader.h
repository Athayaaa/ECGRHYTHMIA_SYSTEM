#pragma once

#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

#include "config.h"

#if ENABLE_ADS1293
#include <SPI.h>
#endif

class ADS1293Reader {
 public:
  static constexpr uint8_t ECG_DATA_READY_MASK = 0xE0;
  static constexpr uint8_t EXPECTED_DEVICE_ID = 0x01;
  static constexpr size_t REGISTER_COUNT = 23;

  enum class SelfTestMode : uint8_t {
    Off = 0,
    Positive = 1,
    Negative = 2,
    Zero = 3,
  };

  struct RawSample {
    // ADS1293 ECG results are 24-bit offset-binary codes, not signed
    // two's-complement values.  The original 24 bits remain in the low bits.
    uint32_t leadIRaw = 0;
    uint32_t leadIIRaw = 0;
    bool dataReady = false;
    uint8_t errorStatus = 0;
    bool ok = false;
  };

  struct RegisterSnapshot {
    uint8_t address = 0;
    uint8_t written = 0;
    uint8_t readback = 0;
    bool matched = false;
  };

  ADS1293Reader();

  bool begin();
  bool startAcquisition();
  bool stopAcquisition();
  bool refreshStatus();
  bool refreshErrorStatus();
  bool refreshConfigurationReadback();
  bool readRawSample(RawSample& sample);
  // Clear an already-asserted DRDYB/data latch before relying exclusively on
  // falling-edge interrupts.  The discarded conversion belongs to the text /
  // startup boundary and is never exposed as a protocol-V2 sample.
  bool primeDataReadyForInterrupt(uint32_t timeoutMs, bool& discardedSample);

  // Maintained project-local register access.  This deliberately avoids the
  // upstream library's signed ECG decode and incorrect DATA_STATUS mask.
  bool writeRegister(uint8_t address, uint8_t value);
  bool readRegister(uint8_t address, uint8_t& value);

  bool setSelfTestMode(SelfTestMode mode);
  SelfTestMode selfTestMode() const;
  const char* selfTestModeName() const;

  void printStatus(Stream& stream) const;
  void printConfigDumpJson(Stream& stream);
  void printSelfTestStatusJson(Stream& stream) const;
  void printRawSample(Stream& stream, const RawSample& sample) const;

  bool isConfigured() const;
  bool isConfigurationVerified() const;
  bool isAcquisitionEnabled() const;
  bool isDataReady() const;
  uint8_t deviceId() const;
  uint8_t errorStatus() const;
  const char* lastMessage() const;

  const RegisterSnapshot* registerSnapshots() const;
  static constexpr uint8_t decodeR1(uint8_t registerValue, uint8_t channel) {
    return channel < 1 || channel > 3
               ? 0
               : ((registerValue & (1U << (channel - 1))) != 0 ? 2 : 4);
  }
  static constexpr uint8_t decodeR2(uint8_t registerValue) {
    return (registerValue & 0x0F) == 0x01
               ? 4
               : (registerValue & 0x0F) == 0x02
                     ? 5
                     : (registerValue & 0x0F) == 0x04
                           ? 6
                           : (registerValue & 0x0F) == 0x08 ? 8 : 0;
  }
  static constexpr uint8_t decodeR3(uint8_t registerValue) {
    return registerValue == 0x01
               ? 4
               : registerValue == 0x02
                     ? 6
                     : registerValue == 0x04
                           ? 8
                           : registerValue == 0x08
                                 ? 12
                                 : registerValue == 0x10
                                       ? 16
                                       : registerValue == 0x20
                                             ? 32
                                             : registerValue == 0x40
                                                   ? 64
                                                   : registerValue == 0x80 ? 128
                                                                           : 0;
  }
  static double calculateNativeOdrHz(uint32_t afeResHz,
                                     uint8_t r1,
                                     uint8_t r2,
                                     uint8_t r3);
  static constexpr uint32_t composeUInt24(uint8_t msb,
                                          uint8_t middle,
                                          uint8_t lsb) {
    return (static_cast<uint32_t>(msb) << 16) |
           (static_cast<uint32_t>(middle) << 8) |
           static_cast<uint32_t>(lsb);
  }
  static uint32_t decodeUInt24(const uint8_t* bytes);

 private:
#if ENABLE_ADS1293
  SPIClass* spi_ = &SPI;
#endif
  bool spiStarted_ = false;
  bool configured_ = false;
  bool configurationVerified_ = false;
  bool acquisitionEnabled_ = false;
  bool dataReady_ = false;
  bool registerDataReady_ = false;
  bool drdyPinAsserted_ = false;
  uint8_t deviceId_ = 0;
  uint8_t errorStatus_ = 0;
  SelfTestMode selfTestMode_ = SelfTestMode::Off;
  RegisterSnapshot registerSnapshots_[REGISTER_COUNT] = {};
  const char* lastMessage_ =
      "ADS1293 support is not enabled in this build";

  void initializeRegisterSnapshots();
  bool applyExplicitNativeConfiguration();
  bool writeAndVerify(uint8_t address, uint8_t value);
  bool readTwoEcgChannels(uint32_t& leadI, uint32_t& leadII);
  bool refreshDataReadyStatus();
  RegisterSnapshot* findRegisterSnapshot(uint8_t address);
  const RegisterSnapshot* findRegisterSnapshot(uint8_t address) const;
  uint8_t expectedFlexChannel1() const;
  uint8_t expectedFlexChannel2() const;
  static void printHexByte(Stream& stream, uint8_t value);
};

static_assert(ADS1293Reader::ECG_DATA_READY_MASK == 0xE0,
              "ADS1293 ECG-ready bits must be DATA_STATUS[7:5]");
static_assert(ADS1293Reader::EXPECTED_DEVICE_ID == 0x01,
              "ADS1293 REVID must identify the supported silicon");
static_assert(ADS1293Reader::composeUInt24(0x80, 0x00, 0x00) == 0x800000UL,
              "uint24 ECG codes above the signed boundary must stay positive");
static_assert(ADS1293Reader::composeUInt24(0xF3, 0x00, 0x00) == 0xF30000UL,
              "uint24 decode must preserve target ADCMAX");
static_assert(ADS1293Reader::decodeR1(0x00, 1) == 4 &&
                  ADS1293Reader::decodeR1(0x00, 2) == 4,
              "R1 register mapping must decode standard pace rate as 4");
static_assert(ADS1293Reader::decodeR2(0x04) == 6,
              "R2 register value 0x04 must decode as 6");
static_assert(ADS1293Reader::decodeR3(0x10) == 16,
              "R3 register value 0x10 must decode as 16");

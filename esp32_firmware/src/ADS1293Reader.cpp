#include "ADS1293Reader.h"

#include "ECGPacket.h"
#include "FirmwareInfo.h"

namespace {

constexpr uint8_t REG_CONFIG = 0x00;
constexpr uint8_t REG_FLEX_CH1_CN = 0x01;
constexpr uint8_t REG_FLEX_CH2_CN = 0x02;
constexpr uint8_t REG_FLEX_VBAT_CN = 0x05;
constexpr uint8_t REG_LOD_CN = 0x06;
constexpr uint8_t REG_LOD_EN = 0x07;
constexpr uint8_t REG_LOD_CURRENT = 0x08;
constexpr uint8_t REG_LOD_AC_CN = 0x09;
constexpr uint8_t REG_CMDET_EN = 0x0A;
constexpr uint8_t REG_RLD_CN = 0x0C;
constexpr uint8_t REG_REF_CN = 0x11;
constexpr uint8_t REG_OSC_CN = 0x12;
constexpr uint8_t REG_AFE_RES = 0x13;
constexpr uint8_t REG_AFE_SHDN_CN = 0x14;
constexpr uint8_t REG_ERR_STATUS = 0x19;
constexpr uint8_t REG_R2_RATE = 0x21;
constexpr uint8_t REG_R3_RATE_CH1 = 0x22;
constexpr uint8_t REG_R3_RATE_CH2 = 0x23;
constexpr uint8_t REG_R1_RATE = 0x25;
constexpr uint8_t REG_DIS_EFILTER = 0x26;
constexpr uint8_t REG_DRDYB_SRC = 0x27;
constexpr uint8_t REG_SYNCB_CN = 0x28;
constexpr uint8_t REG_MASK_DRDYB = 0x29;
constexpr uint8_t REG_CH_CNFG = 0x2F;
constexpr uint8_t REG_DATA_STATUS = 0x30;
constexpr uint8_t REG_REVID = 0x40;
constexpr uint8_t REG_DATA_LOOP = 0x50;

constexpr uint8_t READ_REGISTER_FLAG = 0x80;
constexpr uint8_t WRITE_REGISTER_MASK = 0x7F;

// Values are taken directly from ADS1293 SNAS602C register tables and the
// 3-lead application example.  R1=4, R2=6, R3=16 gives 800/3 native SPS.
constexpr uint8_t FLEX_CH1_NORMAL = 0x11;  // CH1 = IN2 - IN1 = LA - RA.
constexpr uint8_t FLEX_CH2_NORMAL = 0x19;  // CH2 = IN3 - IN1 = LL - RA.
constexpr uint8_t BATTERY_MONITOR_DISABLED = 0x00;
constexpr uint8_t LEAD_OFF_BLOCK_SHUTDOWN = 0x08;
constexpr uint8_t LOD_DISABLED = 0x00;
constexpr uint8_t LOD_CURRENT_DISABLED = 0x00;
constexpr uint8_t LOD_AC_DISABLED = 0x00;
constexpr uint8_t CMDET_RA_LA_LL = 0x07;
constexpr uint8_t RLD_TO_IN4 = 0x04;
constexpr uint8_t INTERNAL_REFERENCES_ENABLED = 0x00;
constexpr uint8_t OSC_SOURCE_AND_OUTPUT_MASK = 0x03;
constexpr uint8_t OSC_INTERNAL_CRYSTAL_CLOCK_STOPPED = 0x00;
constexpr uint8_t OSC_EXTERNAL_CRYSTAL = 0x04;
constexpr uint8_t AFE_RES_102P4KHZ_LOW_RES = 0x00;
constexpr uint8_t AFE_CH1_CH2_ACTIVE = 0x24;
constexpr uint8_t AFE_ALL_SHUTDOWN = 0x3F;
constexpr uint8_t R2_RATE_6 = 0x04;
constexpr uint8_t R3_RATE_16 = 0x10;
constexpr uint8_t R1_RATE_4_CH1_CH2 = 0x00;
constexpr uint8_t ECG_FILTERS_ENABLED = 0x00;
constexpr uint8_t DRDY_SOURCE_CH1_ECG = 0x08;
constexpr uint8_t SYNCB_OUTPUT_DISABLED_NO_SOURCE = 0x40;
constexpr uint8_t DRDY_DEFAULT_MASKING = 0x00;
constexpr uint8_t LOOP_READ_CH1_CH2_ECG = 0x30;
constexpr uint8_t CONVERSION_STOP = 0x00;
constexpr uint8_t CONVERSION_START = 0x01;

struct InitialRegisterValue {
  uint8_t address;
  uint8_t value;
};

constexpr InitialRegisterValue INITIAL_REGISTERS[] = {
    {REG_CONFIG, CONVERSION_STOP},
    {REG_FLEX_CH1_CN, FLEX_CH1_NORMAL},
    {REG_FLEX_CH2_CN, FLEX_CH2_NORMAL},
    {REG_FLEX_VBAT_CN, BATTERY_MONITOR_DISABLED},
    {REG_LOD_CN, LEAD_OFF_BLOCK_SHUTDOWN},
    {REG_LOD_EN, LOD_DISABLED},
    {REG_LOD_CURRENT, LOD_CURRENT_DISABLED},
    {REG_LOD_AC_CN, LOD_AC_DISABLED},
    {REG_CMDET_EN, CMDET_RA_LA_LL},
    {REG_RLD_CN, RLD_TO_IN4},
    {REG_REF_CN, INTERNAL_REFERENCES_ENABLED},
    {REG_OSC_CN, OSC_EXTERNAL_CRYSTAL},
    {REG_AFE_RES, AFE_RES_102P4KHZ_LOW_RES},
    {REG_AFE_SHDN_CN, AFE_ALL_SHUTDOWN},
    {REG_R2_RATE, R2_RATE_6},
    {REG_R3_RATE_CH1, R3_RATE_16},
    {REG_R3_RATE_CH2, R3_RATE_16},
    {REG_R1_RATE, R1_RATE_4_CH1_CH2},
    {REG_DIS_EFILTER, ECG_FILTERS_ENABLED},
    {REG_DRDYB_SRC, DRDY_SOURCE_CH1_ECG},
    {REG_SYNCB_CN, SYNCB_OUTPUT_DISABLED_NO_SOURCE},
    {REG_MASK_DRDYB, DRDY_DEFAULT_MASKING},
    {REG_CH_CNFG, LOOP_READ_CH1_CH2_ECG},
};

static_assert(sizeof(INITIAL_REGISTERS) / sizeof(INITIAL_REGISTERS[0]) ==
                  ADS1293Reader::REGISTER_COUNT,
              "register snapshot count must cover every required register");
static_assert(Config::ADS1293_AFE_RES_HZ /
                      (Config::ADS1293_R1 * Config::ADS1293_R2 *
                       Config::ADS1293_R3_CH1) ==
                  266,
              "target ADS1293 ODR integer part must be 266 Hz");
static_assert(Config::ADS1293_ADCMAX == 0xF30000UL,
              "R1=4/R2=6/R3=16 requires ADCMAX 0xF30000");
static_assert(Config::ADS1293_ZERO_CODE == 0x798000UL,
              "zero differential input must be ADCMAX/2");

}  // namespace

ADS1293Reader::ADS1293Reader() {
  initializeRegisterSnapshots();
}

bool ADS1293Reader::begin() {
#if ENABLE_ADS1293
  // ProtoCentral routes DRDYB through a TXB0108 auto-direction level shifter.
  // Do not enable the ESP32 internal pull-up: TXB0108 explicitly discourages
  // external pull resistors below 50 kohm and the board already provides the
  // required ADS-side pull-up.
  pinMode(Config::ADS1293_DRDY_PIN, INPUT);
  pinMode(Config::ADS1293_SPI_CS_PIN, OUTPUT);
  digitalWrite(Config::ADS1293_SPI_CS_PIN, HIGH);

  spi_->begin(Config::ADS1293_SPI_SCLK_PIN,
              Config::ADS1293_SPI_MISO_PIN,
              Config::ADS1293_SPI_MOSI_PIN);
  spiStarted_ = true;
  acquisitionEnabled_ = false;
  selfTestMode_ = SelfTestMode::Off;

  if (!readRegister(REG_REVID, deviceId_)) {
    configured_ = false;
    configurationVerified_ = false;
    lastMessage_ = "ADS1293 device ID read failed";
    return false;
  }
  if (deviceId_ != EXPECTED_DEVICE_ID) {
    configured_ = false;
    configurationVerified_ = false;
    lastMessage_ = "ADS1293 REVID mismatch";
    return false;
  }

  if (!applyExplicitNativeConfiguration()) {
    configured_ = false;
    configurationVerified_ = false;
    lastMessage_ = "ADS1293 explicit register configuration/readback failed";
    return false;
  }

  configured_ = true;
  configurationVerified_ = true;
  if (!refreshStatus() || !configurationVerified_) {
    configured_ = false;
    configurationVerified_ = false;
    lastMessage_ = "ADS1293 identity/status verification failed";
    return false;
  }
  lastMessage_ =
      "ADS1293 verified at native 800/3 SPS; acquisition stopped";
  return true;
#else
  spiStarted_ = false;
  configured_ = false;
  configurationVerified_ = false;
  acquisitionEnabled_ = false;
  dataReady_ = false;
  registerDataReady_ = false;
  drdyPinAsserted_ = false;
  deviceId_ = 0;
  errorStatus_ = 0;
  lastMessage_ = "ADS1293 support is not enabled in this build";
  return false;
#endif
}

bool ADS1293Reader::startAcquisition() {
#if ENABLE_ADS1293
  if (!configured_ || !configurationVerified_) {
    lastMessage_ = "ADS1293 configuration is not verified";
    return false;
  }

  bool ok = writeAndVerify(REG_AFE_SHDN_CN, AFE_CH1_CH2_ACTIVE);
  ok &= writeAndVerify(REG_CONFIG, CONVERSION_START);
  ok &= refreshConfigurationReadback();

  acquisitionEnabled_ = ok;
  configurationVerified_ = ok;
  lastMessage_ = ok ? "ADS1293 acquisition started at native 800/3 SPS"
                    : "ADS1293 acquisition start/readback failed";
  return ok;
#else
  lastMessage_ = "ADS1293 support is not enabled in this build";
  return false;
#endif
}

bool ADS1293Reader::stopAcquisition() {
#if ENABLE_ADS1293
  if (!spiStarted_) {
    lastMessage_ = "ADS1293 SPI has not been initialized";
    return false;
  }

  bool ok = writeAndVerify(REG_CONFIG, CONVERSION_STOP);
  ok &= writeAndVerify(REG_AFE_SHDN_CN, AFE_ALL_SHUTDOWN);
  acquisitionEnabled_ = false;
  configurationVerified_ = ok && refreshConfigurationReadback();
  lastMessage_ = configurationVerified_ ? "ADS1293 acquisition stopped"
                                        : "ADS1293 stop/readback failed";
  return configurationVerified_;
#else
  lastMessage_ = "ADS1293 support is not enabled in this build";
  return false;
#endif
}

bool ADS1293Reader::refreshStatus() {
#if ENABLE_ADS1293
  if (!spiStarted_) {
    return false;
  }

  bool ok = readRegister(REG_REVID, deviceId_);
  if (!ok || deviceId_ != EXPECTED_DEVICE_ID) {
    configurationVerified_ = false;
  }
  ok &= readRegister(REG_ERR_STATUS, errorStatus_);
  ok &= refreshDataReadyStatus();
  return ok;
#else
  return false;
#endif
}

bool ADS1293Reader::refreshErrorStatus() {
#if ENABLE_ADS1293
  if (!spiStarted_) {
    return false;
  }
  return readRegister(REG_ERR_STATUS, errorStatus_);
#else
  return false;
#endif
}

bool ADS1293Reader::refreshConfigurationReadback() {
#if ENABLE_ADS1293
  if (!spiStarted_) {
    configurationVerified_ = false;
    return false;
  }

  const bool identityRead = readRegister(REG_REVID, deviceId_);
  bool allMatched = identityRead && deviceId_ == EXPECTED_DEVICE_ID;
  for (size_t i = 0; i < REGISTER_COUNT; ++i) {
    uint8_t readback = 0;
    const bool readOk = readRegister(registerSnapshots_[i].address, readback);
    registerSnapshots_[i].readback = readback;
    registerSnapshots_[i].matched =
        readOk && readback == registerSnapshots_[i].written;
    allMatched &= registerSnapshots_[i].matched;
  }

  configurationVerified_ = allMatched;
  return allMatched;
#else
  configurationVerified_ = false;
  return false;
#endif
}

bool ADS1293Reader::readRawSample(RawSample& sample) {
  sample = RawSample();

#if ENABLE_ADS1293
  if (!spiStarted_ || !configured_ || !configurationVerified_ ||
      !acquisitionEnabled_) {
    lastMessage_ = "ADS1293 acquisition/configuration is not ready";
    sample.errorStatus = errorStatus_;
    return false;
  }

  // The caller claimed one or more DRDY edges and is reading the latest
  // conversion retained by the ADS1293 output registers.
  sample.dataReady = true;
  sample.errorStatus = errorStatus_;
  if (!readTwoEcgChannels(sample.leadIRaw, sample.leadIIRaw)) {
    lastMessage_ = "ADS1293 unsigned CH1/CH2 SPI read failed";
    return false;
  }

  sample.ok = true;
  lastMessage_ = "ADS1293 unsigned offset-code sample read";
  return true;
#else
  lastMessage_ = "ADS1293 support is not enabled in this build";
  return false;
#endif
}

bool ADS1293Reader::primeDataReadyForInterrupt(uint32_t timeoutMs,
                                                bool& discardedSample) {
  discardedSample = false;

#if ENABLE_ADS1293
  if (!spiStarted_ || !configured_ || !configurationVerified_ ||
      !acquisitionEnabled_) {
    lastMessage_ =
        "ADS1293 acquisition/configuration is not ready for DRDY priming";
    return false;
  }

  const uint32_t deadlineMs = millis() + timeoutMs;
  while (true) {
    uint8_t status = 0;
    const bool statusRead = readRegister(REG_DATA_STATUS, status);
    if (!statusRead) {
      lastMessage_ = "ADS1293 DATA_STATUS read failed during DRDY priming";
      return false;
    }

    registerDataReady_ = (status & ECG_DATA_READY_MASK) != 0;
    drdyPinAsserted_ = digitalRead(Config::ADS1293_DRDY_PIN) == LOW;
    dataReady_ = registerDataReady_ || drdyPinAsserted_;
    if (dataReady_) {
      uint32_t discardedLeadI = 0;
      uint32_t discardedLeadII = 0;
      if (!readTwoEcgChannels(discardedLeadI, discardedLeadII)) {
        lastMessage_ = "ADS1293 startup ECG read failed during DRDY priming";
        return false;
      }
      discardedSample = true;

      // SNAS602C section 8.5.7 specifies that a complete ECG/PACE read
      // deasserts DRDYB on the 14th rising SCLK edge.  Give the GPIO a bounded
      // interval to reflect that state before arming a clean falling edge.
      const uint32_t highDeadlineUs = micros() + 1000U;
      while (digitalRead(Config::ADS1293_DRDY_PIN) == LOW &&
             static_cast<int32_t>(micros() - highDeadlineUs) < 0) {
        yield();
      }
      drdyPinAsserted_ = digitalRead(Config::ADS1293_DRDY_PIN) == LOW;
      registerDataReady_ = false;
      dataReady_ = drdyPinAsserted_;
      if (drdyPinAsserted_) {
        lastMessage_ =
            "ADS1293 DRDY remained low after startup ECG priming read";
        return false;
      }

      lastMessage_ =
          "ADS1293 DRDY startup latch cleared; falling-edge interrupt armed";
      return true;
    }

    if (static_cast<int32_t>(millis() - deadlineMs) >= 0) {
      lastMessage_ = "ADS1293 DRDY did not assert during startup priming";
      return false;
    }
    delayMicroseconds(100);
    yield();
  }
#else
  (void)timeoutMs;
  lastMessage_ = "ADS1293 support is not enabled in this build";
  return false;
#endif
}

bool ADS1293Reader::writeRegister(uint8_t address, uint8_t value) {
#if ENABLE_ADS1293
  if (!spiStarted_) {
    return false;
  }

  spi_->beginTransaction(
      SPISettings(Config::ADS1293_SPI_CLOCK_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(Config::ADS1293_SPI_CS_PIN, LOW);
  spi_->transfer(address & WRITE_REGISTER_MASK);
  spi_->transfer(value);
  digitalWrite(Config::ADS1293_SPI_CS_PIN, HIGH);
  spi_->endTransaction();
  delayMicroseconds(10);
  return true;
#else
  (void)address;
  (void)value;
  return false;
#endif
}

bool ADS1293Reader::readRegister(uint8_t address, uint8_t& value) {
#if ENABLE_ADS1293
  if (!spiStarted_) {
    return false;
  }

  spi_->beginTransaction(
      SPISettings(Config::ADS1293_SPI_CLOCK_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(Config::ADS1293_SPI_CS_PIN, LOW);
  spi_->transfer(address | READ_REGISTER_FLAG);
  value = spi_->transfer(0x00);
  digitalWrite(Config::ADS1293_SPI_CS_PIN, HIGH);
  spi_->endTransaction();
  return true;
#else
  (void)address;
  value = 0;
  return false;
#endif
}

bool ADS1293Reader::setSelfTestMode(SelfTestMode mode) {
#if ENABLE_ADS1293
  if (!configured_) {
    lastMessage_ = "ADS1293 must be configured before selecting self-test";
    return false;
  }
  if (acquisitionEnabled_) {
    lastMessage_ =
        "stop ADS1293 acquisition before changing self-test routing";
    return false;
  }

  selfTestMode_ = mode;
  bool ok = writeAndVerify(REG_FLEX_CH1_CN, expectedFlexChannel1());
  ok &= writeAndVerify(REG_FLEX_CH2_CN, expectedFlexChannel2());
  configurationVerified_ = ok && refreshConfigurationReadback();
  lastMessage_ = configurationVerified_ ? "ADS1293 self-test mode updated"
                                        : "ADS1293 self-test readback failed";
  return configurationVerified_;
#else
  (void)mode;
  lastMessage_ = "ADS1293 support is not enabled in this build";
  return false;
#endif
}

ADS1293Reader::SelfTestMode ADS1293Reader::selfTestMode() const {
  return selfTestMode_;
}

const char* ADS1293Reader::selfTestModeName() const {
  switch (selfTestMode_) {
    case SelfTestMode::Positive:
      return "positive";
    case SelfTestMode::Negative:
      return "negative";
    case SelfTestMode::Zero:
      return "zero";
    case SelfTestMode::Off:
    default:
      return "off";
  }
}

void ADS1293Reader::printStatus(Stream& stream) const {
  stream.println(F("ads1293:"));
  stream.print(F("  enabled="));
  stream.println(Config::ADS1293_ENABLED ? F("yes") : F("no"));
  stream.print(F("  spi="));
  stream.println(spiStarted_ ? F("started") : F("not_started"));
  stream.print(F("  configured="));
  stream.println(configured_ ? F("yes") : F("no"));
  stream.print(F("  config_verified="));
  stream.println(configurationVerified_ ? F("yes") : F("no"));
  stream.print(F("  acquisition="));
  stream.println(acquisitionEnabled_ ? F("on") : F("off"));
  stream.print(F("  native_odr_hz="));
  stream.println(calculateNativeOdrHz(Config::ADS1293_AFE_RES_HZ,
                                      Config::ADS1293_R1,
                                      Config::ADS1293_R2,
                                      Config::ADS1293_R3_CH1),
                 10);
  stream.print(F("  native_rate_fraction="));
  stream.print(Config::ADS1293_NATIVE_RATE_NUMERATOR);
  stream.print('/');
  stream.println(Config::ADS1293_NATIVE_RATE_DENOMINATOR);
  stream.print(F("  adc_encoding=offset_binary_uint24 adcmax="));
  stream.print(Config::ADS1293_ADCMAX);
  stream.print(F(" zero_code="));
  stream.println(Config::ADS1293_ZERO_CODE);
  stream.print(F("  device_id="));
  printHexByte(stream, deviceId_);
  stream.println();
  stream.print(F("  error_status="));
  printHexByte(stream, errorStatus_);
  stream.println();
  stream.print(F("  register_data_ready="));
  stream.println(registerDataReady_ ? F("yes") : F("no"));
  stream.print(F("  drdy_pin="));
  stream.println(drdyPinAsserted_ ? F("low/asserted") : F("high/idle"));
  stream.print(F("  selftest="));
  stream.println(selfTestModeName());
  stream.print(F("  message="));
  stream.println(lastMessage_);
}

void ADS1293Reader::printConfigDumpJson(Stream& stream) {
  if (spiStarted_) {
    refreshConfigurationReadback();
  }

  stream.print(F("{\"config_verified\":"));
  stream.print(configurationVerified_ ? F("true") : F("false"));
  stream.print(F(",\"device_id\":"));
  stream.print(deviceId_);
  stream.print(F(",\"protocol_version\":"));
  stream.print(FirmwareInfo::PROTOCOL_VERSION);
  stream.print(F(",\"configuration_profile\":\""));
  stream.print(Config::ADS1293_NATIVE_PROFILE);
  stream.print('"');
  stream.print(F(",\"afe_res_hz\":"));
  stream.print(Config::ADS1293_AFE_RES_HZ);
  stream.print(F(",\"r1\":"));
  stream.print(decodeR1(findRegisterSnapshot(REG_R1_RATE)->readback, 1));
  stream.print(F(",\"r2\":"));
  stream.print(decodeR2(findRegisterSnapshot(REG_R2_RATE)->readback));
  stream.print(F(",\"r3_ch1\":"));
  stream.print(decodeR3(findRegisterSnapshot(REG_R3_RATE_CH1)->readback));
  stream.print(F(",\"r3_ch2\":"));
  stream.print(decodeR3(findRegisterSnapshot(REG_R3_RATE_CH2)->readback));
  stream.print(F(",\"native_sample_rate_numerator\":"));
  stream.print(Config::ADS1293_NATIVE_RATE_NUMERATOR);
  stream.print(F(",\"native_sample_rate_denominator\":"));
  stream.print(Config::ADS1293_NATIVE_RATE_DENOMINATOR);
  stream.print(F(",\"native_sample_rate_hz\":"));
  stream.print(calculateNativeOdrHz(Config::ADS1293_AFE_RES_HZ,
                                    Config::ADS1293_R1,
                                    Config::ADS1293_R2,
                                    Config::ADS1293_R3_CH1),
               10);
  stream.print(F(",\"adcmax\":"));
  stream.print(Config::ADS1293_ADCMAX);
  stream.print(F(",\"zero_code\":"));
  stream.print(Config::ADS1293_ZERO_CODE);
  stream.print(F(",\"adc_encoding\":\"offset_binary_uint24\""));
  stream.print(F(",\"vref_nominal_v\":"));
  stream.print(Config::ADS1293_VREF_NOMINAL_V, 6);
  stream.print(F(",\"analog_gain\":"));
  stream.print(Config::ADS1293_ANALOG_GAIN, 6);
  stream.print(F(",\"packet_size\":"));
  stream.print(ECGPacket::PACKET_SIZE);
  stream.print(F(",\"crc_type\":\"CRC16-CCITT-FALSE\""));
  stream.print(F(",\"firmware_commit\":\""));
  stream.print(FirmwareInfo::GIT_COMMIT);
  stream.print(F("\",\"firmware_dirty\":"));
  stream.print(FirmwareInfo::GIT_DIRTY ? F("true") : F("false"));
  stream.print(F(",\"firmware_build_time_utc\":\""));
  stream.print(FirmwareInfo::BUILD_TIME_UTC);
  stream.print(F("\",\"firmware_build_environment\":\""));
  stream.print(FirmwareInfo::BUILD_ENVIRONMENT);
  stream.print(F("\",\"selftest_mode\":\""));
  stream.print(selfTestModeName());
  stream.print(F("\",\"registers\":{"));

  for (size_t i = 0; i < REGISTER_COUNT; ++i) {
    if (i != 0) {
      stream.print(',');
    }
    stream.print('"');
    printHexByte(stream, registerSnapshots_[i].address);
    stream.print(F("\":{\"address\":"));
    stream.print(registerSnapshots_[i].address);
    stream.print(F(",\"written\":"));
    stream.print(registerSnapshots_[i].written);
    stream.print(F(",\"readback\":"));
    stream.print(registerSnapshots_[i].readback);
    stream.print(F(",\"match\":"));
    stream.print(registerSnapshots_[i].matched ? F("true") : F("false"));
    stream.print('}');
  }
  stream.println(F("}}"));
}

void ADS1293Reader::printSelfTestStatusJson(Stream& stream) const {
  stream.print(F("{\"selftest_mode\":\""));
  stream.print(selfTestModeName());
  stream.print(F("\",\"expected_zero_code\":"));
  stream.print(Config::ADS1293_ZERO_CODE);
  stream.print(F(",\"expected_positive_code\":"));
  stream.print(Config::ADS1293_SELFTEST_POSITIVE_CODE);
  stream.print(F(",\"expected_negative_code\":"));
  stream.print(Config::ADS1293_SELFTEST_NEGATIVE_CODE);
  stream.println(F("}"));
}

void ADS1293Reader::printRawSample(Stream& stream,
                                   const RawSample& sample) const {
  stream.print(F("ads sample lead_i_code="));
  stream.print(sample.leadIRaw);
  stream.print(F(" lead_ii_code="));
  stream.print(sample.leadIIRaw);
  stream.print(F(" encoding=offset_binary_uint24 sample_ready="));
  stream.print(sample.dataReady ? F("yes") : F("no"));
  stream.print(F(" error_status="));
  printHexByte(stream, sample.errorStatus);
  stream.println();
}

bool ADS1293Reader::isConfigured() const {
  return configured_;
}

bool ADS1293Reader::isConfigurationVerified() const {
  return configurationVerified_;
}

bool ADS1293Reader::isAcquisitionEnabled() const {
  return acquisitionEnabled_;
}

bool ADS1293Reader::isDataReady() const {
  return dataReady_;
}

uint8_t ADS1293Reader::deviceId() const {
  return deviceId_;
}

uint8_t ADS1293Reader::errorStatus() const {
  return errorStatus_;
}

const char* ADS1293Reader::lastMessage() const {
  return lastMessage_;
}

const ADS1293Reader::RegisterSnapshot*
ADS1293Reader::registerSnapshots() const {
  return registerSnapshots_;
}

double ADS1293Reader::calculateNativeOdrHz(uint32_t afeResHz,
                                           uint8_t r1,
                                           uint8_t r2,
                                           uint8_t r3) {
  const uint32_t denominator =
      static_cast<uint32_t>(r1) * r2 * r3;
  return denominator == 0 ? 0.0
                          : static_cast<double>(afeResHz) / denominator;
}

uint32_t ADS1293Reader::decodeUInt24(const uint8_t* bytes) {
  if (bytes == nullptr) {
    return 0;
  }
  return composeUInt24(bytes[0], bytes[1], bytes[2]);
}

void ADS1293Reader::initializeRegisterSnapshots() {
  for (size_t i = 0; i < REGISTER_COUNT; ++i) {
    registerSnapshots_[i].address = INITIAL_REGISTERS[i].address;
    registerSnapshots_[i].written = INITIAL_REGISTERS[i].value;
    registerSnapshots_[i].readback = 0;
    registerSnapshots_[i].matched = false;
  }
}

bool ADS1293Reader::applyExplicitNativeConfiguration() {
#if ENABLE_ADS1293
  // CONFIG=0 must be first because START_CON locks writes to AFE/filter regs.
  bool ok = writeAndVerify(REG_CONFIG, CONVERSION_STOP);
  ok &= writeAndVerify(REG_AFE_SHDN_CN, AFE_ALL_SHUTDOWN);

  for (size_t i = 0; i < REGISTER_COUNT; ++i) {
    const uint8_t address = INITIAL_REGISTERS[i].address;
    if (address == REG_CONFIG || address == REG_AFE_SHDN_CN) {
      continue;
    }

    if (address == REG_OSC_CN) {
      // SNAS602C section 8.3.19 requires STRTCLK=0 while the clock source is
      // selected, then up to 15 ms for the crystal oscillator to start, and
      // only afterward STRTCLK=1.  First preserve the current source while
      // stopping propagation so an ESP-only reset is safe even if an earlier
      // session selected the external CLK input.  Then select the internal
      // oscillator/external-crystal source, wait with propagation stopped, and
      // finally expose the clock to the digital filters.
      uint8_t currentOsc = 0;
      const bool oscRead = readRegister(REG_OSC_CN, currentOsc);
      ok &= oscRead;
      if (oscRead) {
        ok &= writeAndVerify(
            REG_OSC_CN,
            static_cast<uint8_t>(currentOsc & OSC_SOURCE_AND_OUTPUT_MASK));
      }
      ok &= writeAndVerify(REG_OSC_CN, OSC_INTERNAL_CRYSTAL_CLOCK_STOPPED);
      delay(20);
      ok &= writeAndVerify(REG_OSC_CN, OSC_EXTERNAL_CRYSTAL);
      continue;
    }

    ok &= writeAndVerify(address, INITIAL_REGISTERS[i].value);
  }

  ok &= refreshConfigurationReadback();
  return ok;
#else
  return false;
#endif
}

bool ADS1293Reader::writeAndVerify(uint8_t address, uint8_t value) {
#if ENABLE_ADS1293
  RegisterSnapshot* snapshot = findRegisterSnapshot(address);
  if (snapshot != nullptr) {
    snapshot->written = value;
  }

  uint8_t readback = 0;
  const bool ok = writeRegister(address, value) &&
                  readRegister(address, readback) && readback == value;
  if (snapshot != nullptr) {
    snapshot->readback = readback;
    snapshot->matched = ok;
  }
  return ok;
#else
  (void)address;
  (void)value;
  return false;
#endif
}

bool ADS1293Reader::readTwoEcgChannels(uint32_t& leadI, uint32_t& leadII) {
#if ENABLE_ADS1293
  uint8_t bytes[6] = {};
  spi_->beginTransaction(
      SPISettings(Config::ADS1293_SPI_CLOCK_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(Config::ADS1293_SPI_CS_PIN, LOW);
  // CH_CNFG selects CH1 ECG followed by CH2 ECG. ADS1293 streaming mode
  // requires the special DATA_LOOP read address; it must not be replaced by
  // a random-access read starting at DATA_CH1_ECG.
  spi_->transfer(REG_DATA_LOOP | READ_REGISTER_FLAG);
  for (size_t i = 0; i < sizeof(bytes); ++i) {
    bytes[i] = spi_->transfer(0x00);
  }
  digitalWrite(Config::ADS1293_SPI_CS_PIN, HIGH);
  spi_->endTransaction();

  leadI = decodeUInt24(&bytes[0]);
  leadII = decodeUInt24(&bytes[3]);
  return true;
#else
  leadI = 0;
  leadII = 0;
  return false;
#endif
}

bool ADS1293Reader::refreshDataReadyStatus() {
#if ENABLE_ADS1293
  uint8_t status = 0;
  const bool ok = readRegister(REG_DATA_STATUS, status);
  registerDataReady_ = ok && (status & ECG_DATA_READY_MASK) != 0;
  drdyPinAsserted_ = digitalRead(Config::ADS1293_DRDY_PIN) == LOW;
  dataReady_ = registerDataReady_ || drdyPinAsserted_;
  return ok;
#else
  return false;
#endif
}

ADS1293Reader::RegisterSnapshot* ADS1293Reader::findRegisterSnapshot(
    uint8_t address) {
  for (size_t i = 0; i < REGISTER_COUNT; ++i) {
    if (registerSnapshots_[i].address == address) {
      return &registerSnapshots_[i];
    }
  }
  return nullptr;
}

const ADS1293Reader::RegisterSnapshot* ADS1293Reader::findRegisterSnapshot(
    uint8_t address) const {
  for (size_t i = 0; i < REGISTER_COUNT; ++i) {
    if (registerSnapshots_[i].address == address) {
      return &registerSnapshots_[i];
    }
  }
  return nullptr;
}

uint8_t ADS1293Reader::expectedFlexChannel1() const {
  return static_cast<uint8_t>(FLEX_CH1_NORMAL |
                              (static_cast<uint8_t>(selfTestMode_) << 6));
}

uint8_t ADS1293Reader::expectedFlexChannel2() const {
  return static_cast<uint8_t>(FLEX_CH2_NORMAL |
                              (static_cast<uint8_t>(selfTestMode_) << 6));
}

void ADS1293Reader::printHexByte(Stream& stream, uint8_t value) {
  stream.print(F("0x"));
  if (value < 0x10) {
    stream.print('0');
  }
  stream.print(value, HEX);
}

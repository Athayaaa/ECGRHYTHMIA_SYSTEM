#include "ADS1293Reader.h"

ADS1293Reader::ADS1293Reader()
#if ENABLE_ADS1293
    : ads_(Config::ADS1293_DRDY_PIN, Config::ADS1293_SPI_CS_PIN)
#endif
{}

bool ADS1293Reader::begin() {
#if ENABLE_ADS1293
  pinMode(Config::ADS1293_DRDY_PIN, INPUT_PULLUP);
  pinMode(Config::ADS1293_SPI_CS_PIN, OUTPUT);
  digitalWrite(Config::ADS1293_SPI_CS_PIN, HIGH);

  ads_.begin(Config::ADS1293_SPI_SCLK_PIN,
             Config::ADS1293_SPI_MISO_PIN,
             Config::ADS1293_SPI_MOSI_PIN);
  spiStarted_ = true;

  deviceId_ = ads_.readDeviceID();

  // Follow the official ProtoCentral ADS1293 3-lead reference:
  // CH1 = Lead I (LA-RA), CH2 = Lead II (LL-RA), RLD enabled.
  bool ok = true;
  ok &= ads_.configureChannel1(FlexCh1Mode::Default);
  ok &= ads_.configureChannel2(FlexCh2Mode::Default);
  ok &= ads_.enableCommonModeDetection(CMDetMode::Enabled);
  ok &= ads_.configureRLD(RLDMode::Default);
  ok &= ads_.configureOscillator(OscMode::Default);
  ok &= ads_.configureAFEShutdown(AFEShutdownMode::AllEnabled);
  ok &= ads_.setSamplingRate(ADS1293::SamplingRate::SPS_256);
  ok &= ads_.configureDRDYSource(DRDYSource::Default);
  ok &= ads_.configureChannelConfig(ChannelConfig::Default3Lead);
  ok &= ads_.configureAFEShutdown(AFEShutdownMode::AllShutdown);

  if (ok) {
    configured_ = true;
    acquisitionEnabled_ = false;
    lastMessage_ =
        "ADS1293 SPI ready; Lead I/Lead II/RLD configured; acquisition stopped";
  } else {
    configured_ = false;
    lastMessage_ = "ADS1293 configuration failed";
  }

  refreshStatus();
  return configured_;
#else
  spiStarted_ = false;
  configured_ = false;
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
  if (!configured_) {
    lastMessage_ = "ADS1293 is not configured; run setup again";
    return false;
  }

  bool ok = true;
  ok &= ads_.configureAFEShutdown(AFEShutdownMode::AllEnabled);
  ok &= ads_.applyGlobalConfig(GlobalConfig::Start);

  acquisitionEnabled_ = ok;
  lastMessage_ = ok
                     ? "ADS1293 acquisition started"
                     : "ADS1293 acquisition start failed";
  refreshStatus();
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

  const bool ok = ads_.configureAFEShutdown(AFEShutdownMode::AllShutdown);
  acquisitionEnabled_ = false;
  lastMessage_ = ok ? "ADS1293 acquisition stopped"
                    : "ADS1293 acquisition stop failed";
  refreshStatus();
  return ok;
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

  deviceId_ = ads_.readDeviceID();
  errorStatus_ = ads_.readErrorStatus();
  refreshDataReadyStatus();
  return true;
#else
  return false;
#endif
}

bool ADS1293Reader::readRawSample(RawSample& sample) {
  sample = RawSample();

#if ENABLE_ADS1293
  if (!spiStarted_ || !configured_) {
    lastMessage_ = "ADS1293 is not configured";
    refreshStatus();
    sample.dataReady = dataReady_;
    sample.errorStatus = errorStatus_;
    return false;
  }

  errorStatus_ = ads_.readErrorStatus();
  refreshDataReadyStatus();
  sample.dataReady = dataReady_;
  sample.errorStatus = errorStatus_;

  if (!sample.dataReady) {
    lastMessage_ = "ADS1293 data is not ready";
    return false;
  }

  const ADS1293::Samples ecg = ads_.getECGData();
  if (!ecg.ok) {
    lastMessage_ = "ADS1293 ECG data read failed";
    return false;
  }

  sample.leadIRaw = ecg.ch1;
  sample.leadIIRaw = ecg.ch2;
  sample.leadIIIRaw = sample.leadIIRaw - sample.leadIRaw;
  sample.errorStatus = ads_.readErrorStatus();
  errorStatus_ = sample.errorStatus;
  sample.ok = true;
  lastMessage_ = "ADS1293 raw sample read";
  return true;
#else
  lastMessage_ = "ADS1293 support is not enabled in this build";
  return false;
#endif
}

void ADS1293Reader::printStatus(Stream& stream) const {
  stream.println(F("ads1293:"));
  stream.print(F("  enabled="));
  stream.println(Config::ADS1293_ENABLED ? F("yes") : F("no"));
  stream.print(F("  spi="));
  stream.println(spiStarted_ ? F("started") : F("not_started"));
  stream.print(F("  configured="));
  stream.println(configured_ ? F("yes") : F("no"));
  stream.print(F("  acquisition="));
  stream.println(acquisitionEnabled_ ? F("on") : F("off"));
  stream.print(F("  sclk_gpio="));
  stream.println(Config::ADS1293_SPI_SCLK_PIN);
  stream.print(F("  miso_gpio="));
  stream.println(Config::ADS1293_SPI_MISO_PIN);
  stream.print(F("  mosi_gpio="));
  stream.println(Config::ADS1293_SPI_MOSI_PIN);
  stream.print(F("  cs_gpio="));
  stream.println(Config::ADS1293_SPI_CS_PIN);
  stream.print(F("  drdy_gpio="));
  stream.println(Config::ADS1293_DRDY_PIN);
  stream.print(F("  lead_i=CH1 LA-RA; lead_ii=CH2 LL-RA; lead_iii=Lead II - Lead I"));
  stream.println();
  stream.print(F("  configured_odr_hz="));
  stream.println(Config::ADS1293_CONFIGURED_ODR_HZ);
  stream.print(F("  target_model_sample_rate_hz="));
  stream.println(Config::ADS1293_TARGET_SAMPLE_RATE_HZ);
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
  stream.print(F("  data_ready="));
  stream.println(drdyPinAsserted_ ? F("yes") : F("no"));
  stream.print(F("  sample_ready="));
  stream.println(dataReady_ ? F("yes") : F("no"));
  if (deviceId_ == 0x00 || deviceId_ == 0xFF) {
    stream.println(F("  wiring_note=device_id 0x00/0xFF usually means ADS1293 is absent or SPI wiring is not ready"));
  }
  stream.print(F("  message="));
  stream.println(lastMessage_);
}

void ADS1293Reader::printRawSample(Stream& stream,
                                   const RawSample& sample) const {
  stream.print(F("ads sample lead_i_raw="));
  stream.print(static_cast<long>(sample.leadIRaw));
  stream.print(F(" lead_ii_raw="));
  stream.print(static_cast<long>(sample.leadIIRaw));
  stream.print(F(" lead_iii_raw="));
  stream.print(static_cast<long>(sample.leadIIIRaw));
  stream.print(F(" sample_ready="));
  stream.print(sample.dataReady ? F("yes") : F("no"));
  stream.print(F(" error_status="));
  printHexByte(stream, sample.errorStatus);
  stream.println();
}

bool ADS1293Reader::isConfigured() const {
  return configured_;
}

bool ADS1293Reader::isAcquisitionEnabled() const {
  return acquisitionEnabled_;
}

const char* ADS1293Reader::lastMessage() const {
  return lastMessage_;
}

bool ADS1293Reader::refreshDataReadyStatus() {
#if ENABLE_ADS1293
  registerDataReady_ = ads_.isDataReady();
  drdyPinAsserted_ = digitalRead(Config::ADS1293_DRDY_PIN) == LOW;
  dataReady_ = registerDataReady_ || drdyPinAsserted_;
  return true;
#else
  return false;
#endif
}

void ADS1293Reader::printHexByte(Stream& stream, uint8_t value) {
  stream.print(F("0x"));
  if (value < 0x10) {
    stream.print('0');
  }
  stream.print(value, HEX);
}

#include <Arduino.h>

#include "ADS1293DrdyAccounting.h"
#include "ADS1293Reader.h"
#include "DummyECGGenerator.h"
#include "ECGPacket.h"
#include "FirmwareInfo.h"
#include "config.h"

#if !ECG_OUTPUT_USB
HardwareSerial ecgSerial(Config::ECG_UART_NUM);
#endif

#if ENABLE_ADS1293
ADS1293Reader adsReader;

portMUX_TYPE adsDrdyMux = portMUX_INITIALIZER_UNLOCKED;
volatile uint32_t adsDrdyPending = 0;
volatile uint32_t adsDrdyEventCount = 0;
volatile uint32_t adsDrdyOverrunCount = 0;
volatile uint32_t adsDrdyMaxPending = 0;
volatile uint32_t adsDrdyUnaccountedOverruns = 0;

bool adsDrdyInterruptAttached = false;
bool adsDebugEnabled = false;
uint32_t nextAdsDebugTimeMs = 0;
uint32_t adsSamplesRead = 0;
uint32_t adsSampleReadFailures = 0;
uint32_t adsAmbiguousReadsDiscarded = 0;
uint32_t adsPacketTransmissionFailures = 0;
uint32_t adsLastPacketOverrunCount = 0;
uint32_t nextAdsErrorStatusPollMs = 0;
bool adsPreviousTransmissionFailed = false;
uint32_t adsDrdyBoundaryPrimeReads = 0;
uint32_t adsDrdyBoundaryFailures = 0;
const char* adsDrdyBoundaryMessage = "not_run";
#endif

DummyECGGenerator dummyGenerator(Config::ECG_SAMPLE_RATE_HZ);

bool streamingEnabled = Config::STREAM_ENABLED_DEFAULT;
bool simulationEnabled = Config::SIMULATION_ENABLED_DEFAULT;
uint32_t sampleSequence = 0;
uint32_t nextSampleTimeUs = 0;
uint32_t lastPacketSentTimeMs = 0;
uint32_t heartBlinkCycleStartMs = 0;
bool heartBlinkLedOn = false;
String commandBuffer;

#if ENABLE_ADS1293
struct AdsDrdySnapshot {
  uint32_t pending = 0;
  uint32_t events = 0;
  uint32_t overruns = 0;
  uint32_t maxPending = 0;
};

struct AdsDrdyClaim {
  uint32_t nativeSequence = 0;
  uint32_t pendingAtClaim = 0;
  uint32_t pendingAfterClaim = 0;
  uint32_t missedBeforeRead = 0;
  uint32_t overruns = 0;
  uint32_t maxPending = 0;
};

void IRAM_ATTR onAdsDataReady() {
  portENTER_CRITICAL_ISR(&adsDrdyMux);
  ++adsDrdyEventCount;
  if (adsDrdyPending < Config::ADS1293_MAX_PENDING_DRDY) {
    ++adsDrdyPending;
    if (adsDrdyPending > adsDrdyMaxPending) {
      adsDrdyMaxPending = adsDrdyPending;
    }
  } else {
    // Preserve an exact count even after the bounded pending counter fills.
    ++adsDrdyOverrunCount;
    ++adsDrdyUnaccountedOverruns;
  }
  portEXIT_CRITICAL_ISR(&adsDrdyMux);
}

AdsDrdySnapshot snapshotAdsDrdy() {
  AdsDrdySnapshot snapshot;
  portENTER_CRITICAL(&adsDrdyMux);
  snapshot.pending = adsDrdyPending;
  snapshot.events = adsDrdyEventCount;
  snapshot.overruns = adsDrdyOverrunCount;
  snapshot.maxPending = adsDrdyMaxPending;
  portEXIT_CRITICAL(&adsDrdyMux);
  return snapshot;
}

void resetAdsAcquisitionStatistics() {
  portENTER_CRITICAL(&adsDrdyMux);
  adsDrdyPending = 0;
  adsDrdyEventCount = 0;
  adsDrdyOverrunCount = 0;
  adsDrdyMaxPending = 0;
  adsDrdyUnaccountedOverruns = 0;
  portEXIT_CRITICAL(&adsDrdyMux);

  sampleSequence = 0;
  adsSamplesRead = 0;
  adsSampleReadFailures = 0;
  adsAmbiguousReadsDiscarded = 0;
  adsPacketTransmissionFailures = 0;
  adsLastPacketOverrunCount = 0;
  nextAdsErrorStatusPollMs = millis() + 1000;
  adsPreviousTransmissionFailed = false;
}

bool claimAdsDrdyEvent(AdsDrdyClaim& claim) {
  claim = AdsDrdyClaim();
  uint32_t unrepresentedEvents = 0;
  portENTER_CRITICAL(&adsDrdyMux);
  if (adsDrdyPending == 0) {
    portEXIT_CRITICAL(&adsDrdyMux);
    return false;
  }
  claim.pendingAtClaim = adsDrdyPending;
  unrepresentedEvents = adsDrdyUnaccountedOverruns;
  claim.missedBeforeRead = ADS1293DrdyAccounting::missedEvents(
      claim.pendingAtClaim, unrepresentedEvents);
  adsDrdyPending = 0;
  adsDrdyUnaccountedOverruns = 0;
  claim.pendingAfterClaim = 0;
  // Events beyond the bounded counter were counted by the ISR.  Account here
  // for the older represented events that were also replaced in the ADS1293's
  // latest-only ECG output registers.
  adsDrdyOverrunCount += claim.pendingAtClaim - 1;
  claim.overruns = adsDrdyOverrunCount;
  claim.maxPending = adsDrdyMaxPending;
  portEXIT_CRITICAL(&adsDrdyMux);

  // Read only the newest conversion and assign its actual native-event
  // sequence.  All replaced conversions become a receiver-visible gap.
  claim.nativeSequence = ADS1293DrdyAccounting::latestSequence(
      sampleSequence, claim.pendingAtClaim, unrepresentedEvents);
  sampleSequence = ADS1293DrdyAccounting::sequenceAfterClaim(
      sampleSequence, claim.pendingAtClaim, unrepresentedEvents);
  return true;
}

bool finishAdsDrdyRead(AdsDrdyClaim& claim) {
  portENTER_CRITICAL(&adsDrdyMux);
  claim.pendingAfterClaim = adsDrdyPending;
  const bool ambiguous = ADS1293DrdyAccounting::readIsAmbiguous(
      claim.pendingAfterClaim);
  if (ambiguous) {
    // The just-read claimed conversion is deliberately not emitted because a
    // newer edge may have replaced it.  Keep every new event pending for the
    // next claim, but count the discarded conversion as one exact overrun.
    adsDrdyOverrunCount +=
        ADS1293DrdyAccounting::ambiguousReadLoss(claim.pendingAfterClaim);
  }
  claim.overruns = adsDrdyOverrunCount;
  claim.maxPending = adsDrdyMaxPending;
  portEXIT_CRITICAL(&adsDrdyMux);
  return !ambiguous;
}

bool ensureAdsDrdyInterruptAttached() {
  if (adsDrdyInterruptAttached) {
    return true;
  }
  const int interruptNumber =
      digitalPinToInterrupt(Config::ADS1293_DRDY_PIN);
  if (interruptNumber == NOT_AN_INTERRUPT) {
    return false;
  }
  attachInterrupt(interruptNumber, onAdsDataReady, FALLING);
  adsDrdyInterruptAttached = true;
  return true;
}

bool synchronizeAdsDrdyInterruptBoundary() {
  // DRDYB can already be asserted when a falling-edge interrupt is attached,
  // especially after CONFIG was stopped with unread output data.  Waiting for
  // another falling edge in that state deadlocks: the firmware waits for the
  // ISR while the ADS1293 waits for an ECG read to release DRDYB.  Discard one
  // startup conversion, prove that the following physical falling edge reaches
  // the ISR, then establish a clean sequence-zero boundary.
  bool discardedStartupSample = false;
  if (!adsReader.primeDataReadyForInterrupt(60, discardedStartupSample)) {
    ++adsDrdyBoundaryFailures;
    adsDrdyBoundaryMessage = adsReader.lastMessage();
    return false;
  }
  if (discardedStartupSample) {
    ++adsDrdyBoundaryPrimeReads;
  }

  resetAdsAcquisitionStatistics();
  const uint32_t edgeDeadlineMs = millis() + 20U;
  while (snapshotAdsDrdy().events == 0 &&
         static_cast<int32_t>(millis() - edgeDeadlineMs) < 0) {
    yield();
  }

  AdsDrdyClaim proofClaim;
  if (!claimAdsDrdyEvent(proofClaim)) {
    ++adsDrdyBoundaryFailures;
    adsDrdyBoundaryMessage =
        "no falling-edge interrupt observed after DRDY priming read";
    return false;
  }

  ADS1293Reader::RawSample proofSample;
  const bool readOk = adsReader.readRawSample(proofSample);
  const bool unambiguous = finishAdsDrdyRead(proofClaim);
  if (!readOk || !unambiguous) {
    ++adsDrdyBoundaryFailures;
    adsDrdyBoundaryMessage = !readOk
                                 ? "proof ECG read failed after DRDY edge"
                                 : "DRDY advanced during boundary proof read";
    return false;
  }
  ++adsDrdyBoundaryPrimeReads;

  // The proof sample is intentionally outside the protocol stream.  Its read
  // leaves DRDYB high, so the next conversion produces a real falling edge.
  resetAdsAcquisitionStatistics();
  adsDrdyBoundaryMessage = "verified";
  return true;
}

#endif

Stream& ecgOutputStream() {
#if ECG_OUTPUT_USB
  return Serial;
#else
  return ecgSerial;
#endif
}

void beginEcgOutputTransport() {
#if !ECG_OUTPUT_USB
  ecgSerial.begin(Config::ECG_UART_BAUD,
                  SERIAL_8N1,
                  Config::ECG_UART_RX_PIN,
                  Config::ECG_UART_TX_PIN);
#endif
}

void setHeartBlinkLed(bool on) {
  if (!Config::HEART_BLINK_ENABLED) {
    return;
  }
  digitalWrite(Config::HEART_BLINK_GPIO, on ? HIGH : LOW);
  heartBlinkLedOn = on;
}

void beginHeartBlinkIndicator() {
  if (!Config::HEART_BLINK_ENABLED) {
    return;
  }
  pinMode(Config::HEART_BLINK_GPIO, OUTPUT);
  setHeartBlinkLed(false);
  heartBlinkCycleStartMs = millis();
}

void notePacketSent() {
  if (!Config::HEART_BLINK_ENABLED) {
    return;
  }
  const uint32_t nowMs = millis();
  lastPacketSentTimeMs = nowMs;
  if (heartBlinkCycleStartMs == 0) {
    heartBlinkCycleStartMs = nowMs;
  }
}

void pollHeartBlinkIndicator() {
  if (!Config::HEART_BLINK_ENABLED) {
    return;
  }

  const uint32_t nowMs = millis();
  const bool packetRecentlySent =
      lastPacketSentTimeMs != 0 &&
      static_cast<int32_t>(nowMs - lastPacketSentTimeMs) <=
          static_cast<int32_t>(Config::HEART_BLINK_ACTIVE_TIMEOUT_MS);

  if (!packetRecentlySent) {
    setHeartBlinkLed(false);
    heartBlinkCycleStartMs = nowMs;
    return;
  }

  if (static_cast<int32_t>(nowMs - heartBlinkCycleStartMs) >=
      static_cast<int32_t>(Config::HEART_BLINK_PERIOD_MS)) {
    heartBlinkCycleStartMs = nowMs;
  }

  const bool pulseOn =
      static_cast<int32_t>(nowMs - heartBlinkCycleStartMs) <
      static_cast<int32_t>(Config::HEART_BLINK_ON_MS);
  if (pulseOn != heartBlinkLedOn) {
    setHeartBlinkLed(pulseOn);
  }
}

void printStatus() {
  Serial.println();
  Serial.println(F("ECGRHYTHMIA ESP32 firmware"));
  Serial.print(F("board="));
  Serial.println(Config::BOARD_NAME);
  Serial.print(F("source="));
#if ENABLE_ADS1293
  Serial.println(F("ads1293"));
#else
  Serial.println(F("dummy_ecg"));
#endif
  Serial.print(F("output="));
  Serial.println(Config::ECG_OUTPUT_USB_ENABLED ? F("usb_serial")
                                                : F("uart2"));
  Serial.print(F("protocol_version="));
  Serial.println(FirmwareInfo::PROTOCOL_VERSION);
  Serial.print(F("firmware_commit="));
  Serial.println(FirmwareInfo::GIT_COMMIT);
  Serial.print(F("firmware_dirty="));
  Serial.println(FirmwareInfo::GIT_DIRTY ? F("yes") : F("no"));
  Serial.print(F("firmware_build_time_utc="));
  Serial.println(FirmwareInfo::BUILD_TIME_UTC);
  Serial.print(F("firmware_build_environment="));
  Serial.println(FirmwareInfo::BUILD_ENVIRONMENT);
  Serial.print(F("binary_stream="));
  Serial.println(streamingEnabled ? F("on") : F("off"));
  Serial.print(F("packet_bytes="));
  Serial.println(ECGPacket::PACKET_SIZE);
  Serial.print(F("heart_blink_gpio="));
  if (Config::HEART_BLINK_ENABLED) {
    Serial.println(Config::HEART_BLINK_GPIO);
  } else {
    Serial.println(F("disabled"));
  }

#if ENABLE_ADS1293
  adsReader.refreshStatus();
  adsReader.printStatus(Serial);
  const AdsDrdySnapshot stats = snapshotAdsDrdy();
  Serial.print(F("drdy_events="));
  Serial.println(stats.events);
  Serial.print(F("drdy_pending="));
  Serial.println(stats.pending);
  Serial.print(F("drdy_overruns="));
  Serial.println(stats.overruns);
  Serial.print(F("drdy_max_pending="));
  Serial.println(stats.maxPending);
  Serial.print(F("samples_read="));
  Serial.println(adsSamplesRead);
  Serial.print(F("sample_read_failures="));
  Serial.println(adsSampleReadFailures);
  Serial.print(F("ambiguous_reads_discarded="));
  Serial.println(adsAmbiguousReadsDiscarded);
  Serial.print(F("packet_tx_failures="));
  Serial.println(adsPacketTransmissionFailures);
  Serial.print(F("drdy_boundary_prime_reads="));
  Serial.println(adsDrdyBoundaryPrimeReads);
  Serial.print(F("drdy_boundary_failures="));
  Serial.println(adsDrdyBoundaryFailures);
  Serial.print(F("drdy_boundary_status="));
  Serial.println(adsDrdyBoundaryMessage);
  Serial.print(F("serial_utilization_percent="));
  const uint32_t transportBaud = Config::ECG_OUTPUT_USB_ENABLED
                                     ? Config::USB_SERIAL_BAUD
                                     : Config::ECG_UART_BAUD;
  const double utilization =
      100.0 * ECGPacket::PACKET_SIZE * 10.0 *
      Config::ADS1293_NATIVE_RATE_NUMERATOR /
      (Config::ADS1293_NATIVE_RATE_DENOMINATOR * transportBaud);
  Serial.println(utilization, 3);
  Serial.print(F("debug="));
  Serial.println(adsDebugEnabled ? F("on") : F("off"));
  Serial.println(F("commands: ads status, ads start, ads stop, ads read, ads config dump, ads selftest zero|positive|negative|off|status, ads debug on|off, ads stream start|stop, status"));
#else
  Serial.print(F("sim="));
  Serial.println(simulationEnabled ? F("on") : F("off"));
  Serial.print(F("sample_rate_hz="));
  Serial.println(Config::ECG_SAMPLE_RATE_HZ);
  Serial.println(F("commands: start, stop, status, sim on, sim off"));
#endif
}

#if ENABLE_ADS1293
bool stopAdsBinaryStreamForText() {
  if (!streamingEnabled) {
    return false;
  }
  streamingEnabled = false;
  ecgOutputStream().flush();
  return true;
}

bool startAdsAcquisitionIfNeeded() {
  if (!adsReader.isConfigured() ||
      !adsReader.isConfigurationVerified()) {
    Serial.println(F("ADS1293 not verified; retrying explicit setup"));
    if (!adsReader.begin()) {
      Serial.println(adsReader.lastMessage());
      return false;
    }
    Serial.println(adsReader.lastMessage());
  }

  if (!ensureAdsDrdyInterruptAttached()) {
    Serial.println(F("ADS1293 DRDY pin does not map to an interrupt"));
    return false;
  }

  if (adsReader.isAcquisitionEnabled()) {
    return true;
  }

  resetAdsAcquisitionStatistics();
  if (!adsReader.startAcquisition()) {
    Serial.println(adsReader.lastMessage());
    return false;
  }
  if (!synchronizeAdsDrdyInterruptBoundary()) {
    Serial.print(F("ADS1293 DRDY startup synchronization failed: "));
    Serial.println(adsDrdyBoundaryMessage);
    adsReader.stopAcquisition();
    return false;
  }
  Serial.println(adsReader.lastMessage());
  return true;
}

void printAdsSampleUnavailable(const __FlashStringHelper* prefix,
                               const ADS1293Reader::RawSample& sample) {
  Serial.print(prefix);
  Serial.print(F(": "));
  Serial.println(adsReader.lastMessage());
  Serial.print(prefix);
  Serial.print(F(" state sample_ready="));
  Serial.print(sample.dataReady ? F("yes") : F("no"));
  Serial.print(F(" error_status=0x"));
  if (sample.errorStatus < 0x10) {
    Serial.print('0');
  }
  Serial.println(sample.errorStatus, HEX);
}

void printAdsRawSampleOnce(const __FlashStringHelper* prefix) {
  if (!startAdsAcquisitionIfNeeded()) {
    return;
  }

  AdsDrdyClaim claim;
  const uint32_t deadlineMs = millis() + 20;
  bool claimed = claimAdsDrdyEvent(claim);
  while (!claimed && static_cast<int32_t>(millis() - deadlineMs) < 0) {
    yield();
    claimed = claimAdsDrdyEvent(claim);
  }

  if (!claimed) {
    Serial.println(F("ads read: no DRDY event received within 20 ms"));
    return;
  }

  ADS1293Reader::RawSample sample;
  const bool readOk = adsReader.readRawSample(sample);
  const bool unambiguous = finishAdsDrdyRead(claim);
  if (!readOk) {
    ++adsSampleReadFailures;
    printAdsSampleUnavailable(prefix, sample);
  } else if (!unambiguous) {
    ++adsSamplesRead;
    ++adsAmbiguousReadsDiscarded;
    Serial.println(
        F("ads read: discarded because DRDY advanced during the SPI read"));
  } else {
    ++adsSamplesRead;
    adsReader.printRawSample(Serial, sample);
  }
}

void probeAdsDrdyPin() {
  stopAdsBinaryStreamForText();
  adsDebugEnabled = false;
  adsReader.stopAcquisition();
  if (!ensureAdsDrdyInterruptAttached()) {
    Serial.println(F("{\"ok\":false,\"error\":\"DRDY pin has no interrupt\"}"));
    return;
  }

  uint8_t drdySourceDisabledReadback = 0xFF;
  const bool sourceDisableOk =
      adsReader.writeRegister(0x27, 0x00) &&
      adsReader.readRegister(0x27, drdySourceDisabledReadback) &&
      drdySourceDisabledReadback == 0x00;
  delay(2);
  const bool highWithSourceDisabled =
      digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
  pinMode(Config::ADS1293_DRDY_PIN, INPUT_PULLUP);
  delay(2);
  const bool highWithInternalPullup =
      digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
  pinMode(Config::ADS1293_DRDY_PIN, INPUT);
  delay(2);
  uint8_t drdySourceRestoredReadback = 0xFF;
  const bool sourceRestoreOk =
      adsReader.writeRegister(0x27, 0x08) &&
      adsReader.readRegister(0x27, drdySourceRestoredReadback) &&
      drdySourceRestoredReadback == 0x08;
  if (!sourceDisableOk || !sourceRestoreOk ||
      !adsReader.refreshConfigurationReadback()) {
    Serial.println(F("{\"ok\":false,\"error\":\"DRDY source control/readback failed\"}"));
    return;
  }

  resetAdsAcquisitionStatistics();
  if (!adsReader.startAcquisition()) {
    Serial.print(F("{\"ok\":false,\"error\":\""));
    Serial.print(adsReader.lastMessage());
    Serial.println(F("\"}"));
    return;
  }

  constexpr uint32_t PROBE_DURATION_US = 250000U;
  const uint32_t startedUs = micros();
  bool previousHigh = digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
  const bool initialHigh = previousHigh;
  uint32_t highReads = 0;
  uint32_t lowReads = 0;
  uint32_t risingEdges = 0;
  uint32_t fallingEdges = 0;
  while (static_cast<uint32_t>(micros() - startedUs) < PROBE_DURATION_US) {
    const bool high = digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
    if (high) {
      ++highReads;
    } else {
      ++lowReads;
    }
    if (high != previousHigh) {
      if (high) {
        ++risingEdges;
      } else {
        ++fallingEdges;
      }
      previousHigh = high;
    }
  }

  const bool finalHigh = digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
  const AdsDrdySnapshot interruptSnapshot = snapshotAdsDrdy();
  uint8_t maskErrorReadback = 0xFF;
  const bool maskChannelErrorsOk =
      adsReader.writeRegister(0x2A, 0x30) &&
      adsReader.readRegister(0x2A, maskErrorReadback) &&
      maskErrorReadback == 0x30;
  delay(2);
  const bool highWithChannelErrorsMasked =
      digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
  const bool restoreErrorMaskOk = adsReader.writeRegister(0x2A, 0x00);
  delay(2);
  const bool highAfterErrorMaskRestore =
      digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
  uint8_t firstLoopByte = 0;
  const bool shortLoopReadOk = adsReader.readRegister(0x50, firstLoopByte);
  const bool highImmediatelyAfterLoopRead =
      digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
  delayMicroseconds(500);
  const bool high500UsAfterLoopRead =
      digitalRead(Config::ADS1293_DRDY_PIN) == HIGH;
  uint8_t dataStatus = 0;
  uint8_t errorStatus = 0;
  const bool dataStatusOk = adsReader.readRegister(0x30, dataStatus);
  const bool errorStatusOk = adsReader.readRegister(0x19, errorStatus);
  adsReader.stopAcquisition();

  Serial.print(F("{\"ok\":true,\"duration_us\":"));
  Serial.print(PROBE_DURATION_US);
  Serial.print(F(",\"gpio\":"));
  Serial.print(Config::ADS1293_DRDY_PIN);
  Serial.print(F(",\"source_disabled_readback\":"));
  Serial.print(drdySourceDisabledReadback);
  Serial.print(F(",\"high_with_source_disabled\":"));
  Serial.print(highWithSourceDisabled ? F("true") : F("false"));
  Serial.print(F(",\"high_with_internal_pullup\":"));
  Serial.print(highWithInternalPullup ? F("true") : F("false"));
  Serial.print(F(",\"source_restored_readback\":"));
  Serial.print(drdySourceRestoredReadback);
  Serial.print(F(",\"initial_high\":"));
  Serial.print(initialHigh ? F("true") : F("false"));
  Serial.print(F(",\"final_high\":"));
  Serial.print(finalHigh ? F("true") : F("false"));
  Serial.print(F(",\"high_reads\":"));
  Serial.print(highReads);
  Serial.print(F(",\"low_reads\":"));
  Serial.print(lowReads);
  Serial.print(F(",\"polled_rising_edges\":"));
  Serial.print(risingEdges);
  Serial.print(F(",\"polled_falling_edges\":"));
  Serial.print(fallingEdges);
  Serial.print(F(",\"isr_events\":"));
  Serial.print(interruptSnapshot.events);
  Serial.print(F(",\"mask_channel_errors_ok\":"));
  Serial.print(maskChannelErrorsOk ? F("true") : F("false"));
  Serial.print(F(",\"mask_error_readback\":"));
  Serial.print(maskErrorReadback);
  Serial.print(F(",\"high_with_channel_errors_masked\":"));
  Serial.print(highWithChannelErrorsMasked ? F("true") : F("false"));
  Serial.print(F(",\"restore_error_mask_ok\":"));
  Serial.print(restoreErrorMaskOk ? F("true") : F("false"));
  Serial.print(F(",\"high_after_error_mask_restore\":"));
  Serial.print(highAfterErrorMaskRestore ? F("true") : F("false"));
  Serial.print(F(",\"short_loop_read_ok\":"));
  Serial.print(shortLoopReadOk ? F("true") : F("false"));
  Serial.print(F(",\"first_loop_byte\":"));
  Serial.print(firstLoopByte);
  Serial.print(F(",\"high_immediately_after_loop_read\":"));
  Serial.print(highImmediatelyAfterLoopRead ? F("true") : F("false"));
  Serial.print(F(",\"high_500us_after_loop_read\":"));
  Serial.print(high500UsAfterLoopRead ? F("true") : F("false"));
  Serial.print(F(",\"data_status\":"));
  Serial.print(dataStatusOk ? dataStatus : 0);
  Serial.print(F(",\"error_status\":"));
  Serial.print(errorStatusOk ? errorStatus : 0);
  Serial.println(F("}"));
}

void startAdsBinaryStream() {
  stopAdsBinaryStreamForText();
  if (adsDebugEnabled) {
    adsDebugEnabled = false;
    Serial.println(F("ads debug disabled before binary stream start"));
  }
  if (!startAdsAcquisitionIfNeeded()) {
    return;
  }

  Serial.println(F("ads binary stream preparing: protocol=2 native_rate=800/3 packet_bytes=32 encoding=offset_binary_uint24"));
  Serial.flush();
  // Text output takes several native periods.  Clear the currently asserted
  // sample and prove one subsequent falling edge only after the final text byte
  // has left the transport.  No text may be emitted after this clean boundary.
  if (!synchronizeAdsDrdyInterruptBoundary()) {
    Serial.print(F("ADS1293 binary stream start failed: "));
    Serial.println(adsDrdyBoundaryMessage);
    adsReader.stopAcquisition();
    return;
  }
  streamingEnabled = true;
}

void stopAdsBinaryStream(bool alreadyStoppedForCommand) {
  const bool wasStreaming =
      alreadyStoppedForCommand || stopAdsBinaryStreamForText();
  Serial.println(wasStreaming ? F("ads binary stream stopped")
                              : F("ads binary stream already stopped"));
}

void stopAllAdsOutput() {
  stopAdsBinaryStreamForText();
  adsDebugEnabled = false;
  adsReader.stopAcquisition();
  Serial.println(F("ads stopped: acquisition=off debug=off binary_stream=off"));
}

void handleAdsSelfTestCommand(const String& command) {
  if (command == "ads selftest status") {
    adsReader.printSelfTestStatusJson(Serial);
    return;
  }

  ADS1293Reader::SelfTestMode mode = ADS1293Reader::SelfTestMode::Off;
  bool recognized = true;
  if (command == "ads selftest zero") {
    mode = ADS1293Reader::SelfTestMode::Zero;
  } else if (command == "ads selftest positive") {
    mode = ADS1293Reader::SelfTestMode::Positive;
  } else if (command == "ads selftest negative") {
    mode = ADS1293Reader::SelfTestMode::Negative;
  } else if (command == "ads selftest off") {
    mode = ADS1293Reader::SelfTestMode::Off;
  } else {
    recognized = false;
  }

  if (!recognized) {
    Serial.println(F("{\"ok\":false,\"error\":\"unknown selftest mode\"}"));
    return;
  }

  const bool restartAcquisition = adsReader.isAcquisitionEnabled();
  if (restartAcquisition && !adsReader.stopAcquisition()) {
    Serial.print(F("{\"ok\":false,\"error\":\""));
    Serial.print(adsReader.lastMessage());
    Serial.println(F("\"}"));
    return;
  }
  resetAdsAcquisitionStatistics();
  if (!adsReader.setSelfTestMode(mode)) {
    Serial.print(F("{\"ok\":false,\"error\":\""));
    Serial.print(adsReader.lastMessage());
    Serial.println(F("\"}"));
    return;
  }

  resetAdsAcquisitionStatistics();
  if (restartAcquisition && !adsReader.startAcquisition()) {
    Serial.print(F("{\"ok\":false,\"error\":\""));
    Serial.print(adsReader.lastMessage());
    Serial.println(F("\"}"));
    return;
  }
  adsReader.printSelfTestStatusJson(Serial);
}
#endif

void handleCommand(const String& rawCommand) {
  String command = rawCommand;
  command.trim();
  command.toLowerCase();
  if (command.length() == 0) {
    return;
  }

#if ENABLE_ADS1293
  // Text responses and binary data are never interleaved.
  const bool adsBinaryStoppedForCommand = stopAdsBinaryStreamForText();
#endif

  if (command == "start") {
#if ENABLE_ADS1293
    Serial.println(F("ADS1293 build: use 'ads stream start' for V2 packets"));
#else
    streamingEnabled = true;
    nextSampleTimeUs = micros();
    Serial.println(F("stream started"));
#endif
  } else if (command == "stop") {
    streamingEnabled = false;
    Serial.println(F("stream stopped"));
  } else if (command == "status") {
    printStatus();
#if ENABLE_ADS1293
  } else if (command == "ads status") {
    printStatus();
  } else if (command == "ads config dump") {
    adsReader.printConfigDumpJson(Serial);
  } else if (command == "ads start") {
    streamingEnabled = false;
    if (startAdsAcquisitionIfNeeded()) {
      nextAdsDebugTimeMs = millis();
    }
  } else if (command == "ads stop") {
    stopAllAdsOutput();
  } else if (command == "ads read") {
    printAdsRawSampleOnce(F("ads read"));
  } else if (command == "ads drdy probe") {
    probeAdsDrdyPin();
  } else if (command == "ads debug on") {
    if (startAdsAcquisitionIfNeeded()) {
      adsDebugEnabled = true;
      nextAdsDebugTimeMs = millis();
      Serial.println(F("ads debug enabled: one of the continuously acquired native samples prints at 8 Hz"));
    }
  } else if (command == "ads debug off") {
    adsDebugEnabled = false;
    Serial.println(F("ads debug disabled"));
  } else if (command == "ads stream start") {
    startAdsBinaryStream();
  } else if (command == "ads stream stop") {
    stopAdsBinaryStream(adsBinaryStoppedForCommand);
  } else if (command.startsWith("ads selftest ")) {
    handleAdsSelfTestCommand(command);
#endif
  } else if (command == "sim on") {
#if ENABLE_ADS1293
    Serial.println(F("simulation commands are available in dummy builds only"));
#else
    simulationEnabled = true;
    Serial.println(F("simulation enabled"));
#endif
  } else if (command == "sim off") {
#if ENABLE_ADS1293
    Serial.println(F("simulation commands are available in dummy builds only"));
#else
    simulationEnabled = false;
    Serial.println(F("simulation disabled; streaming sends zero-valued leads"));
#endif
  } else {
    Serial.print(F("unknown command: "));
    Serial.println(rawCommand);
#if ENABLE_ADS1293
    Serial.println(F("commands: ads status, ads start, ads stop, ads read, ads config dump, ads selftest zero|positive|negative|off|status, ads debug on|off, ads stream start|stop, status"));
#else
    Serial.println(F("commands: start, stop, status, sim on, sim off"));
#endif
  }
}

void pollUsbCommands() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n' || c == '\r') {
      if (commandBuffer.length() > 0) {
        handleCommand(commandBuffer);
        commandBuffer = "";
      }
    } else if (commandBuffer.length() < 127) {
      commandBuffer += c;
    } else {
      commandBuffer = "";
    }
  }
}

#if ENABLE_ADS1293
void drainAdsDataReadyEvents() {
  if (!adsReader.isAcquisitionEnabled()) {
    return;
  }

  AdsDrdyClaim claim;
  while (claimAdsDrdyEvent(claim)) {
    ADS1293Reader::RawSample sample;
    const uint32_t readTimestampUs = micros();
    const bool readOk = adsReader.readRawSample(sample);
    const bool unambiguous = finishAdsDrdyRead(claim);
    if (!readOk) {
      ++adsSampleReadFailures;
      continue;
    }
    ++adsSamplesRead;
    if (!unambiguous) {
      ++adsAmbiguousReadsDiscarded;
      continue;
    }

    if (adsDebugEnabled && !streamingEnabled) {
      const uint32_t nowMs = millis();
      if (static_cast<int32_t>(nowMs - nextAdsDebugTimeMs) >= 0) {
        nextAdsDebugTimeMs = nowMs + Config::ADS1293_DEBUG_INTERVAL_MS;
        adsReader.printRawSample(Serial, sample);
      }
    }

    if (!streamingEnabled || adsDebugEnabled) {
      continue;
    }

    uint8_t flags = 0;
    if (adsReader.isConfigurationVerified()) {
      flags |= ECGPacket::V2_CONFIG_VERIFIED;
    }
    if (sample.errorStatus != 0) {
      flags |= ECGPacket::V2_ADS_ERROR;
    }
    if (claim.missedBeforeRead != 0 ||
        claim.overruns != adsLastPacketOverrunCount) {
      flags |= ECGPacket::V2_DRDY_BACKLOG_OR_OVERRUN;
    }
    if (sample.leadIRaw > Config::ADS1293_ADCMAX ||
        sample.leadIIRaw > Config::ADS1293_ADCMAX) {
      flags |= ECGPacket::V2_CODE_OUT_OF_RANGE;
    }
    if (adsPreviousTransmissionFailed) {
      flags |= ECGPacket::V2_PREVIOUS_TX_FAILURE;
    }
    if (adsReader.selfTestMode() != ADS1293Reader::SelfTestMode::Off) {
      flags |= ECGPacket::V2_SELF_TEST_ACTIVE;
    }

    const uint32_t diagnostic =
        static_cast<uint32_t>(sample.errorStatus) |
        (static_cast<uint32_t>(
             ADS1293DrdyAccounting::saturateDiagnosticByte(
                 claim.pendingAfterClaim))
         << 8) |
        (static_cast<uint32_t>(
             ADS1293DrdyAccounting::saturateDiagnosticByte(claim.maxPending))
         << 16) |
        (static_cast<uint32_t>(
             ADS1293DrdyAccounting::saturateDiagnosticByte(claim.overruns))
         << 24);

    const bool sent = ECGPacket::writeNativeSampleV2(ecgOutputStream(),
                                                      claim.nativeSequence,
                                                      readTimestampUs,
                                                      sample.leadIRaw,
                                                      sample.leadIIRaw,
                                                      flags,
                                                      diagnostic);
    if (sent) {
      adsPreviousTransmissionFailed = false;
      adsLastPacketOverrunCount = claim.overruns;
      notePacketSent();
    } else {
      adsPreviousTransmissionFailed = true;
      ++adsPacketTransmissionFailures;
    }
  }

  // Keep ERR_STATUS diagnostics current without adding another SPI
  // transaction to every DRDY event. Poll only after the native queue drains.
  const uint32_t nowMs = millis();
  if (snapshotAdsDrdy().pending == 0 &&
      static_cast<int32_t>(nowMs - nextAdsErrorStatusPollMs) >= 0) {
    adsReader.refreshErrorStatus();
    nextAdsErrorStatusPollMs = nowMs + 1000;
  }
}
#else
void sendDueDummySamples() {
  if (!streamingEnabled) {
    return;
  }

  const uint32_t nowUs = micros();
  if (static_cast<int32_t>(nowUs - nextSampleTimeUs) < 0) {
    return;
  }

  const ECGSample sample =
      dummyGenerator.nextSample(sampleSequence, simulationEnabled);
  const uint8_t flags = simulationEnabled ? 0x01 : 0x00;
  if (ECGPacket::writeSample(ecgOutputStream(),
                             sampleSequence,
                             nowUs,
                             sample,
                             flags)) {
    notePacketSent();
  }
  ++sampleSequence;
  nextSampleTimeUs += Config::ECG_SAMPLE_INTERVAL_US;
  if (static_cast<int32_t>(micros() - nextSampleTimeUs) > 0) {
    nextSampleTimeUs = micros() + Config::ECG_SAMPLE_INTERVAL_US;
  }
}
#endif

void setup() {
  Serial.begin(Config::USB_SERIAL_BAUD);
  beginEcgOutputTransport();
  beginHeartBlinkIndicator();
  nextSampleTimeUs = micros();
  delay(300);

#if ENABLE_ADS1293
  if (adsReader.begin()) {
    ensureAdsDrdyInterruptAttached();
  }
#endif
  printStatus();
}

void loop() {
  pollUsbCommands();
#if ENABLE_ADS1293
  drainAdsDataReadyEvents();
#else
  sendDueDummySamples();
#endif
  pollHeartBlinkIndicator();
}

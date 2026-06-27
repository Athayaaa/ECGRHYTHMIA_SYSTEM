#include <Arduino.h>

#include "ADS1293Reader.h"
#include "DummyECGGenerator.h"
#include "ECGPacket.h"
#include "config.h"

#if !ECG_OUTPUT_USB
HardwareSerial ecgSerial(Config::ECG_UART_NUM);
#endif
#if ENABLE_ADS1293
ADS1293Reader adsReader;
#endif
DummyECGGenerator dummyGenerator(Config::ECG_SAMPLE_RATE_HZ);

bool streamingEnabled = Config::STREAM_ENABLED_DEFAULT;
bool simulationEnabled = Config::SIMULATION_ENABLED_DEFAULT;
#if ENABLE_ADS1293
bool adsDebugEnabled = false;
uint32_t nextAdsDebugTimeMs = 0;
#endif
uint32_t sampleSequence = 0;
uint32_t nextSampleTimeUs = 0;
String commandBuffer;

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

void printStatus() {
  Serial.println();
  Serial.println(F("ECGRHYTHMIA ESP32 firmware"));
  Serial.print(F("phase="));
#if ENABLE_ADS1293
  Serial.println(F("3B ADS1293 raw sample read test"));
  Serial.println(F("source=ads1293"));
#else
  Serial.println(F("2.5 dummy USB pipeline"));
  Serial.println(F("source=dummy_ecg"));
#endif
  Serial.print(F("output="));
  Serial.println(Config::ECG_OUTPUT_USB_ENABLED ? F("usb_serial") : F("uart2"));
  Serial.print(F("binary_stream="));
  Serial.println(streamingEnabled ? F("on") : F("off"));
  Serial.print(F("sim="));
  Serial.println(simulationEnabled ? F("on") : F("off"));
  Serial.print(F("sample_rate_hz="));
  Serial.println(Config::ECG_SAMPLE_RATE_HZ);
  Serial.print(F("uart_baud="));
  Serial.println(Config::ECG_UART_BAUD);
  Serial.print(F("uart_tx_gpio="));
  Serial.println(Config::ECG_UART_TX_PIN);
  Serial.print(F("uart_rx_gpio="));
  Serial.println(Config::ECG_UART_RX_PIN);
  Serial.print(F("packet_bytes="));
  Serial.println(ECGPacket::PACKET_SIZE);
#if ENABLE_ADS1293
  adsReader.refreshStatus();
  adsReader.printStatus(Serial);
  Serial.print(F("ads_debug="));
  Serial.println(adsDebugEnabled ? F("on") : F("off"));
  Serial.println(F("commands: ads status, ads start, ads stop, ads read, ads debug on, ads debug off, status"));
#else
  Serial.println(F("commands: start, stop, status, sim on, sim off"));
#endif
}

#if ENABLE_ADS1293
void printAdsSampleUnavailable(const __FlashStringHelper* prefix,
                               const ADS1293Reader::RawSample& sample) {
  Serial.print(prefix);
  Serial.print(F(": "));
  Serial.println(adsReader.lastMessage());
  Serial.print(prefix);
  Serial.print(F(" state data_ready="));
  Serial.print(sample.dataReady ? F("yes") : F("no"));
  Serial.print(F(" error_status=0x"));
  if (sample.errorStatus < 0x10) {
    Serial.print('0');
  }
  Serial.println(sample.errorStatus, HEX);
}

void printAdsRawSampleOnce(const __FlashStringHelper* prefix) {
  if (streamingEnabled) {
    return;
  }

  ADS1293Reader::RawSample sample;
  if (adsReader.readRawSample(sample)) {
    adsReader.printRawSample(Serial, sample);
  } else {
    printAdsSampleUnavailable(prefix, sample);
  }
}
#endif

void handleCommand(const String& rawCommand) {
  String command = rawCommand;
  command.trim();
  command.toLowerCase();

  if (command.length() == 0) {
    return;
  }

  if (command == "start") {
#if ENABLE_ADS1293
    Serial.println(F("Phase 3B ADS1293 build: use 'ads start'; binary packet streaming is disabled"));
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
    adsReader.refreshStatus();
    adsReader.printStatus(Serial);
  } else if (command == "ads start") {
    streamingEnabled = false;
    adsReader.startAcquisition();
    nextAdsDebugTimeMs = millis();
    Serial.println(adsReader.lastMessage());
  } else if (command == "ads stop") {
    streamingEnabled = false;
    adsReader.stopAcquisition();
    Serial.println(adsReader.lastMessage());
  } else if (command == "ads read") {
    printAdsRawSampleOnce(F("ads read"));
  } else if (command == "ads debug on") {
    if (!streamingEnabled) {
      adsDebugEnabled = true;
      nextAdsDebugTimeMs = millis();
      Serial.println(F("ads debug enabled: raw samples print at 8 Hz while acquisition is on"));
    }
  } else if (command == "ads debug off") {
    if (!streamingEnabled) {
      adsDebugEnabled = false;
      Serial.println(F("ads debug disabled"));
    }
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
    Serial.println(F("commands: ads status, ads start, ads stop, ads read, ads debug on, ads debug off, status"));
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
    } else {
      commandBuffer += c;
    }
  }
}

#if ENABLE_ADS1293
void pollAdsDebug() {
  if (!adsDebugEnabled || !adsReader.isAcquisitionEnabled() ||
      streamingEnabled) {
    return;
  }

  const uint32_t nowMs = millis();
  if (static_cast<int32_t>(nowMs - nextAdsDebugTimeMs) < 0) {
    return;
  }
  nextAdsDebugTimeMs = nowMs + Config::ADS1293_DEBUG_INTERVAL_MS;

  printAdsRawSampleOnce(F("ads debug"));
}
#endif

void sendDueSamples() {
#if ENABLE_ADS1293
  return;
#else
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

  ECGPacket::writeSample(ecgOutputStream(),
                         sampleSequence,
                         nowUs,
                         sample,
                         flags);
  ++sampleSequence;
  nextSampleTimeUs += Config::ECG_SAMPLE_INTERVAL_US;

  if (static_cast<int32_t>(micros() - nextSampleTimeUs) > 0) {
    nextSampleTimeUs = micros() + Config::ECG_SAMPLE_INTERVAL_US;
  }
#endif
}

void setup() {
  Serial.begin(Config::USB_SERIAL_BAUD);
  beginEcgOutputTransport();

  nextSampleTimeUs = micros();
  delay(300);
#if ENABLE_ADS1293
  adsReader.begin();
#endif
  printStatus();
}

void loop() {
  pollUsbCommands();
#if ENABLE_ADS1293
  pollAdsDebug();
#endif
  sendDueSamples();
}

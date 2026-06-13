#include <Arduino.h>

#include "DummyECGGenerator.h"
#include "ECGPacket.h"
#include "config.h"

#if !ECG_OUTPUT_USB
HardwareSerial ecgSerial(Config::ECG_UART_NUM);
#endif
DummyECGGenerator dummyGenerator(Config::ECG_SAMPLE_RATE_HZ);

bool streamingEnabled = Config::STREAM_ENABLED_DEFAULT;
bool simulationEnabled = Config::SIMULATION_ENABLED_DEFAULT;
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

void printStatus() {
  Serial.println();
  Serial.println(F("ECGRHYTHMIA Phase 1 ESP32 dummy ECG"));
  Serial.print(F("stream="));
  Serial.println(streamingEnabled ? F("on") : F("off"));
  Serial.print(F("sim="));
  Serial.println(simulationEnabled ? F("on") : F("off"));
  Serial.print(F("sample_rate_hz="));
  Serial.println(Config::ECG_SAMPLE_RATE_HZ);
  Serial.print(F("output_transport="));
  Serial.println(Config::OUTPUT_USB_ENABLED ? F("usb") : F("uart2"));
  if (!Config::OUTPUT_USB_ENABLED) {
    Serial.print(F("uart_baud="));
    Serial.println(Config::UART_BAUDRATE);
    Serial.print(F("uart_tx_gpio="));
    Serial.println(Config::UART_TX_PIN);
    Serial.print(F("uart_rx_gpio="));
    Serial.println(Config::UART_RX_PIN);
  }
  Serial.print(F("packet_bytes="));
  Serial.println(ECGPacket::PACKET_SIZE);
  Serial.println(F("commands: start, stop, status, sim on, sim off"));
}

void handleCommand(const String& rawCommand) {
  String command = rawCommand;
  command.trim();
  command.toLowerCase();

  if (command.length() == 0) {
    return;
  }

  if (command == "start") {
    streamingEnabled = true;
    nextSampleTimeUs = micros();
    Serial.println(F("stream started"));
  } else if (command == "stop") {
    streamingEnabled = false;
    Serial.println(F("stream stopped"));
  } else if (command == "status") {
    printStatus();
  } else if (command == "sim on") {
    simulationEnabled = true;
    Serial.println(F("simulation enabled"));
  } else if (command == "sim off") {
    simulationEnabled = false;
    Serial.println(F("simulation disabled; streaming sends zero-valued leads"));
  } else {
    Serial.print(F("unknown command: "));
    Serial.println(rawCommand);
    Serial.println(F("commands: start, stop, status, sim on, sim off"));
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

void sendDueSamples() {
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

  ECGPacket::writeSample(ecgOutputStream(), sampleSequence, nowUs, sample, flags);
  ++sampleSequence;
  nextSampleTimeUs += Config::ECG_SAMPLE_INTERVAL_US;

  if (static_cast<int32_t>(micros() - nextSampleTimeUs) > 0) {
    nextSampleTimeUs = micros() + Config::ECG_SAMPLE_INTERVAL_US;
  }
}

void setup() {
  Serial.begin(Config::USB_SERIAL_BAUD);
#if !ECG_OUTPUT_USB
  ecgSerial.begin(Config::UART_BAUDRATE,
                  SERIAL_8N1,
                  Config::UART_RX_PIN,
                  Config::UART_TX_PIN);
#endif

  nextSampleTimeUs = micros();
  delay(300);
  printStatus();
}

void loop() {
  pollUsbCommands();
  sendDueSamples();
}

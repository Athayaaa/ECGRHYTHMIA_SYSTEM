#pragma once

#include <Arduino.h>

namespace Config {

constexpr uint32_t USB_SERIAL_BAUD = 115200;

#ifndef ECG_OUTPUT_USB
#define ECG_OUTPUT_USB 0
#endif

constexpr bool OUTPUT_USB_ENABLED = ECG_OUTPUT_USB;

constexpr int UART_NUM = 2;
constexpr int UART_TX_PIN = 17;
constexpr int UART_RX_PIN = 16;
constexpr uint32_t UART_BAUDRATE = 115200;

constexpr int ECG_UART_NUM = UART_NUM;
constexpr int ECG_UART_TX_PIN = UART_TX_PIN;
constexpr int ECG_UART_RX_PIN = UART_RX_PIN;
constexpr uint32_t ECG_UART_BAUD = UART_BAUDRATE;

constexpr uint16_t ECG_SAMPLE_RATE_HZ = 250;
constexpr uint32_t ECG_SAMPLE_INTERVAL_US = 1000000UL / ECG_SAMPLE_RATE_HZ;

constexpr bool STREAM_ENABLED_DEFAULT = true;

#if ENABLE_DUMMY_DEFAULT
constexpr bool SIMULATION_ENABLED_DEFAULT = true;
#else
constexpr bool SIMULATION_ENABLED_DEFAULT = false;
#endif

}  // namespace Config

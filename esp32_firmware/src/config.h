#pragma once

#include <Arduino.h>

#ifndef ENABLE_DUMMY_DEFAULT
#define ENABLE_DUMMY_DEFAULT 0
#endif

#ifndef ECG_OUTPUT_USB
#define ECG_OUTPUT_USB 0
#endif

#ifndef ENABLE_ADS1293
#define ENABLE_ADS1293 0
#endif

namespace Config {

constexpr uint32_t USB_SERIAL_BAUD = 115200;

#if defined(BOARD_ESP32C3_SUPERMINI)
constexpr const char* BOARD_NAME = "esp32c3_supermini";
constexpr int HEART_BLINK_GPIO = 8;
#else
constexpr const char* BOARD_NAME = "esp32dev";
constexpr int HEART_BLINK_GPIO = -1;
#endif

constexpr int ECG_UART_NUM = 2;
constexpr int ECG_UART_TX_PIN = 17;
constexpr int ECG_UART_RX_PIN = 16;
constexpr uint32_t ECG_UART_BAUD = 921600;
constexpr bool ECG_OUTPUT_USB_ENABLED = ECG_OUTPUT_USB != 0;
constexpr bool ADS1293_ENABLED = ENABLE_ADS1293 != 0;

#if defined(BOARD_ESP32C3_SUPERMINI)
constexpr int ADS1293_SPI_SCLK_PIN = 4;
constexpr int ADS1293_SPI_MISO_PIN = 5;
constexpr int ADS1293_SPI_MOSI_PIN = 6;
constexpr int ADS1293_SPI_CS_PIN = 7;
constexpr int ADS1293_DRDY_PIN = 3;
#else
constexpr int ADS1293_SPI_SCLK_PIN = 18;
constexpr int ADS1293_SPI_MISO_PIN = 19;
constexpr int ADS1293_SPI_MOSI_PIN = 23;
constexpr int ADS1293_SPI_CS_PIN = 4;
constexpr int ADS1293_DRDY_PIN = 2;
#endif

constexpr uint16_t ADS1293_TARGET_SAMPLE_RATE_HZ = 250;
constexpr uint16_t ADS1293_CONFIGURED_ODR_HZ = 256;
constexpr uint32_t ADS1293_DEBUG_INTERVAL_MS = 125;
constexpr bool HEART_BLINK_ENABLED = HEART_BLINK_GPIO >= 0;
constexpr uint32_t HEART_BLINK_PERIOD_MS = 1000;
constexpr uint32_t HEART_BLINK_ON_MS = 80;
constexpr uint32_t HEART_BLINK_ACTIVE_TIMEOUT_MS = 1500;

constexpr uint16_t ECG_SAMPLE_RATE_HZ = 250;
constexpr uint32_t ECG_SAMPLE_INTERVAL_US = 1000000UL / ECG_SAMPLE_RATE_HZ;

#if ENABLE_ADS1293
constexpr bool STREAM_ENABLED_DEFAULT = false;
#else
constexpr bool STREAM_ENABLED_DEFAULT = true;
#endif

#if ENABLE_DUMMY_DEFAULT
constexpr bool SIMULATION_ENABLED_DEFAULT = true;
#else
constexpr bool SIMULATION_ENABLED_DEFAULT = false;
#endif

}  // namespace Config

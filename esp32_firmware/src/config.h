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
// Corrected native ADS1293 configuration.  Keep the rate as an exact rational
// because 102400 / (4 * 6 * 16) is 800/3 Hz, not an integer SPS value.
constexpr const char* ADS1293_NATIVE_PROFILE =
    "ADS1293_NATIVE_266P667_SPS";
constexpr uint32_t ADS1293_AFE_RES_HZ = 102400UL;
constexpr uint8_t ADS1293_R1 = 4;
constexpr uint8_t ADS1293_R2 = 6;
constexpr uint8_t ADS1293_R3_CH1 = 16;
constexpr uint8_t ADS1293_R3_CH2 = 16;
constexpr uint16_t ADS1293_NATIVE_RATE_NUMERATOR = 800;
constexpr uint16_t ADS1293_NATIVE_RATE_DENOMINATOR = 3;
constexpr uint32_t ADS1293_ADCMAX = 0xF30000UL;
constexpr uint32_t ADS1293_ZERO_CODE = ADS1293_ADCMAX / 2UL;
// VTEST=VREF/12 and the ADS1293 internal gain is 3.5, hence the ideal
// positive/negative self-test fractions are 31/48 and 17/48 of ADCMAX.
constexpr uint32_t ADS1293_SELFTEST_POSITIVE_CODE =
    (ADS1293_ADCMAX / 48UL) * 31UL;
constexpr uint32_t ADS1293_SELFTEST_NEGATIVE_CODE =
    (ADS1293_ADCMAX / 48UL) * 17UL;
constexpr float ADS1293_VREF_NOMINAL_V = 2.4f;
constexpr float ADS1293_ANALOG_GAIN = 3.5f;
constexpr uint32_t ADS1293_SPI_CLOCK_HZ = 1000000UL;
constexpr uint16_t ADS1293_MAX_PENDING_DRDY = 255;
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

static_assert(Config::ADS1293_AFE_RES_HZ *
                      Config::ADS1293_NATIVE_RATE_DENOMINATOR ==
                  static_cast<uint32_t>(
                      Config::ADS1293_NATIVE_RATE_NUMERATOR) *
                      Config::ADS1293_R1 * Config::ADS1293_R2 *
                      Config::ADS1293_R3_CH1,
              "ADS1293 native-rate fraction must exactly represent the register profile");
static_assert(Config::ADS1293_SELFTEST_POSITIVE_CODE == 10285056UL,
              "positive self-test code must derive from target ADCMAX");
static_assert(Config::ADS1293_SELFTEST_NEGATIVE_CODE == 5640192UL,
              "negative self-test code must derive from target ADCMAX");

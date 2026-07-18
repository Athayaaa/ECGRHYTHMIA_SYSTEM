#pragma once

#include "config.h"

#ifndef FW_GIT_COMMIT
#define FW_GIT_COMMIT "unknown"
#endif

#ifndef FW_BUILD_ENVIRONMENT
#define FW_BUILD_ENVIRONMENT "unknown"
#endif

#ifndef FW_BUILD_TIME_UTC
#define FW_BUILD_TIME_UTC "unknown"
#endif

#ifndef FW_GIT_DIRTY
#define FW_GIT_DIRTY 0
#endif

namespace FirmwareInfo {

constexpr const char* GIT_COMMIT = FW_GIT_COMMIT;
constexpr const char* BUILD_ENVIRONMENT = FW_BUILD_ENVIRONMENT;
constexpr const char* BUILD_TIME_UTC = FW_BUILD_TIME_UTC;
constexpr bool GIT_DIRTY = FW_GIT_DIRTY != 0;

#if ENABLE_ADS1293
constexpr uint8_t PROTOCOL_VERSION = 2;
#else
// Dummy builds intentionally keep the legacy V1 wire contract.
constexpr uint8_t PROTOCOL_VERSION = 1;
#endif

}  // namespace FirmwareInfo

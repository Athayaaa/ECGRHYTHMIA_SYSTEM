#pragma once

#include <stdint.h>

// Pure helpers for accounting ADS1293 DRDY events.  The ADS1293 ECG output
// registers hold only the latest conversion, so multiple pending edges must be
// collapsed into one SPI read while preserving the corresponding sequence gap.
namespace ADS1293DrdyAccounting {

constexpr uint32_t totalEvents(uint32_t pendingEvents,
                               uint32_t unrepresentedEvents) {
  return pendingEvents + unrepresentedEvents;
}

constexpr uint32_t missedEvents(uint32_t pendingEvents,
                                uint32_t unrepresentedEvents) {
  return totalEvents(pendingEvents, unrepresentedEvents) == 0
             ? 0
             : totalEvents(pendingEvents, unrepresentedEvents) - 1;
}

constexpr uint32_t latestSequence(uint32_t nextSequence,
                                  uint32_t pendingEvents,
                                  uint32_t unrepresentedEvents) {
  return totalEvents(pendingEvents, unrepresentedEvents) == 0
             ? nextSequence
             : nextSequence + totalEvents(pendingEvents, unrepresentedEvents) -
                   1;
}

constexpr uint32_t sequenceAfterClaim(uint32_t nextSequence,
                                      uint32_t pendingEvents,
                                      uint32_t unrepresentedEvents) {
  return nextSequence + totalEvents(pendingEvents, unrepresentedEvents);
}

constexpr uint8_t saturateDiagnosticByte(uint32_t value) {
  return value > UINT8_MAX ? UINT8_MAX : static_cast<uint8_t>(value);
}

// A conversion arriving after the main loop atomically claims pending events
// but before it verifies completion of the SPI read makes that read ambiguous:
// the latest-only output registers may already contain the newer conversion.
// Conservatively discard the read; the newly pending event remains queued and
// the discarded claimed conversion becomes one explicit overrun/sequence gap.
constexpr bool readIsAmbiguous(uint32_t pendingAfterRead) {
  return pendingAfterRead != 0;
}

constexpr uint32_t ambiguousReadLoss(uint32_t pendingAfterRead) {
  return readIsAmbiguous(pendingAfterRead) ? 1U : 0U;
}

}  // namespace ADS1293DrdyAccounting

static_assert(ADS1293DrdyAccounting::missedEvents(1, 0) == 0,
              "one DRDY edge represents one readable conversion");
static_assert(ADS1293DrdyAccounting::missedEvents(4, 0) == 3,
              "queued DRDY edges must collapse to the latest conversion");
static_assert(ADS1293DrdyAccounting::latestSequence(10, 4, 2) == 15 &&
                  ADS1293DrdyAccounting::sequenceAfterClaim(10, 4, 2) == 16,
              "collapsed DRDY events must create an exact sequence gap");
static_assert(ADS1293DrdyAccounting::saturateDiagnosticByte(254) == 254 &&
                  ADS1293DrdyAccounting::saturateDiagnosticByte(256) == 255,
              "one-byte diagnostics must clamp instead of wrapping");
static_assert(!ADS1293DrdyAccounting::readIsAmbiguous(0) &&
                  ADS1293DrdyAccounting::readIsAmbiguous(1) &&
                  ADS1293DrdyAccounting::ambiguousReadLoss(17) == 1,
              "a post-claim DRDY must conservatively discard exactly one read");

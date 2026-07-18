#include <string.h>

#include <unity.h>

#include "CRC16.h"
#include "ECGPacket.h"

namespace {

constexpr uint8_t EXPECTED_PACKET[ECGPacket::PACKET_SIZE] = {
    0xAA, 0x55, 0x02, 0x19, 0x00, 0x78, 0x56, 0x34,
    0x12, 0xEF, 0xCD, 0xAB, 0x89, 0x01, 0x00, 0x20,
    0x03, 0x2D, 0x00, 0x00, 0x80, 0x00, 0x00, 0x00,
    0xF3, 0x00, 0x11, 0x22, 0x33, 0x44, 0x26, 0x89,
};

void test_v2_encoder_matches_independent_wire_fixture() {
  uint8_t actual[ECGPacket::PACKET_SIZE] = {};
  const size_t length = ECGPacket::encodeNativeSampleV2(actual,
                                                         sizeof(actual),
                                                         0x12345678UL,
                                                         0x89ABCDEFUL,
                                                         0x00800000UL,
                                                         0x00F30000UL,
                                                         0x2D,
                                                         0x44332211UL);

  TEST_ASSERT_EQUAL_UINT32(sizeof(EXPECTED_PACKET), length);
  TEST_ASSERT_EQUAL_UINT8_ARRAY(EXPECTED_PACKET, actual, sizeof(actual));
  TEST_ASSERT_EQUAL_HEX16(
      0x8926,
      CRC16::compute(actual, ECGPacket::PACKET_SIZE - sizeof(uint16_t)));
}

void test_v2_encoder_rejects_short_buffer() {
  uint8_t shortBuffer[ECGPacket::PACKET_SIZE - 1] = {};
  TEST_ASSERT_EQUAL_UINT32(
      0,
      ECGPacket::encodeNativeSampleV2(shortBuffer,
                                      sizeof(shortBuffer),
                                      0,
                                      0,
                                      0,
                                      0,
                                      0,
                                      0));
}

}  // namespace

void setUp() {}
void tearDown() {}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_v2_encoder_matches_independent_wire_fixture);
  RUN_TEST(test_v2_encoder_rejects_short_buffer);
  return UNITY_END();
}

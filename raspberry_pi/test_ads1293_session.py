from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ads1293_session import (
    ADCMAX,
    FirmwareCompatibilityError,
    TARGET_STATIC_REGISTERS,
    build_session_metadata,
    expected_selftest_codes,
    parse_config_dump_response,
    serial_utilization,
    verify_firmware_config,
    verify_persisted_session_metadata,
    write_session_metadata,
)


def valid_config() -> dict:
    return {
        "config_verified": True,
        "device_id": 1,
        "protocol_version": 2,
        "configuration_profile": "ADS1293_NATIVE_266P667_SPS",
        "selftest_mode": "off",
        "afe_res_hz": 102400,
        "r1": 4,
        "r2": 6,
        "r3_ch1": 16,
        "r3_ch2": 16,
        "native_sample_rate_numerator": 800,
        "native_sample_rate_denominator": 3,
        "native_sample_rate_hz": 800 / 3,
        "adcmax": ADCMAX,
        "zero_code": ADCMAX // 2,
        "adc_encoding": "offset_binary_uint24",
        "packet_size": 32,
        "crc_type": "CRC16-CCITT-FALSE",
        "vref_nominal_v": 2.4,
        "analog_gain": 3.5,
        "firmware_commit": "0123456789abcdef",
        "firmware_dirty": True,
        "firmware_build_time_utc": "2026-07-18T00:00:00Z",
        "firmware_build_environment": "esp32dev_ads1293_usb",
        "registers": {
            f"0x{address:02X}": {
                "address": address,
                "written": value,
                "readback": value,
                "match": True,
            }
            for address, value in TARGET_STATIC_REGISTERS.items()
        },
    }


class ADS1293SessionTests(unittest.TestCase):
    def test_valid_firmware_config(self) -> None:
        self.assertEqual(verify_firmware_config(valid_config()), [])

    def test_changed_register_fails_strict_mode(self) -> None:
        config = valid_config()
        config["r2"] = 5
        with self.assertRaises(FirmwareCompatibilityError):
            verify_firmware_config(config)

    def test_override_returns_failures(self) -> None:
        config = valid_config()
        config["config_verified"] = False
        config["firmware_commit"] = "unknown"
        failures = verify_firmware_config(config, allow_unverified=True)
        self.assertTrue(failures)

    def test_override_cannot_bypass_wire_or_dsp_critical_fields(self) -> None:
        for field, incompatible in (
            ("protocol_version", 1),
            ("packet_size", 31),
            ("native_sample_rate_numerator", 250),
            ("adcmax", ADCMAX - 1),
            ("zero_code", 0),
            ("adc_encoding", "twos_complement_int24"),
            ("vref_nominal_v", 4.0),
            ("analog_gain", 1.0),
            ("crc_type", "none"),
            ("selftest_mode", "positive"),
            ("packet_size", float("inf")),
        ):
            with self.subTest(field=field):
                config = valid_config()
                config[field] = incompatible
                with self.assertRaises(FirmwareCompatibilityError):
                    verify_firmware_config(config, allow_unverified=True)

    def test_override_allows_absent_readback_evidence_only(self) -> None:
        config = valid_config()
        config["registers"] = {}
        failures = verify_firmware_config(config, allow_unverified=True)
        self.assertTrue(any("register" in failure for failure in failures))

    def test_override_cannot_bypass_known_register_mismatch(self) -> None:
        config = valid_config()
        config["registers"]["0x01"]["readback"] = 0x00
        config["registers"]["0x01"]["match"] = False
        with self.assertRaises(FirmwareCompatibilityError):
            verify_firmware_config(config, allow_unverified=True)

    def test_placeholder_provenance_is_not_verified(self) -> None:
        for placeholder in ("", "unknown", "N/A"):
            with self.subTest(placeholder=placeholder):
                config = valid_config()
                config["firmware_commit"] = placeholder
                with self.assertRaises(FirmwareCompatibilityError):
                    verify_firmware_config(config)
                self.assertTrue(
                    verify_firmware_config(config, allow_unverified=True)
                )

    def test_config_dump_requires_one_json_line(self) -> None:
        config = valid_config()
        response = "command echo\n" + json.dumps(config) + "\n"
        self.assertEqual(parse_config_dump_response(response), config)
        with self.assertRaises(FirmwareCompatibilityError):
            parse_config_dump_response(response + json.dumps(config))

    def test_selftest_codes_come_from_adcmax(self) -> None:
        self.assertEqual(
            expected_selftest_codes(ADCMAX),
            {"zero": 7_962_624, "positive": 10_285_056, "negative": 5_640_192},
        )

    def test_serial_utilization_at_115200(self) -> None:
        value = serial_utilization(
            packet_size=32, baudrate=115200, native_rate_hz=800 / 3
        )
        self.assertAlmostEqual(value, 20 / 27)

    def test_session_metadata_contains_required_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calibration_path = Path(directory) / "calibration.yaml"
            calibration_path.write_text("status: preliminary\n", encoding="utf-8")
            metadata = build_session_metadata(
                firmware_config=valid_config(),
                serial_port="/dev/ttyUSB-test",
                baudrate=115200,
                calibration_config_path=calibration_path,
                calibration_compatible=True,
                calibration_status="preliminary",
                calibration_source="datasheet_nominal",
            )
            path = write_session_metadata(Path(directory), metadata)
            second_path = write_session_metadata(Path(directory), metadata)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotEqual(path, second_path)
            self.assertEqual(path.read_text(encoding="utf-8"), second_path.read_text(encoding="utf-8"))

        required = {
            "session_id",
            "created_at",
            "created_at_utc",
            "serial_port",
            "baud_rate",
            "firmware_commit",
            "firmware_dirty",
            "firmware_build_time_utc",
            "firmware_build_environment",
            "protocol_version",
            "configuration_profile",
            "selftest_mode",
            "device_id",
            "ads1293_device_id",
            "registers",
            "config_verified",
            "afe_res_hz",
            "r1",
            "r2",
            "r3_ch1",
            "r3_ch2",
            "native_sample_rate_numerator",
            "native_sample_rate_denominator",
            "native_sample_rate_hz",
            "output_sample_rate_hz",
            "resample_up",
            "resample_down",
            "adc_encoding",
            "adcmax",
            "zero_code",
            "vref_nominal_v",
            "analog_gain",
            "conversion_source",
            "calibration_status",
            "calibration_config_path",
            "calibration_config_sha256",
            "calibration_compatibility",
            "packet_size",
            "crc_type",
            "channel_mapping",
        }
        self.assertFalse(required - loaded.keys())
        self.assertEqual(
            loaded["configuration_profile"], "ADS1293_NATIVE_266P667_SPS"
        )
        self.assertEqual(loaded["selftest_mode"], "off")
        self.assertEqual(loaded["device_id"], 1)
        self.assertEqual(loaded["serial_port"], "/dev/ttyUSB-test")
        self.assertEqual(loaded["baud_rate"], 115200)
        self.assertEqual(loaded["afe_res_hz"], 102400)
        self.assertEqual(
            (loaded["r1"], loaded["r2"], loaded["r3_ch1"], loaded["r3_ch2"]),
            (4, 6, 16, 16),
        )
        self.assertEqual(
            (
                loaded["native_sample_rate_numerator"],
                loaded["native_sample_rate_denominator"],
                loaded["resample_up"],
                loaded["resample_down"],
            ),
            (800, 3, 15, 16),
        )
        self.assertEqual(loaded["conversion_source"], "datasheet_nominal")
        self.assertEqual(loaded["calibration_status"], "preliminary")
        self.assertEqual(loaded["calibration_config_path"], str(calibration_path))
        self.assertTrue(loaded["calibration_config_sha256"])
        self.assertTrue(loaded["calibration_compatibility"]["compatible"])
        self.assertEqual(loaded["packet_size"], 32)
        self.assertEqual(loaded["crc_type"], "CRC16-CCITT-FALSE")
        self.assertEqual(
            loaded["channel_mapping"]["output_lead_iii"],
            "resampled Lead II - resampled Lead I",
        )
        for register in loaded["registers"].values():
            self.assertEqual(register["written"], register["readback"])
            self.assertTrue(register["match"])
        self.assertNotIn("esp32dev_dummy", json.dumps(loaded))

    def test_session_conversion_source_is_supplied_by_selected_calibration(self) -> None:
        metadata = build_session_metadata(
            firmware_config=valid_config(),
            serial_port="/dev/ttyUSB-test",
            baudrate=115200,
            calibration_config_path=None,
            calibration_compatible=True,
            calibration_status="calibrated",
            calibration_source="prosim_multipoint",
        )
        self.assertEqual(metadata["conversion_source"], "prosim_multipoint")
        self.assertEqual(metadata["calibration_status"], "calibrated")

    def test_persisted_session_requires_full_verified_pass_contract(self) -> None:
        metadata = build_session_metadata(
            firmware_config=valid_config(),
            serial_port="/dev/ttyUSB-test",
            baudrate=115200,
            calibration_config_path=None,
            calibration_compatible=True,
            calibration_status="preliminary",
            calibration_source="datasheet_nominal",
        )
        metadata["acquisition_status"] = "PASS"
        self.assertIs(verify_persisted_session_metadata(metadata), metadata)

        for field, incompatible in (
            ("acquisition_status", "FAIL_UNVERIFIED"),
            ("session_id", "unknown"),
            ("output_sample_rate_hz", 251),
            ("resample_up", 1),
            ("resample_down", 1),
        ):
            with self.subTest(field=field):
                invalid = dict(metadata)
                invalid[field] = incompatible
                with self.assertRaises(FirmwareCompatibilityError):
                    verify_persisted_session_metadata(invalid)

    def test_persisted_session_rejects_incomplete_register_readback(self) -> None:
        metadata = build_session_metadata(
            firmware_config=valid_config(),
            serial_port="/dev/ttyUSB-test",
            baudrate=115200,
            calibration_config_path=None,
            calibration_compatible=True,
            calibration_status="preliminary",
            calibration_source="datasheet_nominal",
        )
        metadata["acquisition_status"] = "PASS"
        metadata["registers"] = dict(metadata["registers"])
        metadata["registers"].pop("0x29")
        with self.assertRaises(FirmwareCompatibilityError):
            verify_persisted_session_metadata(metadata)


if __name__ == "__main__":
    unittest.main()

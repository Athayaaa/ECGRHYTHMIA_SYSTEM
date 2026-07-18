"""Unit tests for the offline corrected-pipeline regression CLI."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ads1293_session import (
    TARGET_STATIC_REGISTERS,
    build_session_metadata,
)
from packet_parser import (
    ECGPacket,
    VERSION_V2,
    V2_FLAG_ADS_ERROR,
    V2_FLAG_BACKLOG_OVERRUN,
    V2_FLAG_CODE_OUT_OF_RANGE,
    V2_FLAG_CONFIG_VERIFIED,
    V2_FLAG_PREVIOUS_TX_FAILURE,
    V2_FLAG_SELFTEST_ACTIVE,
)
from validate_corrected_sampling_pipeline import (
    ADCMAX,
    expected_configuration,
    generate_synthetic_native,
    load_native_capture,
    load_native_capture_data,
    main,
    run_regression,
    verify_configuration,
)
from window_exporter import NativeCaptureWriter


def verified_firmware_config() -> dict:
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
        "vref_nominal_v": 2.4,
        "analog_gain": 3.5,
        "packet_size": 32,
        "crc_type": "CRC16-CCITT-FALSE",
        "firmware_commit": "0123456789ab",
        "firmware_dirty": False,
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


def verified_session() -> dict:
    return build_session_metadata(
        firmware_config=verified_firmware_config(),
        serial_port="/dev/test",
        baudrate=115200,
        calibration_config_path=None,
        calibration_compatible=True,
        calibration_status="preliminary",
    )


class CorrectedPipelineValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.samples, cls.sequences = generate_synthetic_native(30.0)

    def test_synthetic_30_seconds_passes_exact_counts(self) -> None:
        report = run_regression(
            self.samples,
            self.sequences,
            expected_configuration(),
            synthetic=True,
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["native_input_samples"], 8000)
        self.assertEqual(report["actual_output_samples"], 7500)
        self.assertEqual(report["completed_frames"], 3)
        self.assertEqual(report["packet_loss_events"], 0)
        self.assertEqual(report["resampler_resets"], 0)
        self.assertTrue(report["configuration_verified"])
        self.assertTrue(report["metadata_complete"])
        self.assertTrue(report["all_values_finite"])
        self.assertEqual(report["lead_iii_max_consistency_error_count"], 0.0)
        self.assertEqual(report["raw_range_violations"]["near_low_rail"], 0)
        self.assertEqual(report["raw_range_violations"]["near_high_rail"], 0)

    def test_near_rail_measured_codes_are_warning_not_lead_iii_rail_checks(self) -> None:
        samples = self.samples.copy()
        samples[100, 0] = np.uint32(1)
        samples[200, 1] = np.uint32(ADCMAX - 1)
        report = run_regression(
            samples,
            self.sequences,
            expected_configuration(),
            synthetic=True,
        )
        self.assertEqual(report["status"], "WARNING")
        self.assertEqual(report["raw_range_violations"]["near_low_rail"], 1)
        self.assertEqual(report["raw_range_violations"]["near_high_rail"], 1)
        self.assertEqual(report["raw_range_violations"]["below_zero"], 0)
        self.assertEqual(report["raw_range_violations"]["above_adcmax"], 0)

    def test_changed_register_fails_configuration_verification(self) -> None:
        configuration = expected_configuration()
        configuration["r3_ch1"] = 8
        valid, errors = verify_configuration(configuration)
        self.assertFalse(valid)
        self.assertTrue(any("r3_ch1" in error for error in errors))

    def test_missing_capture_metadata_is_not_silently_accepted(self) -> None:
        report = run_regression(
            self.samples[:400],
            None,
            {},
            synthetic=False,
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["configuration_verified"])
        self.assertFalse(report["metadata_complete"])
        self.assertTrue(report["warnings"])

    def test_captured_session_requires_register_readback_verification(self) -> None:
        session = verified_session()
        valid, errors = verify_configuration(
            session,
            require_firmware_verification=True,
        )
        self.assertTrue(valid, errors)

        session["registers"] = {}
        valid, errors = verify_configuration(
            session,
            require_firmware_verification=True,
        )
        self.assertFalse(valid)
        self.assertTrue(any("register" in error for error in errors))

    def test_native_capture_writer_csv_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = NativeCaptureWriter(Path(directory), enabled=True)
            for sequence in (7, 8, 9):
                writer.append(
                    ECGPacket(
                        sequence=sequence,
                        timestamp_us=sequence * 3750,
                        sample_count=1,
                        sampling_rate_hz=0,
                        ch1=0x798000 + sequence,
                        ch2=0x798100 + sequence,
                        ch3=0,
                        flags=1,
                        protocol_version=VERSION_V2,
                        native_rate_numerator=800,
                        diagnostic=0,
                    )
                )
            writer.close()
            self.assertIsNotNone(writer.path)
            measured, sequences = load_native_capture(writer.path)
            capture = load_native_capture_data(writer.path)

        np.testing.assert_array_equal(sequences, [7, 8, 9])
        np.testing.assert_array_equal(
            measured,
            [
                [0x798007, 0x798107],
                [0x798008, 0x798108],
                [0x798009, 0x798109],
            ],
        )
        np.testing.assert_array_equal(
            capture.packet_flags,
            np.full(3, V2_FLAG_CONFIG_VERIFIED, dtype=np.uint8),
        )
        np.testing.assert_array_equal(
            capture.packet_diagnostics,
            np.zeros(3, dtype=np.uint32),
        )
        self.assertTrue(capture.packet_diagnostics_required)
        report = run_regression(
            measured,
            sequences,
            verified_session(),
            synthetic=False,
            packet_flags=capture.packet_flags,
            packet_diagnostics=capture.packet_diagnostics,
            require_packet_diagnostics=capture.packet_diagnostics_required,
        )
        self.assertEqual(report["status"], "PASS", report["failures"])
        self.assertTrue(report["configuration_verified"], report["failures"])
        self.assertTrue(report["metadata_complete"])
        self.assertTrue(report["sequence_metadata_available"])
        self.assertTrue(report["packet_integrity"]["complete"])

    def test_captured_abnormal_or_reserved_flags_fail(self) -> None:
        samples = np.full((64, 2), ADCMAX // 2, dtype=np.uint32)
        sequences = np.arange(samples.shape[0], dtype=np.uint32)
        cases = (
            (V2_FLAG_ADS_ERROR, "ads_error_flag_count"),
            (V2_FLAG_BACKLOG_OVERRUN, "backlog_overrun_flag_count"),
            (V2_FLAG_CODE_OUT_OF_RANGE, "code_out_of_range_flag_count"),
            (V2_FLAG_PREVIOUS_TX_FAILURE, "previous_tx_failure_flag_count"),
            (V2_FLAG_SELFTEST_ACTIVE, "selftest_active_flag_count"),
            (0x40, "reserved_flag_count"),
        )
        for bad_flag, count_key in cases:
            with self.subTest(flag=bad_flag):
                flags = np.full(
                    samples.shape[0],
                    V2_FLAG_CONFIG_VERIFIED,
                    dtype=np.uint8,
                )
                flags[7] |= np.uint8(bad_flag)
                report = run_regression(
                    samples,
                    sequences,
                    verified_session(),
                    synthetic=False,
                    packet_flags=flags,
                    packet_diagnostics=np.zeros(samples.shape[0], dtype=np.uint32),
                )
                self.assertEqual(report["status"], "FAIL")
                self.assertEqual(report["packet_integrity"][count_key], 1)

    def test_captured_config_clear_and_error_diagnostic_fail(self) -> None:
        samples = np.full((64, 2), ADCMAX // 2, dtype=np.uint32)
        sequences = np.arange(samples.shape[0], dtype=np.uint32)
        flags = np.full(
            samples.shape[0],
            V2_FLAG_CONFIG_VERIFIED,
            dtype=np.uint8,
        )
        flags[3] = 0
        diagnostics = np.zeros(samples.shape[0], dtype=np.uint32)
        diagnostics[5] = 1

        report = run_regression(
            samples,
            sequences,
            verified_session(),
            synthetic=False,
            packet_flags=flags,
            packet_diagnostics=diagnostics,
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(
            report["packet_integrity"]["config_verified_clear_count"], 1
        )
        self.assertEqual(
            report["packet_integrity"]["nonzero_error_status_count"], 1
        )

    def test_plain_capture_without_packet_diagnostics_never_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "plain.npy", root / "plain.npz")
            np.save(paths[0], self.samples[:400])
            np.savez(
                paths[1],
                samples=self.samples[:400],
                sequences=self.sequences[:400],
            )
            for path in paths:
                with self.subTest(path=path.name):
                    capture = load_native_capture_data(path)
                    report = run_regression(
                        capture.measured,
                        capture.sequences,
                        verified_session(),
                        synthetic=False,
                        packet_flags=capture.packet_flags,
                        packet_diagnostics=capture.packet_diagnostics,
                    )
                    self.assertEqual(report["status"], "WARNING")
                    self.assertFalse(report["packet_integrity"]["complete"])
                    self.assertTrue(
                        any(
                            "unverifiable" in warning
                            for warning in report["warnings"]
                        )
                    )

    def test_receiver_csv_missing_packet_diagnostics_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native_samples.csv"
            path.write_text(
                "native_sequence,lead_i_offset_code,lead_ii_offset_code\n"
                "0,7962624,7962624\n"
                "1,7962624,7962624\n",
                encoding="utf-8",
            )
            capture = load_native_capture_data(path)

        report = run_regression(
            capture.measured,
            capture.sequences,
            verified_session(),
            synthetic=False,
            packet_flags=capture.packet_flags,
            packet_diagnostics=capture.packet_diagnostics,
            require_packet_diagnostics=capture.packet_diagnostics_required,
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(
            any("receiver CSV" in failure for failure in report["failures"])
        )

    def test_npz_malformed_sequences_are_rejected_before_uint32_cast(self) -> None:
        malformed = (
            np.asarray([-1.0, 0.0]),
            np.asarray([0.5, 1.0]),
            np.asarray([np.nan, 1.0]),
            np.asarray([0.0, float(0x1_0000_0000)]),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, sequences in enumerate(malformed):
                with self.subTest(sequences=sequences):
                    path = root / f"malformed_{index}.npz"
                    np.savez(
                        path,
                        samples=np.full((2, 2), ADCMAX // 2, dtype=np.uint32),
                        sequences=sequences,
                    )
                    with self.assertRaisesRegex(ValueError, "sequences"):
                        load_native_capture_data(path)

    def test_npz_packet_diagnostics_are_loaded_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.npz"
            count = 64
            np.savez(
                path,
                samples=np.full((count, 2), ADCMAX // 2, dtype=np.uint32),
                sequences=np.arange(count, dtype=np.uint32),
                flags=np.full(count, V2_FLAG_CONFIG_VERIFIED, dtype=np.uint8),
                diagnostic=np.zeros(count, dtype=np.uint32),
            )
            capture = load_native_capture_data(path)

        report = run_regression(
            capture.measured,
            capture.sequences,
            verified_session(),
            synthetic=False,
            packet_flags=capture.packet_flags,
            packet_diagnostics=capture.packet_diagnostics,
        )
        self.assertEqual(report["status"], "PASS", report["failures"])
        self.assertTrue(report["packet_integrity"]["complete"])

    def test_cli_synthetic_mode_returns_success(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            return_code = main(["--synthetic", "--duration", "30"])
        self.assertEqual(return_code, 0)
        self.assertIn("status: PASS", output.getvalue())
        self.assertIn("7500/7500", output.getvalue())


if __name__ == "__main__":
    unittest.main()

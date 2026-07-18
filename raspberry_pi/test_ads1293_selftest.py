from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import ads1293_selftest
from ads1293_selftest import (
    SELFTEST_STATEMENT,
    channel_statistics,
    execute_selftest_sequence,
    write_report,
)
from ads1293_session import ADCMAX, expected_selftest_codes
from packet_parser import (
    VERSION_V2,
    V2_FLAG_CONFIG_VERIFIED,
    V2_FLAG_SELFTEST_ACTIVE,
    V2_NATIVE_RATE_NUMERATOR,
)


class ADS1293SelftestTests(unittest.TestCase):
    @staticmethod
    def _cli_args(output_dir: Path, *, allow_unverified: bool = False):
        return SimpleNamespace(
            port="/dev/synthetic",
            baudrate=115200,
            samples=4,
            timeout=1.25,
            boot_delay=0.0,
            output_dir=output_dir,
            tolerance_percent_full_scale=0.1,
            max_std_counts=1.0,
            allow_unverified_firmware=allow_unverified,
        )

    @staticmethod
    def _fake_serial_context():
        serial_port = mock.MagicMock()
        serial_context = mock.MagicMock()
        serial_context.__enter__.return_value = serial_port
        serial_context.__exit__.return_value = False
        return serial_context

    def test_channel_statistics(self) -> None:
        stats = channel_statistics(
            [100.0, 100.0, 100.0],
            expected_code=100,
            adcmax=ADCMAX,
            tolerance_percent_full_scale=0.1,
            max_std_counts=1.0,
        )
        self.assertEqual(stats["status"], "PASS")
        self.assertEqual(stats["absolute_code_error"], 0.0)

    def test_sequence_report_and_restores_off(self) -> None:
        commands: list[str] = []
        current_mode = {"value": "off"}

        def command(_port, command: str, **_kwargs) -> bytes:
            commands.append(command)
            if command.startswith("ads selftest "):
                if command != "ads selftest status":
                    current_mode["value"] = command.rsplit(" ", 1)[-1]
                return (
                    json.dumps({"selftest_mode": current_mode["value"]}) + "\n"
                ).encode()
            return b"ok\n"

        def start(_port, **_kwargs) -> bytes:
            return b"packet"

        def collect(_port, *, sample_count: int, **_kwargs):
            code = expected_selftest_codes(ADCMAX)[current_mode["value"]]
            return [
                SimpleNamespace(
                    lead_i_code=code,
                    lead_ii_code=code,
                    protocol_version=VERSION_V2,
                    sample_count=1,
                    native_rate_numerator=V2_NATIVE_RATE_NUMERATOR,
                    flags=V2_FLAG_CONFIG_VERIFIED | V2_FLAG_SELFTEST_ACTIVE,
                    error_status=0,
                    sequence=index,
                )
                for index in range(sample_count)
            ]

        def stop(_port, **_kwargs) -> None:
            return None

        report = execute_selftest_sequence(
            object(),
            firmware_config={"adcmax": ADCMAX},
            sample_count=4,
            timeout_sec=1.0,
            tolerance_percent_full_scale=0.1,
            max_std_counts=1.0,
            command_fn=command,
            start_stream_fn=start,
            collect_fn=collect,
            stop_fn=stop,
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["restoration"]["status"], "PASS")
        self.assertTrue(report["restoration"]["attempted"])
        self.assertIn(
            "ads selftest status reported selftest_mode=off",
            report["restoration"]["evidence"],
        )
        self.assertEqual(commands[-2:], ["ads selftest off", "ads selftest status"])

        with tempfile.TemporaryDirectory() as directory:
            path = write_report(Path(directory), report)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["expected_codes"], expected_selftest_codes(ADCMAX))

    def test_restores_off_after_collection_exception(self) -> None:
        commands: list[str] = []

        def command(_port, command: str, **_kwargs) -> bytes:
            commands.append(command)
            if command.startswith("ads selftest "):
                mode = command.rsplit(" ", 1)[-1]
                if mode == "status":
                    mode = "off"
                return (json.dumps({"selftest_mode": mode}) + "\n").encode()
            return b"ok\n"

        def failing_collect(*_args, **_kwargs):
            raise RuntimeError("synthetic collection failure")

        with self.assertRaisesRegex(
            RuntimeError, "synthetic collection failure"
        ) as raised:
            execute_selftest_sequence(
                object(),
                firmware_config={"adcmax": ADCMAX},
                sample_count=4,
                timeout_sec=1.0,
                tolerance_percent_full_scale=0.1,
                max_std_counts=1.0,
                command_fn=command,
                start_stream_fn=lambda *_args, **_kwargs: b"packet",
                collect_fn=failing_collect,
                stop_fn=lambda *_args, **_kwargs: None,
            )
        self.assertEqual(
            raised.exception.selftest_restoration["status"],
            "PASS",
        )
        self.assertTrue(
            raised.exception.selftest_restoration["attempted"]
        )
        self.assertIn("ads selftest off", commands)
        self.assertEqual(commands[-1], "ads selftest status")

    def test_restoration_failure_is_not_reported_as_pass(self) -> None:
        current_mode = {"value": "off"}

        def command(_port, command: str, **_kwargs) -> bytes:
            if command == "ads selftest off":
                raise RuntimeError("synthetic off failure")
            if command.startswith("ads selftest "):
                if command != "ads selftest status":
                    current_mode["value"] = command.rsplit(" ", 1)[-1]
                return (json.dumps({"selftest_mode": current_mode["value"]}) + "\n").encode()
            return b"ok\n"

        def collect(_port, *, sample_count: int, **_kwargs):
            code = expected_selftest_codes(ADCMAX)[current_mode["value"]]
            return [
                SimpleNamespace(
                    lead_i_code=code,
                    lead_ii_code=code,
                    protocol_version=VERSION_V2,
                    sample_count=1,
                    native_rate_numerator=V2_NATIVE_RATE_NUMERATOR,
                    flags=V2_FLAG_CONFIG_VERIFIED | V2_FLAG_SELFTEST_ACTIVE,
                    error_status=0,
                    sequence=index,
                )
                for index in range(sample_count)
            ]

        with self.assertRaisesRegex(RuntimeError, "restoration") as raised:
            execute_selftest_sequence(
                object(),
                firmware_config={"adcmax": ADCMAX},
                sample_count=2,
                timeout_sec=1.0,
                tolerance_percent_full_scale=0.1,
                max_std_counts=1.0,
                command_fn=command,
                start_stream_fn=lambda *_args, **_kwargs: b"packet",
                collect_fn=collect,
                stop_fn=lambda *_args, **_kwargs: None,
            )
        self.assertEqual(
            raised.exception.selftest_restoration["status"],
            "FAIL",
        )
        self.assertTrue(
            raised.exception.selftest_restoration["attempted"]
        )
        self.assertTrue(raised.exception.selftest_restoration["evidence"])

    def test_sequence_gap_fails_selftest_acquisition(self) -> None:
        current_mode = {"value": "off"}

        def command(_port, command: str, **_kwargs) -> bytes:
            if command.startswith("ads selftest "):
                if command != "ads selftest status":
                    current_mode["value"] = command.rsplit(" ", 1)[-1]
                return (json.dumps({"selftest_mode": current_mode["value"]}) + "\n").encode()
            return b"ok\n"

        def collect(_port, *, sample_count: int, **_kwargs):
            code = expected_selftest_codes(ADCMAX)[current_mode["value"]]
            return [
                SimpleNamespace(
                    lead_i_code=code,
                    lead_ii_code=code,
                    protocol_version=VERSION_V2,
                    sample_count=1,
                    native_rate_numerator=V2_NATIVE_RATE_NUMERATOR,
                    flags=V2_FLAG_CONFIG_VERIFIED | V2_FLAG_SELFTEST_ACTIVE,
                    error_status=0,
                    sequence=sequence,
                )
                for sequence in (10, 12)
            ][:sample_count]

        with self.assertRaisesRegex(RuntimeError, "sequence"):
            execute_selftest_sequence(
                object(),
                firmware_config={"adcmax": ADCMAX},
                sample_count=2,
                timeout_sec=1.0,
                tolerance_percent_full_scale=0.1,
                max_std_counts=1.0,
                command_fn=command,
                start_stream_fn=lambda *_args, **_kwargs: b"packet",
                collect_fn=collect,
                stop_fn=lambda *_args, **_kwargs: None,
            )

    def test_main_unverified_override_is_fail_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            args = self._cli_args(output_dir, allow_unverified=True)
            handshake = SimpleNamespace(
                config={"adcmax": ADCMAX},
                compatibility_failures=("register fingerprint mismatch",),
            )
            numeric_report = {
                "created_at_utc": "2026-07-18T00:00:00Z",
                "test_type": "ADS1293_internal_selftest",
                "statement": SELFTEST_STATEMENT,
                "modes": {},
                "restoration": {
                    "attempted": True,
                    "status": "PASS",
                    "evidence": ["synthetic restoration evidence"],
                },
                "status": "PASS",
            }
            with (
                mock.patch.object(ads1293_selftest, "parse_args", return_value=args),
                mock.patch.object(
                    ads1293_selftest.serial,
                    "Serial",
                    return_value=self._fake_serial_context(),
                ),
                mock.patch.object(
                    ads1293_selftest,
                    "verified_config_handshake",
                    return_value=handshake,
                ),
                mock.patch.object(
                    ads1293_selftest,
                    "execute_selftest_sequence",
                    return_value=numeric_report,
                ),
            ):
                exit_code = ads1293_selftest.main()

            self.assertEqual(exit_code, 1)
            reports = list(output_dir.glob("selftest_*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "FAIL_UNVERIFIED")
            self.assertEqual(report["numeric_selftest_status"], "PASS")
            self.assertTrue(report["firmware_verification"]["override_used"])
            self.assertEqual(
                report["firmware_compatibility_failures"],
                ["register fingerprint mismatch"],
            )

    def test_main_handshake_exception_writes_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            args = self._cli_args(output_dir)
            with (
                mock.patch.object(ads1293_selftest, "parse_args", return_value=args),
                mock.patch.object(
                    ads1293_selftest.serial,
                    "Serial",
                    return_value=self._fake_serial_context(),
                ),
                mock.patch.object(
                    ads1293_selftest,
                    "verified_config_handshake",
                    side_effect=RuntimeError("synthetic handshake failure"),
                ),
            ):
                exit_code = ads1293_selftest.main()

            self.assertEqual(exit_code, 2)
            reports = list(output_dir.glob("selftest_*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["test_type"], "ADS1293_internal_selftest")
            self.assertEqual(report["statement"], SELFTEST_STATEMENT)
            self.assertIn("synthetic handshake failure", report["error"])
            self.assertEqual(report["serial_settings"]["port"], "/dev/synthetic")
            self.assertFalse(report["restoration"]["attempted"])
            self.assertEqual(report["restoration"]["status"], "UNKNOWN")

    def test_main_sequence_exception_persists_restoration_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            args = self._cli_args(output_dir)
            handshake = SimpleNamespace(
                config={"adcmax": ADCMAX},
                compatibility_failures=(),
            )
            sequence_error = RuntimeError("synthetic sequence failure")
            sequence_error.selftest_restoration = {
                "attempted": True,
                "status": "PASS",
                "evidence": ["ads selftest status reported selftest_mode=off"],
            }
            with (
                mock.patch.object(ads1293_selftest, "parse_args", return_value=args),
                mock.patch.object(
                    ads1293_selftest.serial,
                    "Serial",
                    return_value=self._fake_serial_context(),
                ),
                mock.patch.object(
                    ads1293_selftest,
                    "verified_config_handshake",
                    return_value=handshake,
                ),
                mock.patch.object(
                    ads1293_selftest,
                    "execute_selftest_sequence",
                    side_effect=sequence_error,
                ),
            ):
                exit_code = ads1293_selftest.main()

            self.assertEqual(exit_code, 2)
            reports = list(output_dir.glob("selftest_*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["restoration"]["status"], "PASS")
            self.assertTrue(report["restoration"]["attempted"])
            self.assertEqual(report["firmware_config"], {"adcmax": ADCMAX})


if __name__ == "__main__":
    unittest.main()

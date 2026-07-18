from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from ads1293_session import ADCMAX, FirmwareCompatibilityError, TARGET_STATIC_REGISTERS
from crc16 import compute as crc16_compute
import receiver


def _packet(
    sequence: int,
    *,
    sample_count: int = 1,
    native_rate_numerator: int = 800,
    flags: int = 1,
    lead_i: int | None = None,
    lead_ii: int | None = None,
) -> bytes:
    lead_i = 0x798000 + (sequence % 100) if lead_i is None else lead_i
    lead_ii = 0x798000 + 2 * (sequence % 100) if lead_ii is None else lead_ii
    packet = bytearray(b"\xAA\x55")
    packet.extend(struct.pack("<BH", 2, 25))
    packet.extend(
        struct.pack(
            "<IIHHBIII",
            sequence,
            sequence * 3750,
            sample_count,
            native_rate_numerator,
            flags,
            lead_i,
            lead_ii,
            0,
        )
    )
    packet.extend(struct.pack("<H", crc16_compute(packet)))
    return bytes(packet)


def _firmware_config() -> dict:
    return {
        "config_verified": True,
        "device_id": 1,
        "protocol_version": 2,
        "configuration_profile": "ADS1293_NATIVE_266P667_SPS",
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
        "firmware_commit": "integration-test",
        "firmware_dirty": True,
        "firmware_build_time_utc": "2026-07-18T00:00:00Z",
        "firmware_build_environment": "esp32dev_ads1293_usb",
        "selftest_mode": "off",
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


class _FakeSerial:
    def __init__(self, *args, **kwargs) -> None:
        self._input = bytearray()
        self.is_open = True
        self.dtr = True
        self.rts = True

    def write(self, data: bytes) -> int:
        command = data.decode("ascii").strip().lower()
        if command == "ads config dump":
            self._input.extend((json.dumps(self.firmware_config()) + "\n").encode())
        elif command == "ads stream start":
            self._input.extend(b"ads binary stream started\n")
            for packet in self.stream_packets():
                self._input.extend(packet)
        else:
            self._input.extend((command + " ok\n").encode())
        return len(data)

    def stream_packets(self):
        return (_packet(sequence) for sequence in range(2680))

    def firmware_config(self) -> dict:
        return _firmware_config()

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        if not self._input:
            return b""
        count = min(size, len(self._input))
        result = bytes(self._input[:count])
        del self._input[:count]
        return result

    def reset_input_buffer(self) -> None:
        self._input.clear()

    def close(self) -> None:
        self.is_open = False


class _BadNumeratorSerial(_FakeSerial):
    def stream_packets(self):
        return (_packet(0, native_rate_numerator=799),)


class _SelfTestStartupSerial(_FakeSerial):
    def stream_packets(self):
        return (_packet(0, flags=1 | receiver.V2_FLAG_SELFTEST_ACTIVE),)


class _UnverifiedStartupSerial(_FakeSerial):
    def stream_packets(self):
        return (_packet(0, flags=0),)


class _TrustOnlyOverrideSerial(_FakeSerial):
    def firmware_config(self) -> dict:
        config = _firmware_config()
        config["config_verified"] = False
        return config

    def stream_packets(self):
        return (_packet(sequence, flags=0) for sequence in range(2680))


class _TrustOnlySelfTestSerial(_TrustOnlyOverrideSerial):
    def stream_packets(self):
        return (_packet(0, flags=receiver.V2_FLAG_SELFTEST_ACTIVE),)


class _StaleBlockSerial(_FakeSerial):
    def stream_packets(self):
        sequences = [*range(0, 101), *range(90, 100), *range(101, 2780)]
        return (_packet(sequence) for sequence in sequences)


class _NearRailSerial(_FakeSerial):
    def stream_packets(self):
        for sequence in range(4000):
            if sequence == 1000:
                yield _packet(sequence, lead_i=8192, lead_ii=ADCMAX // 2)
            else:
                yield _packet(sequence)


class _BoundaryAdsErrorSerial(_FakeSerial):
    def stream_packets(self):
        for sequence in range(6000):
            flags = (
                1 | receiver.V2_FLAG_ADS_ERROR
                if sequence == 2676
                else 1
            )
            yield _packet(sequence, flags=flags)


class _MutatingCalibrationSerial(_FakeSerial):
    calibration_path: Path | None = None

    def stream_packets(self):
        assert self.calibration_path is not None
        self.calibration_path.write_text("invalid: [\n", encoding="utf-8")
        return super().stream_packets()


def _run_receiver(
    root: Path,
    serial_type,
    *,
    max_frames: int = 1,
    calibration_config_path: Path | None = None,
    allow_unverified_firmware: bool = False,
) -> tuple[int, Path]:
    output_dir = root / "data"
    args = Namespace(
        allow_unverified_firmware=allow_unverified_firmware,
        native_capture=False,
        max_frames=max_frames,
    )
    with patch.object(receiver.serial, "Serial", serial_type):
        result = receiver.run_corrected_ads1293_receiver(
            args=args,
            config={
                "serial": {"config_response_timeout_seconds": 0.12},
                "output": {"native_directory": "native"},
                "runtime": {"saturation_margin_counts": 8192},
            },
            config_path=Path(receiver.__file__).with_name("config.yaml"),
            port="fake",
            baudrate=115200,
            timeout_seconds=0.001,
            read_size_bytes=1024,
            boot_delay_sec=0,
            output_dir=output_dir,
            raw_csv_name="raw_ecg.csv",
            sqlite_db_path=output_dir / "metadata.sqlite",
            calibration_config_path=(
                calibration_config_path
                if calibration_config_path is not None
                else Path(receiver.__file__).with_name(
                    "calibration_config_ads1293_266p667_nominal.yaml"
                )
            ),
            calibrated_output_dir=output_dir / "calibrated",
            model_ready_enabled=False,
            model_ready_output_dir=output_dir / "model_ready",
            model_ready_max_abs_mv=10.0,
            model_ready_preprocessing_config_path=None,
            sample_rate_hz=250,
            frame_seconds=10,
            status_interval_seconds=999,
            state="stored",
        )
    return result, output_dir


class ReceiverV2IntegrationTests(unittest.TestCase):
    def test_verified_stream_reaches_one_canonical_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, output_dir = _run_receiver(root, _FakeSerial)

            self.assertEqual(result, 0)
            frame = __import__("numpy").load(
                output_dir / "frame_000001.npy",
                allow_pickle=False,
            )
            metadata = json.loads(
                (output_dir / "frame_000001.json").read_text(encoding="utf-8")
            )
            session = json.loads(
                next(output_dir.glob("session_*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(frame.shape, (2500, 3))
            self.assertEqual(str(frame.dtype), "float32")
            self.assertEqual(metadata["protocol_version"], 2)
            self.assertEqual(metadata["acquisition_status"], "PASS")
            self.assertEqual(metadata["signal_quality_reasons"], [])
            self.assertEqual(metadata["signal_quality"]["reasons"], [])
            self.assertEqual(metadata["packet_loss_count_frame"], 0)
            self.assertEqual(metadata["packet_loss_count_cumulative"], 0)
            self.assertEqual(metadata["sequence_gap_events_frame"], 0)
            self.assertEqual(metadata["sequence_gap_events_cumulative"], 0)
            self.assertEqual(metadata["resampler_reset_count_frame"], 0)
            self.assertEqual(metadata["resampler_reset_count_cumulative"], 0)
            self.assertEqual(
                metadata["counter_semantics"]["resampler_reset_count"],
                "compatibility alias of resampler_reset_count_cumulative",
            )
            self.assertEqual(session["conversion_source"], "datasheet_nominal")
            self.assertEqual(session["calibration_status"], "preliminary")
            self.assertNotEqual(metadata["device_id"], "esp32dev_dummy")
            self.assertEqual(metadata["unit"], "ADC_count")
            self.assertEqual(
                metadata["sample_domain"], "resampled_offset_code_counts"
            )

            calibrated = __import__("numpy").load(
                output_dir / "calibrated" / "frame_000001_mv.npy",
                allow_pickle=False,
            )
            calibrated_metadata = json.loads(
                (output_dir / "calibrated" / "frame_000001_mv.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertLess(float(abs(calibrated).max()), 0.02)
            self.assertEqual(calibrated_metadata["unit"], "mV")
            self.assertEqual(
                calibrated_metadata["sample_domain"],
                "input_referred_millivolts",
            )

    def test_crc_valid_packet_semantic_mutations_are_rejected(self) -> None:
        cases = (
            ({"sample_count": 2}, "sample_count=2"),
            ({"native_rate_numerator": 799}, "native_rate_numerator=799"),
            ({"flags": 1 | 0x40}, "reserved flags are set"),
        )
        for mutation, message in cases:
            with self.subTest(mutation=mutation):
                parser = receiver.PacketParser(allowed_versions={2})
                packet = parser.feed(_packet(0, **mutation))[0]
                with self.assertRaisesRegex(FirmwareCompatibilityError, message):
                    receiver.validate_v2_packet_semantics(
                        packet,
                        expected_native_rate_numerator=800,
                    )

    def test_startup_abi_or_acquisition_flags_never_persist_pass_session(self) -> None:
        for serial_type in (
            _BadNumeratorSerial,
            _SelfTestStartupSerial,
            _UnverifiedStartupSerial,
        ):
            with self.subTest(serial_type=serial_type.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    result, output_dir = _run_receiver(
                        Path(directory),
                        serial_type,
                        max_frames=0,
                    )
                    self.assertEqual(result, 2)
                    self.assertFalse(list(output_dir.glob("session_*.json")))
                    self.assertFalse(list(output_dir.glob("frame_*.npy")))

    def test_trust_only_override_captures_raw_as_fail_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, output_dir = _run_receiver(
                Path(directory),
                _TrustOnlyOverrideSerial,
                allow_unverified_firmware=True,
            )
            self.assertEqual(result, 0)

            session = json.loads(
                next(output_dir.glob("session_*.json")).read_text(encoding="utf-8")
            )
            frame = json.loads(
                (output_dir / "frame_000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session["acquisition_status"], "FAIL_UNVERIFIED")
            self.assertTrue(session["firmware_verification_override"])
            self.assertEqual(session["conversion_source"], "unavailable")
            self.assertEqual(session["calibration_status"], "incompatible")
            self.assertEqual(frame["acquisition_status"], "FAIL_UNVERIFIED")
            self.assertIn("firmware_unverified", frame["acquisition_reasons"])
            self.assertIn(
                "packet_missing_config_verified_flag",
                frame["acquisition_reasons"],
            )
            self.assertFalse(list((output_dir / "calibrated").glob("*.npy")))

    def test_trust_only_override_still_rejects_abnormal_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, output_dir = _run_receiver(
                Path(directory),
                _TrustOnlySelfTestSerial,
                max_frames=0,
                allow_unverified_firmware=True,
            )
            self.assertEqual(result, 2)
            self.assertFalse(list(output_dir.glob("session_*.json")))
            self.assertFalse(list(output_dir.glob("frame_*.npy")))

    def test_stale_block_is_dropped_until_original_expected_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, output_dir = _run_receiver(
                Path(directory),
                _StaleBlockSerial,
            )
            self.assertEqual(result, 0)
            metadata = json.loads(
                (output_dir / "frame_000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["native_sequence_start"], 101)
            self.assertEqual(metadata["parser_stats"]["out_of_order_count"], 10)
            self.assertEqual(metadata["resampler_reset_count"], 1)
            self.assertEqual(metadata["resampler_reset_count_cumulative"], 1)
            self.assertEqual(metadata["resampler_reset_count_frame"], 0)
            self.assertEqual(metadata["packet_loss_count_frame"], 0)
            self.assertEqual(metadata["sequence_gap_events_frame"], 0)
            self.assertEqual(metadata["acquisition_status"], "PASS")

            events = [
                json.loads(line)
                for line in (output_dir / "acquisition_events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["expected_sequence"], 101)
            self.assertEqual(events[0]["received_sequence"], 90)

    def test_native_near_rail_packet_is_quarantined_before_dsp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, output_dir = _run_receiver(
                Path(directory),
                _NearRailSerial,
            )
            self.assertEqual(result, 0)
            metadata = json.loads(
                (output_dir / "frame_000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["signal_quality_status"], "PASS")
            self.assertGreaterEqual(metadata["native_sequence_start"], 1001)
            self.assertGreaterEqual(metadata["resampler_reset_count_cumulative"], 1)
            events = [
                json.loads(line)
                for line in (output_dir / "acquisition_events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            quarantine = [
                event for event in events
                if event.get("event") == "native_packet_quarantined"
            ]
            self.assertEqual(len(quarantine), 1)
            self.assertEqual(quarantine[0]["native_sequence"], 1000)
            self.assertEqual(quarantine[0]["status"], "WARNING")
            self.assertTrue(
                any("low ADC rail" in reason for reason in quarantine[0]["reasons"])
            )

    def test_ads_error_at_fir_frame_boundary_cannot_taint_pass_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, output_dir = _run_receiver(
                Path(directory),
                _BoundaryAdsErrorSerial,
            )
            self.assertEqual(result, 0)
            metadata = json.loads(
                (output_dir / "frame_000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["acquisition_status"], "PASS")
            self.assertGreaterEqual(metadata["native_sequence_start"], 2677)
            self.assertTrue(
                (output_dir / "calibrated" / "frame_000001_mv.npy").exists()
            )
            events = [
                json.loads(line)
                for line in (output_dir / "acquisition_events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            quarantine = [
                event for event in events
                if event.get("event") == "native_packet_quarantined"
            ]
            self.assertEqual(len(quarantine), 1)
            self.assertEqual(quarantine[0]["native_sequence"], 2676)
            self.assertEqual(quarantine[0]["status"], "FAIL")
            self.assertIn("ads1293_error_status", quarantine[0]["reasons"])

    def test_existing_output_artifact_fails_before_serial_open(self) -> None:
        class MustNotOpenSerial:
            def __init__(self, *args, **kwargs) -> None:
                raise AssertionError("serial must not open when output is not clean")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "data"
            output_dir.mkdir(parents=True)
            (output_dir / "raw_ecg.csv").write_text("old data\n", encoding="utf-8")
            result, _ = _run_receiver(root, MustNotOpenSerial)
            self.assertEqual(result, 2)

    def test_calibration_file_change_does_not_change_session_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(receiver.__file__).with_name(
                "calibration_config_ads1293_266p667_nominal.yaml"
            )
            calibration_path = root / "calibration.yaml"
            calibration_path.write_bytes(source.read_bytes())
            _MutatingCalibrationSerial.calibration_path = calibration_path
            result, output_dir = _run_receiver(
                root,
                _MutatingCalibrationSerial,
                calibration_config_path=calibration_path,
            )
            self.assertEqual(result, 0)
            session_path = next(output_dir.glob("session_*.json"))
            session = json.loads(session_path.read_text(encoding="utf-8"))
            calibrated = json.loads(
                (output_dir / "calibrated" / "frame_000001_mv.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                calibrated["calibration"]["calibration_config_sha256"],
                session["calibration_config_sha256"],
            )


if __name__ == "__main__":
    unittest.main()

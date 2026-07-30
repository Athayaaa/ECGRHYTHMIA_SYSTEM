"""Unit tests for mqtt_publisher.py."""

from __future__ import annotations

import dataclasses
import json
import os
from io import BytesIO
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, mock

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import mqtt_publisher


def make_frame(shape=(2500, 3), dtype=np.float32) -> np.ndarray:
    frame = np.zeros(shape, dtype=dtype)
    if shape == (2500, 3):
        frame[:, 0] = np.linspace(-0.1, 0.1, 2500, dtype=dtype)
        frame[:, 1] = np.linspace(0.2, 0.4, 2500, dtype=dtype)
        frame[:, 2] = frame[:, 1] - frame[:, 0]
    return frame


def make_metadata(status="PASS") -> dict:
    return {
        "unit": "mV",
        "shape": [2500, 3],
        "dtype": "float32",
        "sample_rate_hz": 250,
        "duration_seconds": 10.0,
        "channel_order": ["Lead I", "Lead II", "Lead III"],
        "validation": {"status": status, "warnings": ["Large DC baseline detected"] if status == "WARNING" else []},
        "source_files": {"/bad": "/home/athaya/source-not-published"},
    }


def make_prediction(status="PASS") -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "frame_id": "000001",
        "source_file": "/home/athaya/model-ready-not-published.npy",
        "created_at": "2026-07-22T10:00:00Z",
        "sampling_rate_hz": 250.0,
        "shape": [2500, 3],
        "dtype": "float32",
        "unit": "mV",
        "prediction": "Normal",
        "confidence_percent": 98.7,
        "probabilities": {
            "Normal": 98.7,
            "AF": 0.2,
            "Takikardia": 0.1,
            "Bradikardia": 1.0,
        },
        "threshold": 0.5,
        "latency_ms": 12.0,
        "model_path": "/home/athaya/model-not-published.tflite",
        "runtime": "ai-edge-litert",
        "warning": "input warning" if status == "WARNING" else None,
    }


def write_pair(root: Path, frame_id="000001", frame=None, metadata=None, prediction=None) -> tuple[Path, Path]:
    ecg_dir = root / "data" / "calibrated"
    prediction_dir = root / "data" / "predictions"
    ecg_dir.mkdir(parents=True, exist_ok=True)
    prediction_dir.mkdir(parents=True, exist_ok=True)
    frame = make_frame() if frame is None else frame
    metadata = make_metadata() if metadata is None else metadata
    prediction = make_prediction() if prediction is None else prediction
    prediction["frame_id"] = frame_id
    np.save(ecg_dir / f"frame_{frame_id}_mv.npy", frame)
    (ecg_dir / f"frame_{frame_id}_mv.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    (prediction_dir / f"frame_{frame_id}_prediction.json").write_text(
        json.dumps(prediction),
        encoding="utf-8",
    )
    return ecg_dir, prediction_dir


class MockPublishInfo:
    def __init__(self, rc=0, ack_log=None, topic=None):
        self.rc = rc
        self.acked = False
        self.ack_log = ack_log
        self.topic = topic

    def wait_for_publish(self):
        self.acked = True
        if self.ack_log is not None:
            self.ack_log.append(self.topic)
        return True

    def is_published(self):
        return self.acked


class MockMqttClient:
    def __init__(self, rc=0):
        self.rc = rc
        self.published = []
        self.infos = []
        self.ack_log = []
        self.info = MockPublishInfo(rc=0)
        self.connected = False
        self.tls_context = None
        self.tls_insecure = None

    def username_pw_set(self, username, password):
        self.username = username
        self.password = password

    def tls_set_context(self, context):
        self.tls_context = context

    def tls_insecure_set(self, value):
        self.tls_insecure = value

    def connect(self, broker, port):
        self.connected = True
        self.broker = broker
        self.port = port
        return 0

    def loop_start(self):
        self.loop_started = True

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        info = MockPublishInfo(rc=self.rc, ack_log=self.ack_log, topic=topic)
        self.infos.append(info)
        self.info = info
        return info


class FakeMemory:
    percent = 36.7
    used = 812 * 1024 * 1024


class FakePsutil:
    def __init__(self, *, temperatures=None):
        self.cpu_calls = []
        self.temperatures = temperatures if temperatures is not None else {}

    def cpu_percent(self, interval=None):
        self.cpu_calls.append(interval)
        return 41.2

    def virtual_memory(self):
        return FakeMemory()

    def boot_time(self):
        return 1000.0

    def sensors_temperatures(self):
        return self.temperatures


class SequencedMockMqttClient(MockMqttClient):
    def __init__(self, rcs):
        super().__init__(rc=0)
        self.rcs = list(rcs)

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        rc = self.rcs.pop(0) if self.rcs else 0
        info = MockPublishInfo(rc=rc, ack_log=self.ack_log, topic=topic)
        self.infos.append(info)
        self.info = info
        return info

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.connected = False


class DiscoveryTests(TestCase):
    def test_matching_frame_id_and_numerical_sorting(self):
        with tempfile.TemporaryDirectory() as td:
            ecg_dir = Path(td)
            for frame_id in ("000010", "000002", "000001"):
                np.save(ecg_dir / f"frame_{frame_id}_mv.npy", make_frame())
            np.save(ecg_dir / "frame_999999_input.npy", make_frame())

            frames = mqtt_publisher.discover_ecg_frames(ecg_dir)

            self.assertEqual([frame_id for frame_id, _ in frames], ["000001", "000002", "000010"])

    def test_latest_prediction_is_not_matched(self):
        with tempfile.TemporaryDirectory() as td:
            prediction_dir = Path(td)
            (prediction_dir / "latest_prediction.json").write_text("{}", encoding="utf-8")
            self.assertIsNone(mqtt_publisher.prediction_path_for(prediction_dir, "000001"))


class PayloadValidationTests(TestCase):
    def test_parser_defaults_to_json_and_accepts_only_known_formats(self):
        parser = mqtt_publisher.build_parser()
        base_args = [
            "--ecg-dir",
            "data/calibrated",
            "--prediction-dir",
            "data/predictions",
            "--broker",
            "localhost",
            "--topic",
            "ecgrhythmia/device01",
            "--device-id",
            "device01",
        ]

        default_config = mqtt_publisher.resolve_config(parser.parse_args(base_args))
        json_config = mqtt_publisher.resolve_config(
            parser.parse_args([*base_args, "--payload-format", "json"])
        )
        npy_config = mqtt_publisher.resolve_config(
            parser.parse_args([*base_args, "--payload-format", "npy"])
        )

        self.assertEqual(default_config.payload_format, "json")
        self.assertEqual(json_config.payload_format, "json")
        self.assertEqual(npy_config.payload_format, "npy")
        exact_config = mqtt_publisher.resolve_config(
            parser.parse_args([*base_args, "--json-topic-exact"])
        )
        self.assertTrue(exact_config.json_topic_exact)
        repeat_config = mqtt_publisher.resolve_config(
            parser.parse_args(
                [*base_args, "--frame-id", "1", "--force-republish", "--follow"]
            )
        )
        self.assertEqual(repeat_config.frame_id, "000001")
        self.assertTrue(repeat_config.force_republish)
        self.assertTrue(repeat_config.follow)
        with self.assertRaises(SystemExit):
            parser.parse_args([*base_args, "--payload-format", "csv"])

    def test_prediction_missing_keeps_frame_waiting(self):
        with tempfile.TemporaryDirectory() as td:
            ecg_dir, prediction_dir = write_pair(Path(td))
            (prediction_dir / "frame_000001_prediction.json").unlink()
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01/frame",
                device_id="device01",
                dry_run=True,
                stable_delay=0,
            )
            state = mqtt_publisher.PublishState(path=prediction_dir / "mqtt_publish_state.json")

            stats = mqtt_publisher.scan_once(config, state=state, connection=None)

            self.assertEqual(stats.waiting, 1)
            self.assertEqual(stats.ready, 0)

    def test_pass_payload_contains_2500x3_mv_and_prediction_source_values(self):
        with tempfile.TemporaryDirectory() as td:
            ecg_dir, prediction_dir = write_pair(Path(td))
            prepared = mqtt_publisher.prepare_frame(
                ecg_dir / "frame_000001_mv.npy",
                prediction_dir / "frame_000001_prediction.json",
                device_id="device01",
                ecg_dir=ecg_dir,
            )
            payload = prepared.payload

            self.assertEqual(payload["unit"], "mV")
            self.assertEqual(payload["sampling_rate_hz"], 250.0)
            self.assertEqual(payload["shape"], [2500, 3])
            self.assertEqual(len(payload["ecg"]["samples"]), 2500)
            self.assertEqual(len(payload["ecg"]["samples"][0]), 3)
            self.assertEqual(payload["prediction"]["label"], "Normal")
            self.assertEqual(payload["prediction"]["confidence_percent"], 98.7)
            self.assertEqual(
                payload["prediction"]["probabilities"],
                make_prediction()["probabilities"],
            )
            self.assertNotIn("/home/athaya", prepared.payload_bytes.decode("utf-8"))

    def test_payload_includes_system_and_default_stress_test_fields(self):
        fake_psutil = FakePsutil()
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            mqtt_publisher, "psutil", fake_psutil
        ), mock.patch.object(mqtt_publisher.time, "time", return_value=20352.0):
            root = Path(td)
            thermal = root / "temp"
            thermal.write_text("56900", encoding="utf-8")
            ecg_dir, prediction_dir = write_pair(root, frame_id="001936")
            telemetry = mqtt_publisher.SystemTelemetry(thermal_path=thermal)
            prepared = mqtt_publisher.prepare_frame(
                ecg_dir / "frame_001936_mv.npy",
                prediction_dir / "frame_001936_prediction.json",
                device_id="device01",
                ecg_dir=ecg_dir,
                telemetry=telemetry,
            )

            system = prepared.payload["system"]
            self.assertEqual(system["cpu_usage_percent"], 41.2)
            self.assertEqual(system["memory_usage_percent"], 36.7)
            self.assertEqual(system["memory_usage_mb"], 812)
            self.assertEqual(system["cpu_temperature_c"], 56.9)
            self.assertEqual(system["uptime_s"], 19352)
            self.assertTrue(0 <= system["cpu_usage_percent"] <= 100)
            self.assertTrue(0 <= system["memory_usage_percent"] <= 100)
            self.assertIsInstance(system["memory_usage_mb"], int)
            self.assertIsInstance(system["uptime_s"], int)
            self.assertEqual(prepared.payload["stress_test"], {"enabled": False, "frame_counter": 1936})
            self.assertEqual(
                prepared.payload["network"],
                {
                    "mqtt_publish_latency_ms": None,
                    "wifi_rssi_dbm": None,
                    "mqtt_connected": False,
                },
            )
            self.assertNotIn("power", system)
            self.assertNotIn("battery_voltage_v", system)
            self.assertNotIn("battery_percent", system)
            json.dumps(prepared.payload, separators=(",", ":"), allow_nan=False)

    def test_session_id_from_calibrated_metadata_is_used_for_pairing(self):
        with tempfile.TemporaryDirectory() as td:
            metadata = make_metadata()
            metadata["source_metadata"] = {"session_id": "session_28072026_153845"}
            ecg_dir, prediction_dir = write_pair(Path(td), metadata=metadata)

            prepared = mqtt_publisher.prepare_frame(
                ecg_dir / "frame_000001_mv.npy",
                prediction_dir / "frame_000001_prediction.json",
                device_id="device01",
                ecg_dir=ecg_dir,
            )

            self.assertEqual(prepared.session_id, "session_28072026_153845")
            self.assertEqual(prepared.payload["session_id"], "session_28072026_153845")

    def test_temperature_unavailable_is_null(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            mqtt_publisher, "psutil", FakePsutil()
        ):
            telemetry = mqtt_publisher.SystemTelemetry(thermal_path=Path(td) / "missing")
            system, _stress = telemetry.collect("not-numeric", stress_enabled=True)

            self.assertIsNone(system["cpu_temperature_c"])
            self.assertEqual(_stress, {"enabled": True, "frame_counter": 1})

    def test_stress_test_cli_flag_is_preserved(self):
        parser = mqtt_publisher.build_parser()
        base_args = [
            "--ecg-dir", "data/calibrated", "--prediction-dir", "data/predictions",
            "--broker", "localhost", "--topic", "ecgrhythmia/device01", "--device-id", "device01",
        ]
        self.assertFalse(mqtt_publisher.resolve_config(parser.parse_args(base_args)).stress_test)
        self.assertTrue(
            mqtt_publisher.resolve_config(parser.parse_args([*base_args, "--stress-test"])).stress_test
        )

    def test_warning_payload_is_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            ecg_dir, prediction_dir = write_pair(
                Path(td),
                metadata=make_metadata(status="WARNING"),
                prediction=make_prediction(status="WARNING"),
            )
            prepared = mqtt_publisher.prepare_frame(
                ecg_dir / "frame_000001_mv.npy",
                prediction_dir / "frame_000001_prediction.json",
                device_id="device01",
                ecg_dir=ecg_dir,
            )

            self.assertEqual(prepared.validation_status, "WARNING")
            self.assertEqual(prepared.payload["prediction"]["status"], "WARNING")

    def test_fail_prediction_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            ecg_dir, prediction_dir = write_pair(Path(td), prediction=make_prediction(status="FAIL"))

            with self.assertRaisesRegex(ValueError, "prediction status is FAIL"):
                mqtt_publisher.prepare_frame(
                    ecg_dir / "frame_000001_mv.npy",
                    prediction_dir / "frame_000001_prediction.json",
                    device_id="device01",
                    ecg_dir=ecg_dir,
                )

    def test_wrong_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            ecg_dir, prediction_dir = write_pair(Path(td), frame=np.zeros((100, 3), dtype=np.float32))

            with self.assertRaisesRegex(ValueError, "invalid shape"):
                mqtt_publisher.prepare_frame(
                    ecg_dir / "frame_000001_mv.npy",
                    prediction_dir / "frame_000001_prediction.json",
                    device_id="device01",
                    ecg_dir=ecg_dir,
                )

    def test_nan_and_inf_are_rejected(self):
        for bad_value in (np.nan, np.inf):
            with self.subTest(bad_value=bad_value):
                with tempfile.TemporaryDirectory() as td:
                    frame = make_frame()
                    frame[0, 0] = bad_value
                    ecg_dir, prediction_dir = write_pair(Path(td), frame=frame)

                    with self.assertRaisesRegex(ValueError, "NaN or Inf"):
                        mqtt_publisher.prepare_frame(
                            ecg_dir / "frame_000001_mv.npy",
                            prediction_dir / "frame_000001_prediction.json",
                            device_id="device01",
                            ecg_dir=ecg_dir,
                        )

    def test_source_npy_and_prediction_json_are_not_modified(self):
        with tempfile.TemporaryDirectory() as td:
            ecg_dir, prediction_dir = write_pair(Path(td))
            ecg_path = ecg_dir / "frame_000001_mv.npy"
            prediction_path = prediction_dir / "frame_000001_prediction.json"
            before = (mqtt_publisher.sha256_file(ecg_path), mqtt_publisher.sha256_file(prediction_path))

            mqtt_publisher.prepare_frame(
                ecg_path,
                prediction_path,
                device_id="device01",
                ecg_dir=ecg_dir,
            )

            after = (mqtt_publisher.sha256_file(ecg_path), mqtt_publisher.sha256_file(prediction_path))
            self.assertEqual(before, after)


class StateAndPublishTests(TestCase):
    def test_ack_marks_frame_sent_and_restart_skips_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            state_path = prediction_dir / "mqtt_publish_state.json"
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01/frame",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            client = MockMqttClient()

            stats = mqtt_publisher.run_publisher(config, client=client)

            self.assertEqual(stats.sent, 1)
            self.assertTrue(client.info.acked)
            self.assertTrue(state_path.exists())
            self.assertFalse(list(prediction_dir.glob("*.tmp")))
            self.assertEqual(client.published[0][0], "ecgrhythmia/device01/frame/json")
            self.assertEqual(client.published[0][2], 1)
            self.assertFalse(client.published[0][3])

            second_client = MockMqttClient()
            second_stats = mqtt_publisher.run_publisher(config, client=second_client)

            self.assertEqual(second_stats.skipped, 1)
            self.assertEqual(second_client.published, [])

    def test_json_publish_uses_frame_json_topic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=prediction_dir / "mqtt_publish_state.json",
            )
            client = MockMqttClient()

            stats = mqtt_publisher.run_publisher(config, client=client)

            self.assertEqual(stats.sent, 1)
            self.assertEqual(len(client.published), 1)
            topic, payload, qos, retain = client.published[0]
            self.assertEqual(topic, "ecgrhythmia/device01/frame/json")
            self.assertEqual(qos, 1)
            self.assertFalse(retain)
            decoded = json.loads(payload.decode("utf-8"))
            self.assertEqual(decoded["ecg"]["format"], "samples_by_time")
            self.assertEqual(decoded["frame_id"], "000001")
            self.assertIn("system", decoded)
            self.assertIn("stress_test", decoded)
            self.assertEqual(decoded["network"]["mqtt_publish_latency_ms"], None)
            self.assertTrue(decoded["network"]["mqtt_connected"])

    def test_json_publish_can_use_exact_configured_topic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                json_topic_exact=True,
                dry_run=False,
                stable_delay=0,
                state_path=prediction_dir / "mqtt_publish_state.json",
            )
            client = MockMqttClient()

            stats = mqtt_publisher.run_publisher(config, client=client)

            self.assertEqual(stats.sent, 1)
            self.assertEqual(client.published[0][0], "ecgrhythmia/device01")

    def test_exact_json_topic_is_not_skipped_after_default_json_publish(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            state_path = prediction_dir / "mqtt_publish_state.json"
            default_config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            exact_config = dataclasses.replace(default_config, json_topic_exact=True)

            mqtt_publisher.run_publisher(default_config, client=MockMqttClient())
            exact_client = MockMqttClient()
            exact_stats = mqtt_publisher.run_publisher(exact_config, client=exact_client)

            self.assertEqual(exact_stats.sent, 1)
            self.assertEqual(exact_client.published[0][0], "ecgrhythmia/device01")

    def test_npy_publish_sends_ecg_then_prediction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            ecg_path = ecg_dir / "frame_000001_mv.npy"
            prediction_path = prediction_dir / "frame_000001_prediction.json"
            source = np.load(ecg_dir / "frame_000001_mv.npy", allow_pickle=False)
            before = (
                mqtt_publisher.sha256_file(ecg_path),
                mqtt_publisher.sha256_file(prediction_path),
            )
            state_path = prediction_dir / "mqtt_publish_state.json"
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                payload_format="npy",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            client = MockMqttClient()

            stats = mqtt_publisher.run_publisher(config, client=client)

            self.assertEqual(stats.sent, 1)
            after = (
                mqtt_publisher.sha256_file(ecg_path),
                mqtt_publisher.sha256_file(prediction_path),
            )
            self.assertEqual(before, after)
            self.assertEqual(len(client.published), 2)
            self.assertEqual(client.published[0][0], "ecgrhythmia/device01/frame/npy")
            self.assertEqual(client.published[1][0], "ecgrhythmia/device01/prediction")
            self.assertEqual(
                client.ack_log,
                ["ecgrhythmia/device01/frame/npy", "ecgrhythmia/device01/prediction"],
            )
            for _, _, qos, retain in client.published:
                self.assertEqual(qos, 1)
                self.assertFalse(retain)

            npy_payload = client.published[0][1]
            restored = np.load(BytesIO(npy_payload), allow_pickle=False)
            np.testing.assert_array_equal(restored, source)
            self.assertEqual(restored.dtype, np.dtype("float32"))
            self.assertNotIn(b"prediction", npy_payload)
            self.assertNotIn(b"Normal", npy_payload)

            prediction_payload = json.loads(client.published[1][1].decode("utf-8"))
            self.assertEqual(prediction_payload["frame_id"], "000001")
            self.assertEqual(prediction_payload["message_id"], "device01-default-frame_000001")
            self.assertEqual(prediction_payload["ecg_format"], "npy")
            self.assertEqual(
                prediction_payload["ecg_topic"],
                "ecgrhythmia/device01/frame/npy",
            )
            self.assertEqual(prediction_payload["shape"], [2500, 3])
            self.assertEqual(
                prediction_payload["channel_order"],
                ["Lead I", "Lead II", "Lead III"],
            )
            self.assertEqual(prediction_payload["prediction"]["label"], "Normal")
            self.assertIn("system", prediction_payload)
            self.assertIn("stress_test", prediction_payload)
            self.assertIn("network", prediction_payload)

    def test_telemetry_failure_does_not_stop_publish(self):
        class BrokenPsutil:
            def cpu_percent(self, interval=None):
                raise RuntimeError("cpu unavailable")

            def virtual_memory(self):
                raise RuntimeError("memory unavailable")

            def boot_time(self):
                raise RuntimeError("uptime unavailable")

            def sensors_temperatures(self):
                raise RuntimeError("temperature unavailable")

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            mqtt_publisher, "psutil", BrokenPsutil()
        ), mock.patch.object(mqtt_publisher, "THERMAL_ZONE_PATH", Path(td) / "missing"):
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=prediction_dir / "mqtt_publish_state.json",
            )
            client = MockMqttClient()

            stats = mqtt_publisher.run_publisher(config, client=client)

            self.assertEqual(stats.sent, 1)
            payload = json.loads(client.published[0][1].decode("utf-8"))
            self.assertEqual(
                payload["system"],
                {
                    "cpu_usage_percent": None,
                    "memory_usage_percent": None,
                    "memory_usage_mb": None,
                    "cpu_temperature_c": None,
                    "uptime_s": None,
                },
            )

    def test_publisher_warms_up_cpu_usage_once(self):
        fake_psutil = FakePsutil()
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            mqtt_publisher, "psutil", fake_psutil
        ):
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                dry_run=True,
                stable_delay=0,
                state_path=prediction_dir / "mqtt_publish_state.json",
            )

            mqtt_publisher.run_publisher(config)

            self.assertEqual(fake_psutil.cpu_calls, [None, None])

    def test_wifi_rssi_reads_dbm_from_procfs(self):
        with tempfile.TemporaryDirectory() as td:
            wireless = Path(td) / "wireless"
            wireless.write_text(
                "Inter-| sta\n face |status\nwlan0: 0000   70.  -59.  -256\n",
                encoding="utf-8",
            )
            self.assertEqual(mqtt_publisher.wifi_rssi_dbm(wireless), -59)

    def test_publish_latency_and_network_metrics(self):
        config = mqtt_publisher.PublisherConfig(
            ecg_dir=Path("."),
            prediction_dir=Path("."),
            broker="localhost",
            topic="ecgrhythmia/device01",
            device_id="device01",
        )
        connection = mqtt_publisher.MqttConnection(config, client=MockMqttClient())
        with mock.patch.object(mqtt_publisher.time, "perf_counter", side_effect=[10.0, 10.0087]), mock.patch.object(
            mqtt_publisher, "wifi_rssi_dbm", return_value=-59
        ):
            connection.publish("ecgrhythmia/device01/frame/json", b"{}", qos=1, retain=False)
            network = mqtt_publisher.collect_network_metrics(connection)

        self.assertEqual(network["mqtt_publish_latency_ms"], 8.7)
        self.assertEqual(network["wifi_rssi_dbm"], -59)
        self.assertTrue(network["mqtt_connected"])

    def test_npy_prediction_failure_does_not_mark_state_sent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            state_path = prediction_dir / "mqtt_publish_state.json"
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                payload_format="npy",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            client = SequencedMockMqttClient([0, 4])

            stats = mqtt_publisher.run_publisher(config, client=client)

            self.assertEqual(stats.errors, 1)
            self.assertEqual(stats.sent, 0)
            self.assertEqual(len(client.published), 2)
            self.assertFalse(state_path.exists())

    def test_state_distinguishes_json_and_npy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            state_path = prediction_dir / "mqtt_publish_state.json"
            json_config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                payload_format="json",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            npy_config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                payload_format="npy",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )

            json_client = MockMqttClient()
            npy_client = MockMqttClient()
            second_json_client = MockMqttClient()
            mqtt_publisher.run_publisher(json_config, client=json_client)
            mqtt_publisher.run_publisher(npy_config, client=npy_client)
            second_json_stats = mqtt_publisher.run_publisher(
                json_config,
                client=second_json_client,
            )

            self.assertEqual(len(json_client.published), 1)
            self.assertEqual(len(npy_client.published), 2)
            self.assertEqual(second_json_stats.skipped, 1)
            self.assertEqual(second_json_client.published, [])
            state_text = state_path.read_text(encoding="utf-8")
            self.assertIn("|json", state_text)
            self.assertIn("|npy", state_text)

    def test_follow_skips_already_sent_frame_without_reloading_prediction_or_logging(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            state_path = prediction_dir / "mqtt_publish_state.json"
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01/frame",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            mqtt_publisher.run_publisher(config, client=MockMqttClient())
            (prediction_dir / "frame_000001_prediction.json").unlink()

            follow_config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01/frame",
                device_id="device01",
                follow=True,
                once=False,
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            state = mqtt_publisher.PublishState.load(state_path)

            with self.assertNoLogs(mqtt_publisher.LOGGER, level="INFO"):
                stats = mqtt_publisher.scan_once(
                    follow_config,
                    state=state,
                    connection=mqtt_publisher.MqttConnection(
                        follow_config,
                        client=MockMqttClient(),
                    ),
                )

            self.assertEqual(stats.skipped, 1)
            self.assertEqual(stats.waiting, 0)
            self.assertEqual(stats.sent, 0)

    def test_force_republish_sends_only_selected_frame_again(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root, frame_id="000001")
            write_pair(root, frame_id="000002")
            state_path = prediction_dir / "mqtt_publish_state.json"
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01",
                device_id="device01",
                stable_delay=0,
                state_path=state_path,
                frame_id="000001",
                force_republish=True,
            )

            first_client = MockMqttClient()
            second_client = MockMqttClient()
            first_stats = mqtt_publisher.run_publisher(config, client=first_client)
            second_stats = mqtt_publisher.run_publisher(config, client=second_client)

            self.assertEqual(first_stats.sent, 1)
            self.assertEqual(second_stats.sent, 1)
            self.assertEqual(len(first_client.published), 1)
            self.assertEqual(len(second_client.published), 1)

    def test_corrupt_state_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            state_path = prediction_dir / "mqtt_publish_state.json"
            state_path.write_text("{bad json", encoding="utf-8")
            state = mqtt_publisher.PublishState.load(state_path)

            self.assertEqual(state.sent, {})

    def test_broker_disconnect_does_not_stop_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01/frame",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=prediction_dir / "mqtt_publish_state.json",
            )
            client = MockMqttClient(rc=4)

            stats = mqtt_publisher.run_publisher(config, client=client)

            self.assertEqual(stats.errors, 1)
            self.assertEqual(stats.sent, 0)

    def test_state_distinguishes_broker_and_topic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            state_path = prediction_dir / "mqtt_publish_state.json"
            base_config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="localhost",
                topic="ecgrhythmia/device01/frame",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )

            first = MockMqttClient()
            mqtt_publisher.run_publisher(base_config, client=first)
            self.assertEqual(len(first.published), 1)

            hivemq_config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="example.s1.eu.hivemq.cloud",
                port=8883,
                topic="ecgrhythmia/device01/frame",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            second = MockMqttClient()
            mqtt_publisher.run_publisher(hivemq_config, client=second)
            self.assertEqual(len(second.published), 1)

            topic_config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="example.s1.eu.hivemq.cloud",
                port=8883,
                topic="ecgrhythmia/device01/other",
                device_id="device01",
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            third = MockMqttClient()
            mqtt_publisher.run_publisher(topic_config, client=third)
            self.assertEqual(len(third.published), 1)

            state_text = state_path.read_text(encoding="utf-8")
            self.assertIn("localhost", state_text)
            self.assertIn("example.s1.eu.hivemq.cloud", state_text)
            self.assertIn("ecgrhythmia/device01/other", state_text)


class HiveMqConnectionTests(TestCase):
    def test_tls_uses_verified_context_and_port_8883(self):
        config = mqtt_publisher.PublisherConfig(
            ecg_dir=Path("."),
            prediction_dir=Path("."),
            broker="example.s1.eu.hivemq.cloud",
            port=8883,
            topic="ecgrhythmia/device01/frame",
            device_id="device01",
            username="ecgrhythmia-device01",
            password_env="HIVEMQ_PASSWORD",
            tls=True,
            client_id="ecgrhythmia-device01-publisher",
        )
        client = MockMqttClient()

        with mock.patch.dict(os.environ, {"HIVEMQ_PASSWORD": "secret-value"}):
            connection = mqtt_publisher.MqttConnection(config, client=client)
            connection.connect()

        self.assertEqual(client.broker, "example.s1.eu.hivemq.cloud")
        self.assertEqual(client.port, 8883)
        self.assertIsNotNone(client.tls_context)
        self.assertTrue(client.tls_context.check_hostname)
        self.assertEqual(
            client.tls_context.verify_mode,
            mqtt_publisher.ssl.CERT_REQUIRED,
        )
        self.assertIsNone(client.tls_insecure)
        self.assertEqual(client.username, "ecgrhythmia-device01")
        self.assertEqual(client.password, "secret-value")

    def test_password_missing_fails_without_leaking_password(self):
        config = mqtt_publisher.PublisherConfig(
            ecg_dir=Path("."),
            prediction_dir=Path("."),
            broker="example.s1.eu.hivemq.cloud",
            port=8883,
            topic="ecgrhythmia/device01/frame",
            device_id="device01",
            username="ecgrhythmia-device01",
            password_env="HIVEMQ_PASSWORD",
            tls=True,
        )
        client = MockMqttClient()

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "HIVEMQ_PASSWORD"):
                mqtt_publisher.MqttConnection(config, client=client).connect()

    def test_password_not_in_payload_or_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ecg_dir, prediction_dir = write_pair(root)
            state_path = prediction_dir / "mqtt_publish_state_hivemq.json"
            config = mqtt_publisher.PublisherConfig(
                ecg_dir=ecg_dir,
                prediction_dir=prediction_dir,
                broker="example.s1.eu.hivemq.cloud",
                port=8883,
                topic="ecgrhythmia/device01/frame",
                device_id="device01",
                username="ecgrhythmia-device01",
                password_env="HIVEMQ_PASSWORD",
                tls=True,
                dry_run=False,
                stable_delay=0,
                state_path=state_path,
            )
            client = MockMqttClient()

            with mock.patch.dict(os.environ, {"HIVEMQ_PASSWORD": "secret-value"}):
                with self.assertLogs(mqtt_publisher.LOGGER, level="INFO") as logs:
                    stats = mqtt_publisher.run_publisher(config, client=client)

            self.assertEqual(stats.sent, 1)
            payload_text = client.published[0][1].decode("utf-8")
            state_text = state_path.read_text(encoding="utf-8")
            log_text = "\n".join(logs.output)
            self.assertNotIn("secret-value", payload_text)
            self.assertNotIn("secret-value", state_text)
            self.assertNotIn("secret-value", log_text)

    def test_default_client_id_is_stable_and_unique(self):
        parser = mqtt_publisher.build_parser()
        args = parser.parse_args(
            [
                "--ecg-dir",
                "data/calibrated",
                "--prediction-dir",
                "data/predictions",
                "--broker",
                "example.s1.eu.hivemq.cloud",
                "--topic",
                "ecgrhythmia/device01/frame",
                "--device-id",
                "device01",
            ]
        )

        config = mqtt_publisher.resolve_config(args)

        self.assertEqual(config.client_id, "ecgrhythmia-device01-publisher")

    def test_state_file_argument_is_supported(self):
        parser = mqtt_publisher.build_parser()
        args = parser.parse_args(
            [
                "--ecg-dir",
                "data/calibrated",
                "--prediction-dir",
                "data/predictions",
                "--broker",
                "example.s1.eu.hivemq.cloud",
                "--topic",
                "ecgrhythmia/device01/frame",
                "--device-id",
                "device01",
                "--state-file",
                "data/predictions/mqtt_publish_state_hivemq.json",
            ]
        )

        config = mqtt_publisher.resolve_config(args)

        self.assertEqual(
            config.state_path.name,
            "mqtt_publish_state_hivemq.json",
        )


class SafetyTests(TestCase):
    def test_module_does_not_import_serial(self):
        self.assertNotIn("serial", sys.modules)

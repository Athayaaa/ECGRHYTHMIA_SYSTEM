"""MQTT publisher for calibrated ECG mV frames and AI prediction JSON files.

The publisher pairs raw calibrated ECG frames with predictions by frame ID,
builds one compact JSON payload per complete frame, and publishes it through
MQTT only after the broker acknowledges the message. It intentionally does not
open serial ports, run inference, or modify ECG/prediction source files.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import time
from typing import Any

import numpy as np

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - exercised only without dependency
    mqtt = None


SCHEMA_VERSION = 1
EXPECTED_SHAPE = (2500, 3)
EXPECTED_DTYPE = np.dtype("float32")
EXPECTED_SAMPLE_RATE_HZ = 250.0
EXPECTED_DURATION_S = 10.0
CHANNEL_ORDER = ["Lead I", "Lead II", "Lead III"]
DEFAULT_PORT = 1883
DEFAULT_QOS = 1
DEFAULT_POLL_INTERVAL_S = 0.5
PAYLOAD_FORMAT_JSON = "json"
PAYLOAD_FORMAT_NPY = "npy"
PAYLOAD_FORMATS = (PAYLOAD_FORMAT_JSON, PAYLOAD_FORMAT_NPY)

ECG_PATTERN = re.compile(r"^frame_(\d+)_mv\.npy$")
PREDICTION_PATTERN = re.compile(r"^frame_(\d+)_prediction\.json$")
ABSOLUTE_PATH_PATTERN = re.compile(r"(^/)|([A-Za-z]:\\)")

LOGGER = logging.getLogger("mqtt_publisher")
SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass
class PreparedFrame:
    frame_id: str
    session_id: str
    message_id: str
    ecg_sha256: str
    validation_status: str
    prediction_label: str | None
    confidence_percent: float | None
    payload: dict[str, Any]
    payload_bytes: bytes
    ecg_samples: np.ndarray


@dataclass
class ScanStats:
    ready: int = 0
    sent: int = 0
    waiting: int = 0
    rejected: int = 0
    skipped: int = 0
    errors: int = 0

    def add(self, other: "ScanStats") -> None:
        self.ready += other.ready
        self.sent += other.sent
        self.waiting += other.waiting
        self.rejected += other.rejected
        self.skipped += other.skipped
        self.errors += other.errors


@dataclass
class PublisherConfig:
    ecg_dir: Path
    prediction_dir: Path
    broker: str
    port: int = DEFAULT_PORT
    topic: str = "ecgrhythmia/device01/frame"
    device_id: str = "device01"
    username: str | None = None
    password_env: str | None = None
    client_id: str | None = None
    tls: bool = False
    ca_cert: Path | None = None
    qos: int = DEFAULT_QOS
    follow: bool = False
    once: bool = True
    poll_interval: float = DEFAULT_POLL_INTERVAL_S
    dry_run: bool = False
    log_level: str = "INFO"
    retain: bool = False
    state_path: Path | None = None
    stable_delay: float = 0.2
    payload_format: str = PAYLOAD_FORMAT_JSON


@dataclass
class PublishState:
    path: Path
    sent: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PublishState":
        if not path.is_file():
            return cls(path=path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            LOGGER.warning("State file %s is unreadable, starting empty: %s", path, error)
            return cls(path=path)
        sent = data.get("sent", {}) if isinstance(data, dict) else {}
        if not isinstance(sent, dict):
            sent = {}
        return cls(path=path, sent=sent)

    def key_for(
        self,
        broker: str,
        base_topic: str,
        session_id: str,
        frame_id: str,
        payload_format: str,
    ) -> str:
        broker_key = broker.strip().lower()
        topic_key = normalize_base_topic(base_topic)
        return f"{broker_key}|{topic_key}|{session_id}|{frame_id}|{payload_format}"

    def is_sent(
        self,
        prepared: PreparedFrame,
        broker: str,
        base_topic: str,
        payload_format: str,
    ) -> bool:
        return (
            self.key_for(
                broker,
                base_topic,
                prepared.session_id,
                prepared.frame_id,
                payload_format,
            )
            in self.sent
        )

    def is_frame_file_sent(
        self,
        *,
        frame_id: str,
        ecg_sha256: str,
        broker: str,
        base_topic: str,
        payload_format: str,
    ) -> bool:
        broker_key = broker.strip().lower()
        topic_key = normalize_base_topic(base_topic)
        for record in self.sent.values():
            if not isinstance(record, dict):
                continue
            record_broker = str(record.get("broker", "")).strip().lower()
            record_topic = normalize_base_topic(
                str(record.get("base_topic", record.get("topic", "")))
            )
            if (
                record_broker == broker_key
                and record_topic == topic_key
                and record.get("frame_id") == frame_id
                and record.get("ecg_sha256") == ecg_sha256
                and record.get("payload_format", PAYLOAD_FORMAT_JSON) == payload_format
            ):
                return True
        return False

    def mark_sent(
        self,
        prepared: PreparedFrame,
        broker: str,
        port: int,
        base_topic: str,
        qos: int,
        payload_format: str,
        topics: list[str],
    ) -> None:
        topic_key = normalize_base_topic(base_topic)
        key = self.key_for(
            broker,
            topic_key,
            prepared.session_id,
            prepared.frame_id,
            payload_format,
        )
        self.sent[key] = {
            "broker": broker,
            "port": port,
            "session_id": prepared.session_id,
            "frame_id": prepared.frame_id,
            "ecg_sha256": prepared.ecg_sha256,
            "message_id": prepared.message_id,
            "base_topic": topic_key,
            "topic": topic_key,
            "topics": topics,
            "payload_format": payload_format,
            "qos": qos,
            "acked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def save_atomic(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "sent": self.sent,
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=".mqtt_publish_state_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


class MqttConnection:
    def __init__(self, config: PublisherConfig, client: Any | None = None) -> None:
        self.config = config
        self.client = client
        self.connected = False

    def connect(self) -> None:
        if self.connected:
            return
        if self.client is None:
            if mqtt is None:
                raise RuntimeError("paho-mqtt is not installed")
            try:
                self.client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id=self.config.client_id,
                )
            except (AttributeError, TypeError):
                self.client = mqtt.Client(client_id=self.config.client_id)

        if self.config.tls:
            LOGGER.info("Connecting to HiveMQ Cloud")
            LOGGER.info("Broker: %s:%s", self.config.broker, self.config.port)
            LOGGER.info("TLS: enabled")
            context = ssl.create_default_context(
                cafile=str(self.config.ca_cert) if self.config.ca_cert else None
            )
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True
            self.client.tls_set_context(context)

        password = resolve_password(self.config)
        if self.config.username is not None:
            self.client.username_pw_set(self.config.username, password)

        try:
            result = self.client.connect(self.config.broker, self.config.port)
        except Exception as error:
            raise RuntimeError(describe_connection_error(error)) from error
        if result not in (0, None):
            raise RuntimeError(describe_connect_rc(result))
        if hasattr(self.client, "loop_start"):
            self.client.loop_start()
        self.connected = True
        LOGGER.info("Connected to broker")

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.connect()
        info = self.client.publish(topic, payload=payload, qos=qos, retain=retain)
        rc = getattr(info, "rc", 0)
        if rc not in (0, None):
            self.connected = False
            raise RuntimeError(f"MQTT publish failed with rc={rc}")

        wait_for_publish = getattr(info, "wait_for_publish", None)
        if wait_for_publish is not None:
            result = wait_for_publish()
            if result is False:
                self.connected = False
                raise RuntimeError("MQTT publish acknowledgement timed out")

        is_published = getattr(info, "is_published", None)
        if callable(is_published) and not is_published():
            self.connected = False
            raise RuntimeError("MQTT publish was not acknowledged")

    def close(self) -> None:
        if self.client is None:
            return
        try:
            if hasattr(self.client, "loop_stop"):
                self.client.loop_stop()
            if hasattr(self.client, "disconnect"):
                self.client.disconnect()
        finally:
            self.connected = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish calibrated ECG mV frames and AI predictions over MQTT",
    )
    parser.add_argument("--ecg-dir", type=Path, required=True)
    parser.add_argument("--prediction-dir", type=Path, required=True)
    parser.add_argument("--broker", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--username")
    parser.add_argument("--password-env")
    parser.add_argument("--client-id")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--ca-cert", type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--qos", type=int, default=DEFAULT_QOS, choices=(0, 1, 2))
    parser.add_argument(
        "--payload-format",
        choices=PAYLOAD_FORMATS,
        default=PAYLOAD_FORMAT_JSON,
        help="MQTT ECG payload format: json publishes one JSON message, npy publishes ECG NPY plus prediction JSON",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--follow", action="store_true")
    mode_group.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def resolve_config(args: argparse.Namespace) -> PublisherConfig:
    ecg_dir = resolve_path(args.ecg_dir)
    prediction_dir = resolve_path(args.prediction_dir)
    once = args.once or not args.follow
    poll_interval = max(0.1, float(args.poll_interval))
    state_path = (
        resolve_path(args.state_file)
        if args.state_file is not None
        else prediction_dir / "mqtt_publish_state.json"
    )
    ca_cert = resolve_path(args.ca_cert) if args.ca_cert is not None else None
    client_id = args.client_id or f"ecgrhythmia-{sanitize_id(args.device_id)}-publisher"
    return PublisherConfig(
        ecg_dir=ecg_dir,
        prediction_dir=prediction_dir,
        broker=args.broker,
        port=args.port,
        topic=args.topic,
        device_id=args.device_id,
        username=args.username,
        password_env=args.password_env,
        client_id=client_id,
        tls=args.tls,
        ca_cert=ca_cert,
        qos=args.qos,
        follow=args.follow,
        once=once,
        poll_interval=poll_interval,
        dry_run=args.dry_run,
        log_level=args.log_level,
        state_path=state_path,
        stable_delay=min(0.2, poll_interval * 0.4),
        payload_format=args.payload_format,
    )


def resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (SCRIPT_DIR / path).resolve()


def normalize_base_topic(topic: str) -> str:
    topic = topic.strip().strip("/")
    for suffix in ("/frame/json", "/frame/npy", "/prediction", "/frame"):
        if topic.endswith(suffix):
            return topic[: -len(suffix)]
    return topic


def topic_for(config: PublisherConfig, suffix: str) -> str:
    base_topic = normalize_base_topic(config.topic)
    return f"{base_topic}/{suffix.strip('/')}"


def json_frame_topic(config: PublisherConfig) -> str:
    return topic_for(config, "frame/json")


def npy_frame_topic(config: PublisherConfig) -> str:
    return topic_for(config, "frame/npy")


def prediction_topic(config: PublisherConfig) -> str:
    return topic_for(config, "prediction")


def configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(message)s",
    )


def resolve_password(config: PublisherConfig) -> str | None:
    if config.username is None and config.password_env is None:
        if config.tls and config.broker.endswith(".hivemq.cloud"):
            raise RuntimeError(
                "HiveMQ Cloud requires --username and --password-env"
            )
        return None
    if config.username is None:
        raise RuntimeError("--username is required when --password-env is used")
    if config.password_env is None:
        raise RuntimeError("--password-env is required when --username is used")

    password = os.environ.get(config.password_env)
    if password is None:
        raise RuntimeError(
            f"environment variable {config.password_env!r} is not set"
        )
    return password


def describe_connect_rc(rc: Any) -> str:
    try:
        rc_int = int(rc)
    except (TypeError, ValueError):
        rc_text = str(rc).lower()
        if "auth" in rc_text:
            return "authentication failed"
        if "authorized" in rc_text or "authorised" in rc_text:
            return "not authorized"
        return f"MQTT connect failed with rc={rc}"

    if rc_int == 4:
        return "authentication failed"
    if rc_int == 5:
        return "not authorized"
    return f"MQTT connect failed with rc={rc}"


def describe_connection_error(error: Exception) -> str:
    if isinstance(error, ssl.SSLError):
        return "TLS certificate failed"
    if isinstance(error, TimeoutError):
        return "connection timeout"
    if isinstance(error, socket.gaierror):
        return "network unavailable"
    if isinstance(error, OSError):
        return f"network unavailable: {error}"
    return str(error)


def discover_ecg_frames(ecg_dir: Path) -> list[tuple[str, Path]]:
    if not ecg_dir.is_dir():
        return []
    frames: list[tuple[int, str, Path]] = []
    try:
        entries = list(ecg_dir.iterdir())
    except OSError as error:
        LOGGER.warning("Unable to list ECG directory %s: %s", ecg_dir, error)
        return []

    for entry in entries:
        if not entry.is_file():
            continue
        if entry.name.startswith(".") or entry.name.endswith(".tmp"):
            continue
        match = ECG_PATTERN.match(entry.name)
        if not match:
            continue
        frame_id = match.group(1).zfill(6)
        frames.append((int(frame_id), frame_id, entry))
    frames.sort(key=lambda item: item[0])
    return [(frame_id, path) for _, frame_id, path in frames]


def prediction_path_for(prediction_dir: Path, frame_id: str) -> Path | None:
    path = prediction_dir / f"frame_{frame_id}_prediction.json"
    if not path.is_file():
        return None
    if not PREDICTION_PATTERN.match(path.name):
        return None
    return path


def is_file_stable(path: Path, delay: float = 0.2) -> bool:
    try:
        first = path.stat()
    except OSError:
        return False
    if delay > 0:
        time.sleep(delay)
    try:
        second = path.stat()
    except OSError:
        return False
    return (
        first.st_size > 0
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} does not contain a JSON object")
    return data


def load_ecg_metadata(ecg_path: Path) -> dict[str, Any] | None:
    metadata_path = ecg_path.with_suffix(".json")
    if not metadata_path.is_file():
        return None
    return load_json(metadata_path)


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_ecg_frame(
    samples: np.ndarray,
    metadata: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    if samples.shape != EXPECTED_SHAPE:
        raise ValueError(f"invalid shape {samples.shape}, expected {EXPECTED_SHAPE}")
    if samples.dtype != EXPECTED_DTYPE:
        raise ValueError(f"invalid dtype {samples.dtype}, expected {EXPECTED_DTYPE}")
    if not np.all(np.isfinite(samples)):
        raise ValueError("ECG contains NaN or Inf values")

    status = "PASS"
    warnings: list[str] = []
    if metadata is None:
        warnings.append("ECG metadata file not found")
        return "WARNING", warnings

    unit = metadata.get("unit")
    if unit != "mV":
        raise ValueError(f"unit is {unit!r}, expected 'mV'")

    shape = metadata.get("shape")
    if shape is not None and list(shape) != list(EXPECTED_SHAPE):
        raise ValueError(f"metadata shape is {shape}, expected {list(EXPECTED_SHAPE)}")

    sample_rate = metadata.get("sample_rate_hz")
    if sample_rate is not None and float(sample_rate) != EXPECTED_SAMPLE_RATE_HZ:
        raise ValueError(
            f"metadata sample_rate_hz is {sample_rate}, expected {EXPECTED_SAMPLE_RATE_HZ}"
        )

    channel_order = metadata.get("channel_order")
    if channel_order is not None and list(channel_order) != CHANNEL_ORDER:
        raise ValueError(
            f"metadata channel_order is {channel_order}, expected {CHANNEL_ORDER}"
        )

    for block_name in ("validation", "signal_quality"):
        block = metadata.get(block_name)
        if not isinstance(block, dict):
            continue
        block_status = str(block.get("status", "")).upper()
        if block_status == "FAIL":
            raise ValueError(f"{block_name} status is FAIL")
        if block_status == "WARNING":
            status = "WARNING"
        warnings.extend(as_string_list(block.get("warnings")))
        warnings.extend(as_string_list(block.get("reasons")))

    return status, unique_strings(warnings)


def validate_prediction(prediction: dict[str, Any], frame_id: str) -> None:
    if prediction.get("frame_id") != frame_id:
        raise ValueError(
            f"prediction frame_id is {prediction.get('frame_id')!r}, expected {frame_id!r}"
        )
    status = str(prediction.get("status", "")).upper()
    if status == "FAIL":
        raise ValueError("prediction status is FAIL")
    if status not in ("PASS", "WARNING"):
        raise ValueError(f"prediction status is {status!r}, expected PASS or WARNING")

    input_status = prediction.get("input_validation_status")
    if isinstance(input_status, str) and input_status.upper() == "FAIL":
        raise ValueError("prediction input_validation_status is FAIL")

    unit = prediction.get("unit")
    if unit is not None and unit != "mV":
        raise ValueError(f"prediction unit is {unit!r}, expected 'mV'")

    shape = prediction.get("shape")
    if shape is not None and list(shape) != list(EXPECTED_SHAPE):
        raise ValueError(
            f"prediction shape is {shape}, expected {list(EXPECTED_SHAPE)}"
        )

    sample_rate = prediction.get("sampling_rate_hz")
    if sample_rate is not None and float(sample_rate) != EXPECTED_SAMPLE_RATE_HZ:
        raise ValueError(
            f"prediction sampling_rate_hz is {sample_rate}, "
            f"expected {EXPECTED_SAMPLE_RATE_HZ}"
        )


def infer_session_id(
    ecg_dir: Path,
    metadata: dict[str, Any] | None,
    prediction: dict[str, Any],
) -> str:
    for source in (metadata or {}, (metadata or {}).get("source_metadata", {}), prediction):
        if not isinstance(source, dict):
            continue
        for key in ("session_id", "session", "measurement_id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return sanitize_id(value)

    parts = ecg_dir.resolve().parts
    if "corrected_sessions" in parts:
        index = parts.index("corrected_sessions")
        if index + 1 < len(parts):
            return sanitize_id(parts[index + 1])

    if ecg_dir.name in ("calibrated", "predictions") and ecg_dir.parent.name != "data":
        return sanitize_id(ecg_dir.parent.name)

    return "default"


def prepare_frame(
    ecg_path: Path,
    prediction_path: Path,
    *,
    device_id: str,
    ecg_dir: Path,
    ecg_sha256: str | None = None,
) -> PreparedFrame:
    frame_match = ECG_PATTERN.match(ecg_path.name)
    if frame_match is None:
        raise ValueError(f"not an ECG mV frame: {ecg_path.name}")
    frame_id = frame_match.group(1).zfill(6)

    prediction = load_json(prediction_path)
    validate_prediction(prediction, frame_id)

    samples = np.load(ecg_path, allow_pickle=False)
    metadata = load_ecg_metadata(ecg_path)
    validation_status, validation_warnings = validate_ecg_frame(samples, metadata)

    prediction_input_status = prediction.get("input_validation_status")
    if isinstance(prediction_input_status, str):
        normalized_input_status = prediction_input_status.upper()
        if normalized_input_status == "WARNING":
            validation_status = "WARNING"
        elif normalized_input_status == "FAIL":
            raise ValueError("prediction input_validation_status is FAIL")
    validation_warnings.extend(as_string_list(prediction.get("input_warnings")))
    validation_warnings = unique_strings(validation_warnings)

    session_id = infer_session_id(ecg_dir, metadata, prediction)
    message_id = f"{sanitize_id(device_id)}-{session_id}-frame_{frame_id}"
    if ecg_sha256 is None:
        ecg_sha256 = sha256_file(ecg_path)
    payload = build_payload(
        samples=samples,
        metadata=metadata,
        prediction=prediction,
        device_id=device_id,
        session_id=session_id,
        frame_id=frame_id,
        message_id=message_id,
        validation_status=validation_status,
        validation_warnings=validation_warnings,
    )
    reject_absolute_paths(payload)
    payload_bytes = json_bytes(payload)
    return PreparedFrame(
        frame_id=frame_id,
        session_id=session_id,
        message_id=message_id,
        ecg_sha256=ecg_sha256,
        validation_status=validation_status,
        prediction_label=payload["prediction"].get("label"),
        confidence_percent=payload["prediction"].get("confidence_percent"),
        payload=payload,
        payload_bytes=payload_bytes,
        ecg_samples=samples.astype(np.float32, copy=False),
    )


def build_npy_payload(samples: np.ndarray) -> bytes:
    buffer = BytesIO()
    np.save(buffer, samples.astype(np.float32, copy=False), allow_pickle=False)
    return buffer.getvalue()


def build_npy_prediction_payload(
    prepared: PreparedFrame,
    *,
    ecg_topic: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "message_id": prepared.message_id,
        "device_id": prepared.payload["device_id"],
        "session_id": prepared.session_id,
        "frame_id": prepared.frame_id,
        "ecg_format": PAYLOAD_FORMAT_NPY,
        "ecg_topic": ecg_topic,
        "sampling_rate_hz": prepared.payload["sampling_rate_hz"],
        "unit": "mV",
        "shape": list(prepared.ecg_samples.shape),
        "channel_order": prepared.payload["channel_order"],
        "validation": prepared.payload["validation"],
        "prediction": prepared.payload["prediction"],
    }
    return normalize_json_value(payload)


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def build_payload(
    *,
    samples: np.ndarray,
    metadata: dict[str, Any] | None,
    prediction: dict[str, Any],
    device_id: str,
    session_id: str,
    frame_id: str,
    message_id: str,
    validation_status: str,
    validation_warnings: list[str],
) -> dict[str, Any]:
    sampling_rate_hz = value_from_sources(
        (metadata or {}).get("sample_rate_hz"),
        prediction.get("sampling_rate_hz"),
        EXPECTED_SAMPLE_RATE_HZ,
    )
    duration_s = value_from_sources(
        (metadata or {}).get("duration_seconds"),
        (metadata or {}).get("duration_s"),
        EXPECTED_DURATION_S,
    )

    prediction_payload = {
        "status": prediction.get("status"),
        "label": prediction.get("prediction", prediction.get("label")),
        "confidence_percent": prediction.get("confidence_percent"),
        "probabilities": prediction.get("probabilities"),
        "threshold": prediction.get("threshold"),
        "latency_ms": prediction.get("latency_ms"),
        "runtime": prediction.get("runtime"),
        "warning": prediction.get("warning"),
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "message_id": message_id,
        "device_id": device_id,
        "session_id": session_id,
        "frame_id": frame_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sampling_rate_hz": float(sampling_rate_hz),
        "duration_s": float(duration_s),
        "unit": "mV",
        "shape": list(EXPECTED_SHAPE),
        "channel_order": CHANNEL_ORDER,
        "validation": {
            "status": validation_status,
            "warnings": validation_warnings,
        },
        "ecg": {
            "format": "samples_by_time",
            "samples": samples.astype(np.float32, copy=False).tolist(),
        },
        "prediction": compact_dict(prediction_payload),
    }
    return normalize_json_value(payload)


def value_from_sources(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def compact_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def normalize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): normalize_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_json_value(item) for item in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite numeric value in payload")
        return number
    return value


def reject_absolute_paths(value: Any) -> None:
    if isinstance(value, dict):
        for child in value.values():
            reject_absolute_paths(child)
        return
    if isinstance(value, list):
        for child in value:
            reject_absolute_paths(child)
        return
    if isinstance(value, str) and ABSOLUTE_PATH_PATTERN.search(value):
        raise ValueError("payload contains an absolute local path")


def as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def sanitize_id(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return sanitized.strip("_") or "default"


def scan_once(
    config: PublisherConfig,
    *,
    state: PublishState,
    connection: MqttConnection | None,
) -> ScanStats:
    stats = ScanStats()
    frames = discover_ecg_frames(config.ecg_dir)
    payload_format = config.payload_format
    for frame_id, ecg_path in frames:
        try:
            if not is_file_stable(ecg_path, delay=config.stable_delay):
                stats.waiting += 1
                LOGGER.info("Frame %s waiting for ECG file to finish", frame_id)
                continue

            ecg_sha256 = sha256_file(ecg_path)
            if (
                not config.dry_run
                and state.is_frame_file_sent(
                    frame_id=frame_id,
                    ecg_sha256=ecg_sha256,
                    broker=config.broker,
                    base_topic=config.topic,
                    payload_format=payload_format,
                )
            ):
                stats.skipped += 1
                if not config.follow:
                    LOGGER.info("Frame %s skipped: already sent", frame_id)
                continue

            prediction_path = prediction_path_for(config.prediction_dir, frame_id)
            if prediction_path is None:
                stats.waiting += 1
                LOGGER.info("Frame %s waiting for prediction", frame_id)
                continue
            if not is_file_stable(prediction_path, delay=config.stable_delay):
                stats.waiting += 1
                LOGGER.info("Frame %s waiting for prediction", frame_id)
                continue

            prepared = prepare_frame(
                ecg_path,
                prediction_path,
                device_id=config.device_id,
                ecg_dir=config.ecg_dir,
                ecg_sha256=ecg_sha256,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            stats.rejected += 1
            LOGGER.warning("Frame %s rejected: %s", frame_id, error)
            continue

        if (
            not config.dry_run
            and state.is_sent(
                prepared,
                config.broker,
                config.topic,
                payload_format,
            )
        ):
            stats.skipped += 1
            LOGGER.info("Frame %s skipped: already sent", frame_id)
            continue

        stats.ready += 1
        json_topic = json_frame_topic(config)
        npy_topic = npy_frame_topic(config)
        pred_topic = prediction_topic(config)
        if payload_format == PAYLOAD_FORMAT_NPY:
            npy_payload = build_npy_payload(prepared.ecg_samples)
            prediction_payload = build_npy_prediction_payload(
                prepared,
                ecg_topic=npy_topic,
            )
            reject_absolute_paths(prediction_payload)
            prediction_bytes = json_bytes(prediction_payload)
        else:
            npy_payload = b""
            prediction_bytes = b""

        if config.dry_run:
            print(f"Frame {prepared.frame_id} ready")
            print(f"Format: {payload_format.upper()}")
            if payload_format == PAYLOAD_FORMAT_NPY:
                print(f"ECG topic: {npy_topic}")
                print(f"Prediction topic: {pred_topic}")
                print(f"ECG shape: {prepared.ecg_samples.shape}")
                print(f"ECG dtype: {prepared.ecg_samples.dtype}")
                print(f"NPY size: {len(npy_payload)} bytes")
                print(f"Prediction payload size: {len(prediction_bytes)} bytes")
            else:
                print(f"Topic: {json_topic}")
                print(f"ECG shape: {prepared.ecg_samples.shape}")
                print(f"ECG dtype: {prepared.ecg_samples.dtype}")
                print(f"Payload size: {len(prepared.payload_bytes)} bytes")
            print(
                "Prediction: "
                f"{prepared.prediction_label} {prepared.confidence_percent}%"
            )
            print("Dry run: not connected to broker")
            continue

        try:
            if connection is None:
                raise RuntimeError("MQTT connection is not available")
            LOGGER.info("Frame %s ready", prepared.frame_id)
            LOGGER.info("Format: %s", payload_format.upper())
            if payload_format == PAYLOAD_FORMAT_NPY:
                LOGGER.info("ECG topic: %s", npy_topic)
                LOGGER.info("Prediction topic: %s", pred_topic)
                LOGGER.info(
                    "ECG shape: (%s,%s)",
                    prepared.ecg_samples.shape[0],
                    prepared.ecg_samples.shape[1],
                )
                LOGGER.info("NPY size: %s bytes", len(npy_payload))
                connection.publish(
                    npy_topic,
                    npy_payload,
                    qos=config.qos,
                    retain=config.retain,
                )
                connection.publish(
                    pred_topic,
                    prediction_bytes,
                    qos=config.qos,
                    retain=config.retain,
                )
                sent_topics = [npy_topic, pred_topic]
            else:
                LOGGER.info("Topic: %s", json_topic)
                LOGGER.info("Payload size: %s bytes", len(prepared.payload_bytes))
                connection.publish(
                    json_topic,
                    prepared.payload_bytes,
                    qos=config.qos,
                    retain=config.retain,
                )
                sent_topics = [json_topic]
        except Exception as error:
            stats.errors += 1
            LOGGER.warning("MQTT disconnected, retrying: %s", error)
            break

        state.mark_sent(
            prepared,
            config.broker,
            config.port,
            config.topic,
            config.qos,
            payload_format,
            sent_topics,
        )
        state.save_atomic()
        stats.sent += 1
        LOGGER.info("Frame %s published, QoS %s", prepared.frame_id, config.qos)

    return stats


def run_publisher(config: PublisherConfig, client: Any | None = None) -> ScanStats:
    LOGGER.info("MQTT publisher started")
    LOGGER.info("Watching ECG: %s", config.ecg_dir)
    LOGGER.info("Watching predictions: %s", config.prediction_dir)

    state_path = config.state_path or (config.prediction_dir / "mqtt_publish_state.json")
    state = PublishState.load(state_path)
    connection = None if config.dry_run else MqttConnection(config, client=client)
    total = ScanStats()

    try:
        if config.once:
            stats = scan_once(config, state=state, connection=connection)
            total.add(stats)
            return total

        while True:
            stats = scan_once(config, state=state, connection=connection)
            total.add(stats)
            time.sleep(config.poll_interval)
    finally:
        if connection is not None:
            connection.close()


def main() -> int:
    args = build_parser().parse_args()
    config = resolve_config(args)
    configure_logging(config.log_level)
    stats = run_publisher(config)
    if config.once and not config.dry_run and stats.errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

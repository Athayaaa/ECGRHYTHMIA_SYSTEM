"""Raspberry Pi receiver for legacy V1 and corrected ADS1293 V2 ECG."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import struct
import sys
import time
from typing import Any

import numpy as np
import serial
import yaml

from ads1293_serial import (
    start_v2_binary_stream,
    stop_text_and_binary_output,
    verified_config_handshake,
)
from ads1293_session import (
    FirmwareCompatibilityError,
    build_session_metadata,
    utc_now_iso,
    write_session_metadata,
)
from calibration_utils import (
    CalibrationError,
    CalibratedExportPaths,
    convert_raw_to_mv,
    load_calibration_config,
    offset_code_range_stats,
    validate_calibration_compatibility,
    write_calibrated_frame,
)
from data_preparer import prepare_frame_array
from ecg_buffer import ECGFrame, ECGRingBuffer
from metadata_store import MetadataStore
from model_input_utils import DEFAULT_MAX_ABS_MV, prepare_model_input_file
from packet_parser import (
    HEADER_SIZE,
    PAYLOAD_SIZE,
    SYNC,
    VERSION,
    VERSION_V2,
    V2_FLAG_ADS_ERROR,
    V2_FLAG_BACKLOG_OVERRUN,
    V2_FLAG_CODE_OUT_OF_RANGE,
    V2_FLAG_CONFIG_VERIFIED,
    V2_FLAG_PREVIOUS_TX_FAILURE,
    V2_FLAG_SELFTEST_ACTIVE,
    ECGPacket,
    PacketParser,
)
from streaming_resampler import StreamingECGFramePipeline
from window_exporter import (
    FrameExportPaths,
    NativeCaptureWriter,
    WindowExporter,
)


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
DEFAULT_COMMAND_DELAY_SEC = 0.25
DEFAULT_ADS1293_BOOT_DELAY_SEC = 6.0
DEFAULT_STREAM_DETECT_TIMEOUT_SEC = 5.0
DEFAULT_STARTUP_RETRIES = 3
DEFAULT_SERIAL_SESSION_RETRIES = 2
DEFAULT_SERIAL_REOPEN_DELAY_SEC = 1.0
DEFAULT_READY_CHECK_TIMEOUT_SEC = 3.0
DEFAULT_READY_POLL_INTERVAL_SEC = 0.5
DEFAULT_STATUS_READ_TIMEOUT_SEC = 0.8
DEFAULT_REQUIRE_DRDY_PIN = True
DEFAULT_ADS1293_START_COMMANDS = [
    "ads debug off",
    "ads stop",
    "ads start",
    "ads stream start",
]
ADS1293_RETRY_COMMANDS = [
    "ads start",
]
ADS1293_STREAM_START_COMMAND = "ads stream start"
DISALLOWED_START_COMMANDS = {"ads read", "ads debug on"}
V2_KNOWN_FLAGS_MASK = (
    V2_FLAG_CONFIG_VERIFIED
    | V2_FLAG_ADS_ERROR
    | V2_FLAG_BACKLOG_OVERRUN
    | V2_FLAG_CODE_OUT_OF_RANGE
    | V2_FLAG_PREVIOUS_TX_FAILURE
    | V2_FLAG_SELFTEST_ACTIVE
)


def validate_v2_packet_semantics(
    packet: ECGPacket,
    *,
    expected_native_rate_numerator: int,
) -> None:
    """Reject CRC-valid packets that violate the protocol-V2 sample ABI."""

    failures: list[str] = []
    if packet.protocol_version != VERSION_V2:
        failures.append(f"protocol_version={packet.protocol_version}, expected 2")
    if packet.sample_count != 1:
        failures.append(f"sample_count={packet.sample_count}, expected 1")
    if packet.native_rate_numerator != expected_native_rate_numerator:
        failures.append(
            "native_rate_numerator="
            f"{packet.native_rate_numerator}, expected {expected_native_rate_numerator}"
        )
    reserved_flags = int(packet.flags) & (~V2_KNOWN_FLAGS_MASK & 0xFF)
    if reserved_flags:
        failures.append(f"reserved flags are set: 0x{reserved_flags:02X}")
    if failures:
        raise FirmwareCompatibilityError(
            "protocol-V2 packet semantic validation failed: " + "; ".join(failures)
        )


def validate_v2_startup_packet(
    packet: ECGPacket,
    *,
    expected_native_rate_numerator: int,
    require_config_verified: bool = True,
) -> None:
    """Require a normal packet and, for verified sessions, matching trust proof."""

    validate_v2_packet_semantics(
        packet,
        expected_native_rate_numerator=expected_native_rate_numerator,
    )
    failures: list[str] = []
    if require_config_verified and not (packet.flags & V2_FLAG_CONFIG_VERIFIED):
        failures.append("CONFIG_VERIFIED flag is clear")
    abnormal_flags = int(packet.flags) & (V2_KNOWN_FLAGS_MASK & ~V2_FLAG_CONFIG_VERIFIED)
    if abnormal_flags:
        failures.append(f"abnormal acquisition flags are set: 0x{abnormal_flags:02X}")
    if packet.error_status:
        failures.append(f"ADS1293 error_status=0x{packet.error_status:02X}")
    if failures:
        raise FirmwareCompatibilityError(
            "protocol-V2 startup proof failed: " + "; ".join(failures)
        )


def find_existing_ads_output_artifacts(
    *,
    output_dir: Path,
    raw_csv_name: str,
    sqlite_db_path: Path,
    calibrated_output_dir: Path,
    native_output_dir: Path,
    native_capture_enabled: bool,
    model_ready_output_dir: Path,
    model_ready_enabled: bool,
) -> list[Path]:
    """Return files that a new corrected session could overwrite or mix with."""

    found: dict[str, Path] = {}

    def add(path: Path) -> None:
        if path.exists():
            found[str(path.absolute())] = path

    add(output_dir / raw_csv_name)
    add(output_dir / "acquisition_events.jsonl")
    add(sqlite_db_path)
    for pattern in (
        "session_*.json",
        "frame_*.csv",
        "frame_*.bin",
        "frame_*.npy",
        "frame_*.json",
    ):
        if output_dir.exists():
            for path in output_dir.glob(pattern):
                add(path)
    if calibrated_output_dir.exists():
        for pattern in ("frame_*_mv.csv", "frame_*_mv.npy", "frame_*_mv.json"):
            for path in calibrated_output_dir.glob(pattern):
                add(path)
    if native_capture_enabled:
        add(native_output_dir / "native_samples.csv")
    if model_ready_enabled and model_ready_output_dir.exists():
        for pattern in ("frame_*_input.npy", "frame_*_input.json"):
            for path in model_ready_output_dir.glob(pattern):
                add(path)
    return sorted(found.values(), key=lambda path: str(path))


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ECGRHYTHMIA ECG serial receiver (dummy V1 / ADS1293 V2)"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.yaml",
    )
    parser.add_argument("--port", help="Serial device path override")
    parser.add_argument("--baudrate", type=int, help="Serial baudrate override")
    parser.add_argument("--output-dir", type=Path, help="Output directory override")
    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="Stop after exporting this many frames; 0 means run until interrupted",
    )
    parser.add_argument(
        "--allow-unverified-firmware",
        action="store_true",
        help=(
            "Development override: preserve raw acquisition despite missing or "
            "untrusted firmware provenance; wire/DSP incompatibilities still fail "
            "and calibrated/model-ready export remains disabled"
        ),
    )
    parser.add_argument(
        "--native-capture",
        action="store_true",
        help="Enable optional measured-channel native diagnostic CSV capture",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_config(config_path)

    serial_config = config.get("serial", config.get("uart", {}))
    sampling_config = config.get("sampling", {})
    output_config = config.get("output", {})
    runtime_config = config.get("runtime", {})
    device_config = config.get("device", {})
    model_ready_config = config.get("model_ready_export", {})

    port = args.port or serial_config.get("port", "/dev/ttyUSB0")
    baudrate = args.baudrate or int(serial_config.get("baudrate", 115200))
    timeout_seconds = float(serial_config.get("timeout_seconds", 0.1))
    read_size_bytes = int(serial_config.get("read_size_bytes", 1024))
    startup_delay_sec = float(serial_config.get("startup_delay_sec", 3.0))
    flush_on_start = bool(serial_config.get("flush_on_start", True))
    source = str(config.get("source", serial_config.get("source", "dummy"))).lower()
    is_ads1293_source = source == "ads1293"
    send_start_commands = bool(
        config.get("send_start_commands", serial_config.get("send_start_commands", False))
    )
    start_commands = normalize_start_commands(config.get("start_commands", []))
    if is_ads1293_source and not start_commands:
        start_commands = list(DEFAULT_ADS1293_START_COMMANDS)
    ads1293_boot_delay_sec = float(
        config.get(
            "ads1293_boot_delay_sec",
            serial_config.get(
                "ads1293_boot_delay_sec", DEFAULT_ADS1293_BOOT_DELAY_SEC
            ),
        )
    )
    boot_delay_sec = ads1293_boot_delay_sec if is_ads1293_source else startup_delay_sec
    command_delay_sec = float(
        config.get(
            "command_delay_sec",
            serial_config.get("command_delay_sec", DEFAULT_COMMAND_DELAY_SEC),
        )
    )
    stream_detect_timeout_sec = float(
        config.get(
            "stream_detect_timeout_sec",
            serial_config.get(
                "stream_detect_timeout_sec", DEFAULT_STREAM_DETECT_TIMEOUT_SEC
            ),
        )
    )
    startup_retries = int(
        config.get(
            "startup_retries",
            serial_config.get("startup_retries", DEFAULT_STARTUP_RETRIES),
        )
    )
    serial_session_retries = int(
        config.get(
            "serial_session_retries",
            serial_config.get(
                "serial_session_retries", DEFAULT_SERIAL_SESSION_RETRIES
            ),
        )
    )
    serial_reopen_delay_sec = float(
        config.get(
            "serial_reopen_delay_sec",
            serial_config.get(
                "serial_reopen_delay_sec", DEFAULT_SERIAL_REOPEN_DELAY_SEC
            ),
        )
    )
    ads1293_ready_check_timeout_sec = float(
        config.get(
            "ads1293_ready_check_timeout_sec",
            serial_config.get(
                "ads1293_ready_check_timeout_sec",
                DEFAULT_READY_CHECK_TIMEOUT_SEC,
            ),
        )
    )
    ads1293_ready_poll_interval_sec = float(
        config.get(
            "ads1293_ready_poll_interval_sec",
            serial_config.get(
                "ads1293_ready_poll_interval_sec",
                DEFAULT_READY_POLL_INTERVAL_SEC,
            ),
        )
    )
    ads1293_status_read_timeout_sec = float(
        config.get(
            "ads1293_status_read_timeout_sec",
            serial_config.get(
                "ads1293_status_read_timeout_sec",
                DEFAULT_STATUS_READ_TIMEOUT_SEC,
            ),
        )
    )
    ads1293_require_drdy_pin = bool(
        config.get(
            "ads1293_require_drdy_pin",
            serial_config.get("ads1293_require_drdy_pin", DEFAULT_REQUIRE_DRDY_PIN),
        )
    )

    sample_rate_hz = int(sampling_config.get("sample_rate_hz", 250))
    frame_seconds = int(sampling_config.get("frame_seconds", 10))
    frame_size = sample_rate_hz * frame_seconds

    if args.output_dir is not None:
        output_dir = args.output_dir
    else:
        output_dir = Path(output_config.get("directory", "data"))
        if not output_dir.is_absolute():
            output_dir = config_path.parent / output_dir
    raw_csv_name = str(output_config.get("raw_csv", "raw_ecg.csv"))
    sqlite_db_name = str(output_config.get("sqlite_db", "ecg_metadata.sqlite"))
    sqlite_db_path = Path(sqlite_db_name)
    if not sqlite_db_path.is_absolute():
        sqlite_db_path = output_dir / sqlite_db_path
    calibration_config_path = Path(
        output_config.get("calibration_config", "calibration_config.yaml")
    )
    if not calibration_config_path.is_absolute():
        calibration_config_path = config_path.parent / calibration_config_path
    calibrated_output_dir = Path(output_config.get("calibrated_directory", "calibrated"))
    if not calibrated_output_dir.is_absolute():
        calibrated_output_dir = output_dir / calibrated_output_dir
    model_ready_enabled = bool(model_ready_config.get("enabled", False))
    model_ready_output_dir = Path(model_ready_config.get("output_dir", "data/model_ready"))
    if not model_ready_output_dir.is_absolute():
        model_ready_output_dir = config_path.parent / model_ready_output_dir
    model_ready_max_abs_mv = float(
        model_ready_config.get("max_abs_mv", DEFAULT_MAX_ABS_MV)
    )
    preprocessing_config_value = model_ready_config.get("preprocessing_config")
    model_ready_preprocessing_config_path = (
        Path(preprocessing_config_value) if preprocessing_config_value else None
    )
    if (
        model_ready_preprocessing_config_path is not None
        and not model_ready_preprocessing_config_path.is_absolute()
    ):
        model_ready_preprocessing_config_path = (
            config_path.parent / model_ready_preprocessing_config_path
        )
    status_interval_seconds = float(runtime_config.get("status_interval_seconds", 5))
    device_id = str(device_config.get("device_id", "esp32dev_dummy"))
    state = str(device_config.get("state", "stored"))

    if is_ads1293_source:
        return run_corrected_ads1293_receiver(
            args=args,
            config=config,
            config_path=config_path,
            port=port,
            baudrate=baudrate,
            timeout_seconds=timeout_seconds,
            read_size_bytes=read_size_bytes,
            boot_delay_sec=boot_delay_sec,
            output_dir=output_dir,
            raw_csv_name=raw_csv_name,
            sqlite_db_path=sqlite_db_path,
            calibration_config_path=calibration_config_path,
            calibrated_output_dir=calibrated_output_dir,
            model_ready_enabled=model_ready_enabled,
            model_ready_output_dir=model_ready_output_dir,
            model_ready_max_abs_mv=model_ready_max_abs_mv,
            model_ready_preprocessing_config_path=(
                model_ready_preprocessing_config_path
            ),
            sample_rate_hz=sample_rate_hz,
            frame_seconds=frame_seconds,
            status_interval_seconds=status_interval_seconds,
            state=state,
        )

    parser = PacketParser()
    ring_buffer = ECGRingBuffer(sample_rate_hz, frame_seconds)
    exporter = WindowExporter(output_dir, raw_csv_name, sample_rate_hz, device_id, state)
    metadata_store = MetadataStore(sqlite_db_path)

    frame_index = 0
    last_status_time = time.monotonic()

    print(
        "ECGRHYTHMIA Phase 2 receiver "
        f"port={port} baudrate={baudrate} frame_size={frame_size} source={source}"
    )

    serial_session_count = (
        max(1, serial_session_retries)
        if is_ads1293_source and send_start_commands
        else 1
    )
    serial_session_attempt = 1

    try:
        while serial_session_attempt <= serial_session_count:
            if serial_session_count > 1:
                print(
                    "serial startup session "
                    f"{serial_session_attempt}/{serial_session_count}"
                )
            if serial_session_attempt > 1 and serial_reopen_delay_sec > 0:
                print(
                    "waiting "
                    f"{serial_reopen_delay_sec:.1f}s before reopening serial port"
                )
                time.sleep(serial_reopen_delay_sec)

            with serial.Serial(
                port=port, baudrate=baudrate, timeout=timeout_seconds
            ) as serial_port:
                disable_modem_control(serial_port)
                if boot_delay_sec > 0:
                    print(f"waiting {boot_delay_sec:.1f}s for device boot")
                    time.sleep(boot_delay_sec)
                if flush_on_start or is_ads1293_source:
                    serial_port.reset_input_buffer()

                startup_data = b""
                if is_ads1293_source and send_start_commands:
                    startup_data = start_ads1293_binary_stream(
                        serial_port=serial_port,
                        start_commands=start_commands,
                        command_delay_sec=command_delay_sec,
                        read_size_bytes=read_size_bytes,
                        detect_timeout_sec=stream_detect_timeout_sec,
                        startup_retries=startup_retries,
                        ready_check_timeout_sec=ads1293_ready_check_timeout_sec,
                        ready_poll_interval_sec=ads1293_ready_poll_interval_sec,
                        status_read_timeout_sec=ads1293_status_read_timeout_sec,
                        require_drdy_pin=ads1293_require_drdy_pin,
                    )
                    if not startup_data:
                        if serial_session_attempt < serial_session_count:
                            print(
                                "ADS1293 stream not detected; "
                                "reopening serial port"
                            )
                            serial_session_attempt += 1
                            continue

                        print(
                            "error: ADS1293 was not data-ready or binary "
                            "stream was not detected after startup retries"
                        )
                        return 2
                    # Drop all startup text from parser state; the first valid
                    # packet becomes the sequence baseline.
                    parser = PacketParser()
                    last_status_time = time.monotonic()
                elif send_start_commands:
                    send_serial_start_commands(
                        serial_port, start_commands, command_delay_sec
                    )

                while True:
                    if startup_data:
                        data = startup_data
                        startup_data = b""
                    else:
                        data = serial_port.read(read_size_bytes)
                    if data:
                        packets = parser.feed(data)
                        for packet in packets:
                            exporter.append_raw_packet(packet)
                            ring_buffer.append(packet)

                            if (
                                ring_buffer.is_full
                                and ring_buffer.total_samples % frame_size == 0
                            ):
                                frame_index += 1
                                frame = ring_buffer.snapshot()
                                samples = prepare_frame_array(
                                    frame.samples, sample_rate_hz, frame_seconds
                                )
                                paths = exporter.export_frame(
                                    frame_index=frame_index,
                                    frame=frame,
                                    samples=samples,
                                    metadata={"parser_stats": parser.stats.as_dict()},
                                )
                                metadata_store.insert_frame(
                                    paths.metadata, paths.metadata_path.name
                                )
                                print(
                                    f"exported frame {frame_index:06d}: "
                                    f"{paths.csv_path}, {paths.bin_path}, "
                                    f"{paths.npy_path}, "
                                    f"{paths.metadata_path}"
                                )
                                calibrated_paths = try_export_calibrated_frame(
                                    calibration_config_path=calibration_config_path,
                                    calibrated_output_dir=calibrated_output_dir,
                                    raw_export=paths,
                                    samples=samples,
                                    sample_rate_hz=sample_rate_hz,
                                )
                                if model_ready_enabled and calibrated_paths is not None:
                                    try_export_model_ready_frame(
                                        calibrated_paths=calibrated_paths,
                                        model_ready_output_dir=model_ready_output_dir,
                                        max_abs_mv=model_ready_max_abs_mv,
                                        preprocessing_config_path=(
                                            model_ready_preprocessing_config_path
                                        ),
                                    )

                                if args.max_frames and frame_index >= args.max_frames:
                                    return 0

                    now = time.monotonic()
                    if now - last_status_time >= status_interval_seconds:
                        stats = parser.stats.as_dict()
                        print(
                            "status "
                            f"ok={stats['packets_ok']} "
                            f"crc_errors={stats['crc_errors']} "
                            f"loss={stats['packet_loss_count']} "
                            f"out_of_order={stats['out_of_order_count']}"
                        )
                        last_status_time = now

    except KeyboardInterrupt:
        print("receiver stopped")
        return 0
    finally:
        exporter.close()
        metadata_store.close()


def run_corrected_ads1293_receiver(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    config_path: Path,
    port: str,
    baudrate: int,
    timeout_seconds: float,
    read_size_bytes: int,
    boot_delay_sec: float,
    output_dir: Path,
    raw_csv_name: str,
    sqlite_db_path: Path,
    calibration_config_path: Path,
    calibrated_output_dir: Path,
    model_ready_enabled: bool,
    model_ready_output_dir: Path,
    model_ready_max_abs_mv: float,
    model_ready_preprocessing_config_path: Path | None,
    sample_rate_hz: int,
    frame_seconds: int,
    status_interval_seconds: float,
    state: str,
) -> int:
    """Run the strict native-ADS1293 -> continuous 15/16 pipeline."""

    if sample_rate_hz != 250 or frame_seconds != 10:
        print(
            "error: corrected ADS1293 mode requires 250 Hz, 10-second "
            "canonical frames",
            file=sys.stderr,
        )
        return 2

    serial_config = config.get("serial", config.get("uart", {}))
    output_config = config.get("output", {})
    runtime_config = config.get("runtime", {})
    response_timeout_sec = float(
        serial_config.get("config_response_timeout_seconds", 1.5)
    )
    stream_detect_timeout_sec = float(
        config.get(
            "stream_detect_timeout_sec",
            serial_config.get("stream_detect_timeout_sec", 5.0),
        )
    )
    saturation_margin_counts = float(
        runtime_config.get("saturation_margin_counts", 8192.0)
    )
    native_capture_enabled = bool(
        args.native_capture or output_config.get("native_capture_enabled", False)
    )
    native_output_dir = Path(output_config.get("native_directory", "native"))
    if not native_output_dir.is_absolute():
        native_output_dir = output_dir / native_output_dir

    existing_artifacts = find_existing_ads_output_artifacts(
        output_dir=output_dir,
        raw_csv_name=raw_csv_name,
        sqlite_db_path=sqlite_db_path,
        calibrated_output_dir=calibrated_output_dir,
        native_output_dir=native_output_dir,
        native_capture_enabled=native_capture_enabled,
        model_ready_output_dir=model_ready_output_dir,
        model_ready_enabled=model_ready_enabled,
    )
    if existing_artifacts:
        detail = "\n  - ".join(str(path) for path in existing_artifacts)
        print(
            "error: corrected ADS1293 output directory contains existing session "
            "artifacts; choose a unique --output-dir:\n  - " + detail,
            file=sys.stderr,
        )
        return 2

    print(
        "ECGRHYTHMIA corrected ADS1293 receiver "
        f"port={port} baudrate={baudrate} native=800/3Hz output=250Hz "
        "resampler=15/16"
    )

    exporter: WindowExporter | None = None
    native_capture: NativeCaptureWriter | None = None
    metadata_store: MetadataStore | None = None
    event_handle = None
    serial_port: serial.Serial | None = None

    try:
        serial_port = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout_seconds,
        )
        disable_modem_control(serial_port)
        if boot_delay_sec > 0:
            print(f"waiting {boot_delay_sec:.1f}s for device boot")
            time.sleep(boot_delay_sec)
        serial_port.reset_input_buffer()

        handshake = verified_config_handshake(
            serial_port,
            allow_unverified=args.allow_unverified_firmware,
            response_timeout_sec=response_timeout_sec,
            read_size=read_size_bytes,
        )
        if handshake.compatibility_failures:
            print(
                "warning: development override accepted unverified firmware:\n  - "
                + "\n  - ".join(handshake.compatibility_failures),
                file=sys.stderr,
            )
        else:
            print("ADS1293 firmware/register configuration verified")

        calibration = None
        calibration_compatible = False
        calibration_status = "unavailable"
        calibration_failures: list[str] = []
        calibration_runtime_fingerprint = {
            **handshake.config,
            "output_sample_rate_hz": 250.0,
            "resample_up": 15,
            "resample_down": 16,
        }
        if calibration_config_path.exists():
            try:
                calibration = load_calibration_config(calibration_config_path)
                compatibility = validate_calibration_compatibility(
                    calibration,
                    calibration_runtime_fingerprint,
                    strict=True,
                )
                calibration = deepcopy(calibration)
                calibration["compatibility"] = compatibility.to_dict()
                calibration_compatible = (
                    compatibility.compatible and handshake.verified
                )
                calibration_failures.extend(compatibility.reasons)
                if not handshake.verified:
                    calibration_failures.append(
                        "firmware verification was overridden; calibrated export disabled"
                    )
                calibration_status = (
                    str(calibration.get("calibration_status", "available"))
                    if calibration_compatible
                    else "incompatible"
                )
            except (CalibrationError, OSError, ValueError) as error:
                calibration_failures.append(str(error))
                calibration_status = "incompatible"
        else:
            calibration_failures.append(
                f"calibration configuration not found: {calibration_config_path}"
            )

        calibration_source = (
            str(calibration.get("calibration_source", "unavailable"))
            if calibration_compatible and calibration is not None
            else "unavailable"
        )

        session_metadata = build_session_metadata(
            firmware_config=handshake.config,
            serial_port=port,
            baudrate=baudrate,
            calibration_config_path=(
                calibration_config_path if calibration_config_path.exists() else None
            ),
            calibration_compatible=calibration_compatible,
            calibration_status=calibration_status,
            calibration_source=calibration_source,
            compatibility_failures=calibration_failures,
        )
        session_metadata.update(
            {
                "firmware_compatibility_failures": list(
                    handshake.compatibility_failures
                ),
                "firmware_verification_override": bool(
                    handshake.compatibility_failures
                ),
                "resampling_method": "stateful_polyphase_fir",
                "resampler_filter_delay_output_samples": 10,
                "resampler_filter_delay_ms": 40.0,
                "native_capture_enabled": native_capture_enabled,
                "acquisition_status": (
                    "PASS" if handshake.verified else "FAIL_UNVERIFIED"
                ),
            }
        )
        if calibration is not None:
            calibration["calibration_config_sha256"] = session_metadata.get(
                "calibration_config_sha256"
            )
        if calibration_failures:
            print(
                "calibrated/model-ready export unavailable: "
                + "; ".join(calibration_failures),
                file=sys.stderr,
            )

        startup_data = start_v2_binary_stream(
            serial_port,
            response_timeout_sec=response_timeout_sec,
            detect_timeout_sec=stream_detect_timeout_sec,
            read_size=read_size_bytes,
        )
        if not startup_data:
            print("error: protocol-V2 binary stream was not detected", file=sys.stderr)
            return 2

        try:
            expected_native_rate_numerator = int(
                handshake.config["native_sample_rate_numerator"]
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FirmwareCompatibilityError(
                "verified firmware config has no valid native rate numerator"
            ) from error
        if expected_native_rate_numerator != 800:
            raise FirmwareCompatibilityError(
                "the corrected 15/16 DSP ABI requires native rate numerator 800; "
                f"firmware reported {expected_native_rate_numerator}"
            )
        startup_parser = PacketParser(allowed_versions={VERSION_V2})
        startup_packets = startup_parser.feed(startup_data)
        if not startup_packets:
            raise FirmwareCompatibilityError(
                "stream startup did not preserve a complete CRC-valid V2 packet"
            )
        for startup_packet in startup_packets:
            validate_v2_startup_packet(
                startup_packet,
                expected_native_rate_numerator=expected_native_rate_numerator,
                require_config_verified=handshake.verified,
            )

        # Persist the session only after at least one complete CRC-valid,
        # semantically compatible packet has been observed.  A trust-only
        # override remains explicitly FAIL_UNVERIFIED in all persisted output.
        session_path = write_session_metadata(output_dir, session_metadata)
        print(f"session metadata: {session_path}")

        device_id_value = handshake.config.get("device_id", "unknown")
        if isinstance(device_id_value, int):
            device_id = f"ads1293_0x{device_id_value:02X}"
        else:
            device_id = f"ads1293_{device_id_value}"
        exporter = WindowExporter(
            output_dir,
            raw_csv_name,
            sample_rate_hz,
            device_id,
            state,
            session_metadata=session_metadata,
        )
        native_capture = NativeCaptureWriter(
            native_output_dir,
            enabled=native_capture_enabled,
        )
        metadata_store = MetadataStore(sqlite_db_path)
        event_path = output_dir / "acquisition_events.jsonl"
        event_handle = event_path.open("a", encoding="utf-8", buffering=1)

        parser = PacketParser(allowed_versions={VERSION_V2})
        pipeline = StreamingECGFramePipeline(frame_size=2500)
        frame_index = 0
        last_status_time = time.monotonic()
        pending_data = startup_data

        frame_sequence_start: int | None = None
        frame_timestamp_start: int | None = None
        frame_sequence_end: int | None = None
        frame_timestamp_end: int | None = None
        frame_native_samples = 0
        frame_native_below = 0
        frame_native_above = 0
        frame_native_near_low = 0
        frame_native_near_high = 0
        frame_acquisition_reasons: set[str] = set()
        last_parser_accepted_sequence: int | None = None
        dropping_stale_block = False
        adcmax = int(handshake.config["adcmax"])

        while True:
            data = pending_data or serial_port.read(read_size_bytes)
            pending_data = b""
            if data:
                for packet in parser.feed(data):
                    assert packet.protocol_version == VERSION_V2
                    validate_v2_packet_semantics(
                        packet,
                        expected_native_rate_numerator=(
                            expected_native_rate_numerator
                        ),
                    )
                    native_capture.append(packet)

                    if packet.out_of_order_before:
                        if not dropping_stale_block:
                            expected_sequence = (
                                (last_parser_accepted_sequence + 1) & 0xFFFFFFFF
                                if last_parser_accepted_sequence is not None
                                else packet.sequence
                            )
                            kind = (
                                "duplicate"
                                if last_parser_accepted_sequence == packet.sequence
                                else "out_of_order"
                            )
                            discarded = pipeline.reset_after_discontinuity()
                            event_metadata = {
                                "created_at_utc": utc_now_iso(),
                                "session_id": session_metadata["session_id"],
                                "event": "native_sequence_discontinuity",
                                "kind": kind,
                                "expected_sequence": expected_sequence,
                                "received_sequence": packet.sequence,
                                "missing_native_samples": 0,
                                "discarded_partial_output_samples": discarded,
                                "resampler_reset_count": (
                                    pipeline.resampler.stats.resampler_reset_count
                                ),
                            }
                            event_handle.write(json.dumps(event_metadata) + "\n")
                            print(
                                "native sequence discontinuity "
                                f"kind={kind} expected={expected_sequence} "
                                f"received={packet.sequence} missing=0 "
                                f"discarded_output={discarded}",
                                file=sys.stderr,
                            )

                            # One reset is sufficient for a contiguous stale
                            # block. Parser retains the last-good baseline; all
                            # following stale packets are dropped until the
                            # true expected sequence arrives.
                            frame_sequence_start = None
                            frame_timestamp_start = None
                            frame_sequence_end = None
                            frame_timestamp_end = None
                            frame_native_samples = 0
                            frame_native_below = 0
                            frame_native_above = 0
                            frame_native_near_low = 0
                            frame_native_near_high = 0
                            frame_acquisition_reasons.clear()
                            dropping_stale_block = True
                        continue

                    dropping_stale_block = False
                    last_parser_accepted_sequence = packet.sequence

                    quarantine_reasons: list[str] = []
                    if handshake.verified and not (
                        packet.flags & V2_FLAG_CONFIG_VERIFIED
                    ):
                        quarantine_reasons.append(
                            "packet_missing_config_verified_flag"
                        )
                    if packet.flags & V2_FLAG_ADS_ERROR or packet.error_status:
                        quarantine_reasons.append("ads1293_error_status")
                    if packet.flags & V2_FLAG_BACKLOG_OVERRUN:
                        quarantine_reasons.append("native_backlog_or_overrun")
                    if packet.flags & V2_FLAG_CODE_OUT_OF_RANGE:
                        quarantine_reasons.append("firmware_code_out_of_range")
                    if packet.flags & V2_FLAG_PREVIOUS_TX_FAILURE:
                        quarantine_reasons.append(
                            "firmware_previous_packet_transmission_failed"
                        )
                    if packet.flags & V2_FLAG_SELFTEST_ACTIVE:
                        quarantine_reasons.append(
                            "internal_selftest_active_during_normal_capture"
                        )

                    packet_near_rail = False
                    for lead_name, code in (
                        ("Lead I", packet.lead_i_code),
                        ("Lead II", packet.lead_ii_code),
                    ):
                        if code < 0 or code > adcmax:
                            quarantine_reasons.append(
                                f"{lead_name} offset code outside 0..ADCMAX"
                            )
                        elif code <= saturation_margin_counts:
                            packet_near_rail = True
                            quarantine_reasons.append(
                                f"{lead_name} within {saturation_margin_counts:g} "
                                "count(s) of the low ADC rail"
                            )
                        elif code >= adcmax - saturation_margin_counts:
                            packet_near_rail = True
                            quarantine_reasons.append(
                                f"{lead_name} within {saturation_margin_counts:g} "
                                "count(s) of the high ADC rail"
                            )

                    if quarantine_reasons:
                        discarded = pipeline.reset_after_discontinuity()
                        event_metadata = {
                            "created_at_utc": utc_now_iso(),
                            "session_id": session_metadata["session_id"],
                            "event": "native_packet_quarantined",
                            "status": (
                                "WARNING"
                                if packet_near_rail
                                and not (
                                    packet.flags
                                    & (
                                        V2_FLAG_ADS_ERROR
                                        | V2_FLAG_BACKLOG_OVERRUN
                                        | V2_FLAG_CODE_OUT_OF_RANGE
                                        | V2_FLAG_PREVIOUS_TX_FAILURE
                                        | V2_FLAG_SELFTEST_ACTIVE
                                    )
                                )
                                and packet.error_status == 0
                                and (
                                    not handshake.verified
                                    or bool(packet.flags & V2_FLAG_CONFIG_VERIFIED)
                                )
                                else "FAIL"
                            ),
                            "native_sequence": packet.sequence,
                            "timestamp_us": packet.timestamp_us,
                            "flags": packet.flags,
                            "diagnostic": packet.diagnostic,
                            "reasons": quarantine_reasons,
                            "discarded_partial_output_samples": discarded,
                            "resampler_reset_count": (
                                pipeline.resampler.stats.resampler_reset_count
                            ),
                        }
                        event_handle.write(json.dumps(event_metadata) + "\n")
                        print(
                            "native packet quarantined "
                            f"sequence={packet.sequence} discarded_output={discarded} "
                            f"reasons={'; '.join(quarantine_reasons)}",
                            file=sys.stderr,
                        )
                        frame_sequence_start = None
                        frame_timestamp_start = None
                        frame_sequence_end = None
                        frame_timestamp_end = None
                        frame_native_samples = 0
                        frame_native_below = 0
                        frame_native_above = 0
                        frame_native_near_low = 0
                        frame_native_near_high = 0
                        frame_acquisition_reasons.clear()
                        continue

                    result = pipeline.process(
                        np.asarray(
                            [[packet.lead_i_code, packet.lead_ii_code]],
                            dtype=np.float64,
                        ),
                        [packet.sequence],
                    )

                    accepted = True
                    if result.loss_events:
                        for event in result.loss_events:
                            event_metadata = {
                                "created_at_utc": utc_now_iso(),
                                "session_id": session_metadata["session_id"],
                                "event": "native_sequence_discontinuity",
                                "kind": event.kind,
                                "expected_sequence": event.expected_sequence,
                                "received_sequence": event.received_sequence,
                                "missing_native_samples": event.missing_native_samples,
                                "discarded_partial_output_samples": (
                                    event.discarded_partial_output_samples
                                ),
                                "resampler_reset_count": (
                                    pipeline.resampler.stats.resampler_reset_count
                                ),
                            }
                            event_handle.write(json.dumps(event_metadata) + "\n")
                            print(
                                "native sequence discontinuity "
                                f"kind={event.kind} expected={event.expected_sequence} "
                                f"received={event.received_sequence} "
                                f"missing={event.missing_native_samples} "
                                f"discarded_output={event.discarded_partial_output_samples}",
                                file=sys.stderr,
                            )
                            accepted = event.kind == "gap"

                        # The affected partial frame was discarded by the DSP
                        # pipeline.  Start all per-frame provenance again.
                        frame_sequence_start = None
                        frame_timestamp_start = None
                        frame_sequence_end = None
                        frame_timestamp_end = None
                        frame_native_samples = 0
                        frame_native_below = 0
                        frame_native_above = 0
                        frame_native_near_low = 0
                        frame_native_near_high = 0
                        frame_acquisition_reasons.clear()

                    if accepted:
                        if frame_sequence_start is None:
                            frame_sequence_start = packet.sequence
                            frame_timestamp_start = packet.timestamp_us
                        frame_sequence_end = packet.sequence
                        frame_timestamp_end = packet.timestamp_us
                        frame_native_samples += 1

                        for code in (packet.lead_i_code, packet.lead_ii_code):
                            if code < 0:
                                frame_native_below += 1
                            elif code > adcmax:
                                frame_native_above += 1
                            elif code <= saturation_margin_counts:
                                frame_native_near_low += 1
                            elif code >= adcmax - saturation_margin_counts:
                                frame_native_near_high += 1

                        if not (packet.flags & V2_FLAG_CONFIG_VERIFIED):
                            frame_acquisition_reasons.add(
                                "packet_missing_config_verified_flag"
                            )
                        if packet.flags & V2_FLAG_ADS_ERROR or packet.error_status:
                            frame_acquisition_reasons.add("ads1293_error_status")
                        if packet.flags & V2_FLAG_BACKLOG_OVERRUN:
                            frame_acquisition_reasons.add("native_backlog_or_overrun")
                        if packet.flags & V2_FLAG_CODE_OUT_OF_RANGE:
                            frame_acquisition_reasons.add("firmware_code_out_of_range")
                        if packet.flags & V2_FLAG_PREVIOUS_TX_FAILURE:
                            frame_acquisition_reasons.add(
                                "firmware_previous_packet_transmission_failed"
                            )
                        if packet.flags & V2_FLAG_SELFTEST_ACTIVE:
                            frame_acquisition_reasons.add(
                                "internal_selftest_active_during_normal_capture"
                            )

                    if result.output_samples.size:
                        exporter.append_resampled_samples(result.output_samples)

                    for frame_samples in result.frames:
                        if (
                            frame_sequence_start is None
                            or frame_sequence_end is None
                            or frame_timestamp_start is None
                            or frame_timestamp_end is None
                        ):
                            raise RuntimeError("missing native provenance for output frame")

                        frame_index += 1
                        range_stats = offset_code_range_stats(
                            frame_samples,
                            adcmax=adcmax,
                            saturation_margin_counts=saturation_margin_counts,
                        )
                        range_reasons = list(range_stats.get("reasons", []))
                        range_stats["native_below_range_count"] = frame_native_below
                        range_stats["native_above_range_count"] = frame_native_above
                        if frame_native_below or frame_native_above:
                            range_stats["status"] = "FAIL"
                            if frame_native_below:
                                range_reasons.append(
                                    "Native measured Lead I/II: "
                                    f"{frame_native_below} sample(s) below offset-code rail 0"
                                )
                            if frame_native_above:
                                range_reasons.append(
                                    "Native measured Lead I/II: "
                                    f"{frame_native_above} sample(s) above ADCMAX {adcmax}"
                                )
                        range_stats["native_near_low_rail_count"] = (
                            frame_native_near_low
                        )
                        range_stats["native_near_high_rail_count"] = (
                            frame_native_near_high
                        )
                        if (
                            range_stats["status"] == "PASS"
                            and (frame_native_near_low or frame_native_near_high)
                        ):
                            range_stats["status"] = "WARNING"
                        if frame_native_near_low:
                            range_reasons.append(
                                "Native measured Lead I/II: "
                                f"{frame_native_near_low} sample(s) within "
                                f"{saturation_margin_counts:g} count(s) of the low ADC rail"
                            )
                        if frame_native_near_high:
                            range_reasons.append(
                                "Native measured Lead I/II: "
                                f"{frame_native_near_high} sample(s) within "
                                f"{saturation_margin_counts:g} count(s) of the high ADC rail"
                            )
                        range_stats["reasons"] = range_reasons

                        if not handshake.verified:
                            frame_acquisition_reasons.add("firmware_unverified")
                        if not handshake.verified:
                            acquisition_status = "FAIL_UNVERIFIED"
                        else:
                            acquisition_status = (
                                "PASS" if not frame_acquisition_reasons else "FAIL"
                            )
                        signal_quality_status = str(range_stats["status"])
                        calibration_frame_status = (
                            str(calibration.get("calibration_status", "available"))
                            if calibration_compatible and calibration is not None
                            else "unavailable"
                        )
                        stats = pipeline.resampler.stats
                        provenance = ECGFrame(
                            samples=frame_samples,
                            sequences=np.asarray(
                                [frame_sequence_start, frame_sequence_end],
                                dtype=np.uint32,
                            ),
                            timestamp_us=np.asarray(
                                [frame_timestamp_start, frame_timestamp_end],
                                dtype=np.uint32,
                            ),
                        )
                        frame_metadata = {
                            "session_id": session_metadata["session_id"],
                            "native_sequence_start": frame_sequence_start,
                            "native_sequence_end": frame_sequence_end,
                            "native_timestamp_us_start": frame_timestamp_start,
                            "native_timestamp_us_end": frame_timestamp_end,
                            "native_samples_consumed": frame_native_samples,
                            "native_samples_consumed_cumulative": (
                                stats.native_samples_consumed
                            ),
                            "output_samples_produced": 2500,
                            "output_samples_produced_cumulative": (
                                stats.output_samples_produced
                            ),
                            # Loss-tainted partial frames are discarded before
                            # export. Therefore these frame-local counters are
                            # zero for every emitted clean output epoch; the
                            # cumulative counters retain session history.
                            "packet_loss_count_frame": 0,
                            "sequence_gap_events_frame": 0,
                            "resampler_reset_count_frame": 0,
                            "packet_loss_count_cumulative": (
                                parser.stats.packet_loss_count
                            ),
                            "sequence_gap_events_cumulative": (
                                parser.stats.sequence_gap_events
                            ),
                            "resampler_reset_count_cumulative": (
                                stats.resampler_reset_count
                            ),
                            # Compatibility aliases retain their historical
                            # cumulative meaning. `counter_semantics` makes this
                            # explicit for old metadata consumers.
                            "packet_loss_count": parser.stats.packet_loss_count,
                            "sequence_gap_events": parser.stats.sequence_gap_events,
                            "out_of_order_count": parser.stats.out_of_order_count,
                            "resampler_reset_count": stats.resampler_reset_count,
                            "counter_semantics": {
                                "packet_loss_count": (
                                    "compatibility alias of "
                                    "packet_loss_count_cumulative"
                                ),
                                "sequence_gap_events": (
                                    "compatibility alias of "
                                    "sequence_gap_events_cumulative"
                                ),
                                "resampler_reset_count": (
                                    "compatibility alias of "
                                    "resampler_reset_count_cumulative"
                                ),
                                "frame_local": (
                                    "*_frame counts only events affecting this emitted "
                                    "frame; loss-tainted partial frames are discarded"
                                ),
                            },
                            "resampler_current_phase": stats.current_phase,
                            "resampler_filter_warmup_discard_count": (
                                stats.filter_warmup_discard_count
                            ),
                            "resampler_filter_delay_output_samples": (
                                stats.filter_delay_output_samples
                            ),
                            "resample_up": 15,
                            "resample_down": 16,
                            "resampling_method": "stateful_polyphase_fir",
                            "acquisition_status": acquisition_status,
                            "acquisition_reasons": sorted(
                                frame_acquisition_reasons
                            ),
                            "calibration_status": calibration_frame_status,
                            "signal_quality_status": signal_quality_status,
                            "signal_quality_reasons": range_reasons,
                            "signal_quality": range_stats,
                            "parser_stats": parser.stats.as_dict(),
                        }
                        for fingerprint_key in (
                            "firmware_commit",
                            "protocol_version",
                            "afe_res_hz",
                            "r1",
                            "r2",
                            "r3_ch1",
                            "r3_ch2",
                            "native_sample_rate_hz",
                            "output_sample_rate_hz",
                            "adc_encoding",
                            "adcmax",
                            "zero_code",
                        ):
                            frame_metadata[fingerprint_key] = session_metadata.get(
                                fingerprint_key
                            )

                        paths = exporter.export_frame(
                            frame_index=frame_index,
                            frame=provenance,
                            samples=frame_samples,
                            metadata=frame_metadata,
                        )
                        metadata_store.insert_frame(
                            paths.metadata,
                            paths.metadata_path.name,
                        )
                        print(
                            f"exported corrected frame {frame_index:06d}: "
                            f"{paths.npy_path} acquisition={acquisition_status} "
                            f"signal={signal_quality_status} "
                            f"native_samples={frame_native_samples}"
                        )

                        calibrated_paths = None
                        if (
                            calibration_compatible
                            and calibration is not None
                            and acquisition_status == "PASS"
                            and signal_quality_status != "FAIL"
                        ):
                            calibrated_paths = try_export_calibrated_frame(
                                calibration_config_path=calibration_config_path,
                                calibrated_output_dir=calibrated_output_dir,
                                raw_export=paths,
                                samples=frame_samples,
                                sample_rate_hz=sample_rate_hz,
                                calibration_snapshot=calibration,
                            )
                        if (
                            model_ready_enabled
                            and calibrated_paths is not None
                            and acquisition_status == "PASS"
                            and signal_quality_status == "PASS"
                        ):
                            try_export_model_ready_frame(
                                calibrated_paths=calibrated_paths,
                                model_ready_output_dir=model_ready_output_dir,
                                max_abs_mv=model_ready_max_abs_mv,
                                preprocessing_config_path=(
                                    model_ready_preprocessing_config_path
                                ),
                            )

                        frame_sequence_start = None
                        frame_timestamp_start = None
                        frame_sequence_end = None
                        frame_timestamp_end = None
                        frame_native_samples = 0
                        frame_native_below = 0
                        frame_native_above = 0
                        frame_native_near_low = 0
                        frame_native_near_high = 0
                        frame_acquisition_reasons.clear()

                        if args.max_frames and frame_index >= args.max_frames:
                            return 0

            now = time.monotonic()
            if now - last_status_time >= status_interval_seconds:
                stats = parser.stats.as_dict()
                dsp = pipeline.resampler.stats
                print(
                    "status "
                    f"ok={stats['packets_ok']} crc_errors={stats['crc_errors']} "
                    f"loss={stats['packet_loss_count']} "
                    f"out_of_order={stats['out_of_order_count']} "
                    f"native={dsp.native_samples_consumed} "
                    f"output={dsp.output_samples_produced} "
                    f"phase={dsp.current_phase} resets={dsp.resampler_reset_count}"
                )
                last_status_time = now

    except KeyboardInterrupt:
        print("receiver stopped")
        return 0
    except FirmwareCompatibilityError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (OSError, serial.SerialException, TimeoutError, ValueError) as error:
        print(f"error: ADS1293 receiver failed: {error}", file=sys.stderr)
        return 2
    finally:
        if serial_port is not None and getattr(serial_port, "is_open", True):
            try:
                stop_text_and_binary_output(
                    serial_port,
                    response_timeout_sec=0.4,
                    read_size=read_size_bytes,
                )
            except (OSError, serial.SerialException, ValueError):
                pass
        if event_handle is not None:
            event_handle.close()
        if native_capture is not None:
            native_capture.close()
        if exporter is not None:
            exporter.close()
        if metadata_store is not None:
            metadata_store.close()
        if serial_port is not None:
            try:
                serial_port.close()
            except (OSError, serial.SerialException):
                pass


def disable_modem_control(serial_port: serial.Serial) -> None:
    """Disable USB serial DTR/RTS lines when the adapter exposes them."""

    for attribute_name in ("dtr", "rts"):
        try:
            setattr(serial_port, attribute_name, False)
        except (AttributeError, OSError, ValueError, serial.SerialException):
            continue

    for setter_name in ("setDTR", "setRTS"):
        setter = getattr(serial_port, setter_name, None)
        if setter is None:
            continue
        try:
            setter(False)
        except (OSError, ValueError, serial.SerialException):
            continue


def try_export_calibrated_frame(
    calibration_config_path: Path,
    calibrated_output_dir: Path,
    raw_export: FrameExportPaths,
    samples,
    sample_rate_hz: int,
    runtime_metadata: dict[str, Any] | None = None,
    calibration_snapshot: dict[str, Any] | None = None,
) -> CalibratedExportPaths | None:
    if calibration_snapshot is None and not calibration_config_path.exists():
        return None

    try:
        if calibration_snapshot is None:
            calibration = load_calibration_config(
                calibration_config_path,
                runtime_metadata=runtime_metadata,
            )
        else:
            # Use the configuration verified and hashed at session startup.
            # Never re-read a mutable YAML file during a corrected acquisition.
            calibration = deepcopy(calibration_snapshot)
        samples_mv = convert_raw_to_mv(samples, calibration)
        paths = write_calibrated_frame(
            output_dir=calibrated_output_dir,
            stem=raw_export.csv_path.stem,
            samples_mv=samples_mv,
            sample_rate_hz=sample_rate_hz,
            calibration=calibration,
            source_metadata=raw_export.metadata,
            source_files={
                "csv": str(raw_export.csv_path),
                "bin": str(raw_export.bin_path),
                "npy": str(raw_export.npy_path),
                "json": str(raw_export.metadata_path),
            },
            raw_range_stats=(
                raw_export.metadata.get("signal_quality")
                if isinstance(raw_export.metadata.get("signal_quality"), dict)
                else offset_code_range_stats(
                    samples,
                    adcmax=calibration["adcmax"],
                )
            ),
        )
    except Exception as error:
        print(f"warning: calibrated mV export skipped: {error}", file=sys.stderr)
        return None

    print(
        "exported calibrated frame "
        f"{raw_export.csv_path.stem}: {paths.csv_path}, {paths.npy_path}, "
        f"{paths.json_path}"
    )
    return paths


def try_export_model_ready_frame(
    calibrated_paths: CalibratedExportPaths,
    model_ready_output_dir: Path,
    max_abs_mv: float,
    preprocessing_config_path: Path | None,
) -> None:
    try:
        prepared = prepare_model_input_file(
            input_path=calibrated_paths.npy_path,
            metadata_path=calibrated_paths.json_path,
            output_dir=model_ready_output_dir,
            max_abs_mV=max_abs_mv,
            overwrite=True,
            preprocessing_config_path=preprocessing_config_path,
        )
    except Exception as error:
        print(f"warning: model-ready export skipped: {error}", file=sys.stderr)
        return

    print(
        "exported model-ready frame "
        f"{prepared.frame_id}: {prepared.paths.npy_path}, "
        f"{prepared.paths.metadata_path}"
    )


def normalize_start_commands(raw_commands: Any) -> list[str]:
    if raw_commands is None:
        return []
    if not isinstance(raw_commands, list):
        raise ValueError("start_commands must be a YAML list")

    commands: list[str] = []
    for raw_command in raw_commands:
        command = str(raw_command).strip()
        if command:
            commands.append(command)
    return commands


def send_serial_start_commands(
    serial_port: serial.Serial, commands: list[str], command_delay_sec: float
) -> None:
    for command in commands:
        send_serial_command(serial_port, command, command_delay_sec)


def send_serial_command(
    serial_port: serial.Serial, command: str, command_delay_sec: float
) -> bool:
    normalized = command.lower()
    if normalized in DISALLOWED_START_COMMANDS:
        print(f"skipped unsafe startup command: {command}")
        return False

    serial_port.write(f"{command}\r\n".encode("utf-8"))
    serial_port.flush()
    print(f"sent {command}")
    if command_delay_sec > 0:
        time.sleep(command_delay_sec)
    return True


def start_ads1293_binary_stream(
    serial_port: serial.Serial,
    start_commands: list[str],
    command_delay_sec: float,
    read_size_bytes: int,
    detect_timeout_sec: float,
    startup_retries: int,
    ready_check_timeout_sec: float,
    ready_poll_interval_sec: float,
    status_read_timeout_sec: float,
    require_drdy_pin: bool,
) -> bytes:
    attempts = max(1, startup_retries)
    pre_stream_commands = [
        command
        for command in start_commands
        if command.lower() != ADS1293_STREAM_START_COMMAND
    ]
    stream_start_command = next(
        (
            command
            for command in start_commands
            if command.lower() == ADS1293_STREAM_START_COMMAND
        ),
        ADS1293_STREAM_START_COMMAND,
    )

    for attempt in range(1, attempts + 1):
        print(f"ADS1293 startup attempt {attempt}/{attempts}")
        if attempt == 1:
            send_serial_start_commands(
                serial_port, pre_stream_commands, command_delay_sec
            )
        else:
            send_serial_command(serial_port, "ads stop", command_delay_sec)
            try:
                serial_port.reset_input_buffer()
            except (OSError, ValueError, serial.SerialException):
                pass
            send_serial_start_commands(
                serial_port, ADS1293_RETRY_COMMANDS, command_delay_sec
            )

        if not wait_for_ads1293_ready(
            serial_port=serial_port,
            command_delay_sec=command_delay_sec,
            read_size_bytes=read_size_bytes,
            ready_check_timeout_sec=ready_check_timeout_sec,
            ready_poll_interval_sec=ready_poll_interval_sec,
            status_read_timeout_sec=status_read_timeout_sec,
            require_drdy_pin=require_drdy_pin,
        ):
            if attempt < attempts:
                print("ADS1293 data not ready, retrying startup")
            continue

        send_serial_command(serial_port, stream_start_command, command_delay_sec)
        startup_data = wait_for_binary_stream(
            serial_port, read_size_bytes, detect_timeout_sec
        )
        if startup_data:
            return startup_data

        if attempt < attempts:
            print("binary stream not detected, retrying")

    print("binary stream not detected")
    return b""


def wait_for_ads1293_ready(
    serial_port: serial.Serial,
    command_delay_sec: float,
    read_size_bytes: int,
    ready_check_timeout_sec: float,
    ready_poll_interval_sec: float,
    status_read_timeout_sec: float,
    require_drdy_pin: bool,
) -> bool:
    deadline = time.monotonic() + max(0.0, ready_check_timeout_sec)

    while True:
        try:
            serial_port.reset_input_buffer()
        except (OSError, ValueError, serial.SerialException):
            pass

        if not send_serial_command(serial_port, "ads status", command_delay_sec):
            return False

        response = read_serial_text(
            serial_port=serial_port,
            read_size_bytes=read_size_bytes,
            read_timeout_sec=status_read_timeout_sec,
        )
        status = parse_ads1293_status(response)
        print_ads1293_readiness(status)

        if ads1293_status_is_ready(status, require_drdy_pin):
            return True

        if time.monotonic() >= deadline:
            print("ADS1293 DRDY not asserted; binary stream start skipped")
            return False

        if ready_poll_interval_sec > 0:
            time.sleep(ready_poll_interval_sec)


def read_serial_text(
    serial_port: serial.Serial,
    read_size_bytes: int,
    read_timeout_sec: float,
) -> str:
    deadline = time.monotonic() + max(0.0, read_timeout_sec)
    response = bytearray()

    while time.monotonic() < deadline:
        data = serial_port.read(read_size_bytes)
        if data:
            response.extend(data)

    return response.decode("utf-8", errors="replace")


def parse_ads1293_status(response: str) -> dict[str, str]:
    keys = {
        "device_id",
        "error_status",
        "register_data_ready",
        "data_ready",
        "sample_ready",
        "drdy_pin",
        "acquisition",
        "configured",
        "message",
    }
    status: dict[str, str] = {}

    for raw_line in response.splitlines():
        line = raw_line.strip()
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue

        key = key.strip()
        value = value.strip()
        if key in keys:
            status[key] = value

    return status


def ads1293_status_is_ready(status: dict[str, str], require_drdy_pin: bool) -> bool:
    data_ready = status.get("data_ready", "").lower()
    register_data_ready = status.get("register_data_ready", "").lower()
    sample_ready = status.get("sample_ready", "").lower()
    drdy_pin = status.get("drdy_pin", "").lower()
    if require_drdy_pin and drdy_pin:
        return drdy_pin.startswith("low")
    return (
        data_ready == "yes"
        or register_data_ready == "yes"
        or sample_ready == "yes"
        or drdy_pin.startswith("low")
    )


def print_ads1293_readiness(status: dict[str, str]) -> None:
    if not status:
        print("ADS1293 readiness: status response not parsed")
        return

    parts = [
        f"data_ready={status.get('data_ready', 'unknown')}",
        f"register_data_ready={status.get('register_data_ready', 'unknown')}",
        f"sample_ready={status.get('sample_ready', 'unknown')}",
        f"drdy_pin={status.get('drdy_pin', 'unknown')}",
        f"acquisition={status.get('acquisition', 'unknown')}",
        f"device_id={status.get('device_id', 'unknown')}",
        f"error_status={status.get('error_status', 'unknown')}",
    ]
    print("ADS1293 readiness: " + " ".join(parts))


def wait_for_binary_stream(
    serial_port: serial.Serial,
    read_size_bytes: int,
    detect_timeout_sec: float,
) -> bytes:
    deadline = time.monotonic() + detect_timeout_sec
    buffer = bytearray()

    while time.monotonic() < deadline:
        data = serial_port.read(read_size_bytes)
        if not data:
            continue

        buffer.extend(data)
        header_index = find_binary_header(buffer)
        if header_index is not None:
            if header_index > 0:
                del buffer[:header_index]
            print("binary stream detected")
            return bytes(buffer)

        trim_to_possible_sync(buffer)

    return b""


def find_binary_header(buffer: bytearray) -> int | None:
    search_from = 0
    while True:
        sync_index = buffer.find(SYNC, search_from)
        if sync_index < 0:
            return None
        if len(buffer) - sync_index < HEADER_SIZE:
            return None

        version = buffer[sync_index + 2]
        payload_size = struct.unpack_from("<H", buffer, sync_index + 3)[0]
        if version == VERSION and payload_size == PAYLOAD_SIZE:
            return sync_index

        search_from = sync_index + 1


def trim_to_possible_sync(buffer: bytearray) -> None:
    if len(buffer) <= HEADER_SIZE:
        return

    last_possible_sync = buffer.rfind(SYNC[:1])
    if last_possible_sync >= 0:
        del buffer[:last_possible_sync]
    else:
        buffer.clear()


if __name__ == "__main__":
    sys.exit(main())

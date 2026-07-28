"""Raspberry Pi serial receiver for Phase 2 ECG frames."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
from typing import Any

import serial
import yaml

from calibration_utils import (
    CalibratedExportPaths,
    convert_raw_to_mv,
    load_calibration_config,
    write_calibrated_frame,
)
from data_preparer import prepare_frame_array
from ecg_buffer import ECGRingBuffer
from metadata_store import MetadataStore
from model_input_utils import DEFAULT_MAX_ABS_MV, prepare_model_input_file
from packet_parser import HEADER_SIZE, PAYLOAD_SIZE, SYNC, VERSION, PacketParser
from session_manager import (
    SessionPaths,
    clean_legacy_output,
    create_recording_session,
    finish_recording_session,
    legacy_output_files,
    paths_for_session,
)
from window_exporter import FrameExportPaths, WindowExporter


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_GUI_PYTHON = SCRIPT_DIR / "venv_gui" / "bin" / "python"
GUI_SHUTDOWN_TIMEOUT_SEC = 3.0
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


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ECGRHYTHMIA Phase 2 serial receiver")
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
        "--new-session",
        action="store_true",
        help="Create a timestamped session and write all outputs below it",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Root directory for --new-session (default: data/sessions)",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="Use an existing session directory created by the OLED service",
    )
    parser.add_argument(
        "--clean-legacy-output",
        action="store_true",
        help="Remove only recognized legacy outputs outside data/sessions",
    )
    parser.add_argument(
        "--dry-run-session",
        action="store_true",
        help="Create and validate a new session without opening serial",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="Stop after exporting this many frames; 0 means run until interrupted",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Run the receiver without automatically opening the ECG viewer",
    )
    parser.add_argument(
        "--gui-python",
        type=Path,
        help="Python executable for the ECG viewer (default: venv_gui/bin/python)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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

    session_paths: SessionPaths | None = None
    sessions_root = args.sessions_root
    if not sessions_root.is_absolute():
        sessions_root = config_path.parent / sessions_root
    sessions_root = sessions_root.resolve()

    if args.clean_legacy_output:
        legacy_root = sessions_root.parent
        legacy_files = legacy_output_files(legacy_root)
        print(f"Legacy output files to remove: {len(legacy_files)}")
        removed = clean_legacy_output(legacy_root)
        print(f"Legacy output files removed: {len(removed)}")
        if not args.new_session and args.session_dir is None:
            return 0

    if args.new_session and args.session_dir is not None:
        raise ValueError("--new-session and --session-dir cannot be used together")
    if args.dry_run_session and not args.new_session:
        raise ValueError("--dry-run-session requires --new-session")

    if args.new_session:
        try:
            session_paths = create_recording_session(
                sessions_root,
                sample_rate_hz=float(sample_rate_hz),
                duration_per_frame_s=float(frame_seconds),
            )
        except OSError as error:
            print(f"error: unable to create recording session: {error}", file=sys.stderr)
            return 1
    elif args.session_dir is not None:
        session_paths = paths_for_session(args.session_dir)
        if not session_paths.metadata_path.is_file():
            print(
                f"error: session metadata not found: {session_paths.metadata_path}",
                file=sys.stderr,
            )
            return 1

    if session_paths is not None:
        output_dir = session_paths.raw_adc
        calibrated_output_dir = session_paths.calibrated
        model_ready_enabled = True
        model_ready_output_dir = session_paths.model_ready
    elif args.output_dir is not None:
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
    if session_paths is None:
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
    launch_gui = bool(runtime_config.get("launch_ecg_viewer", True)) and not args.no_gui
    configured_gui_python = args.gui_python or runtime_config.get("ecg_viewer_python")
    gui_python = resolve_gui_python(configured_gui_python, config_path.parent)
    device_id = str(device_config.get("device_id", "esp32dev_dummy"))
    state = str(device_config.get("state", "stored"))

    if session_paths is not None:
        print("New recording session created")
        print(f"Session ID : {session_paths.session_id}")
        print(f"Output     : {session_paths.root}")
        if args.dry_run_session:
            finish_recording_session(session_paths, 0)
            print("Dry run session: receiver not started")
            return 0

    parser = PacketParser()
    ring_buffer = ECGRingBuffer(sample_rate_hz, frame_seconds)
    exporter: WindowExporter | None = None
    metadata_store: MetadataStore | None = None

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
    gui_process: subprocess.Popen[bytes] | None = None
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, handle_receiver_termination)

    try:
        exporter = WindowExporter(
            output_dir, raw_csv_name, sample_rate_hz, device_id, state
        )
        metadata_store = MetadataStore(sqlite_db_path)
        gui_process = (
            launch_ecg_viewer(output_dir, gui_python)
            if launch_gui
            else None
        )
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
                                    metadata={
                                        "parser_stats": parser.stats.as_dict(),
                                        **(
                                            {"session_id": session_paths.session_id}
                                            if session_paths is not None
                                            else {}
                                        ),
                                    },
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
        stop_ecg_viewer(gui_process)
        if exporter is not None:
            exporter.close()
        if metadata_store is not None:
            metadata_store.close()
        if session_paths is not None:
            try:
                finish_recording_session(session_paths, frame_index)
            except Exception as error:
                print(f"warning: unable to finalize session metadata: {error}", file=sys.stderr)
        signal.signal(signal.SIGTERM, previous_sigterm_handler)


def handle_receiver_termination(_signum: int, _frame: Any) -> None:
    """Route SIGTERM through the normal KeyboardInterrupt cleanup path."""

    raise KeyboardInterrupt


def resolve_gui_python(
    configured_value: str | Path | None,
    config_directory: Path,
) -> Path:
    """Return the dedicated GUI interpreter, with a receiver-env fallback."""

    if configured_value:
        candidate = Path(configured_value).expanduser()
        if not candidate.is_absolute():
            candidate = config_directory / candidate
        return Path(os.path.abspath(candidate))
    if DEFAULT_GUI_PYTHON.is_file():
        # Do not resolve the venv's python symlink to /usr/bin/python: invoking
        # the symlink itself is what activates the virtualenv site-packages.
        return DEFAULT_GUI_PYTHON
    return Path(os.path.abspath(sys.executable))


def launch_ecg_viewer(
    output_dir: str | Path,
    python_executable: str | Path,
) -> subprocess.Popen[bytes] | None:
    """Open the standalone viewer as an owned child of the receiver."""

    executable = Path(os.path.abspath(Path(python_executable).expanduser()))
    if not executable.is_file():
        print(
            f"warning: ECG viewer not started; Python not found: {executable}",
            file=sys.stderr,
        )
        return None

    data_dir = Path(output_dir).expanduser().resolve()
    command = [
        str(executable),
        "-m",
        "ecg_qt_viewer.main",
        "--data-dir",
        str(data_dir),
        "--source-mode",
        "raw",
        "--follow-live",
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(SCRIPT_DIR),
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError as error:
        print(f"warning: ECG viewer could not start: {error}", file=sys.stderr)
        return None

    print(f"ECG viewer started pid={process.pid} data_dir={data_dir}")
    return process


def stop_ecg_viewer(process: subprocess.Popen[bytes] | None) -> None:
    """Close a viewer launched by this receiver, tolerating manual closure."""

    if process is None or process.poll() is not None:
        return
    print("closing ECG viewer")
    process.terminate()
    try:
        process.wait(timeout=GUI_SHUTDOWN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


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
) -> CalibratedExportPaths | None:
    if not calibration_config_path.exists():
        return None

    try:
        calibration = load_calibration_config(calibration_config_path)
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

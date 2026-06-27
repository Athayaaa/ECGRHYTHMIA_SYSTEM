"""Raspberry Pi serial receiver for Phase 2 ECG frames."""

from __future__ import annotations

import argparse
from pathlib import Path
import struct
import sys
import time
from typing import Any

import serial
import yaml

from data_preparer import prepare_frame_array
from ecg_buffer import ECGRingBuffer
from metadata_store import MetadataStore
from packet_parser import HEADER_SIZE, PAYLOAD_SIZE, SYNC, VERSION, PacketParser
from window_exporter import WindowExporter


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
DEFAULT_COMMAND_DELAY_SEC = 0.25
DEFAULT_ADS1293_BOOT_DELAY_SEC = 6.0
DEFAULT_STREAM_DETECT_TIMEOUT_SEC = 5.0
DEFAULT_STARTUP_RETRIES = 3
DEFAULT_ADS1293_START_COMMANDS = [
    "ads debug off",
    "ads stop",
    "ads start",
    "ads stream start",
]
ADS1293_RETRY_COMMANDS = [
    "ads start",
    "ads stream start",
]
DISALLOWED_START_COMMANDS = {"ads read", "ads debug on"}


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data


def parse_args() -> argparse.Namespace:
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
        "--max-frames",
        type=int,
        default=0,
        help="Stop after exporting this many frames; 0 means run until interrupted",
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
    status_interval_seconds = float(runtime_config.get("status_interval_seconds", 5))
    device_id = str(device_config.get("device_id", "esp32dev_dummy"))
    state = str(device_config.get("state", "stored"))

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

    try:
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
                )
                if not startup_data:
                    print(
                        "error: ADS1293 binary stream was not detected "
                        "after startup retries"
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
) -> bytes:
    attempts = max(1, startup_retries)

    for attempt in range(1, attempts + 1):
        print(f"ADS1293 startup attempt {attempt}/{attempts}")
        if attempt == 1:
            send_serial_start_commands(serial_port, start_commands, command_delay_sec)
        else:
            send_serial_command(serial_port, "ads stop", command_delay_sec)
            try:
                serial_port.reset_input_buffer()
            except (OSError, ValueError, serial.SerialException):
                pass
            send_serial_start_commands(
                serial_port, ADS1293_RETRY_COMMANDS, command_delay_sec
            )

        startup_data = wait_for_binary_stream(
            serial_port, read_size_bytes, detect_timeout_sec
        )
        if startup_data:
            return startup_data

        if attempt < attempts:
            print("binary stream not detected, retrying")

    print("binary stream not detected")
    return b""


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

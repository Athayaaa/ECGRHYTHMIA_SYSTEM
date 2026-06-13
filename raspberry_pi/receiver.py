"""Raspberry Pi UART receiver for Phase 2 ECG frames."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Any

import serial
import yaml

from data_preparer import prepare_frame_array
from ecg_buffer import ECGRingBuffer
from metadata_store import MetadataStore
from packet_parser import PacketParser
from window_exporter import WindowExporter


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ECGRHYTHMIA Phase 2 UART receiver")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.yaml",
    )
    parser.add_argument("--port", help="UART device path override")
    parser.add_argument("--baudrate", type=int, help="UART baudrate override")
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

    uart_config = config.get("uart", {})
    sampling_config = config.get("sampling", {})
    output_config = config.get("output", {})
    runtime_config = config.get("runtime", {})
    device_config = config.get("device", {})

    port = args.port or uart_config.get("port", "/dev/serial0")
    baudrate = args.baudrate or int(uart_config.get("baudrate", 921600))
    timeout_seconds = float(uart_config.get("timeout_seconds", 0.1))
    read_size_bytes = int(uart_config.get("read_size_bytes", 1024))

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
        f"port={port} baudrate={baudrate} frame_size={frame_size}"
    )

    try:
        with serial.Serial(port=port, baudrate=baudrate, timeout=timeout_seconds) as uart:
            while True:
                data = uart.read(read_size_bytes)
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


if __name__ == "__main__":
    sys.exit(main())

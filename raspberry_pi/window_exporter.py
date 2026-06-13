"""CSV, BIN, NPY, and metadata export for completed ECG windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np

from data_preparer import CHANNEL_ORDER, frame_time_axis
from ecg_buffer import ECGFrame
from packet_parser import ECGPacket


@dataclass(frozen=True)
class FrameExportPaths:
    csv_path: Path
    bin_path: Path
    npy_path: Path
    metadata_path: Path
    metadata: dict


class WindowExporter:
    def __init__(
        self,
        output_dir: Path,
        raw_csv_name: str,
        sample_rate_hz: int,
        device_id: str,
        state: str,
    ) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sample_rate_hz = sample_rate_hz
        self.device_id = device_id
        self.state = state
        self.raw_csv_path = self.output_dir / raw_csv_name
        self._raw_sample_index = 0
        self._raw_file = self.raw_csv_path.open("w", newline="", buffering=1)
        self._raw_writer = csv.writer(self._raw_file)
        self._raw_writer.writerow(("time", "ch1", "ch2", "ch3"))

    def close(self) -> None:
        self._raw_file.close()

    def append_raw_packet(self, packet: ECGPacket) -> None:
        sample_time = self._raw_sample_index / float(self.sample_rate_hz)
        self._raw_writer.writerow(
            (
                f"{sample_time:.3f}",
                packet.ch1,
                packet.ch2,
                packet.ch3,
            )
        )
        self._raw_sample_index += 1

    def export_frame(
        self,
        frame_index: int,
        frame: ECGFrame,
        samples: np.ndarray,
        metadata: dict,
    ) -> FrameExportPaths:
        stem = f"frame_{frame_index:06d}"
        csv_path = self.output_dir / f"{stem}.csv"
        bin_path = self.output_dir / f"{stem}.bin"
        npy_path = self.output_dir / f"{stem}.npy"
        metadata_path = self.output_dir / f"{stem}.json"

        self._write_frame_csv(csv_path, samples)
        frame_bytes = self._write_frame_bin(bin_path, samples)
        sha256_checksum = hashlib.sha256(frame_bytes).hexdigest()
        np.save(npy_path, samples.astype(np.float32, copy=False))

        full_metadata = self._build_metadata(
            frame_index=frame_index,
            frame=frame,
            samples=samples,
            metadata=metadata,
            csv_path=csv_path,
            bin_path=bin_path,
            npy_path=npy_path,
            sha256_checksum=sha256_checksum,
        )
        metadata_path.write_text(
            json.dumps(full_metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        return FrameExportPaths(
            csv_path=csv_path,
            bin_path=bin_path,
            npy_path=npy_path,
            metadata_path=metadata_path,
            metadata=full_metadata,
        )

    def _write_frame_csv(self, csv_path: Path, samples: np.ndarray) -> None:
        times = frame_time_axis(self.sample_rate_hz, samples.shape[0])
        with csv_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("time", "ch1", "ch2", "ch3"))
            for sample_time, row in zip(times, samples):
                writer.writerow(
                    (
                        f"{float(sample_time):.3f}",
                        f"{float(row[0]):.6g}",
                        f"{float(row[1]):.6g}",
                        f"{float(row[2]):.6g}",
                    )
                )

    def _write_frame_bin(self, bin_path: Path, samples: np.ndarray) -> bytes:
        frame_bytes = np.ascontiguousarray(samples, dtype="<f4").tobytes(order="C")
        bin_path.write_bytes(frame_bytes)
        return frame_bytes

    def _build_metadata(
        self,
        frame_index: int,
        frame: ECGFrame,
        samples: np.ndarray,
        metadata: dict,
        csv_path: Path,
        bin_path: Path,
        npy_path: Path,
        sha256_checksum: str,
    ) -> dict:
        return {
            "measurement_id": str(uuid.uuid4()),
            "device_id": self.device_id,
            "created_at_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "frame_index": frame_index,
            "state": self.state,
            "sample_rate_hz": self.sample_rate_hz,
            "sample_count": int(samples.shape[0]),
            "duration_seconds": samples.shape[0] / float(self.sample_rate_hz),
            "shape": list(samples.shape),
            "dtype": str(samples.dtype),
            "sha256_checksum": sha256_checksum,
            "channel_order": CHANNEL_ORDER,
            "lead_mapping": {
                "ch1": "Lead I = LA - RA",
                "ch2": "Lead II = LL - RA",
                "ch3": "Lead III = Lead II - Lead I",
            },
            "ch3_derived": True,
            "sequence_start": int(frame.sequences[0]),
            "sequence_end": int(frame.sequences[-1]),
            "timestamp_us_start": int(frame.timestamp_us[0]),
            "timestamp_us_end": int(frame.timestamp_us[-1]),
            "csv_file": csv_path.name,
            "bin_file": bin_path.name,
            "npy_file": npy_path.name,
            **metadata,
        }

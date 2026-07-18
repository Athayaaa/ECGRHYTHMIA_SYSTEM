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
        session_metadata: dict | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sample_rate_hz = sample_rate_hz
        self.device_id = device_id
        self.state = state
        self.session_metadata = dict(session_metadata or {})
        self.session_id = self.session_metadata.get("session_id")
        self.raw_csv_path = self.output_dir / raw_csv_name
        if self.raw_csv_path.exists():
            raise FileExistsError(
                f"raw stream export already exists: {self.raw_csv_path}; "
                "use a new acquisition output directory"
            )
        self._raw_sample_index = 0
        self._raw_file = self.raw_csv_path.open("w", newline="", buffering=1)
        self._raw_writer = csv.writer(self._raw_file)
        self._raw_writer.writerow(("time", "ch1", "ch2", "ch3"))

    def close(self) -> None:
        self._raw_file.close()

    def append_raw_packet(self, packet: ECGPacket) -> None:
        if packet.is_ads1293_native:
            raise ValueError(
                "protocol V2 native packets must be resampled before raw export"
            )
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

    def append_resampled_samples(self, samples: np.ndarray) -> None:
        values = np.asarray(samples)
        if values.ndim != 2 or values.shape[1] != 3:
            raise ValueError(f"resampled samples shape is {values.shape}, expected Nx3")
        for row in values:
            sample_time = self._raw_sample_index / float(self.sample_rate_hz)
            self._raw_writer.writerow(
                (
                    f"{sample_time:.6f}",
                    f"{float(row[0]):.9g}",
                    f"{float(row[1]):.9g}",
                    f"{float(row[2]):.9g}",
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

        existing = [
            path for path in (csv_path, bin_path, npy_path, metadata_path) if path.exists()
        ]
        if existing:
            raise FileExistsError(
                f"raw frame output already exists for {stem}: "
                + ", ".join(str(path) for path in existing)
            )

        self._write_frame_csv(csv_path, samples)
        frame_bytes = self._write_frame_bin(bin_path, samples)
        bin_sha256 = hashlib.sha256(frame_bytes).hexdigest()
        np.save(npy_path, samples.astype(np.float32, copy=False))
        npy_sha256 = hashlib.sha256(npy_path.read_bytes()).hexdigest()

        full_metadata = self._build_metadata(
            frame_index=frame_index,
            frame=frame,
            samples=samples,
            metadata=metadata,
            csv_path=csv_path,
            bin_path=bin_path,
            npy_path=npy_path,
            bin_sha256=bin_sha256,
            npy_sha256=npy_sha256,
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
        bin_sha256: str,
        npy_sha256: str,
    ) -> dict:
        native_sequence_start = metadata.get(
            "native_sequence_start", int(frame.sequences[0])
        )
        native_sequence_end = metadata.get(
            "native_sequence_end", int(frame.sequences[-1])
        )
        native_timestamp_start = metadata.get(
            "native_timestamp_us_start", int(frame.timestamp_us[0])
        )
        native_timestamp_end = metadata.get(
            "native_timestamp_us_end", int(frame.timestamp_us[-1])
        )
        base_metadata = {
            "measurement_id": str(uuid.uuid4()),
            "device_id": self.device_id,
            "session_id": self.session_id,
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
            "unit": "ADC_count",
            "sample_domain": "resampled_offset_code_counts",
            # ``sha256_checksum`` is retained as the historical BIN checksum.
            # The explicitly named hashes bind both canonical raw artifacts.
            "sha256_checksum": bin_sha256,
            "bin_sha256": bin_sha256,
            "npy_sha256": npy_sha256,
            "channel_order": CHANNEL_ORDER,
            "channel_encoding": {
                "ch1": "ADS1293 Lead I offset-binary count, resampled float",
                "ch2": "ADS1293 Lead II offset-binary count, resampled float",
                "ch3": "signed derived count; ch2 - ch1",
            },
            "lead_mapping": {
                "ch1": "Lead I = LA - RA",
                "ch2": "Lead II = LL - RA",
                "ch3": "Lead III = Lead II - Lead I",
            },
            "ch3_derived": True,
            # Backward-compatible names now refer to the native provenance
            # range for protocol V2 frames.
            "sequence_start": int(native_sequence_start),
            "sequence_end": int(native_sequence_end),
            "native_sequence_start": int(native_sequence_start),
            "native_sequence_end": int(native_sequence_end),
            "timestamp_us_start": int(native_timestamp_start),
            "timestamp_us_end": int(native_timestamp_end),
            "native_timestamp_us_start": int(native_timestamp_start),
            "native_timestamp_us_end": int(native_timestamp_end),
            "csv_file": csv_path.name,
            "bin_file": bin_path.name,
            "npy_file": npy_path.name,
        }
        for key in (
            "firmware_commit",
            "protocol_version",
            "resample_up",
            "resample_down",
            "adc_encoding",
            "adcmax",
            "zero_code",
            "vref_nominal_v",
            "analog_gain",
        ):
            if key in self.session_metadata:
                base_metadata[key] = self.session_metadata[key]
        base_metadata.update(metadata)
        return base_metadata


class NativeCaptureWriter:
    """Optional streaming diagnostic capture for measured V2 channels only."""

    def __init__(self, output_dir: Path, enabled: bool = False) -> None:
        self.enabled = enabled
        self._handle = None
        self._writer = None
        self.path: Path | None = None
        if not enabled:
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        self.path = output_dir / "native_samples.csv"
        if self.path.exists():
            raise FileExistsError(
                f"native capture already exists: {self.path}; "
                "use a new acquisition output directory"
            )
        self._handle = self.path.open("w", newline="", buffering=1)
        self._writer = csv.writer(self._handle)
        self._writer.writerow(
            (
                "native_sequence",
                "timestamp_us",
                "lead_i_offset_code",
                "lead_ii_offset_code",
                "flags",
                "diagnostic",
            )
        )

    def append(self, packet: ECGPacket) -> None:
        if not self.enabled:
            return
        if not packet.is_ads1293_native:
            raise ValueError("native diagnostic capture requires protocol V2")
        assert self._writer is not None
        self._writer.writerow(
            (
                packet.sequence,
                packet.timestamp_us,
                packet.lead_i_code,
                packet.lead_ii_code,
                packet.flags,
                packet.diagnostic,
            )
        )

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()

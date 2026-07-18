"""SQLite storage for exported ECG frame metadata."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any


class MetadataStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.db_path)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def insert_frame(self, metadata: dict[str, Any], json_file: str) -> None:
        self._connection.execute(
            """
            INSERT OR REPLACE INTO ecg_frames (
                measurement_id,
                device_id,
                frame_index,
                state,
                created_at_utc,
                sample_rate_hz,
                sample_count,
                duration_seconds,
                shape_json,
                dtype,
                sha256_checksum,
                channel_order_json,
                lead_mapping_json,
                ch3_derived,
                sequence_start,
                sequence_end,
                timestamp_us_start,
                timestamp_us_end,
                csv_file,
                bin_file,
                npy_file,
                json_file,
                parser_stats_json,
                session_id,
                protocol_version,
                acquisition_status,
                calibration_status,
                signal_quality_status,
                frame_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata["measurement_id"],
                metadata["device_id"],
                metadata["frame_index"],
                metadata["state"],
                metadata["created_at_utc"],
                metadata["sample_rate_hz"],
                metadata["sample_count"],
                metadata["duration_seconds"],
                json.dumps(metadata["shape"]),
                metadata["dtype"],
                metadata["sha256_checksum"],
                json.dumps(metadata["channel_order"]),
                json.dumps(metadata["lead_mapping"]),
                1 if metadata["ch3_derived"] else 0,
                metadata["sequence_start"],
                metadata["sequence_end"],
                metadata["timestamp_us_start"],
                metadata["timestamp_us_end"],
                metadata["csv_file"],
                metadata["bin_file"],
                metadata["npy_file"],
                json_file,
                json.dumps(metadata.get("parser_stats", {})),
                metadata.get("session_id"),
                metadata.get("protocol_version"),
                metadata.get("acquisition_status"),
                metadata.get("calibration_status"),
                metadata.get("signal_quality_status"),
                json.dumps(metadata, sort_keys=True),
            ),
        )
        self._connection.commit()

    def get_latest_metadata(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM ecg_frames
            ORDER BY created_at_utc DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ecg_frames (
                measurement_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                frame_index INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                sample_rate_hz INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                shape_json TEXT NOT NULL,
                dtype TEXT NOT NULL,
                sha256_checksum TEXT NOT NULL,
                channel_order_json TEXT NOT NULL,
                lead_mapping_json TEXT NOT NULL,
                ch3_derived INTEGER NOT NULL,
                sequence_start INTEGER NOT NULL,
                sequence_end INTEGER NOT NULL,
                timestamp_us_start INTEGER NOT NULL,
                timestamp_us_end INTEGER NOT NULL,
                csv_file TEXT NOT NULL,
                bin_file TEXT NOT NULL,
                npy_file TEXT NOT NULL,
                json_file TEXT NOT NULL,
                parser_stats_json TEXT NOT NULL
                , session_id TEXT
                , protocol_version INTEGER
                , acquisition_status TEXT
                , calibration_status TEXT
                , signal_quality_status TEXT
                , frame_metadata_json TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ecg_frames_frame_index
            ON ecg_frames(frame_index)
            """
        )
        self._ensure_optional_columns()
        self._connection.commit()

    def _ensure_optional_columns(self) -> None:
        existing = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(ecg_frames)")
        }
        optional_columns = {
            "session_id": "TEXT",
            "protocol_version": "INTEGER",
            "acquisition_status": "TEXT",
            "calibration_status": "TEXT",
            "signal_quality_status": "TEXT",
            "frame_metadata_json": "TEXT",
        }
        for name, column_type in optional_columns.items():
            if name not in existing:
                self._connection.execute(
                    f"ALTER TABLE ecg_frames ADD COLUMN {name} {column_type}"
                )

    @staticmethod
    def _row_to_metadata(row: sqlite3.Row) -> dict[str, Any]:
        if "frame_metadata_json" in row.keys() and row["frame_metadata_json"]:
            full = json.loads(row["frame_metadata_json"])
            if isinstance(full, dict):
                return full
        return {
            "measurement_id": row["measurement_id"],
            "device_id": row["device_id"],
            "frame_index": row["frame_index"],
            "state": row["state"],
            "created_at_utc": row["created_at_utc"],
            "sample_rate_hz": row["sample_rate_hz"],
            "sample_count": row["sample_count"],
            "duration_seconds": row["duration_seconds"],
            "shape": json.loads(row["shape_json"]),
            "dtype": row["dtype"],
            "sha256_checksum": row["sha256_checksum"],
            "channel_order": json.loads(row["channel_order_json"]),
            "lead_mapping": json.loads(row["lead_mapping_json"]),
            "ch3_derived": bool(row["ch3_derived"]),
            "sequence_start": row["sequence_start"],
            "sequence_end": row["sequence_end"],
            "timestamp_us_start": row["timestamp_us_start"],
            "timestamp_us_end": row["timestamp_us_end"],
            "csv_file": row["csv_file"],
            "bin_file": row["bin_file"],
            "npy_file": row["npy_file"],
            "json_file": row["json_file"],
            "parser_stats": json.loads(row["parser_stats_json"]),
        }

"""Validate the latest calibrated mV ECG frame export."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import yaml

from calibration_utils import (
    CALIBRATED_CSV_HEADER,
    CH3_CONSISTENCY_TOLERANCE_MV,
    EXPECTED_DTYPE,
    EXPECTED_SHAPE,
    ch3_max_error,
    count_nan_inf,
    file_sha256,
)


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
EXPECTED_CSV_ROWS = 2501


class ValidationError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the latest calibrated ECGRHYTHMIA mV frame"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to receiver config.yaml",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Directory containing frame_*_mv.csv/.npy/.json files",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_data_dir(config_path: Path, data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir.resolve()

    config = load_config(config_path)
    output_config = config.get("output", {})
    configured_dir = Path(output_config.get("directory", "data"))
    if configured_dir.is_absolute():
        raw_data_dir = configured_dir
    else:
        raw_data_dir = config_path.parent / configured_dir

    calibrated_dir = Path(output_config.get("calibrated_directory", "calibrated"))
    if calibrated_dir.is_absolute():
        return calibrated_dir.resolve()
    return (raw_data_dir / calibrated_dir).resolve()


def latest_mv_frame_set(data_dir: Path) -> tuple[Path, Path, Path]:
    csv_candidates = sorted(data_dir.glob("frame_*_mv.csv"))
    if not csv_candidates:
        raise ValidationError(f"no frame_*_mv.csv files found in {data_dir}")

    csv_path = csv_candidates[-1]
    stem = csv_path.name.removesuffix("_mv.csv")
    npy_path = data_dir / f"{stem}_mv.npy"
    json_path = data_dir / f"{stem}_mv.json"
    if not npy_path.exists():
        raise ValidationError(f"matching NPY not found for {csv_path.name}")
    if not json_path.exists():
        raise ValidationError(f"matching JSON not found for {csv_path.name}")
    return csv_path, npy_path, json_path


def validate_csv(csv_path: Path) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    if len(rows) != EXPECTED_CSV_ROWS:
        raise ValidationError(
            f"{csv_path.name} has {len(rows)} rows, expected {EXPECTED_CSV_ROWS}"
        )
    if rows[0] != CALIBRATED_CSV_HEADER:
        raise ValidationError(
            f"{csv_path.name} header is {rows[0]}, expected {CALIBRATED_CSV_HEADER}"
        )

    first_time = rows[1][0]
    last_time = rows[-1][0]
    if first_time != "0.000" or last_time != "9.996":
        raise ValidationError(
            f"{csv_path.name} time range is {first_time}..{last_time}, "
            "expected 0.000..9.996"
        )


def validate_npy(npy_path: Path) -> np.ndarray:
    samples = np.load(npy_path, allow_pickle=False)
    if samples.shape != EXPECTED_SHAPE:
        raise ValidationError(
            f"{npy_path.name} shape is {samples.shape}, expected {EXPECTED_SHAPE}"
        )
    if samples.dtype != EXPECTED_DTYPE:
        raise ValidationError(
            f"{npy_path.name} dtype is {samples.dtype}, expected {EXPECTED_DTYPE}"
        )
    bad_count = count_nan_inf(samples)
    if bad_count:
        raise ValidationError(f"{npy_path.name} has {bad_count} NaN/Inf values")

    max_error = ch3_max_error(samples)
    if max_error > CH3_CONSISTENCY_TOLERANCE_MV:
        raise ValidationError(
            f"{npy_path.name} Lead III max error {max_error:.6g} mV"
        )
    return samples


def validate_json(json_path: Path) -> dict[str, Any]:
    with json_path.open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    if not isinstance(metadata, dict):
        raise ValidationError(f"{json_path.name} does not contain a JSON object")
    if metadata.get("unit") != "mV":
        raise ValidationError(f"{json_path.name} unit is not mV")
    if metadata.get("shape") != list(EXPECTED_SHAPE):
        raise ValidationError(
            f"{json_path.name} shape metadata is {metadata.get('shape')}"
        )
    if metadata.get("dtype") != "float32":
        raise ValidationError(f"{json_path.name} dtype metadata is not float32")
    return metadata


def validate_npy_hash(npy_path: Path, metadata: dict[str, Any]) -> None:
    expected = metadata.get("npy_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValidationError(f"{npy_path.name} metadata is missing valid npy_sha256")
    actual = file_sha256(npy_path)
    if actual != expected.lower():
        raise ValidationError(f"{npy_path.name} SHA-256 does not match metadata")


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    data_dir = resolve_data_dir(config_path, args.data_dir)

    try:
        csv_path, npy_path, json_path = latest_mv_frame_set(data_dir)
        validate_csv(csv_path)
        metadata = validate_json(json_path)
        validate_npy_hash(npy_path, metadata)
        samples = validate_npy(npy_path)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, ValidationError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("OK: latest calibrated mV ECG frame is valid")
    print(f"CSV: {csv_path}")
    print(f"NPY: {npy_path}")
    print(f"JSON: {json_path}")
    print(f"shape: {samples.shape}")
    print(f"unit: {metadata['unit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

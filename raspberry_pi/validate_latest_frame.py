"""Validate the latest exported dummy ECG frame."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
from typing import Any

import numpy as np
import yaml


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
EXPECTED_HEADER = ["time", "ch1", "ch2", "ch3"]
EXPECTED_CSV_ROWS = 2501
EXPECTED_SHAPE = (2500, 3)
EXPECTED_DTYPE = np.dtype("float32")
CHECK_INDICES = (0, 1, 2, 625, 1250, 1875, 2499)


class ValidationError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the latest ECGRHYTHMIA frame CSV and NPY output"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Directory containing frame_*.csv and frame_*.npy files",
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
        return configured_dir
    return (config_path.parent / configured_dir).resolve()


def latest_frame_pair(data_dir: Path) -> tuple[Path, Path]:
    csv_candidates = sorted(data_dir.glob("frame_*.csv"))
    if not csv_candidates:
        raise ValidationError(f"no frame_*.csv files found in {data_dir}")

    csv_path = csv_candidates[-1]
    npy_path = csv_path.with_suffix(".npy")
    if not npy_path.exists():
        raise ValidationError(f"matching NPY not found for {csv_path.name}")

    return csv_path, npy_path


def validate_csv(csv_path: Path) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    if len(rows) != EXPECTED_CSV_ROWS:
        raise ValidationError(
            f"{csv_path.name} has {len(rows)} rows, expected {EXPECTED_CSV_ROWS}"
        )

    if rows[0] != EXPECTED_HEADER:
        raise ValidationError(
            f"{csv_path.name} header is {rows[0]}, expected {EXPECTED_HEADER}"
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

    return samples


def validate_lead_iii(samples: np.ndarray) -> None:
    for index in CHECK_INDICES:
        lead_i = samples[index, 0]
        lead_ii = samples[index, 1]
        lead_iii = samples[index, 2]
        expected = lead_ii - lead_i
        if not np.isclose(lead_iii, expected, rtol=0.0, atol=1e-5):
            raise ValidationError(
                f"sample {index} ch3={lead_iii} does not equal ch2-ch1={expected}"
            )


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    data_dir = resolve_data_dir(config_path, args.data_dir)

    try:
        csv_path, npy_path = latest_frame_pair(data_dir)
        validate_csv(csv_path)
        samples = validate_npy(npy_path)
        validate_lead_iii(samples)
    except (OSError, ValidationError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("OK: latest ECG frame is valid")
    print(f"CSV: {csv_path}")
    print(f"NPY: {npy_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

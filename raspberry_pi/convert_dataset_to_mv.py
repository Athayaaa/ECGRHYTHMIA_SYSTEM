"""Convert raw ADS1293 frame datasets to calibrated mV frame files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml

from calibration_utils import (
    CalibrationError,
    convert_raw_to_mv,
    find_complete_frame_sets,
    load_calibration_config,
    load_raw_frame,
    notch_50hz_processing,
    write_calibrated_frame,
)
from ecg_filters import apply_notch_filter


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "calibration_config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert raw frame_*.npy datasets to calibrated mV CSV/NPY/JSON"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory containing raw frame_*.csv/.bin/.npy/.json files",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to calibration_config.yaml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Output directory for frame_xxxxxx_mv.* files",
    )
    parser.add_argument(
        "--filtered-output-dir",
        type=Path,
        help="Optional output directory for automatic notch-filtered mV frames",
    )
    return parser.parse_args()


def path_is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    filtered_output_dir = (
        args.filtered_output_dir.resolve()
        if args.filtered_output_dir is not None
        else output_dir.parent / "filtered"
    )

    try:
        if path_is_inside(output_dir, input_dir):
            raise CalibrationError(
                "--output-dir must be outside --input-dir to keep raw datasets unchanged"
            )

        calibration = load_calibration_config(args.config.resolve())
        frames = find_complete_frame_sets(input_dir)
        if not frames:
            raise CalibrationError(f"no complete frame sets found in {input_dir}")

        written = 0
        for frame in frames:
            raw_samples, metadata = load_raw_frame(frame)
            sample_rate_hz = int(metadata.get("sample_rate_hz", 250))
            if sample_rate_hz <= 0:
                sample_rate_hz = 250

            samples_mv = convert_raw_to_mv(raw_samples, calibration)
            paths = write_calibrated_frame(
                output_dir=output_dir,
                stem=frame.stem,
                samples_mv=samples_mv,
                sample_rate_hz=sample_rate_hz,
                calibration=calibration,
                source_metadata=metadata,
                source_files={
                    "csv": str(frame.csv_path),
                    "bin": str(frame.bin_path),
                    "npy": str(frame.npy_path),
                    "json": str(frame.json_path),
                },
            )
            processing = notch_50hz_processing(calibration)
            if processing is not None:
                filtered_samples = apply_notch_filter(
                    samples_mv,
                    sample_rate_hz,
                    frequency_hz=float(processing["frequency_hz"]),
                    quality_factor=float(processing["quality_factor"]),
                )
                write_calibrated_frame(
                    output_dir=filtered_output_dir,
                    stem=frame.stem,
                    samples_mv=filtered_samples,
                    sample_rate_hz=sample_rate_hz,
                    calibration=calibration,
                    source_metadata=metadata,
                    source_files={
                        "csv": str(frame.csv_path),
                        "bin": str(frame.bin_path),
                        "npy": str(frame.npy_path),
                        "json": str(frame.json_path),
                    },
                    processing={"filters": [processing]},
                )
            written += 1
            print(
                f"exported {frame.stem}: {paths.csv_path.name}, "
                f"{paths.npy_path.name}, {paths.json_path.name}"
            )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, CalibrationError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print(f"OK: converted {written} frame(s) to mV")
    print(f"output_dir: {output_dir}")
    if notch_50hz_processing(calibration) is not None:
        print(f"filtered_output_dir: {filtered_output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

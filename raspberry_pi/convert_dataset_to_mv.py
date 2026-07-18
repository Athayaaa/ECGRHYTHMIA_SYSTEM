"""Convert raw ADS1293 frame datasets to calibrated mV frame files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml

from ads1293_session import verify_persisted_session_metadata
from calibration_utils import (
    CalibrationError,
    convert_raw_to_mv,
    find_complete_frame_sets,
    is_legacy_calibration,
    load_calibration_config,
    load_raw_frame,
    offset_code_range_stats,
    require_calibration_compatible,
    require_corrected_frame_provenance,
    require_frame_output_sample_rate,
    require_frame_session_link,
    write_calibrated_frame,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "calibration_config_ads1293_266p667_nominal.yaml"
LEGACY_CONFIG_NAME = "calibration_config_legacy_640_to_250.yaml"


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
        help=(
            "Calibration YAML. Defaults to the corrected preliminary ADS1293 "
            f"nominal config ({DEFAULT_CONFIG_PATH.name}). Legacy conversion is "
            f"explicit-only via {LEGACY_CONFIG_NAME}."
        ),
    )
    parser.add_argument(
        "--session-metadata",
        type=Path,
        help=(
            "Required verified session JSON for corrected-pipeline input. The "
            "legacy profile is the only mode that may omit this artifact."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Output directory for frame_xxxxxx_mv.* files",
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

    try:
        if path_is_inside(output_dir, input_dir):
            raise CalibrationError(
                "--output-dir must be outside --input-dir to keep raw datasets unchanged"
            )
        if output_dir.exists() or output_dir.is_symlink():
            raise CalibrationError(
                "--output-dir must be a new, non-existing path so prior conversion "
                f"artifacts cannot be overwritten: {output_dir}"
            )

        config_path = (
            args.config.resolve() if args.config is not None else DEFAULT_CONFIG_PATH.resolve()
        )
        explicit_legacy = (
            args.config is not None and config_path.name == LEGACY_CONFIG_NAME
        )
        calibration = load_calibration_config(
            config_path,
            allow_legacy=explicit_legacy,
        )
        if is_legacy_calibration(calibration) and not explicit_legacy:
            raise CalibrationError(
                f"legacy calibration must be supplied explicitly as {LEGACY_CONFIG_NAME}"
            )

        legacy = is_legacy_calibration(calibration)
        if not legacy and args.session_metadata is None:
            raise CalibrationError(
                "--session-metadata is required for corrected-pipeline conversion"
            )

        session_metadata = None
        if args.session_metadata is not None:
            session_metadata = json.loads(args.session_metadata.resolve().read_text(encoding="utf-8"))
            if not isinstance(session_metadata, dict):
                raise CalibrationError("--session-metadata must contain a JSON object")
            if not legacy:
                verify_persisted_session_metadata(session_metadata)
        frames = find_complete_frame_sets(input_dir)
        if not frames:
            raise CalibrationError(f"no complete frame sets found in {input_dir}")

        # Atomically reserve this run's output path after all global preflight
        # checks. Per-frame writers may now create artifacts only inside this
        # uniquely owned directory.
        output_dir.mkdir(parents=True, exist_ok=False)
        written = 0
        for frame in frames:
            raw_samples, metadata = load_raw_frame(frame)
            if not legacy:
                assert session_metadata is not None
                require_frame_session_link(metadata, session_metadata)
                compatibility = require_calibration_compatible(
                    calibration,
                    session_metadata,
                )
                calibration["compatibility"] = compatibility.to_dict()
                require_corrected_frame_provenance(metadata)
            range_stats = offset_code_range_stats(
                raw_samples,
                adcmax=calibration["adcmax"],
            )
            if range_stats["out_of_range_count"]:
                raise CalibrationError(
                    f"{frame.stem} has {range_stats['out_of_range_count']} Lead I/II "
                    "offset-code value(s) outside the configured ADC range"
                )
            sample_rate_hz = require_frame_output_sample_rate(metadata, calibration)

            samples_mv = convert_raw_to_mv(raw_samples, calibration)
            paths = write_calibrated_frame(
                output_dir=output_dir,
                stem=frame.stem,
                samples_mv=samples_mv,
                sample_rate_hz=sample_rate_hz,
                calibration=calibration,
                source_metadata=metadata,
                raw_range_stats=range_stats,
                source_files={
                    "csv": str(frame.csv_path),
                    "bin": str(frame.bin_path),
                    "npy": str(frame.npy_path),
                    "json": str(frame.json_path),
                },
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
    print(f"calibration_config: {config_path}")
    print(f"pipeline_profile: {calibration['pipeline_profile']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

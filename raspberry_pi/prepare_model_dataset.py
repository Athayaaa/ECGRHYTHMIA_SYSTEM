"""Batch prepare calibrated mV ECG frames as model-ready NPY inputs."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

import yaml

from model_input_utils import (
    DEFAULT_MAX_ABS_MV,
    MODEL_INPUT_UNIT,
    ModelInputError,
    frame_id_from_input,
    output_paths_for_frame,
    prepare_model_input_file,
)


MANIFEST_FIELDS = [
    "frame_id",
    "npy_path",
    "metadata_path",
    "shape",
    "dtype",
    "unit",
    "sample_rate_hz",
    "lead_i_min_mV",
    "lead_i_max_mV",
    "lead_ii_min_mV",
    "lead_ii_max_mV",
    "lead_iii_min_mV",
    "lead_iii_max_mV",
    "nan_inf_count",
    "lead_iii_max_error_mV",
    "validation_status",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a directory of calibrated mV frames for model input"
    )
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-abs-mv", type=float, default=DEFAULT_MAX_ABS_MV)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--preprocessing-config", type=Path)
    return parser.parse_args()


def manifest_row_from_prepared(prepared) -> dict[str, object]:
    stats = prepared.validation.lead_stats
    return {
        "frame_id": prepared.frame_id,
        "npy_path": str(prepared.paths.npy_path),
        "metadata_path": str(prepared.paths.metadata_path),
        "shape": str(tuple(prepared.metadata["shape"])),
        "dtype": prepared.metadata["dtype"],
        "unit": prepared.metadata["unit"],
        "sample_rate_hz": prepared.metadata["sample_rate_hz"],
        "lead_i_min_mV": stats["lead_i"]["min_mV"],
        "lead_i_max_mV": stats["lead_i"]["max_mV"],
        "lead_ii_min_mV": stats["lead_ii"]["min_mV"],
        "lead_ii_max_mV": stats["lead_ii"]["max_mV"],
        "lead_iii_min_mV": stats["lead_iii"]["min_mV"],
        "lead_iii_max_mV": stats["lead_iii"]["max_mV"],
        "nan_inf_count": prepared.validation.nan_inf_count,
        "lead_iii_max_error_mV": prepared.validation.lead_iii_max_error_mV,
        "validation_status": prepared.validation.status,
    }


def manifest_row_from_failure(
    npy_path: Path,
    output_dir: Path,
    message: str,
) -> dict[str, object]:
    frame_id = frame_id_from_input(npy_path)
    output_paths = output_paths_for_frame(output_dir, frame_id)
    return {
        "frame_id": frame_id,
        "npy_path": str(output_paths.npy_path),
        "metadata_path": str(output_paths.metadata_path),
        "shape": "",
        "dtype": "",
        "unit": MODEL_INPUT_UNIT,
        "sample_rate_hz": "",
        "lead_i_min_mV": "",
        "lead_i_max_mV": "",
        "lead_ii_min_mV": "",
        "lead_ii_max_mV": "",
        "lead_iii_min_mV": "",
        "lead_iii_max_mV": "",
        "nan_inf_count": "",
        "lead_iii_max_error_mV": "",
        "validation_status": f"FAIL: {message}",
    }


def write_manifest(output_dir: Path, rows: list[dict[str, object]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "dataset_manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return manifest_path


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    rows: list[dict[str, object]] = []
    processed = 0
    skipped = 0
    failed = 0

    try:
        for npy_path in sorted(input_dir.glob("frame_*_mv.npy")):
            metadata_path = npy_path.with_suffix(".json")
            if not metadata_path.exists():
                skipped += 1
                print(f"skipped incomplete pair: {npy_path.name}")
                continue

            try:
                prepared = prepare_model_input_file(
                    input_path=npy_path,
                    metadata_path=metadata_path,
                    output_dir=output_dir,
                    max_abs_mV=float(args.max_abs_mv),
                    overwrite=bool(args.overwrite),
                    preprocessing_config_path=(
                        args.preprocessing_config.resolve()
                        if args.preprocessing_config is not None
                        else None
                    ),
                )
                rows.append(manifest_row_from_prepared(prepared))
                processed += 1
                print(f"prepared {prepared.frame_id}")
            except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, ModelInputError) as error:
                failed += 1
                rows.append(manifest_row_from_failure(npy_path, output_dir, str(error)))
                print(f"failed {npy_path.name}: {error}")

        manifest_path = write_manifest(output_dir, rows)
    except OSError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("OK: model-ready dataset preparation complete")
    print(f"processed: {processed}")
    print(f"skipped_incomplete: {skipped}")
    print(f"failed: {failed}")
    print(f"manifest: {manifest_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

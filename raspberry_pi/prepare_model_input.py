"""Prepare one calibrated mV ECG frame as a canonical model-ready NPY input."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml

from model_input_utils import (
    DEFAULT_MAX_ABS_MV,
    ModelInputError,
    prepare_model_input_file,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a calibrated ECG mV frame for future 1D-CNN input"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-abs-mv", type=float, default=DEFAULT_MAX_ABS_MV)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--preprocessing-config", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        prepared = prepare_model_input_file(
            input_path=args.input.resolve(),
            metadata_path=args.metadata.resolve(),
            output_dir=args.output_dir.resolve(),
            max_abs_mV=float(args.max_abs_mv),
            overwrite=bool(args.overwrite),
            preprocessing_config_path=(
                args.preprocessing_config.resolve()
                if args.preprocessing_config is not None
                else None
            ),
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, ModelInputError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("OK: model-ready ECG input prepared")
    print(f"frame_id: {prepared.frame_id}")
    print(f"NPY: {prepared.paths.npy_path}")
    print(f"JSON: {prepared.paths.metadata_path}")
    print(f"shape: {tuple(prepared.metadata['shape'])}")
    print(f"dtype: {prepared.metadata['dtype']}")
    print(f"unit: {prepared.metadata['unit']}")
    print(f"validation_status: {prepared.validation.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

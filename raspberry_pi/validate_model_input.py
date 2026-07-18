"""Validate a model-ready ECG NPY input and metadata pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

from calibration_utils import CHANNEL_ORDER
from model_input_utils import (
    DEFAULT_MAX_ABS_MV,
    MODEL_SAMPLE_RATE_HZ,
    ModelInputError,
    include_raw_range_validation,
    load_model_metadata,
    validate_model_samples,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a model-ready ECG frame")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--max-abs-mv", type=float, default=None)
    return parser.parse_args()


def metadata_max_abs_limit(metadata: dict, override: float | None) -> float:
    if override is not None:
        return float(override)
    validation = metadata.get("validation", {})
    if isinstance(validation, dict) and "max_abs_limit_mV" in validation:
        return float(validation["max_abs_limit_mV"])
    return DEFAULT_MAX_ABS_MV


def metadata_checks(metadata: dict) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []

    sample_rate_hz = metadata.get("sample_rate_hz")
    if sample_rate_hz != MODEL_SAMPLE_RATE_HZ:
        failures.append(
            f"metadata sample_rate_hz {sample_rate_hz} != {MODEL_SAMPLE_RATE_HZ}"
        )

    channel_order = metadata.get("channel_order")
    if channel_order != CHANNEL_ORDER:
        failures.append(f"metadata channel_order {channel_order} != {CHANNEL_ORDER}")

    if metadata.get("unit") != "mV":
        failures.append(f"metadata unit {metadata.get('unit')!r} != 'mV'")

    if metadata.get("preprocessing_steps_applied") not in ([], None):
        warnings.append(
            f"preprocessing steps present: {metadata.get('preprocessing_steps_applied')}"
        )

    return failures, warnings


def print_lead_stats(stats: dict[str, dict[str, float]]) -> None:
    for lead in ("lead_i", "lead_ii", "lead_iii"):
        values = stats[lead]
        print(
            f"{lead}: min={values['min_mV']:.6g} max={values['max_mV']:.6g} "
            f"mean={values['mean_mV']:.6g} std={values['std_mV']:.6g}"
        )


def main() -> int:
    args = parse_args()
    try:
        input_path = args.input.resolve()
        metadata = load_model_metadata(
            args.metadata.resolve(),
            input_path=input_path,
        )
        samples = np.load(input_path, allow_pickle=False)
        max_abs_mV = metadata_max_abs_limit(metadata, args.max_abs_mv)
        report = validate_model_samples(
            samples,
            max_abs_mV=max_abs_mV,
            require_float32=True,
        )
        report = include_raw_range_validation(report, metadata)
        metadata_failures, metadata_warnings = metadata_checks(metadata)
    except (OSError, ValueError, json.JSONDecodeError, ModelInputError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    failures = list(report.failures) + metadata_failures
    warnings = list(report.warnings) + metadata_warnings
    status = "FAIL" if failures else "WARNING" if warnings else "PASS"

    print(f"shape: {report.shape}")
    print(f"dtype: {report.dtype}")
    print_lead_stats(report.lead_stats)
    print(f"nan_inf_count: {report.nan_inf_count}")
    print(f"maximum_absolute_mV: {report.max_abs_mV:.6g}")
    print(f"lead_iii_max_error_mV: {report.lead_iii_max_error_mV:.6g}")
    print(f"sample_rate_hz: {metadata.get('sample_rate_hz')}")
    print(f"channel_order: {metadata.get('channel_order')}")
    print(f"result: {status}")
    for failure in failures:
        print(f"FAIL: {failure}")
    for warning in warnings:
        print(f"WARNING: {warning}")

    return 0 if status in {"PASS", "WARNING"} else 1


if __name__ == "__main__":
    sys.exit(main())

"""Automatic ProSim raw-count to mV calibration for ADS1293 frame exports."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import yaml

from calibration_utils import (
    CHANNELS,
    CH3_CONSISTENCY_TOLERANCE_COUNTS,
    CalibrationError,
    compute_offsets,
    count_saturation,
    find_complete_frame_sets,
    latest_n_frames,
    lead_summary,
    load_raw_frame,
    load_yaml,
    robust_peak_to_peak,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.yaml"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "calibration_config.yaml"
DEFAULT_LOWER_PERCENTILE = 1.0
DEFAULT_UPPER_PERCENTILE = 99.0
DEFAULT_MIN_AMPLITUDE_COUNT = 100.0
DEFAULT_WARN_AMPLITUDE_COUNT = 1000.0


@dataclass(frozen=True)
class CalibrationResult:
    config: dict[str, Any]
    raw_reference_amplitude_count: float
    lower_count_after_median_removal: float
    upper_count_after_median_removal: float
    warnings: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto-calibrate ADS1293 raw counts to mV from ProSim frames"
    )
    parser.add_argument(
        "--input-dir",
        "--data-dir",
        dest="input_dir",
        type=Path,
        help="Directory containing raw frame_*.csv/.bin/.npy/.json files",
    )
    parser.add_argument("--expected-mv", type=float, default=1.0)
    parser.add_argument(
        "--lead",
        choices=("lead_ii",),
        default="lead_ii",
        help="Calibration reference lead. Phase 3E uses Lead II only.",
    )
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument(
        "--method",
        choices=("robust_pp",),
        default="robust_pp",
        help="Amplitude estimation method",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Calibration YAML output path",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Receiver config.yaml used only when --input-dir is omitted",
    )
    parser.add_argument("--lower-percentile", type=float, default=DEFAULT_LOWER_PERCENTILE)
    parser.add_argument("--upper-percentile", type=float, default=DEFAULT_UPPER_PERCENTILE)
    parser.add_argument(
        "--min-amplitude-count",
        type=float,
        default=DEFAULT_MIN_AMPLITUDE_COUNT,
        help="Fail when robust peak-to-peak amplitude is below this count",
    )
    parser.add_argument(
        "--warn-amplitude-count",
        type=float,
        default=DEFAULT_WARN_AMPLITUDE_COUNT,
        help="Warn when robust peak-to-peak amplitude is below this count",
    )
    return parser.parse_args()


def resolve_input_dir(config_path: Path, input_dir: Path | None) -> Path:
    if input_dir is not None:
        return input_dir.resolve()

    config = load_yaml(config_path)
    output_config = config.get("output", {})
    configured_dir = Path(output_config.get("directory", "data"))
    if configured_dir.is_absolute():
        return configured_dir.resolve()
    return (config_path.parent / configured_dir).resolve()


def resolve_output_path(output_path: Path) -> Path:
    if output_path.is_absolute():
        return output_path
    return (Path.cwd() / output_path).resolve()


def build_calibration(args: argparse.Namespace, input_dir: Path) -> CalibrationResult:
    available_frames = find_complete_frame_sets(input_dir)
    if not available_frames:
        raise CalibrationError(f"no complete frame sets found in {input_dir}")

    selected_frames = latest_n_frames(input_dir, args.frames)
    arrays: list[np.ndarray] = []
    metadata_rows: list[dict[str, Any]] = []
    for frame in selected_frames:
        samples, metadata = load_raw_frame(frame)
        arrays.append(samples)
        metadata_rows.append(metadata)

    raw_samples = np.concatenate(arrays, axis=0)
    offsets = compute_offsets(raw_samples)
    sample_rate_hz = int(metadata_rows[-1].get("sample_rate_hz", 250))
    if sample_rate_hz <= 0:
        sample_rate_hz = 250

    raw_amplitude_count, lower_count, upper_count = robust_peak_to_peak(
        samples=raw_samples,
        lead=args.lead,
        lower_percentile=float(args.lower_percentile),
        upper_percentile=float(args.upper_percentile),
    )
    if raw_amplitude_count < args.min_amplitude_count:
        raise CalibrationError(
            f"raw reference amplitude {raw_amplitude_count:.6g} count is too small"
        )

    warnings: list[str] = []
    if raw_amplitude_count < args.warn_amplitude_count:
        warnings.append(
            f"raw reference amplitude is small ({raw_amplitude_count:.6g} count)"
        )

    if not np.isfinite(float(args.expected_mv)) or float(args.expected_mv) <= 0.0:
        raise CalibrationError("--expected-mv must be a finite positive value")

    scale_mV_per_count = float(args.expected_mv) / float(raw_amplitude_count)
    frames_used = [frame.stem for frame in selected_frames]
    duration_sec = raw_samples.shape[0] / float(sample_rate_hz)

    config = {
        "calibration_source": "prosim",
        "expected_mV": float(args.expected_mv),
        "selected_lead": "lead_ii",
        "method": args.method,
        "unit": "mV",
        "offset_count": offsets,
        "scale_mV_per_count": scale_mV_per_count,
        "scale_applied_to_all_leads": True,
        "channel_order": ["Lead I", "Lead II", "Lead III"],
        "lead_mapping": {
            "lead_i": "Lead I = LA - RA",
            "lead_ii": "Lead II = LL - RA",
            "lead_iii": "Lead III = Lead II - Lead I",
        },
        "invert": {key: False for key, _, _ in CHANNELS},
        "filters": {
            "remove_mean": False,
            "high_pass_cutoff_hz": 0.0,
            "low_pass_cutoff_hz": 0.0,
            "notch_50hz": False,
        },
        "metadata": {
            "input_dir": str(input_dir),
            "frames_available": len(available_frames),
            "frames_used": frames_used,
            "sample_rate_hz": sample_rate_hz,
            "sample_count": int(raw_samples.shape[0]),
            "duration_sec": duration_sec,
            "created_at_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "reference_lead": "lead_ii",
            "reference_lead_label": "Lead II",
            "raw_reference_amplitude_count": raw_amplitude_count,
            "lower_percentile": float(args.lower_percentile),
            "upper_percentile": float(args.upper_percentile),
            "lower_count_after_median_removal": lower_count,
            "upper_count_after_median_removal": upper_count,
            "offset_method": "median Lead I and Lead II; Lead III offset = Lead II - Lead I",
            "scale_method": "expected_mV / robust Lead II peak-to-peak count",
            "ch3_tolerance_count": CH3_CONSISTENCY_TOLERANCE_COUNTS,
            "saturation_count": count_saturation(raw_samples),
            "lead_statistics": lead_summary(raw_samples),
        },
    }

    return CalibrationResult(
        config=config,
        raw_reference_amplitude_count=raw_amplitude_count,
        lower_count_after_median_removal=lower_count,
        upper_count_after_median_removal=upper_count,
        warnings=warnings,
    )


def save_calibration_config(config: dict[str, Any], output_path: Path) -> Path:
    output_path = resolve_output_path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return output_path


def print_summary(result: CalibrationResult, output_path: Path) -> None:
    config = result.config
    metadata = config["metadata"]
    offsets = config["offset_count"]

    print("OK: ProSim calibration complete")
    print(f"frames_used: {', '.join(metadata['frames_used'])}")
    print(f"input_dir: {metadata['input_dir']}")
    print(f"sample_rate_hz: {metadata['sample_rate_hz']}")
    print(f"duration_sec: {metadata['duration_sec']:.3f}")
    print("offset_count:")
    print(f"  lead_i: {offsets['lead_i']:.6g}")
    print(f"  lead_ii: {offsets['lead_ii']:.6g}")
    print(f"  lead_iii: {offsets['lead_iii']:.6g} (lead_ii - lead_i)")
    print("reference_lead: lead_ii (Lead II only)")
    print(f"expected_mV: {config['expected_mV']:.6g}")
    print(
        "raw_reference_amplitude_count: "
        f"{result.raw_reference_amplitude_count:.6g} "
        f"(p{metadata['lower_percentile']:.3g}={result.lower_count_after_median_removal:.6g}, "
        f"p{metadata['upper_percentile']:.3g}={result.upper_count_after_median_removal:.6g})"
    )
    print(f"scale_mV_per_count: {config['scale_mV_per_count']:.12g}")
    print("scale_applied_to_all_leads: true")
    print(f"calibration_config: {output_path}")
    for warning in result.warnings:
        print(f"WARNING: {warning}")


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()

    try:
        input_dir = resolve_input_dir(config_path, args.input_dir)
        result = build_calibration(args, input_dir)
        output_path = save_calibration_config(result.config, args.output)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, CalibrationError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print_summary(result, output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    RPeakBaselineResult,
    compute_offsets,
    count_saturation,
    find_complete_frame_sets,
    latest_n_frames,
    lead_summary,
    load_raw_frame,
    load_yaml,
    measure_rpeak_baseline_amplitude,
    robust_peak_to_peak,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.yaml"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "calibration_config.yaml"
DEFAULT_LOWER_PERCENTILE = 1.0
DEFAULT_UPPER_PERCENTILE = 99.0
DEFAULT_MIN_AMPLITUDE_COUNT = 100.0
DEFAULT_WARN_AMPLITUDE_COUNT = 1000.0
METHODS = ("rpeak_baseline", "multipoint_rpeak", "robust_pp")


@dataclass(frozen=True)
class LoadedDataset:
    input_dir: Path
    raw_samples: np.ndarray
    sample_rate_hz: int
    frames_used: list[str]
    frames_available: int
    metadata_rows: list[dict[str, Any]]


@dataclass(frozen=True)
class CalibrationPoint:
    input_dir: Path
    expected_mV: float


@dataclass(frozen=True)
class SinglePointDetails:
    raw_reference_amplitude_count: float
    lower_count_after_median_removal: float | None
    upper_count_after_median_removal: float | None
    rpeak_result: RPeakBaselineResult | None


@dataclass(frozen=True)
class CalibrationResult:
    config: dict[str, Any]
    warnings: list[str]
    single_point: SinglePointDetails | None = None
    multipoint_rows: list[dict[str, Any]] | None = None


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
    parser.add_argument(
        "--point",
        action="append",
        default=[],
        metavar="DIRECTORY:EXPECTED_MV",
        help="Multipoint calibration point. Repeat for every ProSim amplitude.",
    )
    parser.add_argument("--expected-mv", type=float, default=1.0)
    parser.add_argument(
        "--expected-bpm",
        type=float,
        help="Expected ProSim BPM for R-peak detection",
    )
    parser.add_argument(
        "--lead",
        choices=("lead_ii",),
        default="lead_ii",
        help="Calibration reference lead. Phase 3F uses Lead II only.",
    )
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument(
        "--method",
        choices=METHODS,
        default="rpeak_baseline",
        help=(
            "Calibration method. robust_pp is legacy/preliminary and is not "
            "recommended for final absolute mV calibration."
        ),
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
        help="Fail when reference amplitude is below this count",
    )
    parser.add_argument(
        "--warn-amplitude-count",
        type=float,
        default=DEFAULT_WARN_AMPLITUDE_COUNT,
        help="Warn when reference amplitude is below this count",
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


def parse_point(raw_point: str) -> CalibrationPoint:
    try:
        directory_text, expected_text = raw_point.rsplit(":", 1)
    except ValueError as error:
        raise CalibrationError(
            f"--point must use DIRECTORY:EXPECTED_MV, got {raw_point!r}"
        ) from error

    if not directory_text:
        raise CalibrationError("--point directory must not be empty")
    try:
        expected_mV = float(expected_text)
    except ValueError as error:
        raise CalibrationError(f"invalid --point expected mV: {expected_text}") from error
    if not np.isfinite(expected_mV) or expected_mV <= 0.0:
        raise CalibrationError("--point expected mV must be finite and positive")

    return CalibrationPoint(input_dir=Path(directory_text).resolve(), expected_mV=expected_mV)


def validate_expected_values(args: argparse.Namespace) -> None:
    if args.method in {"rpeak_baseline", "multipoint_rpeak"}:
        if args.expected_bpm is None:
            raise CalibrationError(f"--expected-bpm is required for {args.method}")
        if not np.isfinite(float(args.expected_bpm)) or float(args.expected_bpm) <= 0.0:
            raise CalibrationError("--expected-bpm must be a finite positive value")

    if args.method != "multipoint_rpeak":
        if not np.isfinite(float(args.expected_mv)) or float(args.expected_mv) <= 0.0:
            raise CalibrationError("--expected-mv must be a finite positive value")


def load_dataset(input_dir: Path, frame_count: int) -> LoadedDataset:
    available_frames = find_complete_frame_sets(input_dir)
    if not available_frames:
        raise CalibrationError(f"no complete frame sets found in {input_dir}")

    selected_frames = latest_n_frames(input_dir, frame_count)
    arrays: list[np.ndarray] = []
    metadata_rows: list[dict[str, Any]] = []
    sample_rate_hz = 250
    for frame in selected_frames:
        samples, metadata = load_raw_frame(frame)
        frame_sample_rate_hz = int(metadata.get("sample_rate_hz", sample_rate_hz))
        if frame_sample_rate_hz > 0:
            sample_rate_hz = frame_sample_rate_hz
        arrays.append(samples)
        metadata_rows.append(metadata)

    return LoadedDataset(
        input_dir=input_dir,
        raw_samples=np.concatenate(arrays, axis=0),
        sample_rate_hz=sample_rate_hz,
        frames_used=[frame.stem for frame in selected_frames],
        frames_available=len(available_frames),
        metadata_rows=metadata_rows,
    )


def base_config(
    method: str,
    expected_mV: float | None,
    expected_bpm: float | None,
    offsets: dict[str, float],
    scale_mV_per_count: float,
) -> dict[str, Any]:
    return {
        "calibration_source": "prosim",
        "expected_mV": expected_mV,
        "expected_bpm": expected_bpm,
        "selected_lead": "lead_ii",
        "method": method,
        "unit": "mV",
        "offset_count": dict(offsets),
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
    }


def check_reference_amplitude(
    amplitude_count: float,
    min_amplitude_count: float,
    warn_amplitude_count: float,
    warnings: list[str],
) -> None:
    if amplitude_count < min_amplitude_count:
        raise CalibrationError(
            f"reference amplitude {amplitude_count:.6g} count is too small"
        )
    if amplitude_count < warn_amplitude_count:
        warnings.append(f"reference amplitude is small ({amplitude_count:.6g} count)")


def build_rpeak_baseline_calibration(
    args: argparse.Namespace,
    input_dir: Path,
) -> CalibrationResult:
    dataset = load_dataset(input_dir, args.frames)
    offsets = compute_offsets(dataset.raw_samples)
    rpeak = measure_rpeak_baseline_amplitude(
        samples=dataset.raw_samples,
        sample_rate_hz=dataset.sample_rate_hz,
        expected_bpm=float(args.expected_bpm),
        lead=args.lead,
    )
    warnings: list[str] = []
    check_reference_amplitude(
        rpeak.median_amplitude,
        float(args.min_amplitude_count),
        float(args.warn_amplitude_count),
        warnings,
    )
    scale_mV_per_count = float(args.expected_mv) / float(rpeak.median_amplitude)
    duration_sec = dataset.raw_samples.shape[0] / float(dataset.sample_rate_hz)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    config = base_config(
        method="rpeak_baseline",
        expected_mV=float(args.expected_mv),
        expected_bpm=float(args.expected_bpm),
        offsets=offsets,
        scale_mV_per_count=scale_mV_per_count,
    )
    config["metadata"] = {
        "method": "rpeak_baseline",
        "input_dir": str(dataset.input_dir),
        "frames_available": dataset.frames_available,
        "frames_used": dataset.frames_used,
        "sample_rate_hz": dataset.sample_rate_hz,
        "sample_count": int(dataset.raw_samples.shape[0]),
        "duration_sec": duration_sec,
        "created_at": created_at,
        "created_at_utc": created_at,
        "expected_mV": float(args.expected_mv),
        "expected_bpm": float(args.expected_bpm),
        "reference_lead": "lead_ii",
        "reference_lead_label": "Lead II",
        "beats_detected": rpeak.beats_detected,
        "beats_accepted": rpeak.beats_accepted,
        "beats_rejected": rpeak.beats_rejected,
        "median_amplitude_count": rpeak.median_amplitude,
        "amplitude_count_mad": rpeak.amplitude_mad,
        "estimated_bpm": rpeak.estimated_bpm,
        "detection_threshold": rpeak.detection_threshold,
        "min_peak_distance_samples": rpeak.min_peak_distance_samples,
        "baseline_window_samples": list(rpeak.baseline_window_samples),
        "scale_mV_per_count": scale_mV_per_count,
        "offset_count": dict(offsets),
        "offset_method": "median Lead I and Lead II; Lead III offset = Lead II - Lead I",
        "scale_method": "expected_mV / median beat-by-beat Lead II baseline-to-R amplitude",
        "ch3_tolerance_count": CH3_CONSISTENCY_TOLERANCE_COUNTS,
        "saturation_count": count_saturation(dataset.raw_samples),
        "lead_statistics": lead_summary(dataset.raw_samples),
    }

    return CalibrationResult(
        config=config,
        warnings=warnings,
        single_point=SinglePointDetails(
            raw_reference_amplitude_count=rpeak.median_amplitude,
            lower_count_after_median_removal=None,
            upper_count_after_median_removal=None,
            rpeak_result=rpeak,
        ),
    )


def build_legacy_robust_calibration(
    args: argparse.Namespace,
    input_dir: Path,
) -> CalibrationResult:
    dataset = load_dataset(input_dir, args.frames)
    offsets = compute_offsets(dataset.raw_samples)
    raw_amplitude_count, lower_count, upper_count = robust_peak_to_peak(
        samples=dataset.raw_samples,
        lead=args.lead,
        lower_percentile=float(args.lower_percentile),
        upper_percentile=float(args.upper_percentile),
    )
    warnings = [
        "robust_pp is a legacy/preliminary method and is not recommended for "
        "final absolute mV calibration; use rpeak_baseline or multipoint_rpeak."
    ]
    check_reference_amplitude(
        raw_amplitude_count,
        float(args.min_amplitude_count),
        float(args.warn_amplitude_count),
        warnings,
    )
    scale_mV_per_count = float(args.expected_mv) / float(raw_amplitude_count)
    duration_sec = dataset.raw_samples.shape[0] / float(dataset.sample_rate_hz)

    config = base_config(
        method="robust_pp",
        expected_mV=float(args.expected_mv),
        expected_bpm=float(args.expected_bpm) if args.expected_bpm else None,
        offsets=offsets,
        scale_mV_per_count=scale_mV_per_count,
    )
    config["legacy_method"] = True
    config["not_recommended_for_final_absolute_calibration"] = True
    config["metadata"] = {
        "method": "robust_pp",
        "method_status": "legacy_preliminary_not_recommended",
        "input_dir": str(dataset.input_dir),
        "frames_available": dataset.frames_available,
        "frames_used": dataset.frames_used,
        "sample_rate_hz": dataset.sample_rate_hz,
        "sample_count": int(dataset.raw_samples.shape[0]),
        "duration_sec": duration_sec,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expected_mV": float(args.expected_mv),
        "expected_bpm": float(args.expected_bpm) if args.expected_bpm else None,
        "reference_lead": "lead_ii",
        "raw_reference_amplitude_count": raw_amplitude_count,
        "lower_percentile": float(args.lower_percentile),
        "upper_percentile": float(args.upper_percentile),
        "lower_count_after_median_removal": lower_count,
        "upper_count_after_median_removal": upper_count,
        "scale_mV_per_count": scale_mV_per_count,
        "offset_count": dict(offsets),
        "scale_method": "legacy expected_mV / robust Lead II percentile peak-to-peak count",
        "ch3_tolerance_count": CH3_CONSISTENCY_TOLERANCE_COUNTS,
        "saturation_count": count_saturation(dataset.raw_samples),
        "lead_statistics": lead_summary(dataset.raw_samples),
    }

    return CalibrationResult(
        config=config,
        warnings=warnings,
        single_point=SinglePointDetails(
            raw_reference_amplitude_count=raw_amplitude_count,
            lower_count_after_median_removal=lower_count,
            upper_count_after_median_removal=upper_count,
            rpeak_result=None,
        ),
    )


def fit_line(
    amplitude_counts: np.ndarray, expected_mV: np.ndarray
) -> tuple[float, float, float, np.ndarray]:
    if amplitude_counts.size < 2:
        raise CalibrationError("multipoint_rpeak requires at least two --point values")
    if np.unique(expected_mV).size < 2:
        raise CalibrationError("multipoint_rpeak requires at least two distinct expected mV values")
    if np.unique(amplitude_counts).size < 2:
        raise CalibrationError("multipoint_rpeak requires at least two distinct amplitudes")

    slope, intercept = np.polyfit(amplitude_counts, expected_mV, 1)
    predicted = slope * amplitude_counts + intercept
    residual = expected_mV - predicted
    ss_res = float(np.sum(residual * residual))
    ss_tot = float(np.sum((expected_mV - float(np.mean(expected_mV))) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else float("nan")
    return float(slope), float(intercept), float(r_squared), predicted


def build_multipoint_calibration(args: argparse.Namespace) -> CalibrationResult:
    points = [parse_point(raw_point) for raw_point in args.point]
    if len(points) < 2:
        raise CalibrationError("multipoint_rpeak requires at least two --point arguments")

    rows: list[dict[str, Any]] = []
    all_samples: list[np.ndarray] = []
    sample_rate_hz: int | None = None
    warnings: list[str] = []

    for point in points:
        dataset = load_dataset(point.input_dir, args.frames)
        if sample_rate_hz is None:
            sample_rate_hz = dataset.sample_rate_hz
        elif sample_rate_hz != dataset.sample_rate_hz:
            raise CalibrationError("all multipoint datasets must use the same sample rate")

        rpeak = measure_rpeak_baseline_amplitude(
            samples=dataset.raw_samples,
            sample_rate_hz=dataset.sample_rate_hz,
            expected_bpm=float(args.expected_bpm),
            lead=args.lead,
        )
        check_reference_amplitude(
            rpeak.median_amplitude,
            float(args.min_amplitude_count),
            float(args.warn_amplitude_count),
            warnings,
        )
        all_samples.append(dataset.raw_samples)
        rows.append(
            {
                "input_dir": str(dataset.input_dir),
                "expected_mV": point.expected_mV,
                "frames_used": dataset.frames_used,
                "frames_available": dataset.frames_available,
                "beats_detected": rpeak.beats_detected,
                "beats_accepted": rpeak.beats_accepted,
                "beats_rejected": rpeak.beats_rejected,
                "median_amplitude_count": rpeak.median_amplitude,
                "amplitude_count_mad": rpeak.amplitude_mad,
                "estimated_bpm": rpeak.estimated_bpm,
                "sample_rate_hz": dataset.sample_rate_hz,
            }
        )

    amplitude_counts = np.asarray(
        [float(row["median_amplitude_count"]) for row in rows], dtype=np.float64
    )
    expected_mV = np.asarray([float(row["expected_mV"]) for row in rows], dtype=np.float64)
    slope, intercept, r_squared, predicted_mV = fit_line(amplitude_counts, expected_mV)
    max_expected_mV = float(np.max(np.abs(expected_mV)))
    intercept_warning_threshold = max(0.05, 0.05 * max_expected_mV)
    if abs(intercept) > intercept_warning_threshold:
        warnings.append(
            f"multipoint intercept {intercept:.6g} mV is not close to zero "
            f"(threshold {intercept_warning_threshold:.6g} mV)"
        )

    for row, predicted in zip(rows, predicted_mV):
        error_mV = float(predicted - float(row["expected_mV"]))
        expected = float(row["expected_mV"])
        row["predicted_mV"] = float(predicted)
        row["error_mV"] = error_mV
        row["error_percent"] = 100.0 * error_mV / expected if expected else float("nan")

    combined_samples = np.concatenate(all_samples, axis=0)
    offsets = compute_offsets(combined_samples)
    assert sample_rate_hz is not None
    duration_sec = combined_samples.shape[0] / float(sample_rate_hz)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    config = base_config(
        method="multipoint_rpeak",
        expected_mV=None,
        expected_bpm=float(args.expected_bpm),
        offsets=offsets,
        scale_mV_per_count=slope,
    )
    config["metadata"] = {
        "method": "multipoint_rpeak",
        "expected_bpm": float(args.expected_bpm),
        "reference_lead": "lead_ii",
        "reference_lead_label": "Lead II",
        "created_at": created_at,
        "created_at_utc": created_at,
        "sample_rate_hz": sample_rate_hz,
        "sample_count": int(combined_samples.shape[0]),
        "duration_sec": duration_sec,
        "calibration_points": rows,
        "validation_results": rows,
        "fit": {
            "slope_mV_per_count": slope,
            "intercept_mV": intercept,
            "r_squared": r_squared,
        },
        "scale_mV_per_count": slope,
        "offset_count": offsets,
        "offset_method": "median Lead I and Lead II across all points; Lead III offset = Lead II - Lead I",
        "scale_method": "linear fit of expected_mV versus median beat-by-beat Lead II baseline-to-R amplitude",
        "saturation_count": count_saturation(combined_samples),
        "lead_statistics": lead_summary(combined_samples),
    }

    return CalibrationResult(
        config=config,
        warnings=warnings,
        multipoint_rows=rows,
    )


def build_calibration(args: argparse.Namespace, input_dir: Path) -> CalibrationResult:
    validate_expected_values(args)
    if args.method == "multipoint_rpeak":
        return build_multipoint_calibration(args)
    if args.method == "rpeak_baseline":
        return build_rpeak_baseline_calibration(args, input_dir)
    return build_legacy_robust_calibration(args, input_dir)


def save_calibration_config(config: dict[str, Any], output_path: Path) -> Path:
    output_path = resolve_output_path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return output_path


def print_single_point_summary(result: CalibrationResult) -> None:
    config = result.config
    metadata = config["metadata"]
    offsets = config["offset_count"]
    details = result.single_point
    assert details is not None

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
    if config.get("expected_bpm") is not None:
        print(f"expected_bpm: {float(config['expected_bpm']):.6g}")

    if details.rpeak_result is not None:
        rpeak = details.rpeak_result
        print(f"beats_detected: {rpeak.beats_detected}")
        print(f"beats_accepted: {rpeak.beats_accepted}")
        print(f"beats_rejected: {rpeak.beats_rejected}")
        print(f"estimated_bpm: {rpeak.estimated_bpm:.6g}")
        print(f"median_amplitude_count: {rpeak.median_amplitude:.6g}")
        print(f"amplitude_count_mad: {rpeak.amplitude_mad:.6g}")
    else:
        print(
            "raw_reference_amplitude_count: "
            f"{details.raw_reference_amplitude_count:.6g} "
            f"(p{metadata['lower_percentile']:.3g}={details.lower_count_after_median_removal:.6g}, "
            f"p{metadata['upper_percentile']:.3g}={details.upper_count_after_median_removal:.6g})"
        )

    print(f"scale_mV_per_count: {config['scale_mV_per_count']:.12g}")
    print("scale_applied_to_all_leads: true")


def print_multipoint_summary(result: CalibrationResult) -> None:
    metadata = result.config["metadata"]
    fit = metadata["fit"]
    print(f"expected_bpm: {metadata['expected_bpm']:.6g}")
    print("multipoint_fit:")
    print(f"  slope_mV_per_count: {fit['slope_mV_per_count']:.12g}")
    print(f"  intercept_mV: {fit['intercept_mV']:.12g}")
    print(f"  r_squared: {fit['r_squared']:.9g}")
    print("calibration_points:")
    for row in result.multipoint_rows or []:
        print(
            "  "
            f"{row['input_dir']} expected={row['expected_mV']:.6g}mV "
            f"amplitude={row['median_amplitude_count']:.6g}count "
            f"predicted={row['predicted_mV']:.6g}mV "
            f"error={row['error_mV']:.6g}mV ({row['error_percent']:.3g}%) "
            f"beats={row['beats_accepted']}/{row['beats_detected']}"
        )
    print(f"scale_mV_per_count: {result.config['scale_mV_per_count']:.12g}")
    print("scale_applied_to_all_leads: true")


def print_summary(result: CalibrationResult, output_path: Path) -> None:
    print("OK: ProSim calibration complete")
    print(f"method: {result.config['method']}")
    if result.config["method"] == "multipoint_rpeak":
        print_multipoint_summary(result)
    else:
        print_single_point_summary(result)
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

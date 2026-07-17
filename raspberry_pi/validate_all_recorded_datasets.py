"""Replay and validate all recorded ProSim datasets for Phase 3H."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import yaml

from calibration_utils import (
    CH3_CONSISTENCY_TOLERANCE_MV,
    EXPECTED_SHAPE,
    CalibrationError,
    ch3_max_error,
    count_nan_inf,
    count_saturation,
    load_json,
    measure_rpeak_baseline_amplitude,
)
from model_input_utils import DEFAULT_MAX_ABS_MV
from replay_dataset import ReplaySummary, find_raw_frame_sets, replay_dataset


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_ROOT = SCRIPT_DIR.parent / "data_kalibrasi"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "calibration_config.yaml"
DEFAULT_REPLAY_ROOT = SCRIPT_DIR / "data" / "replay"
DEFAULT_REPORT_DIR = SCRIPT_DIR / "data" / "validation_reports"

EXPECTED_DATASETS = {
    "60BPM_0p5mV": {"expected_bpm": 60.0, "expected_amplitude_mV": 0.5},
    "60BPM_1mV": {"expected_bpm": 60.0, "expected_amplitude_mV": 1.0},
    "60BPM_2mV": {"expected_bpm": 60.0, "expected_amplitude_mV": 2.0},
    "100BPM_1mV": {"expected_bpm": 100.0, "expected_amplitude_mV": 1.0},
    "120BPM_1mV": {"expected_bpm": 120.0, "expected_amplitude_mV": 1.0},
}

REPORT_FIELDS = [
    "dataset",
    "expected_bpm",
    "expected_amplitude_mV",
    "total_raw_frames",
    "frames_converted",
    "model_ready_frames",
    "failed_frames",
    "measured_median_lead_ii_amplitude_mV",
    "amplitude_error_mV",
    "amplitude_error_percent",
    "estimated_bpm",
    "bpm_error",
    "nan_inf_count",
    "saturation_count",
    "maximum_absolute_mV",
    "lead_iii_max_consistency_error_mV",
    "status",
    "reasons",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay and validate all Phase 3 recorded ProSim datasets"
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=DEFAULT_INPUT_ROOT,
        help="Directory containing recorded dataset subdirectories",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to calibration_config.yaml",
    )
    parser.add_argument(
        "--replay-output-root",
        type=Path,
        default=DEFAULT_REPLAY_ROOT,
        help="Directory where per-dataset replay outputs are written",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help="Directory where JSON/CSV/Markdown validation reports are written",
    )
    parser.add_argument(
        "--max-abs-mv",
        type=float,
        default=DEFAULT_MAX_ABS_MV,
        help="Maximum sane absolute model-input amplitude in mV",
    )
    return parser.parse_args()


def finite_or_nan(values: np.ndarray) -> np.ndarray:
    return values[np.isfinite(values)]


def raw_dataset_metrics(input_dir: Path) -> dict[str, Any]:
    frames = find_raw_frame_sets(input_dir)
    nan_inf_count = 0
    saturation_count = 0
    raw_shape_failures: list[str] = []
    dtype_conversion_failures: list[str] = []

    for frame in frames:
        try:
            samples = np.load(frame.npy_path, allow_pickle=False)
        except (OSError, ValueError) as error:
            raw_shape_failures.append(f"{frame.npy_path.name}: {error}")
            continue

        nan_inf_count += count_nan_inf(samples)
        saturation_count += count_saturation(samples)
        if samples.shape != EXPECTED_SHAPE:
            raw_shape_failures.append(
                f"{frame.npy_path.name}: shape {samples.shape} != {EXPECTED_SHAPE}"
            )
        try:
            np.asarray(samples, dtype=np.float32)
        except (TypeError, ValueError) as error:
            dtype_conversion_failures.append(f"{frame.npy_path.name}: {error}")

    return {
        "nan_inf_count": int(nan_inf_count),
        "saturation_count": int(saturation_count),
        "raw_shape_failures": raw_shape_failures,
        "dtype_conversion_failures": dtype_conversion_failures,
    }


def load_converted_samples(summary: ReplaySummary) -> tuple[np.ndarray | None, int]:
    arrays: list[np.ndarray] = []
    sample_rate_hz = 250

    for result in summary.frame_results:
        if result.calibrated_npy_path is None:
            continue

        samples = np.load(result.calibrated_npy_path, allow_pickle=False)
        arrays.append(np.asarray(samples, dtype=np.float32))

        if result.calibrated_json_path is not None:
            metadata = load_json(Path(result.calibrated_json_path))
            frame_sample_rate_hz = int(metadata.get("sample_rate_hz", sample_rate_hz))
            if frame_sample_rate_hz > 0:
                sample_rate_hz = frame_sample_rate_hz

    if not arrays:
        return None, sample_rate_hz
    return np.concatenate(arrays, axis=0), sample_rate_hz


def measured_signal_metrics(
    samples_mv: np.ndarray | None,
    sample_rate_hz: int,
    expected_bpm: float,
) -> dict[str, float]:
    if samples_mv is None:
        return {
            "measured_median_lead_ii_amplitude_mV": float("nan"),
            "estimated_bpm": float("nan"),
            "maximum_absolute_mV": float("nan"),
            "lead_iii_max_consistency_error_mV": float("nan"),
        }

    finite = finite_or_nan(samples_mv)
    max_abs_mV = float(np.max(np.abs(finite))) if finite.size else float("nan")
    lead_iii_error = ch3_max_error(samples_mv)

    try:
        rpeak = measure_rpeak_baseline_amplitude(
            samples=samples_mv,
            sample_rate_hz=sample_rate_hz,
            expected_bpm=expected_bpm,
            lead="lead_ii",
        )
        amplitude_mV = rpeak.median_amplitude
        estimated_bpm = rpeak.estimated_bpm
    except CalibrationError:
        amplitude_mV = float("nan")
        estimated_bpm = float("nan")

    return {
        "measured_median_lead_ii_amplitude_mV": float(amplitude_mV),
        "estimated_bpm": float(estimated_bpm),
        "maximum_absolute_mV": max_abs_mV,
        "lead_iii_max_consistency_error_mV": lead_iii_error,
    }


def is_finite_number(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def classify_dataset(
    row: dict[str, Any],
    max_abs_mV: float,
    raw_shape_failures: list[str],
    dtype_conversion_failures: list[str],
    frame_statuses: list[str],
) -> tuple[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []

    if row["total_raw_frames"] <= 0:
        failures.append("no raw frames found")
    if row["frames_converted"] != row["total_raw_frames"]:
        failures.append("not all raw frames were converted")
    if row["model_ready_frames"] != row["total_raw_frames"]:
        failures.append("not all raw frames reached model-ready export")
    if row["failed_frames"]:
        failures.append(f"{row['failed_frames']} frame(s) failed replay")
    if raw_shape_failures:
        failures.extend(raw_shape_failures)
    if dtype_conversion_failures:
        failures.extend(dtype_conversion_failures)
    if row["nan_inf_count"]:
        failures.append(f"{row['nan_inf_count']} NaN/Inf values found")
    if row["saturation_count"]:
        failures.append(f"{row['saturation_count']} saturated raw samples found")

    lead_error = row["lead_iii_max_consistency_error_mV"]
    if (not is_finite_number(lead_error)) or lead_error > CH3_CONSISTENCY_TOLERANCE_MV:
        failures.append(
            "Lead III consistency error exceeds "
            f"{CH3_CONSISTENCY_TOLERANCE_MV:.6g} mV"
        )

    bpm_error = row["bpm_error"]
    if (not is_finite_number(bpm_error)) or bpm_error > 2.0:
        failures.append("BPM error exceeds 2 BPM")
    elif bpm_error > 1.0:
        warnings.append("BPM error is above 1 BPM")

    amplitude_error_percent = row["amplitude_error_percent"]
    if (not is_finite_number(amplitude_error_percent)) or amplitude_error_percent > 5.0:
        failures.append("amplitude error exceeds 5%")
    elif amplitude_error_percent > 2.5:
        warnings.append("amplitude error is above 2.5%")

    maximum_abs_mV = row["maximum_absolute_mV"]
    if (not is_finite_number(maximum_abs_mV)) or maximum_abs_mV > max_abs_mV:
        failures.append(f"maximum absolute mV exceeds {max_abs_mV:g} mV")
    elif maximum_abs_mV > 0.8 * max_abs_mV:
        warnings.append("maximum absolute mV is above 80% of sanity limit")

    if any(status == "WARNING" for status in frame_statuses):
        warnings.append("one or more frames reported signal quality WARNING")

    if failures:
        return "FAIL", failures
    if warnings:
        return "WARNING", warnings
    return "PASS", []


def validate_dataset(
    name: str,
    expected: dict[str, float],
    input_root: Path,
    config_path: Path,
    replay_root: Path,
    max_abs_mV: float,
) -> dict[str, Any]:
    input_dir = input_root / name
    summary = replay_dataset(
        input_dir=input_dir,
        config_path=config_path,
        output_dir=replay_root / name,
        realtime=False,
        max_abs_mV=max_abs_mV,
    )

    raw_metrics = raw_dataset_metrics(input_dir)
    samples_mv, sample_rate_hz = load_converted_samples(summary)
    signal_metrics = measured_signal_metrics(
        samples_mv=samples_mv,
        sample_rate_hz=sample_rate_hz,
        expected_bpm=float(expected["expected_bpm"]),
    )

    measured_amplitude = signal_metrics["measured_median_lead_ii_amplitude_mV"]
    expected_amplitude = float(expected["expected_amplitude_mV"])
    amplitude_error_mV = (
        abs(measured_amplitude - expected_amplitude)
        if is_finite_number(measured_amplitude)
        else float("nan")
    )
    amplitude_error_percent = (
        100.0 * amplitude_error_mV / expected_amplitude
        if expected_amplitude > 0.0 and is_finite_number(amplitude_error_mV)
        else float("nan")
    )

    estimated_bpm = signal_metrics["estimated_bpm"]
    expected_bpm = float(expected["expected_bpm"])
    bpm_error = (
        abs(estimated_bpm - expected_bpm)
        if is_finite_number(estimated_bpm)
        else float("nan")
    )

    row: dict[str, Any] = {
        "dataset": name,
        "expected_bpm": expected_bpm,
        "expected_amplitude_mV": expected_amplitude,
        "total_raw_frames": summary.total_raw_frames,
        "frames_converted": summary.frames_converted,
        "model_ready_frames": summary.model_ready_frames,
        "failed_frames": summary.failed_frames,
        "measured_median_lead_ii_amplitude_mV": measured_amplitude,
        "amplitude_error_mV": amplitude_error_mV,
        "amplitude_error_percent": amplitude_error_percent,
        "estimated_bpm": estimated_bpm,
        "bpm_error": bpm_error,
        "nan_inf_count": raw_metrics["nan_inf_count"],
        "saturation_count": raw_metrics["saturation_count"],
        "maximum_absolute_mV": signal_metrics["maximum_absolute_mV"],
        "lead_iii_max_consistency_error_mV": signal_metrics[
            "lead_iii_max_consistency_error_mV"
        ],
    }
    status, reasons = classify_dataset(
        row=row,
        max_abs_mV=max_abs_mV,
        raw_shape_failures=raw_metrics["raw_shape_failures"],
        dtype_conversion_failures=raw_metrics["dtype_conversion_failures"],
        frame_statuses=[result.status for result in summary.frame_results],
    )
    row["status"] = status
    row["reasons"] = reasons
    return row


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def csv_value(value: Any) -> Any:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    if isinstance(value, float) and not np.isfinite(value):
        return ""
    return value


def format_number(value: Any, digits: int = 6) -> str:
    if not is_finite_number(value):
        return "n/a"
    return f"{float(value):.{digits}g}"


def write_json_report(
    report_dir: Path,
    rows: list[dict[str, Any]],
    config_path: Path,
    input_root: Path,
    replay_root: Path,
    max_abs_mV: float,
) -> Path:
    report = {
        "phase": "3H",
        "created_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "calibration_config": str(config_path),
        "input_root": str(input_root),
        "replay_output_root": str(replay_root),
        "acceptance_criteria": {
            "shape": list(EXPECTED_SHAPE),
            "dtype": "convertible to float32",
            "nan_inf_count": 0,
            "saturation_count": 0,
            "lead_iii_max_consistency_error_mV": CH3_CONSISTENCY_TOLERANCE_MV,
            "bpm_error_max": 2.0,
            "amplitude_error_percent_max": 5.0,
            "maximum_absolute_mV": max_abs_mV,
        },
        "datasets": rows,
    }
    path = report_dir / "phase3h_validation_report.json"
    path.write_text(
        json.dumps(json_safe(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def write_csv_report(report_dir: Path, rows: list[dict[str, Any]]) -> Path:
    path = report_dir / "phase3h_validation_report.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field, "")) for field in REPORT_FIELDS})
    return path


def write_markdown_report(
    report_dir: Path,
    rows: list[dict[str, Any]],
    config_path: Path,
) -> Path:
    path = report_dir / "phase3h_validation_report.md"
    lines = [
        "# Phase 3H Validation Report",
        "",
        f"- Calibration config: `{config_path}`",
        "- Canonical frame: `(2500, 3)` float32, 250 Hz, 10 seconds, mV",
        "- Acceptance: no NaN/Inf, no saturation, Lead III error <= 1e-5 mV, "
        "BPM error <= 2, amplitude error <= 5%, max abs <= 10 mV",
        "",
        "| Dataset | Raw | Converted | Model-ready | Failed | Amp mV | Amp Err % | BPM | BPM Err | Max abs mV | Lead III err mV | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {dataset} | {raw} | {converted} | {model_ready} | {failed} | "
            "{amp} | {amp_err} | {bpm} | {bpm_err} | {max_abs} | {lead_err} | {status} |".format(
                dataset=row["dataset"],
                raw=row["total_raw_frames"],
                converted=row["frames_converted"],
                model_ready=row["model_ready_frames"],
                failed=row["failed_frames"],
                amp=format_number(row["measured_median_lead_ii_amplitude_mV"]),
                amp_err=format_number(row["amplitude_error_percent"]),
                bpm=format_number(row["estimated_bpm"]),
                bpm_err=format_number(row["bpm_error"]),
                max_abs=format_number(row["maximum_absolute_mV"]),
                lead_err=format_number(row["lead_iii_max_consistency_error_mV"]),
                status=row["status"],
            )
        )

    lines.append("")
    lines.append("## Reasons")
    lines.append("")
    for row in rows:
        reasons = row.get("reasons") or []
        if reasons:
            lines.append(f"- {row['dataset']}: {'; '.join(str(reason) for reason in reasons)}")
        else:
            lines.append(f"- {row['dataset']}: PASS")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_reports(
    report_dir: Path,
    rows: list[dict[str, Any]],
    config_path: Path,
    input_root: Path,
    replay_root: Path,
    max_abs_mV: float,
) -> tuple[Path, Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = write_json_report(
        report_dir=report_dir,
        rows=rows,
        config_path=config_path,
        input_root=input_root,
        replay_root=replay_root,
        max_abs_mV=max_abs_mV,
    )
    csv_path = write_csv_report(report_dir, rows)
    md_path = write_markdown_report(report_dir, rows, config_path)
    return json_path, csv_path, md_path


def main() -> int:
    args = parse_args()
    input_root = args.input_root.resolve()
    config_path = args.config.resolve()
    replay_root = args.replay_output_root.resolve()
    report_dir = args.report_dir.resolve()

    try:
        rows = [
            validate_dataset(
                name=name,
                expected=expected,
                input_root=input_root,
                config_path=config_path,
                replay_root=replay_root,
                max_abs_mV=float(args.max_abs_mv),
            )
            for name, expected in EXPECTED_DATASETS.items()
        ]
        json_path, csv_path, md_path = write_reports(
            report_dir=report_dir,
            rows=rows,
            config_path=config_path,
            input_root=input_root,
            replay_root=replay_root,
            max_abs_mV=float(args.max_abs_mv),
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
        CalibrationError,
    ) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    for row in rows:
        print(
            f"{row['dataset']}: {row['status']} "
            f"frames={row['model_ready_frames']}/{row['total_raw_frames']} "
            f"amp_error={format_number(row['amplitude_error_percent'])}% "
            f"bpm_error={format_number(row['bpm_error'])}"
        )
    print(f"json_report: {json_path}")
    print(f"csv_report: {csv_path}")
    print(f"markdown_report: {md_path}")
    return 0 if all(row["status"] in {"PASS", "WARNING"} for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())

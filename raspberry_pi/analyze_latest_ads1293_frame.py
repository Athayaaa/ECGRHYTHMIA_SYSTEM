"""Analyze the latest complete ADS1293 ECG frame export."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
import numpy as np
import yaml


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
EXPECTED_CSV_HEADER = ["time", "ch1", "ch2", "ch3"]
EXPECTED_SHAPE = (2500, 3)
EXPECTED_DTYPE = np.dtype("float32")
EXPECTED_CHANNEL_ORDER = ["Lead I", "Lead II", "Lead III"]
CHANNEL_LABELS = ["Lead I", "Lead II", "Lead III"]
CHANNEL_KEYS = ["ch1", "ch2", "ch3"]

ADC_NEGATIVE_LIMIT = -8388608.0
ADC_POSITIVE_LIMIT = 8388607.0
ADC_FULL_SCALE_RANGE = ADC_POSITIVE_LIMIT - ADC_NEGATIVE_LIMIT
DEFAULT_LEAD_III_ATOL = 1e-3
DEFAULT_SATURATION_MARGIN = 8192.0
DEFAULT_MIN_STD = 1.0
DEFAULT_MAX_PTP = 0.80 * ADC_FULL_SCALE_RANGE
DEFAULT_JUMP_THRESHOLD = 0.05 * ADC_FULL_SCALE_RANGE
FRAME_STEM_RE = re.compile(r"^frame_(\d{6})$")


class AnalysisError(Exception):
    pass


@dataclass(frozen=True)
class FrameSet:
    stem: str
    csv_path: Path
    npy_path: Path
    json_path: Path
    bin_path: Path
    frame_index: int
    sort_time: float


@dataclass(frozen=True)
class ChannelMetrics:
    key: str
    label: str
    minimum: float
    maximum: float
    mean: float
    std: float
    peak_to_peak: float
    nan_inf_count: int
    saturation_count: int
    sudden_jump_count: int
    max_abs_jump: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze the latest complete ADS1293 ECG frame export"
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
        help="Directory containing frame_*.csv/.bin/.npy/.json files",
    )
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        help="Directory for PNG analysis plots; defaults to DATA_DIR/analysis",
    )
    parser.add_argument(
        "--lead-iii-atol",
        type=float,
        default=DEFAULT_LEAD_III_ATOL,
        help="Absolute tolerance for ch3 == ch2 - ch1",
    )
    parser.add_argument(
        "--saturation-margin",
        type=float,
        default=DEFAULT_SATURATION_MARGIN,
        help="Raw-count margin from signed 24-bit ADC limits",
    )
    parser.add_argument(
        "--min-std",
        type=float,
        default=DEFAULT_MIN_STD,
        help="Warn when a channel standard deviation is below this raw count",
    )
    parser.add_argument(
        "--max-ptp",
        type=float,
        default=DEFAULT_MAX_PTP,
        help="Warn when channel peak-to-peak exceeds this raw count",
    )
    parser.add_argument(
        "--jump-threshold",
        type=float,
        default=DEFAULT_JUMP_THRESHOLD,
        help="Warn when consecutive samples jump by more than this raw count",
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


def resolve_analysis_dir(data_dir: Path, analysis_dir: Path | None) -> Path:
    if analysis_dir is not None:
        return analysis_dir.resolve()
    return data_dir / "analysis"


def find_latest_complete_frame_set(data_dir: Path) -> FrameSet:
    candidates: list[FrameSet] = []

    for npy_path in data_dir.glob("frame_*.npy"):
        match = FRAME_STEM_RE.match(npy_path.stem)
        if match is None:
            continue

        stem = npy_path.stem
        csv_path = data_dir / f"{stem}.csv"
        json_path = data_dir / f"{stem}.json"
        bin_path = data_dir / f"{stem}.bin"
        if not all(path.exists() for path in (csv_path, json_path, bin_path)):
            continue

        sort_time = frame_sort_time(json_path)
        candidates.append(
            FrameSet(
                stem=stem,
                csv_path=csv_path,
                npy_path=npy_path,
                json_path=json_path,
                bin_path=bin_path,
                frame_index=int(match.group(1)),
                sort_time=sort_time,
            )
        )

    if not candidates:
        raise AnalysisError(f"no complete frame_*.csv/.bin/.npy/.json set in {data_dir}")

    return max(candidates, key=lambda frame: (frame.sort_time, frame.frame_index))


def frame_sort_time(json_path: Path) -> float:
    try:
        metadata = read_json(json_path)
        created_at = metadata.get("created_at_utc")
        if isinstance(created_at, str):
            normalized = created_at.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return json_path.stat().st_mtime


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise AnalysisError(f"{path.name} does not contain a JSON object")
    return data


def read_csv_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            return next(reader)
        except StopIteration as error:
            raise AnalysisError(f"{path.name} is empty") from error


def load_samples(npy_path: Path) -> np.ndarray:
    samples = np.load(npy_path, allow_pickle=False)
    if samples.shape != EXPECTED_SHAPE:
        raise AnalysisError(
            f"{npy_path.name} shape is {samples.shape}, expected {EXPECTED_SHAPE}"
        )
    if samples.dtype != EXPECTED_DTYPE:
        raise AnalysisError(
            f"{npy_path.name} dtype is {samples.dtype}, expected {EXPECTED_DTYPE}"
        )
    return samples


def validate_channel_order(csv_header: list[str], metadata: dict[str, Any]) -> None:
    if csv_header != EXPECTED_CSV_HEADER:
        raise AnalysisError(
            f"CSV header is {csv_header}, expected {EXPECTED_CSV_HEADER}"
        )

    channel_order = metadata.get("channel_order")
    if channel_order != EXPECTED_CHANNEL_ORDER:
        raise AnalysisError(
            f"metadata channel_order is {channel_order}, "
            f"expected {EXPECTED_CHANNEL_ORDER}"
        )


def compute_metrics(
    samples: np.ndarray,
    saturation_margin: float,
    jump_threshold: float,
) -> list[ChannelMetrics]:
    metrics: list[ChannelMetrics] = []
    for channel_index, (key, label) in enumerate(zip(CHANNEL_KEYS, CHANNEL_LABELS)):
        channel = samples[:, channel_index]
        finite_mask = np.isfinite(channel)
        finite = channel[finite_mask]
        nan_inf_count = int(channel.size - finite.size)

        if finite.size == 0:
            minimum = maximum = mean = std = peak_to_peak = float("nan")
            saturation_count = 0
        else:
            minimum = float(np.min(finite))
            maximum = float(np.max(finite))
            mean = float(np.mean(finite))
            std = float(np.std(finite))
            peak_to_peak = float(np.ptp(finite))
            saturation_count = int(
                np.count_nonzero(
                    (np.abs(finite - ADC_NEGATIVE_LIMIT) <= saturation_margin)
                    | (np.abs(finite - ADC_POSITIVE_LIMIT) <= saturation_margin)
                )
            )

        jumps = np.abs(np.diff(channel))
        finite_jumps = jumps[np.isfinite(jumps)]
        if finite_jumps.size == 0:
            sudden_jump_count = 0
            max_abs_jump = float("nan")
        else:
            sudden_jump_count = int(np.count_nonzero(finite_jumps > jump_threshold))
            max_abs_jump = float(np.max(finite_jumps))

        metrics.append(
            ChannelMetrics(
                key=key,
                label=label,
                minimum=minimum,
                maximum=maximum,
                mean=mean,
                std=std,
                peak_to_peak=peak_to_peak,
                nan_inf_count=nan_inf_count,
                saturation_count=saturation_count,
                sudden_jump_count=sudden_jump_count,
                max_abs_jump=max_abs_jump,
            )
        )

    return metrics


def check_pipeline_result(
    metrics: list[ChannelMetrics],
    samples: np.ndarray,
    lead_iii_atol: float,
) -> list[str]:
    issues: list[str] = []

    for metric in metrics:
        if metric.nan_inf_count > 0:
            issues.append(
                f"{metric.key} ({metric.label}): contains "
                f"{metric.nan_inf_count} NaN/Inf values"
            )

    lead_iii_error = np.abs(samples[:, 2] - (samples[:, 1] - samples[:, 0]))
    finite_error = lead_iii_error[np.isfinite(lead_iii_error)]
    if finite_error.size == 0:
        issues.append("ch3 consistency check failed: no finite error values")
    else:
        max_error = float(np.max(finite_error))
        bad_count = int(np.count_nonzero(finite_error > lead_iii_atol))
        if bad_count > 0:
            issues.append(
                "ch3 consistency failed: "
                f"{bad_count} samples exceed tolerance {lead_iii_atol:g}; "
                f"max error {max_error:.6g}"
            )

    return issues


def check_signal_quality(
    metrics: list[ChannelMetrics],
    min_std: float,
    max_ptp: float,
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    failures: list[str] = []

    for metric in metrics:
        prefix = f"{metric.key} ({metric.label})"
        if metric.saturation_count > 0:
            warnings.append(
                f"{prefix}: {metric.saturation_count} samples near signed 24-bit limits"
            )
        if np.isfinite(metric.std) and metric.std < min_std:
            warnings.append(f"{prefix}: standard deviation too small ({metric.std:.3f})")
        if np.isfinite(metric.peak_to_peak) and metric.peak_to_peak > max_ptp:
            warnings.append(
                f"{prefix}: peak-to-peak extremely large ({metric.peak_to_peak:.3f})"
            )
        if metric.sudden_jump_count > 0:
            warnings.append(
                f"{prefix}: {metric.sudden_jump_count} sudden jumps detected; "
                f"max jump {metric.max_abs_jump:.3f}"
            )

    return warnings, failures


def print_metrics(metrics: list[ChannelMetrics]) -> None:
    print("Per-channel metrics:")
    for metric in metrics:
        print(f"  {metric.key} ({metric.label})")
        print(f"    min={metric.minimum:.6g}")
        print(f"    max={metric.maximum:.6g}")
        print(f"    mean={metric.mean:.6g}")
        print(f"    std={metric.std:.6g}")
        print(f"    peak_to_peak={metric.peak_to_peak:.6g}")
        print(f"    nan_inf_count={metric.nan_inf_count}")
        print(f"    saturation_count={metric.saturation_count}")
        print(f"    sudden_jump_count={metric.sudden_jump_count}")
        print(f"    max_abs_jump={metric.max_abs_jump:.6g}")


def save_plots(
    samples: np.ndarray,
    sample_rate_hz: int,
    analysis_dir: Path,
) -> dict[str, Path]:
    analysis_dir.mkdir(parents=True, exist_ok=True)
    time_axis = np.arange(samples.shape[0], dtype=np.float32) / float(sample_rate_hz)

    paths = {
        "lead_i": analysis_dir / "latest_lead_i.png",
        "lead_ii": analysis_dir / "latest_lead_ii.png",
        "lead_iii": analysis_dir / "latest_lead_iii.png",
        "all_leads": analysis_dir / "latest_all_leads.png",
    }

    for index, key in enumerate(("lead_i", "lead_ii", "lead_iii")):
        plot_single_lead(
            time_axis=time_axis,
            values=samples[:, index],
            label=CHANNEL_LABELS[index],
            output_path=paths[key],
        )

    plot_all_leads(time_axis, samples, paths["all_leads"])
    return paths


def plot_single_lead(
    time_axis: np.ndarray,
    values: np.ndarray,
    label: str,
    output_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(time_axis, values, linewidth=0.9)
    ax.set_title(f"{label} latest ADS1293 frame")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Raw ADS1293 count")
    ax.grid(True, linewidth=0.4, alpha=0.35)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_all_leads(
    time_axis: np.ndarray,
    samples: np.ndarray,
    output_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 5))
    for index, label in enumerate(CHANNEL_LABELS):
        ax.plot(time_axis, samples[:, index], linewidth=0.8, label=label)
    ax.set_title("All leads latest ADS1293 frame")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Raw ADS1293 count")
    ax.grid(True, linewidth=0.4, alpha=0.35)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    data_dir = resolve_data_dir(config_path, args.data_dir)
    analysis_dir = resolve_analysis_dir(data_dir, args.analysis_dir)

    try:
        frame_set = find_latest_complete_frame_set(data_dir)
        metadata = read_json(frame_set.json_path)
        csv_header = read_csv_header(frame_set.csv_path)
        validate_channel_order(csv_header, metadata)
        samples = load_samples(frame_set.npy_path)
        sample_rate_hz = int(metadata.get("sample_rate_hz", 250))
        metrics = compute_metrics(
            samples=samples,
            saturation_margin=args.saturation_margin,
            jump_threshold=args.jump_threshold,
        )
        pipeline_issues = check_pipeline_result(
            metrics=metrics,
            samples=samples,
            lead_iii_atol=args.lead_iii_atol,
        )
        signal_warnings, signal_failures = check_signal_quality(
            metrics=metrics,
            min_std=args.min_std,
            max_ptp=args.max_ptp,
        )
        plot_paths = save_plots(samples, sample_rate_hz, analysis_dir)
    except (OSError, AnalysisError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print(f"Frame: {frame_set.stem}")
    print(f"CSV: {frame_set.csv_path}")
    print(f"BIN: {frame_set.bin_path}")
    print(f"NPY: {frame_set.npy_path}")
    print(f"JSON: {frame_set.json_path}")
    print(f"shape={samples.shape} dtype={samples.dtype}")
    print("channel_order=ch1, ch2, ch3")
    print(f"metadata_channel_order={metadata.get('channel_order')}")
    print_metrics(metrics)

    print("Plots:")
    for path in plot_paths.values():
        print(f"  {path}")

    if pipeline_issues:
        print("Pipeline issues:")
        for issue in pipeline_issues:
            print(f"  FAIL: {issue}")

    if signal_failures or signal_warnings:
        print("Signal quality issues:")
        for failure in signal_failures:
            print(f"  FAIL: {failure}")
        for warning in signal_warnings:
            print(f"  WARNING: {warning}")

    pipeline_result = "PASS" if not pipeline_issues else "FAIL"
    if signal_failures:
        signal_quality_result = "FAIL"
    elif signal_warnings:
        signal_quality_result = "WARNING"
    else:
        signal_quality_result = "PASS"

    print(f"PIPELINE RESULT: {pipeline_result}")
    print(f"SIGNAL QUALITY RESULT: {signal_quality_result}")
    if signal_quality_result != "PASS":
        print(
            "NOTE: This is expected for floating/unconnected ADS1293 inputs. "
            "Use ProSim or ECG simulator for full Phase 3D validation."
        )

    return 0 if pipeline_result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

"""Automatic ProSim raw-count to mV calibration for ADS1293 frame exports."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
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


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.yaml"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "calibration_config.yaml"
EXPECTED_SHAPE = (2500, 3)
EXPECTED_DTYPE = np.dtype("float32")
CHANNELS = [
    ("lead_i", "Lead I", "ch1"),
    ("lead_ii", "Lead II", "ch2"),
    ("lead_iii", "Lead III", "ch3"),
]
LEAD_TO_INDEX = {key: index for index, (key, _, _) in enumerate(CHANNELS)}
FRAME_STEM_RE = re.compile(r"^frame_(\d{6})$")

ADC_NEGATIVE_LIMIT = -8388608.0
ADC_POSITIVE_LIMIT = 8388607.0
SATURATION_MARGIN_COUNTS = 8192.0
SUDDEN_JUMP_THRESHOLD_COUNTS = 0.05 * (ADC_POSITIVE_LIMIT - ADC_NEGATIVE_LIMIT)
CH3_CONSISTENCY_TOLERANCE_COUNTS = 1.0
DEFAULT_LOWER_PERCENTILE = 1.0
DEFAULT_UPPER_PERCENTILE = 99.0
DEFAULT_MIN_AMPLITUDE_COUNT = 100.0
DEFAULT_WARN_AMPLITUDE_COUNT = 1000.0
DEFAULT_MAX_SATURATION_COUNT = 0


class CalibrationError(Exception):
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
class CalibrationResult:
    config: dict[str, Any]
    raw_samples: np.ndarray
    converted_mv: np.ndarray
    warnings: list[str]
    selected_lead_raw_amplitude_count: float
    lower_count: float
    upper_count: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto-calibrate ADS1293 raw counts to mV from ProSim frames"
    )
    parser.add_argument("--expected-mv", type=float, default=1.0)
    parser.add_argument(
        "--lead",
        choices=sorted(LEAD_TO_INDEX),
        default="lead_ii",
        help="Lead used as the calibration amplitude reference",
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
        help="Path to receiver config.yaml",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Directory containing frame_*.csv/.bin/.npy/.json files",
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
    parser.add_argument(
        "--max-saturation-count",
        type=int,
        default=DEFAULT_MAX_SATURATION_COUNT,
        help="Fail when saturated samples exceed this count",
    )
    parser.add_argument(
        "--calibration-source-kind",
        choices=("ecg_waveform", "calibration_pulse"),
        default="ecg_waveform",
        help="Use calibration_pulse when ProSim is set to a 1 mV pulse",
    )
    return parser.parse_args()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return {}
    return data


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise CalibrationError(f"{path.name} does not contain a JSON object")
    return data


def resolve_data_dir(config_path: Path, data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir.resolve()

    config = load_yaml(config_path)
    output_config = config.get("output", {})
    configured_dir = Path(output_config.get("directory", "data"))
    if configured_dir.is_absolute():
        return configured_dir
    return (config_path.parent / configured_dir).resolve()


def frame_sort_time(json_path: Path) -> float:
    try:
        metadata = load_json(json_path)
        created_at = metadata.get("created_at_utc")
        if isinstance(created_at, str):
            return datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return json_path.stat().st_mtime


def find_complete_frame_sets(data_dir: Path) -> list[FrameSet]:
    frames: list[FrameSet] = []
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

        frames.append(
            FrameSet(
                stem=stem,
                csv_path=csv_path,
                npy_path=npy_path,
                json_path=json_path,
                bin_path=bin_path,
                frame_index=int(match.group(1)),
                sort_time=frame_sort_time(json_path),
            )
        )

    return sorted(frames, key=lambda frame: (frame.sort_time, frame.frame_index))


def frame_sequence(frame: FrameSet) -> tuple[int, int] | None:
    try:
        metadata = load_json(frame.json_path)
        return int(metadata["sequence_start"]), int(metadata["sequence_end"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def latest_n_frames(data_dir: Path, frame_count: int) -> list[FrameSet]:
    if frame_count <= 0:
        raise CalibrationError("--frames must be greater than 0")

    frames = find_complete_frame_sets(data_dir)
    if not frames:
        raise CalibrationError(f"no complete frame_*.csv/.bin/.npy/.json set in {data_dir}")

    latest = frames[-1]
    selected = [latest]
    used_stems = {latest.stem}
    current = latest

    while len(selected) < frame_count:
        current_sequence = frame_sequence(current)
        previous: FrameSet | None = None

        if current_sequence is not None:
            target_end = current_sequence[0] - 1
            sequence_candidates = [
                frame
                for frame in frames
                if frame.stem not in used_stems
                and (sequence := frame_sequence(frame)) is not None
                and sequence[1] == target_end
            ]
            if sequence_candidates:
                previous = max(
                    sequence_candidates,
                    key=lambda frame: (frame.sort_time, frame.frame_index),
                )

        if previous is None:
            index_candidates = [
                frame
                for frame in frames
                if frame.stem not in used_stems
                and frame.frame_index == current.frame_index - 1
                and frame.sort_time <= current.sort_time
            ]
            if index_candidates:
                previous = max(
                    index_candidates,
                    key=lambda frame: (frame.sort_time, frame.frame_index),
                )

        if previous is None:
            raise CalibrationError(
                f"latest run has only {len(selected)} contiguous complete frame(s); "
                f"requested {frame_count}"
            )

        selected.append(previous)
        used_stems.add(previous.stem)
        current = previous

    return list(reversed(selected))


def load_and_validate_frame(frame: FrameSet) -> tuple[np.ndarray, dict[str, Any]]:
    samples = np.load(frame.npy_path, allow_pickle=False)
    if samples.shape != EXPECTED_SHAPE:
        raise CalibrationError(
            f"{frame.npy_path.name} shape is {samples.shape}, expected {EXPECTED_SHAPE}"
        )
    if samples.dtype != EXPECTED_DTYPE:
        raise CalibrationError(
            f"{frame.npy_path.name} dtype is {samples.dtype}, expected {EXPECTED_DTYPE}"
        )
    if not np.isfinite(samples).all():
        bad_count = int(samples.size - np.count_nonzero(np.isfinite(samples)))
        raise CalibrationError(f"{frame.npy_path.name} has {bad_count} NaN/Inf values")

    ch3_error = np.abs(samples[:, 2] - (samples[:, 1] - samples[:, 0]))
    max_error = float(np.max(ch3_error))
    if max_error > CH3_CONSISTENCY_TOLERANCE_COUNTS:
        raise CalibrationError(
            f"{frame.npy_path.name} ch3 consistency failed; max error {max_error:.6g}"
        )

    metadata = load_json(frame.json_path)
    return samples, metadata


def count_saturation(samples: np.ndarray) -> int:
    return int(
        np.count_nonzero(
            (np.abs(samples - ADC_NEGATIVE_LIMIT) <= SATURATION_MARGIN_COUNTS)
            | (np.abs(samples - ADC_POSITIVE_LIMIT) <= SATURATION_MARGIN_COUNTS)
        )
    )


def count_sudden_jumps(samples: np.ndarray) -> tuple[int, float]:
    jumps = np.abs(np.diff(samples, axis=0))
    if jumps.size == 0:
        return 0, float("nan")
    return (
        int(np.count_nonzero(jumps > SUDDEN_JUMP_THRESHOLD_COUNTS)),
        float(np.max(jumps)),
    )


def lead_summary(samples: np.ndarray) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for index, (key, _, _) in enumerate(CHANNELS):
        lead = samples[:, index]
        summary[key] = {
            "mean": float(np.mean(lead)),
            "median": float(np.median(lead)),
        }
    return summary


def compute_offsets(samples: np.ndarray) -> dict[str, float]:
    lead_i_offset = float(np.median(samples[:, LEAD_TO_INDEX["lead_i"]]))
    lead_ii_offset = float(np.median(samples[:, LEAD_TO_INDEX["lead_ii"]]))
    return {
        "lead_i": lead_i_offset,
        "lead_ii": lead_ii_offset,
        "lead_iii": lead_ii_offset - lead_i_offset,
    }


def robust_peak_to_peak(
    samples: np.ndarray,
    lead: str,
    lower_percentile: float,
    upper_percentile: float,
) -> tuple[float, float, float]:
    if not 0.0 <= lower_percentile < upper_percentile <= 100.0:
        raise CalibrationError("percentiles must satisfy 0 <= lower < upper <= 100")

    values = samples[:, LEAD_TO_INDEX[lead]].astype(np.float64)
    baseline = float(np.median(values))
    detrended = values - baseline
    lower = float(np.percentile(detrended, lower_percentile))
    upper = float(np.percentile(detrended, upper_percentile))
    return upper - lower, lower, upper


def convert_to_mv(samples: np.ndarray, offsets: dict[str, float], scale: float) -> np.ndarray:
    converted = np.empty_like(samples, dtype=np.float32)
    for index, (key, _, _) in enumerate(CHANNELS):
        converted[:, index] = (
            samples[:, index].astype(np.float32) - float(offsets[key])
        ) * float(scale)
    converted -= np.mean(converted, axis=0, keepdims=True).astype(np.float32)
    return converted


def build_calibration(
    args: argparse.Namespace,
    data_dir: Path,
    frames: list[FrameSet],
) -> CalibrationResult:
    arrays: list[np.ndarray] = []
    metadata_rows: list[dict[str, Any]] = []
    for frame in frames:
        samples, metadata = load_and_validate_frame(frame)
        arrays.append(samples)
        metadata_rows.append(metadata)

    raw_samples = np.concatenate(arrays, axis=0)
    sample_rate_hz = int(metadata_rows[-1].get("sample_rate_hz", 250))
    if sample_rate_hz <= 0:
        sample_rate_hz = 250

    saturation_count = count_saturation(raw_samples)
    if saturation_count > args.max_saturation_count:
        raise CalibrationError(
            f"saturation count {saturation_count} exceeds limit {args.max_saturation_count}"
        )

    warnings: list[str] = []
    sudden_jump_count, max_jump = count_sudden_jumps(raw_samples)
    if sudden_jump_count > 0:
        warnings.append(
            f"{sudden_jump_count} sudden jumps above {SUDDEN_JUMP_THRESHOLD_COUNTS:.0f} "
            f"counts detected; max jump {max_jump:.6g}"
        )

    if args.calibration_source_kind == "ecg_waveform":
        warnings.append(
            "Calibration is based on ECG waveform amplitude. Prefer the ProSim 1 mV "
            "calibration pulse when available."
        )

    offsets = compute_offsets(raw_samples)
    raw_amplitude_count, lower_count, upper_count = robust_peak_to_peak(
        samples=raw_samples,
        lead=args.lead,
        lower_percentile=args.lower_percentile,
        upper_percentile=args.upper_percentile,
    )
    if raw_amplitude_count < args.warn_amplitude_count:
        warnings.append(
            f"raw amplitude is small ({raw_amplitude_count:.6g} counts); "
            "check simulator amplitude, leads, and selected calibration lead"
        )
    if raw_amplitude_count < args.min_amplitude_count:
        raise CalibrationError(
            f"raw amplitude {raw_amplitude_count:.6g} counts is too small for calibration"
        )

    scale_mV_per_count = float(args.expected_mv) / float(raw_amplitude_count)
    converted_mv = convert_to_mv(raw_samples, offsets, scale_mV_per_count)
    duration_sec = raw_samples.shape[0] / float(sample_rate_hz)

    config = {
        "calibration_source": "prosim",
        "expected_mV": float(args.expected_mv),
        "selected_lead": args.lead,
        "method": args.method,
        "offset_count": offsets,
        "scale_mV_per_count": scale_mV_per_count,
        "invert": {key: False for key, _, _ in CHANNELS},
        "filter": {
            "remove_mean": True,
            "highpass_hz": 0.5,
            "lowpass_hz": 40.0,
            "notch_50hz": True,
        },
        "metadata": {
            "frames_used": [frame.stem for frame in frames],
            "sample_rate_hz": sample_rate_hz,
            "duration_sec": duration_sec,
            "data_dir": str(data_dir),
            "created_at_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "lead_statistics": lead_summary(raw_samples),
            "raw_amplitude_count": raw_amplitude_count,
            "lower_percentile": float(args.lower_percentile),
            "upper_percentile": float(args.upper_percentile),
            "lower_count_after_median_removal": lower_count,
            "upper_count_after_median_removal": upper_count,
            "saturation_count": saturation_count,
            "sudden_jump_count": sudden_jump_count,
            "max_abs_jump_count": max_jump,
            "calibration_source_kind": args.calibration_source_kind,
        },
    }

    return CalibrationResult(
        config=config,
        raw_samples=raw_samples,
        converted_mv=converted_mv,
        warnings=warnings,
        selected_lead_raw_amplitude_count=raw_amplitude_count,
        lower_count=lower_count,
        upper_count=upper_count,
    )


def write_preview_exports(
    data_dir: Path,
    converted_mv: np.ndarray,
    sample_rate_hz: int,
) -> tuple[Path, Path]:
    export_dir = data_dir / "calibrated"
    export_dir.mkdir(parents=True, exist_ok=True)
    csv_path = export_dir / "latest_prosim_calibrated_mv.csv"
    npy_path = export_dir / "latest_prosim_calibrated_mv.npy"
    time_axis = np.arange(converted_mv.shape[0], dtype=np.float32) / float(sample_rate_hz)

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("time", "ch1", "ch2", "ch3"))
        for sample_time, row in zip(time_axis, converted_mv):
            writer.writerow(
                (
                    f"{float(sample_time):.3f}",
                    f"{float(row[0]):.9g}",
                    f"{float(row[1]):.9g}",
                    f"{float(row[2]):.9g}",
                )
            )

    np.save(npy_path, np.asarray(converted_mv, dtype=np.float32))
    return csv_path, npy_path


def save_calibration_config(config: dict[str, Any], output_path: Path) -> Path:
    if not output_path.is_absolute():
        output_path = (SCRIPT_DIR / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return output_path


def plot_calibration_window(
    output_path: Path,
    samples: np.ndarray,
    sample_rate_hz: int,
    ylabel: str,
    title: str,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    time_axis = np.arange(samples.shape[0], dtype=np.float32) / float(sample_rate_hz)
    fig, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True)
    for index, (_, label, _) in enumerate(CHANNELS):
        axes[index].plot(time_axis, samples[:, index], linewidth=0.6)
        axes[index].set_title(label)
        axes[index].set_ylabel(ylabel)
        axes[index].grid(True, linewidth=0.4, alpha=0.35)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def write_plots(
    data_dir: Path,
    raw_samples: np.ndarray,
    converted_mv: np.ndarray,
    sample_rate_hz: int,
) -> tuple[Path, Path]:
    analysis_dir = data_dir / "analysis"
    raw_path = analysis_dir / "prosim_calibration_raw.png"
    mv_path = analysis_dir / "prosim_calibration_mv.png"
    plot_calibration_window(
        raw_path,
        raw_samples,
        sample_rate_hz,
        "Raw ADS1293 count",
        "ProSim Calibration Raw Window",
    )
    plot_calibration_window(
        mv_path,
        converted_mv,
        sample_rate_hz,
        "mV",
        "ProSim Calibration Converted Window",
    )
    return raw_path, mv_path


def print_summary(result: CalibrationResult, output_path: Path, preview_paths: tuple[Path, Path], plot_paths: tuple[Path, Path]) -> None:
    metadata = result.config["metadata"]
    print("OK: ProSim calibration complete")
    print(f"frames_used: {', '.join(metadata['frames_used'])}")
    print(f"sample_rate_hz: {metadata['sample_rate_hz']}")
    print(f"duration_sec: {metadata['duration_sec']:.3f}")
    print("lead statistics:")
    for key, label, _ in CHANNELS:
        stats = metadata["lead_statistics"][key]
        print(f"  {label}: mean={stats['mean']:.6g} median={stats['median']:.6g}")
    print(f"selected_lead: {result.config['selected_lead']}")
    print(f"expected_mV: {result.config['expected_mV']:.6g}")
    print(
        "raw_amplitude_count: "
        f"{result.selected_lead_raw_amplitude_count:.6g} "
        f"(lower={result.lower_count:.6g}, upper={result.upper_count:.6g})"
    )
    print(f"scale_mV_per_count: {result.config['scale_mV_per_count']:.12g}")
    print(f"calibration_config: {output_path}")
    print(f"preview_csv: {preview_paths[0]}")
    print(f"preview_npy: {preview_paths[1]}")
    print(f"raw_plot: {plot_paths[0]}")
    print(f"mv_plot: {plot_paths[1]}")
    for warning in result.warnings:
        print(f"WARNING: {warning}")


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    data_dir = resolve_data_dir(config_path, args.data_dir)

    try:
        frames = latest_n_frames(data_dir, args.frames)
        result = build_calibration(args, data_dir, frames)
        output_path = save_calibration_config(result.config, args.output)
        sample_rate_hz = int(result.config["metadata"]["sample_rate_hz"])
        preview_mv = result.converted_mv[-EXPECTED_SHAPE[0] :, :]
        preview_paths = write_preview_exports(data_dir, preview_mv, sample_rate_hz)
        plot_paths = write_plots(
            data_dir,
            result.raw_samples,
            result.converted_mv,
            sample_rate_hz,
        )
    except (OSError, ValueError, json.JSONDecodeError, CalibrationError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print_summary(result, output_path, preview_paths, plot_paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())

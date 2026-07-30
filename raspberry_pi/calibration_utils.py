"""Shared ProSim calibration and mV export helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import re
from typing import Any

import numpy as np

from ecg_filters import DEFAULT_NOTCH_QUALITY_FACTOR, NOTCH_50HZ_FREQUENCY_HZ
import yaml


EXPECTED_SHAPE = (2500, 3)
EXPECTED_DTYPE = np.dtype("float32")
CHANNEL_ORDER = ["Lead I", "Lead II", "Lead III"]
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
CH3_CONSISTENCY_TOLERANCE_COUNTS = 1.0
CH3_CONSISTENCY_TOLERANCE_MV = 1e-5
CALIBRATED_CSV_HEADER = ["time", "lead_i_mV", "lead_ii_mV", "lead_iii_mV"]


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
class CalibratedExportPaths:
    csv_path: Path
    npy_path: Path
    json_path: Path


@dataclass(frozen=True)
class RPeakBaselineResult:
    amplitudes: np.ndarray
    accepted_amplitudes: np.ndarray
    rejected_amplitudes: np.ndarray
    peak_indices: np.ndarray
    accepted_peak_indices: np.ndarray
    rejected_peak_indices: np.ndarray
    median_amplitude: float
    amplitude_mad: float
    estimated_bpm: float
    detection_threshold: float
    baseline_window_samples: tuple[int, int]
    min_peak_distance_samples: int
    polarity: int

    @property
    def beats_detected(self) -> int:
        return int(self.peak_indices.size)

    @property
    def beats_accepted(self) -> int:
        return int(self.accepted_amplitudes.size)

    @property
    def beats_rejected(self) -> int:
        return int(self.beats_detected - self.beats_accepted)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise CalibrationError(f"{path} does not contain a YAML object")
    return data


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise CalibrationError(f"{path.name} does not contain a JSON object")
    return data


def frame_sort_time(json_path: Path) -> float:
    try:
        metadata = load_json(json_path)
        created_at = metadata.get("created_at_utc")
        if isinstance(created_at, str):
            return datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
    except (OSError, ValueError, TypeError, json.JSONDecodeError, CalibrationError):
        pass
    return json_path.stat().st_mtime


def find_complete_frame_sets(input_dir: Path) -> list[FrameSet]:
    frames: list[FrameSet] = []
    for npy_path in input_dir.glob("frame_*.npy"):
        match = FRAME_STEM_RE.match(npy_path.stem)
        if match is None:
            continue

        stem = npy_path.stem
        csv_path = input_dir / f"{stem}.csv"
        json_path = input_dir / f"{stem}.json"
        bin_path = input_dir / f"{stem}.bin"
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
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, CalibrationError):
        return None


def latest_n_frames(input_dir: Path, frame_count: int) -> list[FrameSet]:
    if frame_count <= 0:
        raise CalibrationError("--frames must be greater than 0")

    frames = find_complete_frame_sets(input_dir)
    if not frames:
        raise CalibrationError(
            f"no complete frame_*.csv/.bin/.npy/.json set in {input_dir}"
        )

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


def count_nan_inf(samples: np.ndarray) -> int:
    return int(samples.size - np.count_nonzero(np.isfinite(samples)))


def ch3_max_error(samples: np.ndarray) -> float:
    if samples.ndim != 2 or samples.shape[1] != 3:
        return float("inf")
    error = np.abs(samples[:, 2] - (samples[:, 1] - samples[:, 0]))
    return float(np.max(error)) if error.size else float("inf")


def validate_raw_matrix(samples: np.ndarray, name: str) -> None:
    if samples.ndim != 2 or samples.shape[1] != 3:
        raise CalibrationError(f"{name} shape is {samples.shape}, expected Nx3")
    if samples.dtype != EXPECTED_DTYPE:
        raise CalibrationError(f"{name} dtype is {samples.dtype}, expected {EXPECTED_DTYPE}")

    bad_count = count_nan_inf(samples)
    if bad_count:
        raise CalibrationError(f"{name} has {bad_count} NaN/Inf values")

    max_error = ch3_max_error(samples)
    if max_error > CH3_CONSISTENCY_TOLERANCE_COUNTS:
        raise CalibrationError(
            f"{name} ch3 consistency failed; max error {max_error:.6g} count"
        )


def validate_raw_samples(samples: np.ndarray, name: str) -> None:
    if samples.shape != EXPECTED_SHAPE:
        raise CalibrationError(f"{name} shape is {samples.shape}, expected {EXPECTED_SHAPE}")
    validate_raw_matrix(samples, name)


def load_raw_frame(frame: FrameSet) -> tuple[np.ndarray, dict[str, Any]]:
    samples = np.load(frame.npy_path, allow_pickle=False)
    validate_raw_samples(samples, frame.npy_path.name)
    return samples, load_json(frame.json_path)


def count_saturation(samples: np.ndarray) -> int:
    return int(
        np.count_nonzero(
            (np.abs(samples - ADC_NEGATIVE_LIMIT) <= SATURATION_MARGIN_COUNTS)
            | (np.abs(samples - ADC_POSITIVE_LIMIT) <= SATURATION_MARGIN_COUNTS)
        )
    )


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
    if lead not in LEAD_TO_INDEX:
        raise CalibrationError(f"unknown lead {lead}")
    if not 0.0 <= lower_percentile < upper_percentile <= 100.0:
        raise CalibrationError("percentiles must satisfy 0 <= lower < upper <= 100")

    values = samples[:, LEAD_TO_INDEX[lead]].astype(np.float64)
    baseline = float(np.median(values))
    detrended = values - baseline
    lower = float(np.percentile(detrended, lower_percentile))
    upper = float(np.percentile(detrended, upper_percentile))
    return upper - lower, lower, upper


def moving_average(values: np.ndarray, window_samples: int) -> np.ndarray:
    window = max(3, int(window_samples) | 1)
    pad = window // 2
    padded = np.pad(values.astype(np.float64), (pad, pad), mode="edge")
    kernel = np.ones(window, dtype=np.float64) / float(window)
    return np.convolve(padded, kernel, mode="valid")


def detrend_for_peak_detection(values: np.ndarray, sample_rate_hz: int) -> np.ndarray:
    baseline_window = max(3, int(round(0.80 * sample_rate_hz)))
    smooth_window = max(3, int(round(0.04 * sample_rate_hz)))
    slow_baseline = moving_average(values, baseline_window)
    return moving_average(values - slow_baseline, smooth_window)


def detect_r_peaks(
    samples: np.ndarray,
    sample_rate_hz: int,
    expected_bpm: float,
    lead: str = "lead_ii",
) -> tuple[np.ndarray, float, int, int]:
    if sample_rate_hz <= 0:
        raise CalibrationError("sample_rate_hz must be positive")
    if not np.isfinite(expected_bpm) or expected_bpm <= 0.0:
        raise CalibrationError("expected_bpm must be a finite positive number")
    if lead not in LEAD_TO_INDEX:
        raise CalibrationError(f"unknown lead {lead}")
    if samples.ndim != 2 or samples.shape[1] != 3:
        raise CalibrationError(f"samples shape is {samples.shape}, expected Nx3")

    values = samples[:, LEAD_TO_INDEX[lead]].astype(np.float64)
    if count_nan_inf(values):
        raise CalibrationError("samples contain NaN/Inf values")

    detrended = detrend_for_peak_detection(values, sample_rate_hz)
    p1, p90, p99 = np.percentile(detrended, [1.0, 90.0, 99.0])
    polarity = -1 if abs(p1) > abs(p99) else 1
    detector_signal = detrended * polarity
    detector_p90 = float(np.percentile(detector_signal, 90.0))
    detector_p99 = float(np.percentile(detector_signal, 99.0))
    detector_max = float(np.max(detector_signal))
    threshold = max(detector_p90, 0.35 * detector_p99, 0.25 * detector_max)
    min_distance = max(
        1,
        int(round((60.0 / float(expected_bpm)) * float(sample_rate_hz) * 0.60)),
    )

    peaks: list[int] = []
    for index in range(1, len(detector_signal) - 1):
        if (
            detector_signal[index] >= threshold
            and detector_signal[index] >= detector_signal[index - 1]
            and detector_signal[index] > detector_signal[index + 1]
        ):
            if not peaks or index - peaks[-1] >= min_distance:
                peaks.append(index)
            elif detector_signal[index] > detector_signal[peaks[-1]]:
                peaks[-1] = index

    return np.asarray(peaks, dtype=np.int64), threshold, min_distance, polarity


def estimate_bpm_from_peaks(
    peak_indices: np.ndarray, sample_rate_hz: int, sample_count: int
) -> float:
    if peak_indices.size >= 2:
        rr_samples = np.diff(peak_indices.astype(np.float64))
        return 60.0 * float(sample_rate_hz) / float(np.median(rr_samples))
    if sample_count > 0 and sample_rate_hz > 0:
        duration_minutes = sample_count / float(sample_rate_hz) / 60.0
        return peak_indices.size / duration_minutes if duration_minutes > 0.0 else float("nan")
    return float("nan")


def measure_rpeak_baseline_amplitude(
    samples: np.ndarray,
    sample_rate_hz: int,
    expected_bpm: float,
    lead: str = "lead_ii",
) -> RPeakBaselineResult:
    peaks, threshold, min_distance, polarity = detect_r_peaks(
        samples=samples,
        sample_rate_hz=sample_rate_hz,
        expected_bpm=expected_bpm,
        lead=lead,
    )
    values = samples[:, LEAD_TO_INDEX[lead]].astype(np.float64)
    baseline_start = int(round(0.20 * sample_rate_hz))
    baseline_end = int(round(0.08 * sample_rate_hz))

    amplitudes: list[float] = []
    amplitude_peaks: list[int] = []
    rejected_amplitudes: list[float] = []
    rejected_peaks: list[int] = []

    for peak in peaks:
        start = max(0, int(peak) - baseline_start)
        end = max(0, int(peak) - baseline_end)
        if end <= start:
            rejected_amplitudes.append(float("nan"))
            rejected_peaks.append(int(peak))
            continue

        local_baseline = float(np.median(values[start:end]))
        amplitude = float(values[int(peak)] - local_baseline)
        if polarity < 0:
            amplitude *= -1.0
        amplitudes.append(amplitude)
        amplitude_peaks.append(int(peak))

    amplitude_array = np.asarray(amplitudes, dtype=np.float64)
    amplitude_peak_array = np.asarray(amplitude_peaks, dtype=np.int64)
    if amplitude_array.size == 0:
        raise CalibrationError("no R-peaks had enough pre-QRS baseline samples")

    finite_positive = amplitude_array[np.isfinite(amplitude_array) & (amplitude_array > 0.0)]
    if finite_positive.size == 0:
        raise CalibrationError("no positive R-peak baseline amplitudes were found")

    raw_median = float(np.median(finite_positive))
    raw_mad = float(np.median(np.abs(finite_positive - raw_median)))
    rejection_margin = max(6.0 * raw_mad, 0.20 * abs(raw_median), 1.0)
    accepted_mask = (
        np.isfinite(amplitude_array)
        & (amplitude_array > 0.0)
        & (np.abs(amplitude_array - raw_median) <= rejection_margin)
    )

    accepted = amplitude_array[accepted_mask]
    accepted_peaks = amplitude_peak_array[accepted_mask]
    rejected_from_outlier = amplitude_array[~accepted_mask]
    rejected_from_outlier_peaks = amplitude_peak_array[~accepted_mask]
    if rejected_from_outlier.size:
        rejected_amplitudes.extend(float(value) for value in rejected_from_outlier)
        rejected_peaks.extend(int(value) for value in rejected_from_outlier_peaks)

    if accepted.size == 0:
        raise CalibrationError("all R-peak baseline amplitudes were rejected")

    median_amplitude = float(np.median(accepted))
    amplitude_mad = float(np.median(np.abs(accepted - median_amplitude)))
    estimated_bpm = estimate_bpm_from_peaks(peaks, sample_rate_hz, samples.shape[0])

    return RPeakBaselineResult(
        amplitudes=amplitude_array,
        accepted_amplitudes=accepted,
        rejected_amplitudes=np.asarray(rejected_amplitudes, dtype=np.float64),
        peak_indices=peaks,
        accepted_peak_indices=accepted_peaks,
        rejected_peak_indices=np.asarray(rejected_peaks, dtype=np.int64),
        median_amplitude=median_amplitude,
        amplitude_mad=amplitude_mad,
        estimated_bpm=estimated_bpm,
        detection_threshold=float(threshold),
        baseline_window_samples=(baseline_start, baseline_end),
        min_peak_distance_samples=int(min_distance),
        polarity=int(polarity),
    )


def lead_summary(samples: np.ndarray) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for index, (key, _, _) in enumerate(CHANNELS):
        lead = samples[:, index]
        summary[key] = {
            "mean": float(np.mean(lead)),
            "median": float(np.median(lead)),
            "minimum": float(np.min(lead)),
            "maximum": float(np.max(lead)),
        }
    return summary


def normalize_calibration_config(config: dict[str, Any]) -> dict[str, Any]:
    try:
        scale = float(config["scale_mV_per_count"])
        offsets = config["offset_count"]
    except (KeyError, TypeError, ValueError) as error:
        raise CalibrationError(f"invalid calibration config: {error}") from error

    if not np.isfinite(scale) or scale <= 0.0:
        raise CalibrationError("scale_mV_per_count must be a finite positive number")
    if not isinstance(offsets, dict):
        raise CalibrationError("offset_count must be a mapping")

    try:
        lead_i_offset = float(offsets["lead_i"])
        lead_ii_offset = float(offsets["lead_ii"])
    except (KeyError, TypeError, ValueError) as error:
        raise CalibrationError(f"invalid offset_count: {error}") from error

    normalized = dict(config)
    normalized["scale_mV_per_count"] = scale
    normalized["offset_count"] = {
        "lead_i": lead_i_offset,
        "lead_ii": lead_ii_offset,
        "lead_iii": lead_ii_offset - lead_i_offset,
    }
    return normalized


def load_calibration_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CalibrationError(f"calibration config not found: {path}")
    return normalize_calibration_config(load_yaml(path))


def convert_raw_to_mv(samples: np.ndarray, calibration: dict[str, Any]) -> np.ndarray:
    validate_raw_matrix(samples, "raw samples")
    config = normalize_calibration_config(calibration)
    offsets = config["offset_count"]
    scale = float(config["scale_mV_per_count"])

    converted = np.empty_like(samples, dtype=np.float32)
    converted[:, 0] = (
        samples[:, LEAD_TO_INDEX["lead_i"]].astype(np.float32)
        - float(offsets["lead_i"])
    ) * scale
    converted[:, 1] = (
        samples[:, LEAD_TO_INDEX["lead_ii"]].astype(np.float32)
        - float(offsets["lead_ii"])
    ) * scale
    converted[:, 2] = converted[:, 1] - converted[:, 0]
    return np.asarray(converted, dtype=np.float32)


def frame_time_axis(sample_rate_hz: int, frame_size: int) -> np.ndarray:
    return np.arange(frame_size, dtype=np.float32) / float(sample_rate_hz)


def notch_50hz_processing(calibration: dict[str, Any]) -> dict[str, Any] | None:
    """Return validated automatic notch settings, or ``None`` when disabled."""

    filters = calibration.get("filters", {})
    if not isinstance(filters, dict) or not bool(filters.get("notch_50hz", False)):
        return None
    try:
        quality_factor = float(
            filters.get("notch_quality_factor", DEFAULT_NOTCH_QUALITY_FACTOR)
        )
    except (TypeError, ValueError) as error:
        raise CalibrationError("notch_quality_factor must be numeric") from error
    if not np.isfinite(quality_factor) or quality_factor <= 0.0:
        raise CalibrationError("notch_quality_factor must be finite and positive")
    return {
        "name": "notch_50hz",
        "frequency_hz": NOTCH_50HZ_FREQUENCY_HZ,
        "quality_factor": quality_factor,
        "phase": "zero_phase_forward_backward",
    }


def write_calibrated_frame(
    output_dir: Path,
    stem: str,
    samples_mv: np.ndarray,
    sample_rate_hz: int,
    calibration: dict[str, Any],
    source_metadata: dict[str, Any] | None = None,
    source_files: dict[str, str] | None = None,
    processing: dict[str, Any] | None = None,
) -> CalibratedExportPaths:
    if samples_mv.shape != EXPECTED_SHAPE:
        raise CalibrationError(
            f"{stem}_mv shape is {samples_mv.shape}, expected {EXPECTED_SHAPE}"
        )
    samples_mv = np.asarray(samples_mv, dtype=np.float32)
    if count_nan_inf(samples_mv):
        raise CalibrationError(f"{stem}_mv contains NaN/Inf values")

    max_error = ch3_max_error(samples_mv)
    if max_error > CH3_CONSISTENCY_TOLERANCE_MV:
        raise CalibrationError(
            f"{stem}_mv Lead III consistency failed; max error {max_error:.6g} mV"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{stem}_mv.csv"
    npy_path = output_dir / f"{stem}_mv.npy"
    json_path = output_dir / f"{stem}_mv.json"

    time_axis = frame_time_axis(sample_rate_hz, samples_mv.shape[0])
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CALIBRATED_CSV_HEADER)
        for sample_time, row in zip(time_axis, samples_mv):
            writer.writerow(
                (
                    f"{float(sample_time):.3f}",
                    f"{float(row[0]):.9g}",
                    f"{float(row[1]):.9g}",
                    f"{float(row[2]):.9g}",
                )
            )

    np.save(npy_path, samples_mv)
    normalized_config = normalize_calibration_config(calibration)
    metadata = {
        "created_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source_frame": stem,
        "source_files": source_files or {},
        "source_metadata": source_metadata or {},
        "unit": "mV",
        "shape": list(samples_mv.shape),
        "dtype": str(samples_mv.dtype),
        "sample_rate_hz": int(sample_rate_hz),
        "sample_count": int(samples_mv.shape[0]),
        "duration_seconds": samples_mv.shape[0] / float(sample_rate_hz),
        "channel_order": CHANNEL_ORDER,
        "lead_mapping": {
            "lead_i_mV": "Lead I = LA - RA",
            "lead_ii_mV": "Lead II = LL - RA",
            "lead_iii_mV": "Lead III = Lead II - Lead I",
        },
        "ch3_derived": True,
        "ch3_max_error_mV": max_error,
        "calibration": {
            "calibration_source": normalized_config.get("calibration_source"),
            "expected_mV": normalized_config.get("expected_mV"),
            "selected_lead": normalized_config.get("selected_lead"),
            "method": normalized_config.get("method"),
            "offset_count": normalized_config["offset_count"],
            "scale_mV_per_count": normalized_config["scale_mV_per_count"],
            "scale_applied_to_all_leads": True,
        },
    }
    if processing is not None:
        metadata["processing"] = processing
    json_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return CalibratedExportPaths(csv_path=csv_path, npy_path=npy_path, json_path=json_path)


def estimate_bpm(
    samples_mv: np.ndarray,
    sample_rate_hz: int,
    lead: str = "lead_ii",
) -> tuple[float, int]:
    if sample_rate_hz <= 0:
        raise CalibrationError("sample_rate_hz must be positive")
    if lead not in LEAD_TO_INDEX:
        raise CalibrationError(f"unknown lead {lead}")

    values = samples_mv[:, LEAD_TO_INDEX[lead]].astype(np.float64)
    values = values - float(np.median(values))
    p1, p99 = np.percentile(values, [1.0, 99.0])
    if abs(p1) > abs(p99):
        values = -values
        p1, p99 = np.percentile(values, [1.0, 99.0])

    if not np.isfinite(p99) or p99 <= 0.0:
        return float("nan"), 0

    threshold = max(float(np.percentile(values, 90.0)), 0.35 * float(p99))
    min_distance = max(1, int(0.30 * sample_rate_hz))
    peaks: list[int] = []

    for index in range(1, len(values) - 1):
        if (
            values[index] >= threshold
            and values[index] >= values[index - 1]
            and values[index] > values[index + 1]
        ):
            if not peaks or index - peaks[-1] >= min_distance:
                peaks.append(index)
            elif values[index] > values[peaks[-1]]:
                peaks[-1] = index

    if len(peaks) >= 2:
        rr_samples = np.diff(np.asarray(peaks, dtype=np.float64))
        bpm = 60.0 * float(sample_rate_hz) / float(np.median(rr_samples))
    elif samples_mv.shape[0] > 0:
        duration_minutes = samples_mv.shape[0] / float(sample_rate_hz) / 60.0
        bpm = len(peaks) / duration_minutes if duration_minutes > 0.0 else float("nan")
    else:
        bpm = float("nan")

    return bpm, len(peaks)

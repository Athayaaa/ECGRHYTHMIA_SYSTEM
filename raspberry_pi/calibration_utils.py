"""Shared ProSim calibration and mV export helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np
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

SATURATION_MARGIN_COUNTS = 8192.0
CH3_CONSISTENCY_TOLERANCE_COUNTS = 1.0
CH3_CONSISTENCY_TOLERANCE_MV = 1e-5
CALIBRATED_CSV_HEADER = ["time", "lead_i_mV", "lead_ii_mV", "lead_iii_mV"]

ADS1293_ADC_ENCODING = "offset_binary_uint24"
ADS1293_CONVERSION_FORMULA_ID = "ads1293_datasheet_eq13_inverse_v1"
ADS1293_CONVERSION_FORMULA = (
    "VIN_mV = (ADCOUT - zero_code) * "
    "(2 * VREF_V * 1000) / (analog_gain * ADCMAX)"
)
CALIBRATED_AFFINE_FORMULA_ID = "calibrated_offset_code_affine_v1"
CALIBRATED_AFFINE_FORMULA = (
    "lead_mV = (raw_offset_code - lead_offset_count) * scale_mV_per_count"
)

CORRECTED_PIPELINE_PROFILE = "ads1293_native_266p667_resampled_250_v2"
LEGACY_PIPELINE_PROFILE = "ads1293_native_640_software_gated_250_v1"

CORRECTED_PROTOCOL_VERSION = 2
CORRECTED_AFE_RES_HZ = 102400
CORRECTED_R1 = 4
CORRECTED_R2 = 6
CORRECTED_R3 = 16
CORRECTED_NATIVE_SAMPLE_RATE_HZ = 800.0 / 3.0
CORRECTED_OUTPUT_SAMPLE_RATE_HZ = 250.0
CORRECTED_RESAMPLE_UP = 15
CORRECTED_RESAMPLE_DOWN = 16
CORRECTED_ADCMAX = 0xF30000
CORRECTED_ZERO_CODE = CORRECTED_ADCMAX / 2.0

LEGACY_PROTOCOL_VERSION = 1
LEGACY_AFE_RES_HZ = 102400
LEGACY_R1 = 4
LEGACY_R2 = 5
LEGACY_R3 = 8
LEGACY_NATIVE_SAMPLE_RATE_HZ = 640.0
LEGACY_OUTPUT_SAMPLE_RATE_HZ = 250.0
LEGACY_RESAMPLE_UP = 25
LEGACY_RESAMPLE_DOWN = 64
LEGACY_ADCMAX = 0xC35000
LEGACY_ZERO_CODE = LEGACY_ADCMAX / 2.0

ADS1293_NOMINAL_VREF_V = 2.4
ADS1293_ANALOG_GAIN = 3.5

CALIBRATION_FINGERPRINT_FIELDS = (
    "firmware_protocol_version",
    "afe_res_hz",
    "r1",
    "r2",
    "r3_ch1",
    "r3_ch2",
    "native_sample_rate_hz",
    "output_sample_rate_hz",
    "resample_up",
    "resample_down",
    "adc_encoding",
    "adcmax",
    "zero_code",
    "vref_nominal_v",
    "analog_gain",
)
CALIBRATION_INTEGER_FINGERPRINT_FIELDS = {
    "firmware_protocol_version",
    "afe_res_hz",
    "r1",
    "r2",
    "r3_ch1",
    "r3_ch2",
    "resample_up",
    "resample_down",
    "adcmax",
    "zero_code",
}
CALIBRATION_FLOAT_FINGERPRINT_FIELDS = {
    "native_sample_rate_hz",
    "output_sample_rate_hz",
    "vref_nominal_v",
    "analog_gain",
}

FINGERPRINT_FIELD_ALIASES = {
    "firmware_protocol_version": ("firmware_protocol_version", "protocol_version"),
    "afe_res_hz": ("afe_res_hz",),
    "r1": ("r1",),
    "r2": ("r2",),
    "r3_ch1": ("r3_ch1",),
    "r3_ch2": ("r3_ch2",),
    "native_sample_rate_hz": (
        "native_sample_rate_hz",
        "native_odr_hz",
        "native_rate_hz",
    ),
    "output_sample_rate_hz": (
        "output_sample_rate_hz",
        "resampled_sample_rate_hz",
    ),
    "resample_up": ("resample_up",),
    "resample_down": ("resample_down",),
    "adc_encoding": ("adc_encoding", "adc_output_encoding"),
    "adcmax": ("adcmax", "adcmax_count", "adc_max"),
    "zero_code": ("zero_code", "adc_zero_code"),
    "vref_nominal_v": ("vref_nominal_v", "vref_v"),
    "analog_gain": ("analog_gain", "internal_analog_gain"),
}


class CalibrationError(Exception):
    pass


@dataclass(frozen=True)
class CalibrationCompatibilityResult:
    compatible: bool
    reasons: tuple[str, ...]
    calibration_fingerprint: dict[str, Any]
    runtime_fingerprint: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "reasons": list(self.reasons),
            "calibration_fingerprint": dict(self.calibration_fingerprint),
            "runtime_fingerprint": dict(self.runtime_fingerprint),
        }


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
    metadata = load_json(frame.json_path)
    samples = np.load(frame.npy_path, allow_pickle=False)
    validate_raw_samples(samples, frame.npy_path.name)
    require_hashes = metadata.get("protocol_version") == 2

    expected_npy_sha256 = metadata.get("npy_sha256")
    if require_hashes and not isinstance(expected_npy_sha256, str):
        raise CalibrationError(
            f"{frame.npy_path.name} corrected metadata is missing npy_sha256"
        )
    if expected_npy_sha256 is not None:
        if not isinstance(expected_npy_sha256, str) or len(expected_npy_sha256) != 64:
            raise CalibrationError(f"{frame.npy_path.name} has invalid npy_sha256")
        actual_npy_sha256 = file_sha256(frame.npy_path)
        if actual_npy_sha256 != expected_npy_sha256.lower():
            raise CalibrationError(
                f"{frame.npy_path.name} SHA-256 does not match frame metadata"
            )

    expected_bin_sha256 = metadata.get(
        "bin_sha256",
        metadata.get("sha256_checksum"),
    )
    if require_hashes and not isinstance(expected_bin_sha256, str):
        raise CalibrationError(
            f"{frame.bin_path.name} corrected metadata is missing bin_sha256"
        )
    if expected_bin_sha256 is not None:
        if not frame.bin_path.exists():
            raise CalibrationError(f"raw BIN file not found: {frame.bin_path}")
        if not isinstance(expected_bin_sha256, str) or len(expected_bin_sha256) != 64:
            raise CalibrationError(f"{frame.bin_path.name} has invalid bin_sha256")
        actual_bin_sha256 = file_sha256(frame.bin_path)
        if actual_bin_sha256 != expected_bin_sha256.lower():
            raise CalibrationError(
                f"{frame.bin_path.name} SHA-256 does not match frame metadata"
            )

        canonical_bytes = np.ascontiguousarray(samples, dtype="<f4").tobytes(order="C")
        if frame.bin_path.read_bytes() != canonical_bytes:
            raise CalibrationError(
                f"{frame.bin_path.name} content does not match canonical NPY samples"
            )
    return samples, metadata


def ads1293_nominal_scale_mV_per_count(
    adcmax: int | float,
    vref_v: float = ADS1293_NOMINAL_VREF_V,
    analog_gain: float = ADS1293_ANALOG_GAIN,
) -> float:
    """Return the ADS1293 Equation 13 inverse scale in mV per output code."""

    adcmax_value = float(adcmax)
    vref_value = float(vref_v)
    gain_value = float(analog_gain)
    if not np.isfinite(adcmax_value) or adcmax_value <= 0.0:
        raise CalibrationError("adcmax must be a finite positive number")
    if not np.isfinite(vref_value) or vref_value <= 0.0:
        raise CalibrationError("vref_v must be a finite positive number")
    if not np.isfinite(gain_value) or gain_value <= 0.0:
        raise CalibrationError("analog_gain must be a finite positive number")
    return (2.0 * vref_value * 1000.0) / (gain_value * adcmax_value)


def ads1293_offset_code_to_mv(
    raw_code: np.ndarray | float | int,
    adcmax: int | float,
    vref_v: float = ADS1293_NOMINAL_VREF_V,
    analog_gain: float = ADS1293_ANALOG_GAIN,
    zero_code: float | None = None,
) -> np.ndarray | np.float32:
    """Convert unsigned ADS1293 offset-code samples to input-referred mV.

    ADS1293 ECG data uses the offset-code transfer described by datasheet
    Equation 13.  No two's-complement sign extension is performed here.
    Arrays are returned as float32 to preserve the canonical export contract.
    """

    adcmax_value = float(adcmax)
    scale = ads1293_nominal_scale_mV_per_count(
        adcmax=adcmax_value,
        vref_v=vref_v,
        analog_gain=analog_gain,
    )
    zero_value = adcmax_value / 2.0 if zero_code is None else float(zero_code)
    if not np.isfinite(zero_value):
        raise CalibrationError("zero_code must be finite")

    raw = np.asarray(raw_code, dtype=np.float64)
    converted = np.asarray((raw - zero_value) * scale, dtype=np.float32)
    if converted.ndim == 0:
        return np.float32(converted)
    return converted


def pipeline_profile_parameters(profile: str) -> dict[str, Any]:
    """Return a reproducible acquisition/conversion fingerprint for a profile."""

    if profile == CORRECTED_PIPELINE_PROFILE:
        adcmax = CORRECTED_ADCMAX
        zero_code = CORRECTED_ZERO_CODE
        return {
            "pipeline_profile": CORRECTED_PIPELINE_PROFILE,
            "firmware_protocol_version": CORRECTED_PROTOCOL_VERSION,
            "afe_res_hz": CORRECTED_AFE_RES_HZ,
            "r1": CORRECTED_R1,
            "r2": CORRECTED_R2,
            "r3_ch1": CORRECTED_R3,
            "r3_ch2": CORRECTED_R3,
            "native_sample_rate_hz": CORRECTED_NATIVE_SAMPLE_RATE_HZ,
            "output_sample_rate_hz": CORRECTED_OUTPUT_SAMPLE_RATE_HZ,
            "resample_up": CORRECTED_RESAMPLE_UP,
            "resample_down": CORRECTED_RESAMPLE_DOWN,
            "resampling_method": "stateful_polyphase_fir",
            "adc_encoding": ADS1293_ADC_ENCODING,
            "adcmax": adcmax,
            "zero_code": zero_code,
            "vref_nominal_v": ADS1293_NOMINAL_VREF_V,
            "analog_gain": ADS1293_ANALOG_GAIN,
            "datasheet_nominal_scale_mV_per_count": (
                ads1293_nominal_scale_mV_per_count(adcmax)
            ),
        }
    if profile == LEGACY_PIPELINE_PROFILE:
        adcmax = LEGACY_ADCMAX
        zero_code = LEGACY_ZERO_CODE
        return {
            "pipeline_profile": LEGACY_PIPELINE_PROFILE,
            "firmware_protocol_version": LEGACY_PROTOCOL_VERSION,
            "afe_res_hz": LEGACY_AFE_RES_HZ,
            "r1": LEGACY_R1,
            "r2": LEGACY_R2,
            "r3_ch1": LEGACY_R3,
            "r3_ch2": LEGACY_R3,
            "native_sample_rate_hz": LEGACY_NATIVE_SAMPLE_RATE_HZ,
            "output_sample_rate_hz": LEGACY_OUTPUT_SAMPLE_RATE_HZ,
            "resample_up": LEGACY_RESAMPLE_UP,
            "resample_down": LEGACY_RESAMPLE_DOWN,
            "resampling_method": "legacy_software_poll_latest_sample",
            "adc_encoding": ADS1293_ADC_ENCODING,
            "adcmax": adcmax,
            "zero_code": zero_code,
            "vref_nominal_v": ADS1293_NOMINAL_VREF_V,
            "analog_gain": ADS1293_ANALOG_GAIN,
            "datasheet_nominal_scale_mV_per_count": (
                ads1293_nominal_scale_mV_per_count(adcmax)
            ),
        }
    raise CalibrationError(f"unknown ADS1293 pipeline profile {profile!r}")


def offset_code_range_stats(
    samples: np.ndarray,
    adcmax: int | float,
    saturation_margin_counts: float = SATURATION_MARGIN_COUNTS,
) -> dict[str, Any]:
    """Validate measured offset-code leads and the derived Lead III separately."""

    if samples.ndim != 2 or samples.shape[1] != 3:
        raise CalibrationError(f"raw samples shape is {samples.shape}, expected Nx3")
    adcmax_value = float(adcmax)
    margin = float(saturation_margin_counts)
    if not np.isfinite(adcmax_value) or adcmax_value <= 0.0:
        raise CalibrationError("adcmax must be a finite positive number")
    if not np.isfinite(margin) or margin < 0.0 or margin >= adcmax_value / 2.0:
        raise CalibrationError(
            "saturation_margin_counts must be finite, non-negative, and below adcmax/2"
        )

    measured: dict[str, dict[str, Any]] = {}
    totals = {
        "below_range_count": 0,
        "above_range_count": 0,
        "near_low_rail_count": 0,
        "near_high_rail_count": 0,
    }
    for index, key in ((0, "lead_i"), (1, "lead_ii")):
        values = np.asarray(samples[:, index], dtype=np.float64)
        finite_mask = np.isfinite(values)
        finite = values[finite_mask]
        below = int(np.count_nonzero(finite < 0.0))
        above = int(np.count_nonzero(finite > adcmax_value))
        near_low = int(
            np.count_nonzero((finite >= 0.0) & (finite <= margin))
        )
        near_high = int(
            np.count_nonzero(
                (finite <= adcmax_value) & (finite >= adcmax_value - margin)
            )
        )
        row = {
            "encoding": ADS1293_ADC_ENCODING,
            "minimum_count": float(np.min(finite)) if finite.size else float("nan"),
            "maximum_count": float(np.max(finite)) if finite.size else float("nan"),
            "nan_inf_count": int(values.size - finite.size),
            "below_range_count": below,
            "above_range_count": above,
            "near_low_rail_count": near_low,
            "near_high_rail_count": near_high,
        }
        measured[key] = row
        for total_key in totals:
            totals[total_key] += int(row[total_key])

    lead_iii_values = np.asarray(samples[:, 2], dtype=np.float64)
    lead_iii_finite = lead_iii_values[np.isfinite(lead_iii_values)]
    lead_iii = {
        "encoding": "signed_derived_difference",
        "formula": "Lead III = Lead II - Lead I",
        "minimum_count": (
            float(np.min(lead_iii_finite)) if lead_iii_finite.size else float("nan")
        ),
        "maximum_count": (
            float(np.max(lead_iii_finite)) if lead_iii_finite.size else float("nan")
        ),
        "maximum_absolute_count": (
            float(np.max(np.abs(lead_iii_finite)))
            if lead_iii_finite.size
            else float("nan")
        ),
        "nan_inf_count": int(lead_iii_values.size - lead_iii_finite.size),
        "consistency_max_error_count": ch3_max_error(samples),
    }
    out_of_range = totals["below_range_count"] + totals["above_range_count"]
    near_rail = totals["near_low_rail_count"] + totals["near_high_rail_count"]
    reasons: list[str] = []
    for key, label in (("lead_i", "Lead I"), ("lead_ii", "Lead II")):
        row = measured[key]
        if row["below_range_count"]:
            reasons.append(
                f"{label}: {row['below_range_count']} sample(s) below offset-code rail 0"
            )
        if row["above_range_count"]:
            reasons.append(
                f"{label}: {row['above_range_count']} sample(s) above ADCMAX "
                f"{int(adcmax_value)}"
            )
        if row["near_low_rail_count"]:
            reasons.append(
                f"{label}: {row['near_low_rail_count']} sample(s) within "
                f"{margin:g} count(s) of the low ADC rail"
            )
        if row["near_high_rail_count"]:
            reasons.append(
                f"{label}: {row['near_high_rail_count']} sample(s) within "
                f"{margin:g} count(s) of the high ADC rail"
            )
    status = "FAIL" if out_of_range else "WARNING" if near_rail else "PASS"
    return {
        "status": status,
        "reasons": reasons,
        "adc_encoding": ADS1293_ADC_ENCODING,
        "adcmax": int(adcmax_value),
        "valid_range_count": [0, int(adcmax_value)],
        "saturation_margin_counts": margin,
        "measured_leads": measured,
        "derived_lead_iii": lead_iii,
        **totals,
        "out_of_range_count": int(out_of_range),
        "near_rail_count": int(near_rail),
    }


def count_saturation(
    samples: np.ndarray,
    adcmax: int | float = CORRECTED_ADCMAX,
    saturation_margin_counts: float = SATURATION_MARGIN_COUNTS,
) -> int:
    """Count measured Lead I/II offset codes at/outside configured ADC rails."""

    stats = offset_code_range_stats(samples, adcmax, saturation_margin_counts)
    return int(stats["out_of_range_count"] + stats["near_rail_count"])


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


def is_legacy_calibration(config: dict[str, Any]) -> bool:
    return (
        str(config.get("calibration_status", "")).lower() == "legacy"
        or config.get("pipeline_profile") == LEGACY_PIPELINE_PROFILE
        or "firmware_protocol_version" not in config
    )


def normalize_calibration_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise CalibrationError("calibration config must be a mapping")

    normalized = dict(config)
    profile = normalized.get("pipeline_profile")
    if profile is None:
        profile = (
            CORRECTED_PIPELINE_PROFILE
            if normalized.get("firmware_protocol_version") == CORRECTED_PROTOCOL_VERSION
            else LEGACY_PIPELINE_PROFILE
        )
    profile = str(profile)
    if profile == CORRECTED_PIPELINE_PROFILE:
        missing_fingerprint = [
            field for field in CALIBRATION_FINGERPRINT_FIELDS if field not in normalized
        ]
        if missing_fingerprint:
            raise CalibrationError(
                "corrected calibration config has an incomplete acquisition fingerprint; "
                "missing " + ", ".join(missing_fingerprint)
            )
    profile_values = pipeline_profile_parameters(str(profile))
    for key, value in profile_values.items():
        normalized.setdefault(key, value)

    try:
        adcmax = int(normalized["adcmax"])
        zero_code = float(normalized.get("zero_code", adcmax / 2.0))
        vref_v = float(
            normalized.get("vref_nominal_v", normalized.get("vref_v", ADS1293_NOMINAL_VREF_V))
        )
        analog_gain = float(normalized.get("analog_gain", ADS1293_ANALOG_GAIN))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise CalibrationError(f"invalid ADS1293 conversion parameters: {error}") from error
    if adcmax <= 0 or adcmax > 0xFFFFFF:
        raise CalibrationError("adcmax must be within unsigned 24-bit range")
    if not 0.0 <= zero_code <= float(adcmax):
        raise CalibrationError("zero_code must be within 0..adcmax")
    if str(normalized.get("adc_encoding")) != ADS1293_ADC_ENCODING:
        raise CalibrationError(
            f"adc_encoding must be {ADS1293_ADC_ENCODING!r} for ADS1293 ECG output"
        )

    nominal_scale = ads1293_nominal_scale_mV_per_count(
        adcmax=adcmax,
        vref_v=vref_v,
        analog_gain=analog_gain,
    )
    try:
        scale = float(normalized.get("scale_mV_per_count", nominal_scale))
    except (TypeError, ValueError) as error:
        raise CalibrationError(f"invalid scale_mV_per_count: {error}") from error
    if not np.isfinite(scale) or scale <= 0.0:
        raise CalibrationError("scale_mV_per_count must be a finite positive number")

    offsets = normalized.get("offset_count")
    if offsets is None:
        offsets = {"lead_i": zero_code, "lead_ii": zero_code}
    if not isinstance(offsets, dict):
        raise CalibrationError("offset_count must be a mapping")
    try:
        lead_i_offset = float(offsets["lead_i"])
        lead_ii_offset = float(offsets["lead_ii"])
    except (KeyError, TypeError, ValueError) as error:
        raise CalibrationError(f"invalid offset_count: {error}") from error
    if not np.isfinite(lead_i_offset) or not np.isfinite(lead_ii_offset):
        raise CalibrationError("Lead I/II offset_count values must be finite")

    calibration_source = str(normalized.get("calibration_source", "unknown"))
    if "calibration_status" not in normalized:
        if profile == LEGACY_PIPELINE_PROFILE:
            normalized["calibration_status"] = "legacy"
        elif calibration_source == "datasheet_nominal":
            normalized["calibration_status"] = "preliminary"
        else:
            normalized["calibration_status"] = "calibrated"

    if calibration_source == "datasheet_nominal" and not math.isclose(
        scale, nominal_scale, rel_tol=1e-12, abs_tol=1e-15
    ):
        raise CalibrationError(
            "datasheet_nominal scale_mV_per_count does not match ADCMAX/VREF/gain"
        )
    if calibration_source == "datasheet_nominal" and (
        not math.isclose(lead_i_offset, zero_code, rel_tol=0.0, abs_tol=1e-9)
        or not math.isclose(lead_ii_offset, zero_code, rel_tol=0.0, abs_tol=1e-9)
    ):
        raise CalibrationError(
            "datasheet_nominal Lead I/II offset_count must equal verified zero_code; "
            "a measured offset requires a calibrated affine source/formula"
        )

    formula_id = (
        ADS1293_CONVERSION_FORMULA_ID
        if calibration_source == "datasheet_nominal"
        else CALIBRATED_AFFINE_FORMULA_ID
    )
    formula_text = (
        ADS1293_CONVERSION_FORMULA
        if formula_id == ADS1293_CONVERSION_FORMULA_ID
        else CALIBRATED_AFFINE_FORMULA
    )
    normalized.update(
        {
            "pipeline_profile": str(profile),
            "adc_encoding": ADS1293_ADC_ENCODING,
            "adcmax": adcmax,
            "zero_code": zero_code,
            "vref_nominal_v": vref_v,
            "analog_gain": analog_gain,
            "datasheet_nominal_scale_mV_per_count": nominal_scale,
            "scale_mV_per_count": scale,
            "offset_count": {
                "lead_i": lead_i_offset,
                "lead_ii": lead_ii_offset,
                "lead_iii": lead_ii_offset - lead_i_offset,
            },
            "conversion_formula": {
                "formula_id": formula_id,
                "equation": formula_text,
                "adc_encoding": ADS1293_ADC_ENCODING,
                "adcmax": adcmax,
                "zero_code": zero_code,
                "vref_nominal_v": vref_v,
                "analog_gain": analog_gain,
                "datasheet_nominal_scale_mV_per_count": nominal_scale,
                "applied_scale_mV_per_count": scale,
            },
        }
    )
    normalized.setdefault(
        "prosim_recalibration_required",
        calibration_source == "datasheet_nominal" or profile == LEGACY_PIPELINE_PROFILE,
    )
    return normalized


def _metadata_mappings(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = [metadata]
    seen: set[int] = set()
    preferred_nested_keys = (
        "acquisition_fingerprint",
        "firmware_config",
        "ads1293_config",
        "session_metadata",
        "session",
        "source_metadata",
    )
    while queue:
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        mappings.append(current)
        for key in preferred_nested_keys:
            nested = current.get(key)
            if isinstance(nested, dict):
                queue.append(nested)
    return mappings


def _fingerprint_value(metadata: dict[str, Any], field: str) -> Any:
    for mapping in _metadata_mappings(metadata):
        for alias in FINGERPRINT_FIELD_ALIASES[field]:
            if alias in mapping:
                return mapping[alias]
    return None


def calibration_fingerprint(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_calibration_config(config)
    return {field: normalized.get(field) for field in CALIBRATION_FINGERPRINT_FIELDS}


def runtime_fingerprint(metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise CalibrationError("runtime/session metadata must be a mapping")
    return {
        field: _fingerprint_value(metadata, field)
        for field in CALIBRATION_FINGERPRINT_FIELDS
    }


def validate_calibration_compatibility(
    calibration: dict[str, Any],
    runtime_metadata: dict[str, Any],
    strict: bool = True,
) -> CalibrationCompatibilityResult:
    expected = calibration_fingerprint(calibration)
    actual = runtime_fingerprint(runtime_metadata)
    reasons: list[str] = []

    for field in CALIBRATION_FINGERPRINT_FIELDS:
        expected_value = expected.get(field)
        actual_value = actual.get(field)
        if expected_value is None:
            reasons.append(f"calibration fingerprint missing {field}")
            continue
        if actual_value is None:
            if strict:
                reasons.append(f"runtime fingerprint missing {field}")
            continue
        if field in CALIBRATION_INTEGER_FINGERPRINT_FIELDS:
            try:
                actual_number = float(actual_value)
                expected_number = float(expected_value)
                matches = (
                    math.isfinite(actual_number)
                    and math.isfinite(expected_number)
                    and actual_number.is_integer()
                    and expected_number.is_integer()
                    and int(actual_number) == int(expected_number)
                )
            except (TypeError, ValueError):
                matches = False
        elif field in CALIBRATION_FLOAT_FINGERPRINT_FIELDS:
            try:
                matches = math.isclose(
                    float(actual_value),
                    float(expected_value),
                    rel_tol=1e-9,
                    abs_tol=1e-6,
                )
            except (TypeError, ValueError):
                matches = False
        else:
            matches = str(actual_value) == str(expected_value)
        if not matches:
            reasons.append(
                f"fingerprint mismatch {field}: calibration={expected_value!r}, "
                f"runtime={actual_value!r}"
            )

    return CalibrationCompatibilityResult(
        compatible=not reasons,
        reasons=tuple(reasons),
        calibration_fingerprint=expected,
        runtime_fingerprint=actual,
    )


def require_calibration_compatible(
    calibration: dict[str, Any],
    runtime_metadata: dict[str, Any],
) -> CalibrationCompatibilityResult:
    result = validate_calibration_compatibility(
        calibration=calibration,
        runtime_metadata=runtime_metadata,
        strict=True,
    )
    if not result.compatible:
        raise CalibrationError(
            "calibration is incompatible with verified acquisition fingerprint: "
            + "; ".join(result.reasons)
        )
    return result


def require_frame_session_link(
    frame_metadata: dict[str, Any],
    session_metadata: dict[str, Any],
) -> str:
    """Return the shared session ID or reject unrelated provenance.

    A valid firmware fingerprint is not sufficient evidence that an external
    session JSON belongs to a particular frame.  Offline corrected-pipeline
    tools must bind the two artifacts explicitly before using session metadata
    to satisfy the frame's calibration compatibility check.
    """

    def required_id(metadata: dict[str, Any], label: str) -> str:
        if not isinstance(metadata, dict):
            raise CalibrationError(f"{label} metadata must be a mapping")
        value = metadata.get("session_id")
        if not isinstance(value, str) or not value.strip():
            raise CalibrationError(f"{label} metadata is missing a valid session_id")
        if value != value.strip():
            raise CalibrationError(f"{label} metadata session_id contains whitespace")
        return value

    frame_session_id = required_id(frame_metadata, "corrected frame")
    session_id = required_id(session_metadata, "external session")
    if frame_session_id != session_id:
        raise CalibrationError(
            "corrected frame/session provenance mismatch: "
            f"frame session_id={frame_session_id!r}, "
            f"external session_id={session_id!r}"
        )

    frame_commit = frame_metadata.get("firmware_commit")
    session_commit = session_metadata.get("firmware_commit")
    if (
        not isinstance(frame_commit, str)
        or not frame_commit.strip()
        or frame_commit != session_commit
    ):
        raise CalibrationError(
            "corrected frame/session firmware_commit mismatch: "
            f"frame={frame_commit!r}, session={session_commit!r}"
        )

    frame_fingerprint = runtime_fingerprint(frame_metadata)
    session_fingerprint = runtime_fingerprint(session_metadata)
    for field in CALIBRATION_FINGERPRINT_FIELDS:
        frame_value = frame_fingerprint.get(field)
        session_value = session_fingerprint.get(field)
        if frame_value is None or session_value is None:
            raise CalibrationError(
                f"corrected frame/session fingerprint missing {field}"
            )
        try:
            if field in CALIBRATION_INTEGER_FINGERPRINT_FIELDS:
                frame_number = float(frame_value)
                session_number = float(session_value)
                matches = (
                    math.isfinite(frame_number)
                    and math.isfinite(session_number)
                    and frame_number.is_integer()
                    and session_number.is_integer()
                    and int(frame_number) == int(session_number)
                )
            elif field in CALIBRATION_FLOAT_FINGERPRINT_FIELDS:
                matches = math.isclose(
                    float(frame_value),
                    float(session_value),
                    rel_tol=1e-9,
                    abs_tol=1e-6,
                )
            else:
                matches = str(frame_value) == str(session_value)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise CalibrationError(
                "corrected frame/session fingerprint mismatch "
                f"{field}: frame={frame_value!r}, session={session_value!r}"
            )
    return session_id


def require_corrected_frame_provenance(
    metadata: dict[str, Any],
    *,
    require_signal_quality_pass: bool = False,
) -> dict[str, Any]:
    """Reject corrected frames whose acquisition/range provenance is unsafe.

    The verified session fingerprint proves which firmware produced a frame;
    it does not prove that the individual frame survived acquisition without
    an ADS error or that native Lead I/II codes stayed in range.  Corrected
    offline tools therefore require the per-frame statuses written by the V2
    receiver.  Legacy replay intentionally does not call this helper because
    historical V1 metadata predates these fields.
    """

    if not isinstance(metadata, dict):
        raise CalibrationError("corrected frame metadata must be a mapping")

    if metadata.get("protocol_version") != 2:
        raise CalibrationError("corrected frame protocol_version must be 2")
    for field in ("session_id", "firmware_commit"):
        value = metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CalibrationError(f"corrected frame metadata is missing {field}")
    for field in ("npy_sha256", "bin_sha256"):
        value = metadata.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value.lower())
        ):
            raise CalibrationError(
                f"corrected frame metadata is missing a valid {field} digest"
            )

    acquisition_status = str(metadata.get("acquisition_status", "")).strip().upper()
    if acquisition_status != "PASS":
        displayed = acquisition_status or "MISSING"
        raise CalibrationError(
            "corrected frame acquisition_status must be PASS; "
            f"got {displayed}"
        )

    if "acquisition_reasons" not in metadata:
        raise CalibrationError("corrected frame metadata is missing acquisition_reasons")
    acquisition_reasons = metadata.get("acquisition_reasons")
    if not isinstance(acquisition_reasons, list):
        raise CalibrationError("corrected frame acquisition_reasons must be a list")
    if acquisition_reasons:
        raise CalibrationError(
            "corrected frame reports acquisition failure reason(s): "
            + "; ".join(str(reason) for reason in acquisition_reasons)
        )

    signal_status = str(metadata.get("signal_quality_status", "")).strip().upper()
    if signal_status not in {"PASS", "WARNING", "FAIL"}:
        displayed = signal_status or "MISSING"
        raise CalibrationError(
            "corrected frame signal_quality_status must be PASS/WARNING/FAIL; "
            f"got {displayed}"
        )
    if signal_status == "FAIL":
        raise CalibrationError("corrected frame signal_quality_status is FAIL")
    if require_signal_quality_pass and signal_status != "PASS":
        raise CalibrationError(
            "corrected frame is not model-ready because signal_quality_status "
            f"is {signal_status}"
        )

    signal_quality = metadata.get("signal_quality")
    if not isinstance(signal_quality, dict):
        raise CalibrationError(
            "corrected frame metadata is missing signal_quality range validation"
        )
    nested_status = str(signal_quality.get("status", "")).strip().upper()
    if nested_status != signal_status:
        raise CalibrationError(
            "corrected frame signal-quality status mismatch: "
            f"top-level={signal_status}, range-validation={nested_status or 'MISSING'}"
        )

    for field in (
        "reasons",
        "below_range_count",
        "above_range_count",
        "near_low_rail_count",
        "near_high_rail_count",
        "out_of_range_count",
        "near_rail_count",
        "native_below_range_count",
        "native_above_range_count",
        "native_near_low_rail_count",
        "native_near_high_rail_count",
        "measured_leads",
        "derived_lead_iii",
    ):
        if field not in signal_quality:
            raise CalibrationError(
                f"corrected frame signal_quality is missing {field}"
            )
    signal_reasons = signal_quality.get("reasons")
    if not isinstance(signal_reasons, list):
        raise CalibrationError("corrected frame signal_quality.reasons must be a list")
    if signal_status == "PASS" and signal_reasons:
        raise CalibrationError(
            "corrected frame signal_quality PASS contains warning/failure reasons"
        )
    if signal_status == "WARNING" and not signal_reasons:
        raise CalibrationError(
            "corrected frame signal_quality WARNING is missing explicit reasons"
        )

    count_fields = (
        "below_range_count",
        "above_range_count",
        "near_low_rail_count",
        "near_high_rail_count",
        "out_of_range_count",
        "near_rail_count",
        "native_below_range_count",
        "native_above_range_count",
        "native_near_low_rail_count",
        "native_near_high_rail_count",
    )
    counts: dict[str, int] = {}
    for field in count_fields:
        raw_value = signal_quality[field]
        if isinstance(raw_value, (bool, np.bool_)):
            raise CalibrationError(
                f"corrected frame signal_quality.{field} must be an integer"
            )
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError) as error:
            raise CalibrationError(
                f"corrected frame signal_quality.{field} is invalid"
            ) from error
        if (
            not math.isfinite(numeric_value)
            or not numeric_value.is_integer()
            or numeric_value < 0
        ):
            raise CalibrationError(
                f"corrected frame signal_quality.{field} must be a non-negative integer"
            )
        value = int(numeric_value)
        counts[field] = value

    if counts["out_of_range_count"] != (
        counts["below_range_count"] + counts["above_range_count"]
    ):
        raise CalibrationError(
            "corrected frame signal_quality out-of-range totals are inconsistent"
        )
    if counts["near_rail_count"] != (
        counts["near_low_rail_count"] + counts["near_high_rail_count"]
    ):
        raise CalibrationError(
            "corrected frame signal_quality near-rail totals are inconsistent"
        )

    range_failures = (
        counts["out_of_range_count"]
        + counts["native_below_range_count"]
        + counts["native_above_range_count"]
    )
    near_rail = (
        counts["near_rail_count"]
        + counts["native_near_low_rail_count"]
        + counts["native_near_high_rail_count"]
    )
    if range_failures:
        raise CalibrationError(
            "corrected frame has acquired Lead I/II code(s) outside 0..ADCMAX"
        )
    if signal_status == "PASS" and near_rail:
        raise CalibrationError(
            "corrected frame signal_quality PASS contains near-rail samples"
        )
    if signal_status == "WARNING" and near_rail == 0:
        raise CalibrationError(
            "corrected frame signal_quality WARNING has no near-rail count"
        )

    for field in (
        "packet_loss_count_frame",
        "sequence_gap_events_frame",
        "resampler_reset_count_frame",
    ):
        if field not in metadata:
            raise CalibrationError(f"corrected frame metadata is missing {field}")
        raw_value = metadata[field]
        if isinstance(raw_value, (bool, np.bool_)):
            raise CalibrationError(f"corrected frame {field} must be integral")
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError) as error:
            raise CalibrationError(f"corrected frame {field} is invalid") from error
        if not math.isfinite(numeric_value) or not numeric_value.is_integer():
            raise CalibrationError(f"corrected frame {field} must be integral")
        value = int(numeric_value)
        if value != 0:
            raise CalibrationError(
                f"corrected PASS frame has nonzero {field}={value}"
            )

    return {
        "acquisition_status": acquisition_status,
        "signal_quality_status": signal_status,
        "model_ready": signal_status == "PASS",
    }


def require_frame_output_sample_rate(
    metadata: dict[str, Any],
    calibration: dict[str, Any],
) -> int:
    """Return the verified canonical frame rate or reject inconsistent metadata."""

    if not isinstance(metadata, dict) or "sample_rate_hz" not in metadata:
        raise CalibrationError("raw frame metadata missing sample_rate_hz")
    normalized = normalize_calibration_config(calibration)
    expected = float(normalized["output_sample_rate_hz"])
    try:
        actual = float(metadata["sample_rate_hz"])
    except (TypeError, ValueError) as error:
        raise CalibrationError("raw frame sample_rate_hz is invalid") from error
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9):
        raise CalibrationError(
            f"raw frame sample_rate_hz={actual:.12g} does not match calibration "
            f"output_sample_rate_hz={expected:.12g}"
        )
    rounded = int(round(actual))
    if not math.isclose(actual, float(rounded), rel_tol=0.0, abs_tol=1e-9):
        raise CalibrationError("canonical frame sample rate must be an integer Hz value")
    return rounded


def load_calibration_config(
    path: Path,
    *,
    allow_legacy: bool = False,
    runtime_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not path.exists():
        raise CalibrationError(f"calibration config not found: {path}")
    normalized = normalize_calibration_config(load_yaml(path))
    normalized["calibration_config_path"] = str(path.resolve())
    if is_legacy_calibration(normalized) and not allow_legacy:
        raise CalibrationError(
            "legacy 640-to-software-250 calibration is explicit-only; use "
            "calibration_config_legacy_640_to_250.yaml with a legacy replay/conversion tool"
        )
    if is_legacy_calibration(normalized) and allow_legacy:
        normalized["legacy_explicitly_allowed"] = True
    if runtime_metadata is not None:
        compatibility = require_calibration_compatible(normalized, runtime_metadata)
        normalized["compatibility"] = compatibility.to_dict()
    return normalized


def convert_raw_to_mv(samples: np.ndarray, calibration: dict[str, Any]) -> np.ndarray:
    validate_raw_matrix(samples, "raw samples")
    config = normalize_calibration_config(calibration)
    offsets = config["offset_count"]
    scale = float(config["scale_mV_per_count"])
    range_stats = offset_code_range_stats(samples, adcmax=config["adcmax"])
    if range_stats["out_of_range_count"]:
        raise CalibrationError(
            "raw Lead I/II contain "
            f"{range_stats['out_of_range_count']} offset-code value(s) outside "
            f"0..{config['adcmax']}"
        )

    converted = np.empty_like(samples, dtype=np.float32)
    converted[:, 0] = (
        samples[:, LEAD_TO_INDEX["lead_i"]].astype(np.float64)
        - float(offsets["lead_i"])
    ) * scale
    converted[:, 1] = (
        samples[:, LEAD_TO_INDEX["lead_ii"]].astype(np.float64)
        - float(offsets["lead_ii"])
    ) * scale
    converted[:, 2] = converted[:, 1] - converted[:, 0]
    return np.asarray(converted, dtype=np.float32)


def frame_time_axis(sample_rate_hz: int, frame_size: int) -> np.ndarray:
    return np.arange(frame_size, dtype=np.float32) / float(sample_rate_hz)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_calibrated_frame(
    output_dir: Path,
    stem: str,
    samples_mv: np.ndarray,
    sample_rate_hz: int,
    calibration: dict[str, Any],
    source_metadata: dict[str, Any] | None = None,
    source_files: dict[str, str] | None = None,
    raw_range_stats: dict[str, Any] | None = None,
) -> CalibratedExportPaths:
    normalized_config = normalize_calibration_config(calibration)
    if samples_mv.shape != EXPECTED_SHAPE:
        raise CalibrationError(
            f"{stem}_mv shape is {samples_mv.shape}, expected {EXPECTED_SHAPE}"
        )
    samples_mv = np.asarray(samples_mv, dtype=np.float32)
    if count_nan_inf(samples_mv):
        raise CalibrationError(f"{stem}_mv contains NaN/Inf values")
    expected_rate_hz = float(normalized_config["output_sample_rate_hz"])
    if not math.isclose(
        float(sample_rate_hz),
        expected_rate_hz,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise CalibrationError(
            f"{stem}_mv sample_rate_hz={sample_rate_hz} does not match calibration "
            f"output_sample_rate_hz={expected_rate_hz:.12g}"
        )

    max_error = ch3_max_error(samples_mv)
    if max_error > CH3_CONSISTENCY_TOLERANCE_MV:
        raise CalibrationError(
            f"{stem}_mv Lead III consistency failed; max error {max_error:.6g} mV"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{stem}_mv.csv"
    npy_path = output_dir / f"{stem}_mv.npy"
    json_path = output_dir / f"{stem}_mv.json"
    existing = [path for path in (csv_path, npy_path, json_path) if path.exists()]
    if existing:
        raise CalibrationError(
            f"calibrated output already exists for {stem}: "
            + ", ".join(str(path) for path in existing)
        )

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
    npy_sha256 = file_sha256(npy_path)
    calibration_path_text = normalized_config.get("calibration_config_path")
    calibration_path = (
        Path(str(calibration_path_text)) if calibration_path_text is not None else None
    )
    calibration_sha256 = normalized_config.get("calibration_config_sha256")
    if calibration_sha256 is None:
        calibration_sha256 = (
            file_sha256(calibration_path)
            if calibration_path is not None and calibration_path.exists()
            else None
        )
    compatibility = normalized_config.get("compatibility")
    if compatibility is None and normalized_config.get("legacy_explicitly_allowed"):
        compatibility = {
            "compatible": True,
            "status": "LEGACY_EXPLICIT",
            "reasons": [
                "legacy calibration was explicitly selected for legacy replay/conversion"
            ],
        }
    metadata = {
        "created_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source_frame": stem,
        "source_files": source_files or {},
        "source_metadata": source_metadata or {},
        "unit": "mV",
        "sample_domain": "input_referred_millivolts",
        "shape": list(samples_mv.shape),
        "dtype": str(samples_mv.dtype),
        "npy_sha256": npy_sha256,
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
        "raw_range_validation": raw_range_stats,
        "calibration": {
            "calibration_source": normalized_config.get("calibration_source"),
            "calibration_status": normalized_config.get("calibration_status"),
            "pipeline_profile": normalized_config.get("pipeline_profile"),
            "calibration_config_path": calibration_path_text,
            "calibration_config_sha256": calibration_sha256,
            "expected_mV": normalized_config.get("expected_mV"),
            "selected_lead": normalized_config.get("selected_lead"),
            "method": normalized_config.get("method"),
            "offset_count": normalized_config["offset_count"],
            "scale_mV_per_count": normalized_config["scale_mV_per_count"],
            "scale_applied_to_all_leads": True,
            "prosim_recalibration_required": normalized_config.get(
                "prosim_recalibration_required"
            ),
            "conversion_formula": normalized_config["conversion_formula"],
            "acquisition_fingerprint": calibration_fingerprint(normalized_config),
            "compatibility": compatibility,
        },
    }
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

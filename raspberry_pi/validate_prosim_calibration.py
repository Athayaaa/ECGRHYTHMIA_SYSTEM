"""Validate a ProSim calibration config against recorded raw datasets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import yaml

from calibration_utils import (
    CH3_CONSISTENCY_TOLERANCE_COUNTS,
    CH3_CONSISTENCY_TOLERANCE_MV,
    EXPECTED_DTYPE,
    EXPECTED_SHAPE,
    CalibrationError,
    ch3_max_error,
    convert_raw_to_mv,
    count_nan_inf,
    count_saturation,
    find_complete_frame_sets,
    load_calibration_config,
    load_json,
    measure_rpeak_baseline_amplitude,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "calibration_config.yaml"
PASS_AMPLITUDE_RELATIVE_ERROR = 0.20
FAIL_AMPLITUDE_RELATIVE_ERROR = 0.35
PASS_BPM_ERROR = 2.0
FAIL_BPM_ERROR = 5.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate ProSim mV calibration using a raw validation dataset"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory containing raw validation frame_*.csv/.bin/.npy/.json files",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to calibration_config.yaml",
    )
    parser.add_argument("--expected-mv", type=float, required=True)
    parser.add_argument("--expected-bpm", type=float, required=True)
    return parser.parse_args()


def load_validation_samples(input_dir: Path) -> tuple[np.ndarray, int, list[str]]:
    frames = find_complete_frame_sets(input_dir)
    if not frames:
        raise CalibrationError(f"no complete frame sets found in {input_dir}")

    arrays: list[np.ndarray] = []
    frame_stems: list[str] = []
    sample_rate_hz = 250

    for frame in frames:
        samples = np.load(frame.npy_path, allow_pickle=False)
        if samples.shape != EXPECTED_SHAPE:
            raise CalibrationError(
                f"{frame.npy_path.name} shape is {samples.shape}, expected {EXPECTED_SHAPE}"
            )
        if samples.dtype != EXPECTED_DTYPE:
            raise CalibrationError(
                f"{frame.npy_path.name} dtype is {samples.dtype}, expected {EXPECTED_DTYPE}"
            )

        metadata = load_json(frame.json_path)
        frame_sample_rate_hz = int(metadata.get("sample_rate_hz", sample_rate_hz))
        if frame_sample_rate_hz > 0:
            sample_rate_hz = frame_sample_rate_hz

        arrays.append(samples)
        frame_stems.append(frame.stem)

    return np.concatenate(arrays, axis=0), sample_rate_hz, frame_stems


def status_from_checks(
    amplitude_mV: float,
    expected_mV: float,
    bpm: float,
    expected_bpm: float,
    saturation_count: int,
    nan_inf_count: int,
    raw_ch3_error: float,
    mv_ch3_error: float,
    beats_accepted: int,
) -> tuple[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []

    if nan_inf_count:
        failures.append(f"{nan_inf_count} NaN/Inf values found")
    if saturation_count:
        failures.append(f"{saturation_count} saturated raw samples found")
    if raw_ch3_error > CH3_CONSISTENCY_TOLERANCE_COUNTS:
        failures.append(f"raw Lead III max error {raw_ch3_error:.6g} count")
    if mv_ch3_error > CH3_CONSISTENCY_TOLERANCE_MV:
        failures.append(f"mV Lead III max error {mv_ch3_error:.6g} mV")
    if beats_accepted < 2 or not np.isfinite(bpm):
        failures.append("BPM estimation found fewer than 2 R peaks")

    amplitude_relative_error = (
        abs(amplitude_mV - expected_mV) / expected_mV
        if np.isfinite(amplitude_mV)
        else float("inf")
    )
    bpm_error = abs(bpm - expected_bpm) if np.isfinite(bpm) else float("inf")

    if amplitude_relative_error > FAIL_AMPLITUDE_RELATIVE_ERROR:
        failures.append(
            f"reference amplitude error {amplitude_relative_error:.1%} exceeds "
            f"{FAIL_AMPLITUDE_RELATIVE_ERROR:.0%}"
        )
    elif amplitude_relative_error > PASS_AMPLITUDE_RELATIVE_ERROR:
        warnings.append(
            f"reference amplitude error {amplitude_relative_error:.1%} exceeds "
            f"{PASS_AMPLITUDE_RELATIVE_ERROR:.0%}"
        )

    if bpm_error > FAIL_BPM_ERROR:
        failures.append(f"BPM error {bpm_error:.3g} exceeds {FAIL_BPM_ERROR:g}")
    elif bpm_error > PASS_BPM_ERROR:
        warnings.append(f"BPM error {bpm_error:.3g} exceeds {PASS_BPM_ERROR:g}")

    if failures:
        return "FAIL", failures
    if warnings:
        return "WARNING", warnings
    return "PASS", []


def main() -> int:
    args = parse_args()

    try:
        if not np.isfinite(args.expected_mv) or args.expected_mv <= 0.0:
            raise CalibrationError("--expected-mv must be a finite positive value")
        if not np.isfinite(args.expected_bpm) or args.expected_bpm <= 0.0:
            raise CalibrationError("--expected-bpm must be a finite positive value")

        input_dir = args.input_dir.resolve()
        calibration = load_calibration_config(args.config.resolve())
        raw_samples, sample_rate_hz, frame_stems = load_validation_samples(input_dir)

        nan_inf_count = count_nan_inf(raw_samples)
        raw_ch3_error = ch3_max_error(raw_samples)
        saturation_count = count_saturation(raw_samples)

        if nan_inf_count or raw_ch3_error > CH3_CONSISTENCY_TOLERANCE_COUNTS:
            samples_mv = np.empty_like(raw_samples, dtype=np.float32)
            samples_mv.fill(np.nan)
            amplitude_mV = float("nan")
            amplitude_mad_mV = float("nan")
            bpm = float("nan")
            beats_detected = 0
            beats_accepted = 0
            beats_rejected = 0
            mv_ch3_error = float("nan")
        else:
            samples_mv = convert_raw_to_mv(raw_samples, calibration)
            rpeak = measure_rpeak_baseline_amplitude(
                samples=samples_mv,
                sample_rate_hz=sample_rate_hz,
                expected_bpm=float(args.expected_bpm),
                lead="lead_ii",
            )
            amplitude_mV = rpeak.median_amplitude
            amplitude_mad_mV = rpeak.amplitude_mad
            bpm = rpeak.estimated_bpm
            beats_detected = rpeak.beats_detected
            beats_accepted = rpeak.beats_accepted
            beats_rejected = rpeak.beats_rejected
            mv_ch3_error = ch3_max_error(samples_mv)

        amplitude_error_percent = (
            100.0 * (amplitude_mV - float(args.expected_mv)) / float(args.expected_mv)
            if np.isfinite(amplitude_mV)
            else float("inf")
        )
        bpm_error = bpm - float(args.expected_bpm) if np.isfinite(bpm) else float("inf")

        status, messages = status_from_checks(
            amplitude_mV=amplitude_mV,
            expected_mV=float(args.expected_mv),
            bpm=bpm,
            expected_bpm=float(args.expected_bpm),
            saturation_count=saturation_count,
            nan_inf_count=nan_inf_count,
            raw_ch3_error=raw_ch3_error,
            mv_ch3_error=mv_ch3_error,
            beats_accepted=beats_accepted,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, CalibrationError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print(f"validation_dataset: {input_dir}")
    print(f"frames_used: {', '.join(frame_stems)}")
    print(f"sample_rate_hz: {sample_rate_hz}")
    print(f"measured_reference_amplitude_mV: {amplitude_mV:.6g}")
    print(f"amplitude_mad_mV: {amplitude_mad_mV:.6g}")
    print(f"expected_reference_amplitude_mV: {float(args.expected_mv):.6g}")
    print(f"amplitude_error_percent: {amplitude_error_percent:.6g}")
    print(f"estimated_bpm: {bpm:.6g}")
    print(f"expected_bpm: {float(args.expected_bpm):.6g}")
    print(f"bpm_error: {bpm_error:.6g}")
    print(f"beats_detected: {beats_detected}")
    print(f"beats_accepted: {beats_accepted}")
    print(f"beats_rejected: {beats_rejected}")
    print(f"saturation_count: {saturation_count}")
    print(f"nan_inf_count: {nan_inf_count}")
    print(
        "lead_iii_consistency: "
        f"raw_max_error_count={raw_ch3_error:.6g}, "
        f"mv_max_error_mV={mv_ch3_error:.6g}"
    )
    print(f"result: {status}")
    for message in messages:
        print(f"{status}: {message}")

    return 0 if status in {"PASS", "WARNING"} else 1


if __name__ == "__main__":
    sys.exit(main())

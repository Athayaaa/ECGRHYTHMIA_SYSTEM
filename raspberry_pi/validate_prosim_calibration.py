"""Validate a ProSim calibration config against recorded raw datasets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import yaml

from ads1293_session import verify_persisted_session_metadata
from calibration_utils import (
    CH3_CONSISTENCY_TOLERANCE_COUNTS,
    CH3_CONSISTENCY_TOLERANCE_MV,
    CalibrationError,
    ch3_max_error,
    convert_raw_to_mv,
    count_nan_inf,
    find_complete_frame_sets,
    is_legacy_calibration,
    load_calibration_config,
    load_raw_frame,
    measure_rpeak_baseline_amplitude,
    offset_code_range_stats,
    require_calibration_compatible,
    require_corrected_frame_provenance,
    require_frame_session_link,
    require_frame_output_sample_rate,
)


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
        required=True,
        help="Explicit calibration YAML, including the explicit legacy file for legacy data",
    )
    parser.add_argument(
        "--session-metadata",
        type=Path,
        help=(
            "Verified receiver session JSON. Required for corrected V2 "
            "validation; explicit legacy validation does not require it."
        ),
    )
    parser.add_argument("--expected-mv", type=float, required=True)
    parser.add_argument("--expected-bpm", type=float, required=True)
    return parser.parse_args()


def load_validation_samples(
    input_dir: Path,
    calibration: dict,
    session_metadata: dict | None = None,
) -> tuple[np.ndarray, int, list[str]]:
    frames = find_complete_frame_sets(input_dir)
    if not frames:
        raise CalibrationError(f"no complete frame sets found in {input_dir}")

    arrays: list[np.ndarray] = []
    frame_stems: list[str] = []
    sample_rate_hz: int | None = None
    legacy = is_legacy_calibration(calibration)
    verified_session: dict | None = None
    if not legacy:
        if session_metadata is None:
            raise CalibrationError(
                "--session-metadata is required for corrected-pipeline ProSim "
                "validation"
            )
        verified_session = verify_persisted_session_metadata(session_metadata)
        require_calibration_compatible(calibration, verified_session)

    for frame in frames:
        samples, metadata = load_raw_frame(frame)
        if not legacy:
            assert verified_session is not None
            require_frame_session_link(metadata, verified_session)
            require_calibration_compatible(calibration, metadata)
            require_corrected_frame_provenance(
                metadata,
                require_signal_quality_pass=True,
            )
        frame_sample_rate_hz = require_frame_output_sample_rate(metadata, calibration)
        if sample_rate_hz is None:
            sample_rate_hz = frame_sample_rate_hz
        elif sample_rate_hz != frame_sample_rate_hz:
            raise CalibrationError(
                "all ProSim validation frames must use the same output sample rate"
            )

        arrays.append(samples)
        frame_stems.append(frame.stem)

    assert sample_rate_hz is not None
    return np.concatenate(arrays, axis=0), sample_rate_hz, frame_stems


def status_from_checks(
    amplitude_mV: float,
    expected_mV: float,
    bpm: float,
    expected_bpm: float,
    out_of_range_count: int,
    near_rail_count: int,
    nan_inf_count: int,
    raw_ch3_error: float,
    mv_ch3_error: float,
    beats_accepted: int,
) -> tuple[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []

    if nan_inf_count:
        failures.append(f"{nan_inf_count} NaN/Inf values found")
    if out_of_range_count:
        failures.append(
            f"{out_of_range_count} acquired Lead I/II code(s) outside 0..ADCMAX"
        )
    elif near_rail_count:
        warnings.append(
            f"{near_rail_count} acquired Lead I/II code(s) near an ADC rail"
        )
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
        calibration = load_calibration_config(args.config.resolve(), allow_legacy=True)
        session_metadata: dict | None = None
        if not is_legacy_calibration(calibration):
            if args.session_metadata is None:
                raise CalibrationError(
                    "--session-metadata is required for corrected-pipeline ProSim "
                    "validation"
                )
            with args.session_metadata.resolve().open("r", encoding="utf-8") as handle:
                loaded_session = json.load(handle)
            if not isinstance(loaded_session, dict):
                raise CalibrationError(
                    "--session-metadata must contain a JSON object"
                )
            session_metadata = loaded_session
        raw_samples, sample_rate_hz, frame_stems = load_validation_samples(
            input_dir,
            calibration,
            session_metadata,
        )

        nan_inf_count = count_nan_inf(raw_samples)
        raw_ch3_error = ch3_max_error(raw_samples)
        raw_range = offset_code_range_stats(raw_samples, adcmax=calibration["adcmax"])

        if (
            nan_inf_count
            or raw_range["out_of_range_count"]
            or raw_ch3_error > CH3_CONSISTENCY_TOLERANCE_COUNTS
        ):
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
            out_of_range_count=raw_range["out_of_range_count"],
            near_rail_count=raw_range["near_rail_count"],
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
    print(f"below_range_count: {raw_range['below_range_count']}")
    print(f"above_range_count: {raw_range['above_range_count']}")
    print(f"near_low_rail_count: {raw_range['near_low_rail_count']}")
    print(f"near_high_rail_count: {raw_range['near_high_rail_count']}")
    print(f"derived_lead_iii_range: {raw_range['derived_lead_iii']}")
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

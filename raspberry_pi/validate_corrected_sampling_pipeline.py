#!/usr/bin/env python3
"""Offline regression for the corrected ADS1293 sampling pipeline.

The synthetic mode requires no ProSim or connected hardware.  It verifies the
target register-derived constants, streams native samples in irregular chunks,
compares the result with SciPy's trusted offline polyphase reference, and
checks the canonical 2500 x 3 frame contract.

The receiver's native ``native_samples.csv`` is accepted directly; captured
``.npy`` and ``.npz`` files are also supported.  An NPZ may contain ``samples``
(or ``native_samples``), ``sequences``, ``flags``, and ``diagnostic`` arrays.
Captured NPY/NPZ data without complete packet flags and diagnostics is reported
as WARNING, never PASS, because acquisition integrity is unverifiable.  Only
the two measured channels are acquired; Lead III is always derived after
resampling.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from typing import Any

import numpy as np
from scipy import signal

from ads1293_session import firmware_compatibility_failures
from packet_parser import (
    V2_FLAG_ADS_ERROR,
    V2_FLAG_BACKLOG_OVERRUN,
    V2_FLAG_CODE_OUT_OF_RANGE,
    V2_FLAG_CONFIG_VERIFIED,
    V2_FLAG_PREVIOUS_TX_FAILURE,
    V2_FLAG_SELFTEST_ACTIVE,
)

from streaming_resampler import (
    DEFAULT_FRAME_SIZE,
    NATIVE_SAMPLE_RATE_HZ,
    OUTPUT_SAMPLE_RATE_HZ,
    RESAMPLE_DOWN,
    RESAMPLE_UP,
    StreamingECGFramePipeline,
    expected_output_count,
)


AFE_RES_HZ = 102_400
R1 = 4
R2 = 6
R3_CH1 = 16
R3_CH2 = 16
ADCMAX = 0xF30000
ZERO_CODE = ADCMAX // 2
VREF_NOMINAL_V = 2.4
ANALOG_GAIN = 3.5
PROTOCOL_VERSION = 2
ADC_ENCODING = "offset_binary_uint24"
DEFAULT_SATURATION_MARGIN_COUNTS = 8192
UINT32_MAX = 0xFFFFFFFF
UINT8_MAX = 0xFF
V2_KNOWN_FLAGS_MASK = (
    V2_FLAG_CONFIG_VERIFIED
    | V2_FLAG_ADS_ERROR
    | V2_FLAG_BACKLOG_OVERRUN
    | V2_FLAG_CODE_OUT_OF_RANGE
    | V2_FLAG_PREVIOUS_TX_FAILURE
    | V2_FLAG_SELFTEST_ACTIVE
)

CONFIG_REQUIRED_FIELDS = (
    "protocol_version",
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


@dataclass(frozen=True)
class NativeCaptureData:
    """Loaded native samples plus optional packet-integrity evidence.

    Receiver CSV captures are expected to carry ``flags`` and ``diagnostic``
    for every packet.  Plain NPY files cannot carry that evidence.  NPZ files
    may provide ``flags``/``packet_flags`` and
    ``diagnostic``/``diagnostics``/``packet_diagnostics`` arrays.
    """

    measured: np.ndarray
    sequences: np.ndarray | None
    packet_flags: np.ndarray | None
    packet_diagnostics: np.ndarray | None
    packet_diagnostics_required: bool
    source_format: str


def nominal_scale_mv_per_count() -> float:
    return (
        2.0 * VREF_NOMINAL_V * 1000.0
        / (ANALOG_GAIN * float(ADCMAX))
    )


def expected_configuration() -> dict[str, Any]:
    """Return target values derived from the corrected register setup."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "afe_res_hz": AFE_RES_HZ,
        "r1": R1,
        "r2": R2,
        "r3_ch1": R3_CH1,
        "r3_ch2": R3_CH2,
        "native_sample_rate_hz": NATIVE_SAMPLE_RATE_HZ,
        "output_sample_rate_hz": OUTPUT_SAMPLE_RATE_HZ,
        "resample_up": RESAMPLE_UP,
        "resample_down": RESAMPLE_DOWN,
        "adc_encoding": ADC_ENCODING,
        "adcmax": ADCMAX,
        "zero_code": ZERO_CODE,
        "vref_nominal_v": VREF_NOMINAL_V,
        "analog_gain": ANALOG_GAIN,
    }


def _firmware_config_view(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return config-dump fields from either a session or embedded config."""

    embedded = metadata.get("firmware_config")
    config = dict(embedded) if isinstance(embedded, dict) else dict(metadata)
    if "device_id" not in config and "ads1293_device_id" in config:
        config["device_id"] = config["ads1293_device_id"]
    return config


def verify_configuration(
    metadata: dict[str, Any],
    *,
    require_firmware_verification: bool = False,
) -> tuple[bool, list[str]]:
    """Strictly compare session metadata with the corrected target fingerprint."""

    expected = expected_configuration()
    errors: list[str] = []
    for field in CONFIG_REQUIRED_FIELDS:
        if field not in metadata:
            errors.append(f"missing configuration field: {field}")
            continue
        actual = metadata[field]
        wanted = expected[field]
        if isinstance(wanted, float):
            try:
                matches = bool(np.isclose(float(actual), wanted, rtol=0.0, atol=1e-9))
            except (TypeError, ValueError):
                matches = False
        else:
            matches = actual == wanted
        if not matches:
            errors.append(f"{field}: expected {wanted!r}, got {actual!r}")
    if require_firmware_verification:
        errors.extend(firmware_compatibility_failures(_firmware_config_view(metadata)))
    return not errors, errors


def generate_synthetic_native(duration_seconds: float) -> tuple[np.ndarray, np.ndarray]:
    """Generate deterministic, in-range two-lead offset-code test data."""

    if duration_seconds <= 0:
        raise ValueError("duration must be positive")
    count = int(round(duration_seconds * NATIVE_SAMPLE_RATE_HZ))
    time_s = np.arange(count, dtype=np.float64) / NATIVE_SAMPLE_RATE_HZ

    # Low-frequency morphology plus a 40 Hz component exercises both the ECG
    # passband and the anti-alias filter without approaching either ADC rail.
    lead_i = (
        ZERO_CODE
        + 18_000.0 * np.sin(2.0 * np.pi * 1.2 * time_s)
        + 2_000.0 * np.sin(2.0 * np.pi * 40.0 * time_s)
    )
    lead_ii = (
        ZERO_CODE
        + 25_000.0 * np.sin(2.0 * np.pi * 1.2 * time_s + 0.18)
        + 3_000.0 * np.sin(2.0 * np.pi * 40.0 * time_s + 0.35)
    )
    measured = np.rint(np.column_stack((lead_i, lead_ii))).astype(np.uint32)
    sequences = np.arange(count, dtype=np.uint32)
    return measured, sequences


def _parse_capture_integer(value: Any) -> int:
    text = str(value).strip()
    return int(text, 16 if text.lower().startswith("0x") else 10)


def _validated_integer_array(
    values: Any,
    *,
    field: str,
    expected_count: int,
    minimum: int,
    maximum: int,
    dtype: np.dtype | type,
) -> np.ndarray:
    """Validate integer semantics before narrowing to an unsigned dtype."""

    array = np.asarray(values)
    if array.ndim != 1 or array.size != expected_count:
        raise ValueError(
            f"capture {field} must contain exactly {expected_count} values"
        )

    validated: list[int] = []
    for index, raw_value in enumerate(array.tolist()):
        if isinstance(raw_value, (bool, np.bool_)):
            raise ValueError(f"capture {field}[{index}] must be an integer")
        if isinstance(raw_value, (int, np.integer)):
            value = int(raw_value)
        elif isinstance(raw_value, (float, np.floating)):
            numeric = float(raw_value)
            if not math.isfinite(numeric) or not numeric.is_integer():
                raise ValueError(
                    f"capture {field}[{index}] must be a finite integer"
                )
            value = int(numeric)
        else:
            try:
                value = _parse_capture_integer(raw_value)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"capture {field}[{index}] must be an integer"
                ) from error
        if not minimum <= value <= maximum:
            raise ValueError(
                f"capture {field}[{index}]={value} is outside "
                f"{minimum}..{maximum}"
            )
        validated.append(value)
    return np.asarray(validated, dtype=dtype)


def _archive_array(
    archive: Any,
    names: tuple[str, ...],
) -> np.ndarray | None:
    for name in names:
        if name in archive:
            return archive[name]
    return None


def load_native_capture_data(path: Path) -> NativeCaptureData:
    """Load measured leads and all available packet-integrity evidence."""

    packet_flags = None
    packet_diagnostics = None
    packet_diagnostics_required = False
    source_format = path.suffix.lower().lstrip(".")

    if path.suffix.lower() == ".npz":
        with np.load(path, allow_pickle=False) as archive:
            if "samples" in archive:
                samples = archive["samples"]
            elif "native_samples" in archive:
                samples = archive["native_samples"]
            else:
                raise ValueError("NPZ must contain 'samples' or 'native_samples'")
            sequences = archive["sequences"] if "sequences" in archive else None
            packet_flags = _archive_array(archive, ("flags", "packet_flags"))
            packet_diagnostics = _archive_array(
                archive,
                ("diagnostic", "diagnostics", "packet_diagnostics"),
            )
    elif path.suffix.lower() == ".npy":
        samples = np.load(path, allow_pickle=False)
        sequences = None
    elif path.suffix.lower() == ".csv":
        required_columns = {
            "native_sequence",
            "lead_i_offset_code",
            "lead_ii_offset_code",
        }
        measured_rows: list[tuple[int, int]] = []
        sequence_rows: list[int] = []
        flag_rows: list[int] | None = []
        diagnostic_rows: list[int] | None = []
        packet_diagnostics_required = True
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            missing = sorted(required_columns - fields)
            if missing:
                raise ValueError(
                    "native CSV is missing required column(s): " + ", ".join(missing)
                )
            if "flags" not in fields:
                flag_rows = None
            if "diagnostic" not in fields:
                diagnostic_rows = None
            for row_number, row in enumerate(reader, start=2):
                try:
                    sequence = _parse_capture_integer(row["native_sequence"])
                    lead_i = _parse_capture_integer(row["lead_i_offset_code"])
                    lead_ii = _parse_capture_integer(row["lead_ii_offset_code"])
                    flags = (
                        _parse_capture_integer(row["flags"])
                        if flag_rows is not None
                        else None
                    )
                    diagnostic = (
                        _parse_capture_integer(row["diagnostic"])
                        if diagnostic_rows is not None
                        else None
                    )
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"native CSV row {row_number} contains a non-integer value"
                    ) from error
                if not 0 <= sequence <= 0xFFFFFFFF:
                    raise ValueError(
                        f"native CSV row {row_number} sequence is outside uint32"
                )
                sequence_rows.append(sequence)
                measured_rows.append((lead_i, lead_ii))
                if flag_rows is not None:
                    assert flags is not None
                    flag_rows.append(flags)
                if diagnostic_rows is not None:
                    assert diagnostic is not None
                    diagnostic_rows.append(diagnostic)
        if not measured_rows:
            raise ValueError("native CSV contains no samples")
        samples = np.asarray(measured_rows, dtype=np.int64)
        sequences = sequence_rows
        packet_flags = flag_rows
        packet_diagnostics = diagnostic_rows
    else:
        raise ValueError("captured input must be .csv, .npy, or .npz")

    samples = np.asarray(samples)
    if samples.ndim != 2 or samples.shape[1] < 2:
        raise ValueError(f"native samples must have shape (N, >=2), got {samples.shape}")
    measured = samples[:, :2]
    if sequences is not None:
        sequences = _validated_integer_array(
            sequences,
            field="sequences",
            expected_count=measured.shape[0],
            minimum=0,
            maximum=UINT32_MAX,
            dtype=np.uint32,
        )
    if packet_flags is not None:
        packet_flags = _validated_integer_array(
            packet_flags,
            field="flags",
            expected_count=measured.shape[0],
            minimum=0,
            maximum=UINT8_MAX,
            dtype=np.uint8,
        )
    if packet_diagnostics is not None:
        packet_diagnostics = _validated_integer_array(
            packet_diagnostics,
            field="diagnostic",
            expected_count=measured.shape[0],
            minimum=0,
            maximum=UINT32_MAX,
            dtype=np.uint32,
        )
    return NativeCaptureData(
        measured=measured,
        sequences=sequences,
        packet_flags=packet_flags,
        packet_diagnostics=packet_diagnostics,
        packet_diagnostics_required=packet_diagnostics_required,
        source_format=source_format,
    )


def load_native_capture(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Backward-compatible measured-lead/sequence capture loader."""

    capture = load_native_capture_data(path)
    return capture.measured, capture.sequences


def _random_chunk_sizes(total: int, seed: int = 1293) -> list[int]:
    rng = np.random.default_rng(seed)
    sizes: list[int] = []
    remaining = int(total)
    while remaining:
        size = min(remaining, int(rng.integers(1, 193)))
        sizes.append(size)
        remaining -= size
    return sizes


def run_regression(
    measured: np.ndarray,
    sequences: np.ndarray | None,
    configuration: dict[str, Any],
    *,
    synthetic: bool,
    saturation_margin_counts: int = DEFAULT_SATURATION_MARGIN_COUNTS,
    packet_flags: np.ndarray | None = None,
    packet_diagnostics: np.ndarray | None = None,
    require_packet_diagnostics: bool = False,
) -> dict[str, Any]:
    """Run chunked streaming validation and return a JSON-serializable report."""

    measured = np.asarray(measured)
    if measured.ndim != 2 or measured.shape[1] != 2:
        raise ValueError(f"measured must have shape (N, 2), got {measured.shape}")
    native_count = int(measured.shape[0])
    saturation_margin_counts = int(saturation_margin_counts)
    if saturation_margin_counts < 0 or saturation_margin_counts >= ADCMAX // 2:
        raise ValueError(
            "saturation_margin_counts must be between 0 and ADCMAX/2"
        )
    config_ok, config_errors = verify_configuration(
        configuration,
        require_firmware_verification=not synthetic,
    )

    sequence_metadata_available = sequences is not None
    if sequences is None:
        # A plain NPY can still exercise DSP deterministically, but generated
        # continuity must be reported rather than mistaken for packet evidence.
        sequences = np.arange(native_count, dtype=np.uint32)
    else:
        sequences = _validated_integer_array(
            sequences,
            field="sequences",
            expected_count=native_count,
            minimum=0,
            maximum=UINT32_MAX,
            dtype=np.uint32,
        )

    flags_available = packet_flags is not None
    diagnostics_available = packet_diagnostics is not None
    if packet_flags is not None:
        packet_flags = _validated_integer_array(
            packet_flags,
            field="flags",
            expected_count=native_count,
            minimum=0,
            maximum=UINT8_MAX,
            dtype=np.uint8,
        )
    if packet_diagnostics is not None:
        packet_diagnostics = _validated_integer_array(
            packet_diagnostics,
            field="diagnostic",
            expected_count=native_count,
            minimum=0,
            maximum=UINT32_MAX,
            dtype=np.uint32,
        )

    packet_integrity = {
        "required": bool(require_packet_diagnostics),
        "flags_available": flags_available,
        "diagnostic_available": diagnostics_available,
        "complete": flags_available and diagnostics_available,
        "config_verified_clear_count": 0,
        "ads_error_flag_count": 0,
        "backlog_overrun_flag_count": 0,
        "code_out_of_range_flag_count": 0,
        "previous_tx_failure_flag_count": 0,
        "selftest_active_flag_count": 0,
        "reserved_flag_count": 0,
        "nonzero_error_status_count": 0,
        "nonzero_diagnostic_count": 0,
    }
    if packet_flags is not None:
        flags_u16 = packet_flags.astype(np.uint16, copy=False)
        packet_integrity.update(
            {
                "config_verified_clear_count": int(
                    np.count_nonzero(
                        (flags_u16 & V2_FLAG_CONFIG_VERIFIED) == 0
                    )
                ),
                "ads_error_flag_count": int(
                    np.count_nonzero(flags_u16 & V2_FLAG_ADS_ERROR)
                ),
                "backlog_overrun_flag_count": int(
                    np.count_nonzero(flags_u16 & V2_FLAG_BACKLOG_OVERRUN)
                ),
                "code_out_of_range_flag_count": int(
                    np.count_nonzero(flags_u16 & V2_FLAG_CODE_OUT_OF_RANGE)
                ),
                "previous_tx_failure_flag_count": int(
                    np.count_nonzero(flags_u16 & V2_FLAG_PREVIOUS_TX_FAILURE)
                ),
                "selftest_active_flag_count": int(
                    np.count_nonzero(flags_u16 & V2_FLAG_SELFTEST_ACTIVE)
                ),
                "reserved_flag_count": int(
                    np.count_nonzero(flags_u16 & (~V2_KNOWN_FLAGS_MASK & UINT8_MAX))
                ),
            }
        )
    if packet_diagnostics is not None:
        packet_integrity.update(
            {
                "nonzero_error_status_count": int(
                    np.count_nonzero(packet_diagnostics & UINT8_MAX)
                ),
                "nonzero_diagnostic_count": int(
                    np.count_nonzero(packet_diagnostics)
                ),
            }
        )

    pipeline = StreamingECGFramePipeline()
    completed_frames: list[np.ndarray] = []
    loss_events = []
    offset = 0
    for size in _random_chunk_sizes(native_count):
        result = pipeline.process(
            measured[offset : offset + size],
            sequences[offset : offset + size],
        )
        completed_frames.extend(result.frames)
        loss_events.extend(result.loss_events)
        offset += size
    completed_frames.extend(pipeline.finalize())

    pending = pipeline.frames.pending_snapshot()
    output_parts: list[np.ndarray] = list(completed_frames)
    if pending.shape[0]:
        pending_measured = pending[:, :2].astype(np.float32, copy=False)
        output_parts.append(
            np.column_stack(
                (
                    pending_measured,
                    pending_measured[:, 1] - pending_measured[:, 0],
                )
            )
        )
    output = (
        np.concatenate(output_parts, axis=0)
        if output_parts
        else np.empty((0, 3), dtype=np.float32)
    )

    expected_count = expected_output_count(native_count)
    reference_measured = (
        signal.resample_poly(
            measured.astype(np.float64) - ZERO_CODE,
            RESAMPLE_UP,
            RESAMPLE_DOWN,
            axis=0,
        )
        + ZERO_CODE
    )
    comparable = min(output.shape[0], reference_measured.shape[0])
    reference_error = (
        float(
            np.max(
                np.abs(
                    output[:comparable, :2].astype(np.float64)
                    - reference_measured[:comparable]
                )
            )
        )
        if comparable
        else 0.0
    )

    below_range = int(np.count_nonzero(measured < 0))
    above_range = int(np.count_nonzero(measured > ADCMAX))
    near_low = int(
        np.count_nonzero(
            (measured >= 0) & (measured <= saturation_margin_counts)
        )
    )
    near_high = int(
        np.count_nonzero(
            (measured >= ADCMAX - saturation_margin_counts)
            & (measured <= ADCMAX)
        )
    )
    finite = bool(np.isfinite(measured).all() and np.isfinite(output).all())
    lead_iii_error = (
        float(
            np.max(
                np.abs(
                    output[:, 2]
                    - (output[:, 1] - output[:, 0])
                )
            )
        )
        if output.shape[0]
        else 0.0
    )

    failures: list[str] = []
    warnings: list[str] = []
    if not config_ok:
        failures.extend(config_errors)
    if not finite:
        failures.append("native or output data contains NaN/Inf")
    if below_range or above_range:
        failures.append(
            f"raw offset-code range violations: below={below_range}, above={above_range}"
        )
    if near_low or near_high:
        warnings.append(
            "measured channel near ADC rail: "
            f"low={near_low}, high={near_high}, "
            f"margin={saturation_margin_counts} counts"
        )
    if loss_events:
        failures.append(
            f"native packet loss detected: {sum(e.missing_native_samples for e in loss_events)}"
        )
    if not synthetic and not packet_integrity["complete"]:
        missing_evidence = []
        if not flags_available:
            missing_evidence.append("flags")
        if not diagnostics_available:
            missing_evidence.append("diagnostic")
        message = (
            "capture has no complete packet-integrity evidence "
            f"({', '.join(missing_evidence)} missing); ADS errors, self-test, "
            "backlog, and transmission failures are unverifiable"
        )
        if require_packet_diagnostics:
            failures.append("receiver CSV " + message)
        else:
            warnings.append(message)
    if not synthetic and packet_flags is not None:
        flag_failures = (
            ("config_verified_clear_count", "CONFIG_VERIFIED clear"),
            ("ads_error_flag_count", "ADS_ERROR set"),
            ("backlog_overrun_flag_count", "BACKLOG_OVERRUN set"),
            ("code_out_of_range_flag_count", "CODE_OUT_OF_RANGE set"),
            ("previous_tx_failure_flag_count", "PREVIOUS_TX_FAILURE set"),
            ("selftest_active_flag_count", "SELFTEST_ACTIVE set"),
            ("reserved_flag_count", "reserved flag bits set"),
        )
        for count_key, description in flag_failures:
            count = int(packet_integrity[count_key])
            if count:
                failures.append(f"{count} captured packet(s) have {description}")
    if not synthetic and packet_diagnostics is not None:
        error_count = int(packet_integrity["nonzero_error_status_count"])
        if error_count:
            failures.append(
                f"{error_count} captured packet(s) have nonzero ADS1293 error diagnostic"
            )
    if synthetic and output.shape[0] != expected_count:
        failures.append(
            f"output count mismatch: expected {expected_count}, got {output.shape[0]}"
        )
    if synthetic and len(completed_frames) != expected_count // DEFAULT_FRAME_SIZE:
        failures.append(
            "completed-frame mismatch: expected "
            f"{expected_count // DEFAULT_FRAME_SIZE}, got {len(completed_frames)}"
        )
    if lead_iii_error > 1e-5:
        failures.append(f"Lead III consistency error is {lead_iii_error:.9g} count")
    # Output frames intentionally use float32.  At an 8-million-count offset,
    # <= 1 count is the appropriate comparison tolerance after quantization.
    if reference_error > 1.0:
        failures.append(f"SciPy reference error is {reference_error:.9g} count")
    if not sequence_metadata_available and not synthetic:
        warnings.append("capture has no native sequence array; packet loss is unverifiable")

    status = "FAIL" if failures else ("WARNING" if warnings else "PASS")
    stats = pipeline.resampler.stats
    frame_stats = pipeline.frames.stats
    report: dict[str, Any] = {
        "status": status,
        "mode": "synthetic" if synthetic else "captured",
        "configuration": configuration,
        "configuration_verified": config_ok,
        "configuration_verification_scope": (
            "synthetic_expected_values"
            if synthetic
            else "firmware_register_readback_session_metadata"
        ),
        "native_input_samples": native_count,
        "native_input_duration_seconds": native_count / NATIVE_SAMPLE_RATE_HZ,
        "native_sample_rate_hz": NATIVE_SAMPLE_RATE_HZ,
        "expected_output_samples": expected_count,
        "actual_output_samples": int(output.shape[0]),
        "output_sample_rate_hz": OUTPUT_SAMPLE_RATE_HZ,
        "completed_frames": len(completed_frames),
        "frame_size": DEFAULT_FRAME_SIZE,
        "partial_output_samples": frame_stats.partial_samples,
        "packet_loss_events": len(loss_events),
        "missing_native_samples": sum(e.missing_native_samples for e in loss_events),
        "resampler_resets": stats.resampler_reset_count,
        "resampler_phase": stats.current_phase,
        "filter_delay_output_samples": stats.filter_delay_output_samples,
        "filter_delay_seconds": stats.filter_delay_output_samples / OUTPUT_SAMPLE_RATE_HZ,
        "filter_warmup_discard_count": stats.filter_warmup_discard_count,
        "conversion": {
            "calibration_source": "datasheet_nominal",
            "calibration_status": "preliminary",
            "adcmax": ADCMAX,
            "zero_code": ZERO_CODE,
            "vref_nominal_v": VREF_NOMINAL_V,
            "analog_gain": ANALOG_GAIN,
            "scale_mV_per_count": nominal_scale_mv_per_count(),
        },
        "raw_range_violations": {
            "below_zero": below_range,
            "above_adcmax": above_range,
            "near_low_rail": near_low,
            "near_high_rail": near_high,
            "saturation_margin_counts": saturation_margin_counts,
        },
        "all_values_finite": finite,
        "lead_iii_max_consistency_error_count": lead_iii_error,
        "scipy_reference_max_error_count": reference_error,
        "metadata_complete": not config_errors,
        "sequence_metadata_available": sequence_metadata_available or synthetic,
        "packet_integrity": packet_integrity,
        "warnings": warnings,
        "failures": failures,
    }
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate corrected 266.667-to-250 Hz ADS1293 sampling",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--synthetic",
        action="store_true",
        help="Run deterministic validation without hardware or ProSim",
    )
    source.add_argument(
        "--input",
        type=Path,
        help=(
            "Native receiver CSV or .npy/.npz capture "
            "(first two array columns are measured leads)"
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Synthetic duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--session-metadata",
        type=Path,
        help="Verified session JSON required to validate a captured configuration",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report as JSON",
    )
    parser.add_argument(
        "--saturation-margin-counts",
        type=int,
        default=DEFAULT_SATURATION_MARGIN_COUNTS,
        help=(
            "Near-rail warning margin for measured Lead I/II offset codes "
            f"(default: {DEFAULT_SATURATION_MARGIN_COUNTS})"
        ),
    )
    return parser.parse_args(argv)


def _print_human_report(report: dict[str, Any]) -> None:
    print("Corrected ADS1293 sampling regression")
    print(f"status: {report['status']}")
    print(
        "configuration: "
        f"{'verified' if report['configuration_verified'] else 'NOT VERIFIED'} "
        f"({report['configuration_verification_scope']})"
    )
    print(
        "native: "
        f"{report['native_input_samples']} samples, "
        f"{report['native_input_duration_seconds']:.6f} s, "
        f"{report['native_sample_rate_hz']:.12g} Hz"
    )
    print(
        "output: "
        f"{report['actual_output_samples']}/{report['expected_output_samples']} samples, "
        f"{report['output_sample_rate_hz']:.12g} Hz, "
        f"{report['completed_frames']} complete frame(s)"
    )
    print(
        "loss/reset: "
        f"events={report['packet_loss_events']} "
        f"missing={report['missing_native_samples']} "
        f"resets={report['resampler_resets']}"
    )
    packet_integrity = report["packet_integrity"]
    print(
        "packet integrity: "
        f"complete={packet_integrity['complete']} "
        f"config-clear={packet_integrity['config_verified_clear_count']} "
        f"ads-error={packet_integrity['ads_error_flag_count']} "
        f"backlog={packet_integrity['backlog_overrun_flag_count']} "
        f"range-flag={packet_integrity['code_out_of_range_flag_count']} "
        f"tx-failure={packet_integrity['previous_tx_failure_flag_count']} "
        f"selftest={packet_integrity['selftest_active_flag_count']} "
        f"reserved={packet_integrity['reserved_flag_count']} "
        f"error-diagnostic={packet_integrity['nonzero_error_status_count']}"
    )
    print(
        "DSP: "
        f"15/16 polyphase FIR, delay={report['filter_delay_output_samples']} samples "
        f"({report['filter_delay_seconds'] * 1000.0:.1f} ms), "
        f"SciPy max error={report['scipy_reference_max_error_count']:.6g} count"
    )
    print(
        "range/Lead III: "
        f"below={report['raw_range_violations']['below_zero']} "
        f"above={report['raw_range_violations']['above_adcmax']} "
        f"near-low={report['raw_range_violations']['near_low_rail']} "
        f"near-high={report['raw_range_violations']['near_high_rail']} "
        f"Lead III error={report['lead_iii_max_consistency_error_count']:.6g} count"
    )
    conversion = report["conversion"]
    print(
        "conversion: "
        f"{conversion['calibration_source']} ({conversion['calibration_status']}), "
        f"ADCMAX={conversion['adcmax']} zero={conversion['zero_code']} "
        f"scale={conversion['scale_mV_per_count']:.12g} mV/count"
    )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    for failure in report["failures"]:
        print(f"FAIL: {failure}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.synthetic:
            measured, sequences = generate_synthetic_native(args.duration)
            packet_flags = None
            packet_diagnostics = None
            require_packet_diagnostics = False
            configuration = expected_configuration()
            synthetic = True
        else:
            capture = load_native_capture_data(args.input)
            measured = capture.measured
            sequences = capture.sequences
            packet_flags = capture.packet_flags
            packet_diagnostics = capture.packet_diagnostics
            require_packet_diagnostics = capture.packet_diagnostics_required
            if args.session_metadata is None:
                configuration = {}
            else:
                configuration = json.loads(
                    args.session_metadata.read_text(encoding="utf-8")
                )
            synthetic = False

        report = run_regression(
            measured,
            sequences,
            configuration,
            synthetic=synthetic,
            saturation_margin_counts=args.saturation_margin_counts,
            packet_flags=packet_flags,
            packet_diagnostics=packet_diagnostics,
            require_packet_diagnostics=require_packet_diagnostics,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human_report(report)
    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())

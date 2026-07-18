"""Utilities for preparing calibrated mV frames as model-ready NPY inputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from calibration_utils import (
    CHANNEL_ORDER,
    CH3_CONSISTENCY_TOLERANCE_MV,
    CORRECTED_PIPELINE_PROFILE,
    CalibrationError,
    EXPECTED_DTYPE,
    EXPECTED_SHAPE,
    LEGACY_PIPELINE_PROFILE,
    ch3_max_error,
    count_nan_inf,
    require_corrected_frame_provenance,
    validate_calibration_compatibility,
)


MODEL_INPUT_SHAPE = EXPECTED_SHAPE
MODEL_INPUT_DTYPE = EXPECTED_DTYPE
MODEL_INPUT_UNIT = "mV"
MODEL_SAMPLE_RATE_HZ = 250
MODEL_DURATION_SECONDS = 10.0
DEFAULT_MAX_ABS_MV = 10.0
ALLOWED_NOOP_STEPS = {"identity", "none", "pass_through", "passthrough"}


class ModelInputError(Exception):
    pass


@dataclass(frozen=True)
class ModelInputPaths:
    npy_path: Path
    metadata_path: Path


@dataclass(frozen=True)
class ValidationReport:
    status: str
    failures: list[str]
    warnings: list[str]
    shape: tuple[int, ...]
    dtype: str
    nan_inf_count: int
    max_abs_mV: float
    lead_iii_max_error_mV: float
    lead_stats: dict[str, dict[str, float]]


@dataclass(frozen=True)
class PreparedModelInput:
    frame_id: str
    paths: ModelInputPaths
    metadata: dict[str, Any]
    validation: ValidationReport


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ModelInputError(f"metadata file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ModelInputError(f"{path} does not contain a JSON object")
    return data


def load_yaml_or_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ModelInputError(f"preprocessing config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix.lower() == ".json":
            data = json.load(handle)
        else:
            data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ModelInputError(f"{path} does not contain a config object")
    return data


def normalize_step_name(step: object) -> str:
    if isinstance(step, str):
        return step.strip().lower()
    if isinstance(step, dict):
        raw_name = step.get("name", step.get("step", step.get("type", "")))
        return str(raw_name).strip().lower()
    return str(step).strip().lower()


def load_preprocessing_config(path: Path | None) -> tuple[list[str], dict[str, Any] | None]:
    if path is None:
        return [], None

    config = load_yaml_or_json(path)
    raw_steps = config.get("steps", config.get("preprocessing_steps", []))
    if raw_steps is None:
        raw_steps = []
    if not isinstance(raw_steps, list):
        raise ModelInputError("preprocessing steps must be a list")

    steps_applied: list[str] = []
    for raw_step in raw_steps:
        name = normalize_step_name(raw_step)
        if not name:
            continue
        if name not in ALLOWED_NOOP_STEPS:
            raise ModelInputError(
                "Phase 3G model input preserves mV values; unsupported "
                f"preprocessing step {name!r}"
            )
        if name not in {"none"}:
            steps_applied.append("identity")

    return steps_applied, config


def frame_id_from_input(path: Path) -> str:
    stem = path.stem
    if stem.endswith("_mv"):
        return stem[: -len("_mv")]
    if stem.endswith("_input"):
        return stem[: -len("_input")]
    return stem


def output_paths_for_frame(output_dir: Path, frame_id: str) -> ModelInputPaths:
    return ModelInputPaths(
        npy_path=output_dir / f"{frame_id}_input.npy",
        metadata_path=output_dir / f"{frame_id}_input.json",
    )


def lead_statistics(samples: np.ndarray) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for index, label in enumerate(("lead_i", "lead_ii", "lead_iii")):
        if samples.ndim != 2 or samples.shape[1] <= index:
            finite = np.asarray([], dtype=np.float32)
        else:
            values = samples[:, index]
            finite = values[np.isfinite(values)]
        if finite.size:
            stats[label] = {
                "min_mV": float(np.min(finite)),
                "max_mV": float(np.max(finite)),
                "mean_mV": float(np.mean(finite)),
                "std_mV": float(np.std(finite)),
            }
        else:
            stats[label] = {
                "min_mV": float("nan"),
                "max_mV": float("nan"),
                "mean_mV": float("nan"),
                "std_mV": float("nan"),
            }
    return stats


def validate_model_samples(
    samples: np.ndarray,
    max_abs_mV: float,
    require_float32: bool = True,
) -> ValidationReport:
    failures: list[str] = []
    warnings: list[str] = []

    if samples.shape != MODEL_INPUT_SHAPE:
        failures.append(f"shape {samples.shape} != {MODEL_INPUT_SHAPE}")

    if require_float32 and samples.dtype != MODEL_INPUT_DTYPE:
        failures.append(f"dtype {samples.dtype} != {MODEL_INPUT_DTYPE}")

    if not np.isfinite(max_abs_mV) or max_abs_mV <= 0.0:
        failures.append("--max-abs-mv must be finite and positive")

    nan_inf_count = count_nan_inf(samples)
    if nan_inf_count:
        failures.append(f"{nan_inf_count} NaN/Inf values")

    finite = samples[np.isfinite(samples)]
    max_abs = float(np.max(np.abs(finite))) if finite.size else float("nan")
    if finite.size and np.isfinite(max_abs_mV):
        if max_abs > max_abs_mV:
            failures.append(f"maximum absolute mV {max_abs:.6g} exceeds {max_abs_mV:.6g}")
        elif max_abs > 0.8 * max_abs_mV:
            warnings.append(
                f"maximum absolute mV {max_abs:.6g} is above 80% of sanity limit"
            )

    lead_iii_error = ch3_max_error(samples.astype(np.float32, copy=False))
    if lead_iii_error > CH3_CONSISTENCY_TOLERANCE_MV:
        failures.append(
            f"Lead III max error {lead_iii_error:.6g} mV exceeds "
            f"{CH3_CONSISTENCY_TOLERANCE_MV:.6g}"
        )

    status = "FAIL" if failures else "WARNING" if warnings else "PASS"
    return ValidationReport(
        status=status,
        failures=failures,
        warnings=warnings,
        shape=tuple(int(value) for value in samples.shape),
        dtype=str(samples.dtype),
        nan_inf_count=nan_inf_count,
        max_abs_mV=max_abs,
        lead_iii_max_error_mV=lead_iii_error,
        lead_stats=lead_statistics(samples),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ModelInputError(f"source metadata {field} must be a SHA-256 hex digest")
    normalized = value.lower()
    if any(character not in "0123456789abcdef" for character in normalized):
        raise ModelInputError(f"source metadata {field} must be a SHA-256 hex digest")
    return normalized


def require_pass_raw_range(metadata: dict[str, Any], label: str) -> dict[str, Any]:
    raw_range = metadata.get("raw_range_validation")
    if not isinstance(raw_range, dict):
        raise ModelInputError(f"{label} is missing raw_range_validation")
    if str(raw_range.get("status", "")).upper() != "PASS":
        raise ModelInputError(f"{label} raw_range_validation status must be PASS")
    for field in ("out_of_range_count", "near_rail_count"):
        value = raw_range.get(field)
        if isinstance(value, bool):
            raise ModelInputError(f"{label} raw_range_validation.{field} is invalid")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as error:
            raise ModelInputError(
                f"{label} raw_range_validation.{field} is invalid"
            ) from error
        if not np.isfinite(numeric) or not numeric.is_integer() or int(numeric) != 0:
            raise ModelInputError(
                f"{label} raw_range_validation.{field} must be zero"
            )
    return raw_range


def validate_calibrated_metadata(
    metadata: dict[str, Any],
    *,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Require explicit calibration and acquisition provenance.

    A shape-compatible NPY plus an empty JSON object is not a model-ready ECG
    artifact. Corrected frames must retain a verified V2 session link and PASS
    per-frame acquisition/range status. Historical V1 frames are accepted only
    through an explicit legacy opt-in.
    """

    unit = metadata.get("unit")
    if unit != MODEL_INPUT_UNIT:
        raise ModelInputError(f"source metadata unit is {unit!r}, expected mV")

    sample_rate_hz = metadata.get("sample_rate_hz")
    try:
        numeric_sample_rate = float(sample_rate_hz)
    except (TypeError, ValueError) as error:
        raise ModelInputError("source metadata sample_rate_hz is missing or invalid") from error
    if numeric_sample_rate != float(MODEL_SAMPLE_RATE_HZ):
        raise ModelInputError(
            f"source metadata sample_rate_hz is {sample_rate_hz}, expected {MODEL_SAMPLE_RATE_HZ}"
        )

    channel_order = metadata.get("channel_order")
    if not isinstance(channel_order, list) or channel_order != CHANNEL_ORDER:
        raise ModelInputError(
            f"source metadata channel_order is {channel_order}, expected {CHANNEL_ORDER}"
        )

    calibration = metadata.get("calibration")
    if not isinstance(calibration, dict):
        raise ModelInputError("source metadata is missing calibration provenance")
    profile = calibration.get("pipeline_profile")
    compatibility = calibration.get("compatibility")
    if not isinstance(compatibility, dict) or compatibility.get("compatible") is not True:
        raise ModelInputError("source calibration compatibility is missing or not compatible")
    reasons = compatibility.get("reasons", [])
    if not isinstance(reasons, list):
        raise ModelInputError("source calibration compatibility reasons must be a list")

    if profile == CORRECTED_PIPELINE_PROFILE:
        if reasons:
            raise ModelInputError(
                "corrected source calibration compatibility contains failure reasons"
            )
        require_pass_raw_range(metadata, "corrected calibrated frame")
        source_metadata = metadata.get("source_metadata")
        if not isinstance(source_metadata, dict):
            raise ModelInputError("corrected calibrated frame is missing source_metadata")
        session_id = source_metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ModelInputError("corrected source metadata is missing session_id")
        if source_metadata.get("protocol_version") != 2:
            raise ModelInputError("corrected source metadata protocol_version must be 2")
        firmware_commit = source_metadata.get("firmware_commit")
        if not isinstance(firmware_commit, str) or not firmware_commit.strip():
            raise ModelInputError("corrected source metadata is missing firmware_commit")
        try:
            provenance = require_corrected_frame_provenance(
                source_metadata,
                require_signal_quality_pass=True,
            )
        except (TypeError, ValueError, CalibrationError) as error:
            raise ModelInputError(str(error)) from error

        calibration_source = calibration.get("calibration_source")
        calibration_status = str(calibration.get("calibration_status", "")).lower()
        valid_source_status = (
            calibration_source == "datasheet_nominal"
            and calibration_status == "preliminary"
        ) or (
            calibration_source == "prosim"
            and calibration_status == "calibrated"
        )
        if not valid_source_status:
            raise ModelInputError(
                "corrected calibration source/status must be "
                "datasheet_nominal/preliminary or prosim/calibrated"
            )

        acquisition_fingerprint = calibration.get("acquisition_fingerprint")
        if not isinstance(acquisition_fingerprint, dict) or not acquisition_fingerprint:
            raise ModelInputError("corrected calibration is missing acquisition fingerprint")
        try:
            fingerprint_result = validate_calibration_compatibility(
                acquisition_fingerprint,
                source_metadata,
                strict=True,
            )
        except (TypeError, ValueError, CalibrationError) as error:
            raise ModelInputError(
                f"invalid corrected calibration fingerprint: {error}"
            ) from error
        if not fingerprint_result.compatible:
            raise ModelInputError(
                "corrected calibration fingerprint does not match source frame: "
                + "; ".join(fingerprint_result.reasons)
            )
        return {
            "pipeline_profile": profile,
            "legacy_explicit": False,
            "session_id": session_id,
            "firmware_commit": firmware_commit,
            "protocol_version": 2,
            "acquisition_status": provenance["acquisition_status"],
            "signal_quality_status": provenance["signal_quality_status"],
            "calibration_status": calibration_status,
            "calibration_source": calibration_source,
            "acquisition_fingerprint": acquisition_fingerprint,
        }

    if profile == LEGACY_PIPELINE_PROFILE:
        if not allow_legacy:
            raise ModelInputError(
                "legacy calibrated frame requires explicit allow_legacy=True/--allow-legacy"
            )
        if compatibility.get("status") != "LEGACY_EXPLICIT":
            raise ModelInputError("legacy calibration was not marked LEGACY_EXPLICIT")
        return {
            "pipeline_profile": profile,
            "legacy_explicit": True,
            "session_id": None,
            "firmware_commit": None,
            "protocol_version": 1,
            "acquisition_status": "LEGACY_EXPLICIT",
            "signal_quality_status": "LEGACY_EXPLICIT",
            "calibration_status": "legacy",
            "calibration_source": calibration.get("calibration_source"),
            "acquisition_fingerprint": calibration.get("acquisition_fingerprint"),
        }

    raise ModelInputError(f"unsupported or missing pipeline_profile: {profile!r}")


def calibration_summary(source_metadata: dict[str, Any]) -> tuple[Any, Any]:
    calibration = source_metadata.get("calibration", {})
    if not isinstance(calibration, dict):
        calibration = {}
    method = calibration.get(
        "method",
        source_metadata.get("calibration_method"),
    )
    scale = calibration.get(
        "scale_mV_per_count",
        source_metadata.get("scale_mV_per_count"),
    )
    return method, scale


def validation_dict(report: ValidationReport, max_abs_mV: float) -> dict[str, Any]:
    return {
        "status": report.status,
        "failures": report.failures,
        "warnings": report.warnings,
        "nan_inf_count": report.nan_inf_count,
        "max_abs_mV": report.max_abs_mV,
        "max_abs_limit_mV": float(max_abs_mV),
        "lead_iii_max_error_mV": report.lead_iii_max_error_mV,
    }


def signal_quality_dict(report: ValidationReport) -> dict[str, Any]:
    return {
        "status": report.status,
        "reasons": list(report.failures) + list(report.warnings),
    }


def include_raw_range_validation(
    report: ValidationReport,
    source_metadata: dict[str, Any],
) -> ValidationReport:
    """Carry acquired-channel offset-code rail checks into model readiness."""

    raw_range = source_metadata.get("raw_range_validation")
    if not isinstance(raw_range, dict):
        return report

    failures = list(report.failures)
    warnings = list(report.warnings)
    out_of_range = int(raw_range.get("out_of_range_count", 0) or 0)
    near_rail = int(raw_range.get("near_rail_count", 0) or 0)
    if out_of_range:
        failures.append(
            f"{out_of_range} acquired Lead I/II raw offset code(s) outside ADC range"
        )
    elif near_rail:
        warnings.append(
            f"{near_rail} acquired Lead I/II raw offset code(s) near an ADC rail"
        )

    status = "FAIL" if failures else "WARNING" if warnings else "PASS"
    return replace(report, status=status, failures=failures, warnings=warnings)


def build_model_metadata(
    frame_id: str,
    input_path: Path,
    metadata_path: Path,
    samples: np.ndarray,
    source_metadata: dict[str, Any],
    preprocessing_steps: list[str],
    preprocessing_config: dict[str, Any] | None,
    validation: ValidationReport,
    max_abs_mV: float,
    source_npy_sha256: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    calibration_method, calibration_scale = calibration_summary(source_metadata)
    return {
        "frame_id": frame_id,
        "source_file": str(input_path),
        "source_metadata_file": str(metadata_path),
        "source_npy_sha256": source_npy_sha256,
        "sample_rate_hz": MODEL_SAMPLE_RATE_HZ,
        "duration_seconds": MODEL_DURATION_SECONDS,
        "samples_per_channel": MODEL_INPUT_SHAPE[0],
        "channel_count": MODEL_INPUT_SHAPE[1],
        "channel_order": CHANNEL_ORDER,
        "unit": MODEL_INPUT_UNIT,
        "dtype": str(samples.dtype),
        "shape": list(samples.shape),
        "memory_layout": "samples x channels",
        "canonical_format": "npy",
        "calibration_method": calibration_method,
        "calibration_scale_mV_per_count": calibration_scale,
        "pipeline_profile": provenance.get("pipeline_profile"),
        "legacy_explicit": bool(provenance.get("legacy_explicit")),
        "session_id": provenance.get("session_id"),
        "firmware_commit": provenance.get("firmware_commit"),
        "protocol_version": provenance.get("protocol_version"),
        "acquisition_status": provenance.get("acquisition_status"),
        "calibration_status": provenance.get("calibration_status"),
        "calibration_source": provenance.get("calibration_source"),
        "signal_quality_status": provenance.get("signal_quality_status"),
        "acquisition_fingerprint": provenance.get("acquisition_fingerprint"),
        "preprocessing_steps_applied": preprocessing_steps,
        "preprocessing_config": preprocessing_config,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "validation": validation_dict(validation, max_abs_mV),
        "signal_quality": signal_quality_dict(validation),
        "raw_range_validation": source_metadata.get("raw_range_validation"),
        "lead_statistics": validation.lead_stats,
    }


def prepare_model_input_file(
    input_path: Path,
    metadata_path: Path,
    output_dir: Path,
    max_abs_mV: float = DEFAULT_MAX_ABS_MV,
    overwrite: bool = False,
    preprocessing_config_path: Path | None = None,
    allow_legacy: bool = False,
) -> PreparedModelInput:
    if not input_path.exists():
        raise ModelInputError(f"input file not found: {input_path}")

    source_metadata = load_json_object(metadata_path)
    provenance = validate_calibrated_metadata(
        source_metadata,
        allow_legacy=allow_legacy,
    )
    expected_source_sha256 = require_sha256(
        source_metadata.get("npy_sha256"),
        "npy_sha256",
    )
    actual_source_sha256 = file_sha256(input_path)
    if actual_source_sha256 != expected_source_sha256:
        raise ModelInputError(
            "calibrated NPY SHA-256 does not match source metadata: "
            f"expected {expected_source_sha256}, got {actual_source_sha256}"
        )
    preprocessing_steps, preprocessing_config = load_preprocessing_config(
        preprocessing_config_path
    )

    raw_samples = np.load(input_path, allow_pickle=False)
    if raw_samples.shape != MODEL_INPUT_SHAPE:
        raise ModelInputError(f"shape {raw_samples.shape} != {MODEL_INPUT_SHAPE}")

    samples = np.asarray(raw_samples, dtype=MODEL_INPUT_DTYPE)
    validation = validate_model_samples(samples, max_abs_mV=max_abs_mV)
    validation = include_raw_range_validation(validation, source_metadata)
    if validation.status == "FAIL":
        raise ModelInputError("; ".join(validation.failures))

    frame_id = frame_id_from_input(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = output_paths_for_frame(output_dir, frame_id)
    if not overwrite and (paths.npy_path.exists() or paths.metadata_path.exists()):
        raise ModelInputError(
            f"model-ready output already exists for {frame_id}; use --overwrite"
        )

    metadata = build_model_metadata(
        frame_id=frame_id,
        input_path=input_path,
        metadata_path=metadata_path,
        samples=samples,
        source_metadata=source_metadata,
        preprocessing_steps=preprocessing_steps,
        preprocessing_config=preprocessing_config,
        validation=validation,
        max_abs_mV=max_abs_mV,
        source_npy_sha256=actual_source_sha256,
        provenance=provenance,
    )
    np.save(paths.npy_path, samples)
    metadata["npy_sha256"] = file_sha256(paths.npy_path)
    paths.metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return PreparedModelInput(
        frame_id=frame_id,
        paths=paths,
        metadata=metadata,
        validation=validation,
    )


def load_model_metadata(
    path: Path,
    *,
    input_path: Path | None = None,
) -> dict[str, Any]:
    metadata = load_json_object(path)
    unit = metadata.get("unit")
    if unit != MODEL_INPUT_UNIT:
        raise ModelInputError(f"model metadata unit is {unit!r}, expected mV")

    if metadata.get("shape") != list(MODEL_INPUT_SHAPE):
        raise ModelInputError(
            f"model metadata shape is {metadata.get('shape')!r}, "
            f"expected {list(MODEL_INPUT_SHAPE)!r}"
        )
    if metadata.get("dtype") != str(MODEL_INPUT_DTYPE):
        raise ModelInputError(
            f"model metadata dtype is {metadata.get('dtype')!r}, expected float32"
        )
    if metadata.get("sample_rate_hz") != MODEL_SAMPLE_RATE_HZ:
        raise ModelInputError("model metadata sample_rate_hz must be 250")
    if metadata.get("channel_order") != CHANNEL_ORDER:
        raise ModelInputError("model metadata channel_order is invalid")

    profile = metadata.get("pipeline_profile")
    if profile == CORRECTED_PIPELINE_PROFILE:
        for field in (
            "session_id",
            "firmware_commit",
            "acquisition_fingerprint",
        ):
            value = metadata.get(field)
            if value in (None, "", {}):
                raise ModelInputError(f"corrected model metadata is missing {field}")
        if metadata.get("protocol_version") != 2:
            raise ModelInputError("corrected model metadata protocol_version must be 2")
        if metadata.get("acquisition_status") != "PASS":
            raise ModelInputError("corrected model metadata acquisition_status must be PASS")
        if metadata.get("signal_quality_status") != "PASS":
            raise ModelInputError(
                "corrected model metadata signal_quality_status must be PASS"
            )
        calibration_source = metadata.get("calibration_source")
        calibration_status = str(metadata.get("calibration_status", "")).lower()
        if not (
            (
                calibration_source == "datasheet_nominal"
                and calibration_status == "preliminary"
            )
            or (
                calibration_source == "prosim"
                and calibration_status == "calibrated"
            )
        ):
            raise ModelInputError(
                "corrected model metadata has an invalid calibration source/status"
            )
        require_pass_raw_range(metadata, "corrected model metadata")
        validation = metadata.get("validation")
        if (
            not isinstance(validation, dict)
            or validation.get("status") not in {"PASS", "WARNING"}
            or validation.get("failures") not in ([], None)
        ):
            raise ModelInputError("corrected model metadata validation is not usable")
    elif profile == LEGACY_PIPELINE_PROFILE:
        if metadata.get("legacy_explicit") is not True:
            raise ModelInputError("legacy model metadata lacks explicit legacy marker")
    else:
        raise ModelInputError(f"unsupported model pipeline_profile: {profile!r}")

    expected_sha256 = require_sha256(metadata.get("npy_sha256"), "npy_sha256")
    if input_path is not None:
        actual_sha256 = file_sha256(input_path)
        if actual_sha256 != expected_sha256:
            raise ModelInputError(
                "model-ready NPY SHA-256 does not match metadata: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
    return metadata

"""Utilities for preparing calibrated mV frames as model-ready NPY inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from calibration_utils import (
    CHANNEL_ORDER,
    CH3_CONSISTENCY_TOLERANCE_MV,
    EXPECTED_DTYPE,
    EXPECTED_SHAPE,
    ch3_max_error,
    count_nan_inf,
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


def validate_calibrated_metadata(metadata: dict[str, Any]) -> None:
    unit = metadata.get("unit")
    if unit is not None and unit != MODEL_INPUT_UNIT:
        raise ModelInputError(f"source metadata unit is {unit!r}, expected mV")

    sample_rate_hz = metadata.get("sample_rate_hz")
    if sample_rate_hz is not None and int(sample_rate_hz) != MODEL_SAMPLE_RATE_HZ:
        raise ModelInputError(
            f"source metadata sample_rate_hz is {sample_rate_hz}, expected {MODEL_SAMPLE_RATE_HZ}"
        )

    channel_order = metadata.get("channel_order")
    if channel_order is not None and list(channel_order) != CHANNEL_ORDER:
        raise ModelInputError(
            f"source metadata channel_order is {channel_order}, expected {CHANNEL_ORDER}"
        )


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
) -> dict[str, Any]:
    calibration_method, calibration_scale = calibration_summary(source_metadata)
    return {
        "frame_id": frame_id,
        "source_file": str(input_path),
        "source_metadata_file": str(metadata_path),
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
        "preprocessing_steps_applied": preprocessing_steps,
        "preprocessing_config": preprocessing_config,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "validation": validation_dict(validation, max_abs_mV),
        "signal_quality": signal_quality_dict(validation),
        "lead_statistics": validation.lead_stats,
    }


def prepare_model_input_file(
    input_path: Path,
    metadata_path: Path,
    output_dir: Path,
    max_abs_mV: float = DEFAULT_MAX_ABS_MV,
    overwrite: bool = False,
    preprocessing_config_path: Path | None = None,
) -> PreparedModelInput:
    if not input_path.exists():
        raise ModelInputError(f"input file not found: {input_path}")

    source_metadata = load_json_object(metadata_path)
    validate_calibrated_metadata(source_metadata)
    preprocessing_steps, preprocessing_config = load_preprocessing_config(
        preprocessing_config_path
    )

    raw_samples = np.load(input_path, allow_pickle=False)
    if raw_samples.shape != MODEL_INPUT_SHAPE:
        raise ModelInputError(f"shape {raw_samples.shape} != {MODEL_INPUT_SHAPE}")

    samples = np.asarray(raw_samples, dtype=MODEL_INPUT_DTYPE)
    validation = validate_model_samples(samples, max_abs_mV=max_abs_mV)
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
    )
    np.save(paths.npy_path, samples)
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


def load_model_metadata(path: Path) -> dict[str, Any]:
    metadata = load_json_object(path)
    unit = metadata.get("unit")
    if unit != MODEL_INPUT_UNIT:
        raise ModelInputError(f"model metadata unit is {unit!r}, expected mV")
    return metadata

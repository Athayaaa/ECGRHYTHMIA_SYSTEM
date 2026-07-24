"""Standalone AI inference worker for ECGRHYTHMIA model-ready ECG frames.

Watches an input directory for model-ready NPY frames, runs ECGEngine
inference on each new frame exactly once, and writes structured prediction
JSON files.  The worker never opens a serial port and never modifies source
NPY files.

Usage examples::

    # Process all pending frames then exit
    python3 ai_worker.py \\
      --input-dir data/model_ready \\
      --output-dir data/predictions \\
      --once

    # Continuously watch for new frames
    python3 ai_worker.py \\
      --input-dir data/model_ready \\
      --output-dir data/predictions \\
      --follow
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
MODEL_INPUT_SHAPE = (2500, 3)
MODEL_INPUT_DTYPE = np.dtype("float32")
DEFAULT_SAMPLING_RATE_HZ = 250.0
DEFAULT_POLL_INTERVAL_S = 0.5
CHANNEL_ORDER = ["Lead I", "Lead II", "Lead III"]
CH3_CONSISTENCY_TOLERANCE_MV = 1e-3  # relaxed vs calibration_utils for inference
STABILITY_CHECK_DELAY_FRACTION = 0.4  # fraction of poll interval for re-stat

# Frame filename patterns
MODEL_READY_PATTERN = re.compile(r"^frame_(\d+)_input\.npy$")
CALIBRATED_PATTERN = re.compile(r"^frame_(\d+)_mv\.npy$")
PREDICTION_PATTERN = re.compile(r"^frame_(\d+)_prediction\.json$")

LOGGER = logging.getLogger("ai_worker")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ECGRHYTHMIA AI inference worker",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/model_ready"),
        help="Directory containing model-ready NPY frames",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for prediction JSON output (default: predictions/ sibling of input-dir)",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        default=False,
        help="Continuously watch for new frames",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Process all pending frames then exit (default)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help="Polling interval in seconds for follow mode (default: 0.5)",
    )
    parser.add_argument(
        "--sampling-rate",
        type=float,
        default=DEFAULT_SAMPLING_RATE_HZ,
        help="Sampling rate in Hz for inference (default: 250)",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=None,
        help="Override model directory (sets ECG_MODEL_DIR)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--allow-calibrated-input",
        action="store_true",
        default=False,
        help="Also accept calibrated mV NPY files as input",
    )
    return parser


def resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    """Normalize and resolve CLI arguments."""

    script_dir = Path(__file__).resolve().parent
    if not args.input_dir.is_absolute():
        args.input_dir = (script_dir / args.input_dir).resolve()

    if args.output_dir is None:
        args.output_dir = args.input_dir.parent / "predictions"
    if not args.output_dir.is_absolute():
        args.output_dir = (script_dir / args.output_dir).resolve()

    if not args.follow and not args.once:
        args.once = True

    args.poll_interval = max(0.1, args.poll_interval)
    return args


# ---------------------------------------------------------------------------
# Frame discovery
# ---------------------------------------------------------------------------

def discover_frames(
    input_dir: Path,
    allow_calibrated: bool = False,
) -> list[tuple[str, Path]]:
    """Return (frame_id_str, path) pairs sorted numerically."""

    if not input_dir.is_dir():
        return []

    results: list[tuple[int, str, Path]] = []
    try:
        entries = list(input_dir.iterdir())
    except OSError as error:
        LOGGER.warning("Unable to list input directory %s: %s", input_dir, error)
        return []

    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        # Skip hidden and temporary files
        if name.startswith(".") or name.endswith("~") or name.endswith(".tmp"):
            continue

        try:
            mtime = entry.stat().st_mtime_ns
        except OSError:
            continue

        match = MODEL_READY_PATTERN.match(name)
        if match:
            frame_id = match.group(1).zfill(6)
            results.append((mtime, frame_id, entry))
            continue

        if allow_calibrated:
            match = CALIBRATED_PATTERN.match(name)
            if match:
                frame_id = match.group(1).zfill(6)
                results.append((mtime, frame_id, entry))

    results.sort(key=lambda item: item[0])
    return [(frame_id, path) for _, frame_id, path in results]


# ---------------------------------------------------------------------------
# Stability check
# ---------------------------------------------------------------------------

def is_file_stable(path: Path, delay: float = 0.2) -> bool:
    """Return True if the file's size and mtime are unchanged after a delay."""

    try:
        stat1 = path.stat()
    except OSError:
        return False

    time.sleep(delay)

    try:
        stat2 = path.stat()
    except OSError:
        return False

    return (
        stat1.st_size == stat2.st_size
        and stat1.st_mtime_ns == stat2.st_mtime_ns
        and stat1.st_size > 0
    )


# ---------------------------------------------------------------------------
# Prediction skip logic
# ---------------------------------------------------------------------------

def has_valid_prediction(output_dir: Path, frame_id: str) -> bool:
    """Return True if a valid prediction JSON already exists for this frame."""

    prediction_path = output_dir / f"frame_{frame_id}_prediction.json"
    if not prediction_path.is_file():
        return False

    try:
        with prediction_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return False

    if not isinstance(data, dict):
        return False

    return (
        data.get("schema_version") == SCHEMA_VERSION
        and data.get("status") in ("PASS", "FAIL", "WARNING")
        and data.get("frame_id") == frame_id
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def load_frame_metadata(npy_path: Path) -> dict[str, Any] | None:
    """Load the companion JSON metadata for a model-ready frame."""

    json_path = npy_path.with_suffix(".json")
    if not json_path.is_file():
        return None
    try:
        with json_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def validate_frame(
    samples: np.ndarray,
    metadata: dict[str, Any] | None,
    sampling_rate: float,
) -> tuple[bool, list[str], list[str]]:
    """Validate a frame for AI inference.

    Returns (is_valid, errors, warnings).
    """

    errors: list[str] = []
    warnings: list[str] = []

    # Shape check
    if samples.shape != MODEL_INPUT_SHAPE:
        errors.append(f"shape {samples.shape} != {MODEL_INPUT_SHAPE}")
        return False, errors, warnings

    # NaN/Inf check
    nan_count = int(np.count_nonzero(np.isnan(samples)))
    inf_count = int(np.count_nonzero(np.isinf(samples)))
    if nan_count > 0:
        errors.append(f"{nan_count} NaN values detected")
    if inf_count > 0:
        errors.append(f"{inf_count} Inf values detected")
    if errors:
        return False, errors, warnings

    # Sampling rate
    if sampling_rate != DEFAULT_SAMPLING_RATE_HZ:
        errors.append(
            f"sampling rate {sampling_rate} Hz != {DEFAULT_SAMPLING_RATE_HZ} Hz"
        )
        return False, errors, warnings

    # Metadata-based validation
    if metadata is not None:
        # Acquisition status
        validation_block = metadata.get("validation", {})
        if isinstance(validation_block, dict):
            acq_status = validation_block.get("status", "").upper()
            if acq_status == "FAIL":
                errors.append("acquisition status is FAIL")
                return False, errors, warnings

        # Signal quality block
        signal_quality = metadata.get("signal_quality", {})
        if isinstance(signal_quality, dict):
            sq_status = signal_quality.get("status", "").upper()
            if sq_status == "FAIL":
                errors.append("signal quality status is FAIL")
                return False, errors, warnings

        # Unit check
        unit = metadata.get("unit")
        if unit is not None:
            if unit != "mV":
                errors.append(f"unit is {unit!r}, expected 'mV'")
                return False, errors, warnings
        else:
            warnings.append("unit not specified in metadata")

        # Sampling rate check from metadata
        meta_sr = metadata.get("sample_rate_hz")
        if meta_sr is not None:
            if int(meta_sr) != int(DEFAULT_SAMPLING_RATE_HZ):
                errors.append(
                    f"metadata sample_rate_hz is {meta_sr}, expected {int(DEFAULT_SAMPLING_RATE_HZ)}"
                )
                return False, errors, warnings
        else:
            warnings.append("sample_rate_hz not specified in metadata")

        # Channel order check
        channel_order = metadata.get("channel_order")
        if channel_order is not None:
            if list(channel_order) != CHANNEL_ORDER:
                errors.append(
                    f"channel_order is {channel_order}, expected {CHANNEL_ORDER}"
                )
                return False, errors, warnings
        else:
            warnings.append("channel_order not specified in metadata")

        # Packet loss / resampler reset check
        parser_stats = metadata.get("parser_stats", {})
        if isinstance(parser_stats, dict):
            packet_loss = parser_stats.get("packet_loss_count", 0)
            if isinstance(packet_loss, int) and packet_loss > 0:
                errors.append(
                    f"frame has {packet_loss} packet loss events"
                )
                return False, errors, warnings
            resampler_resets = parser_stats.get("resampler_reset_count", 0)
            if isinstance(resampler_resets, int) and resampler_resets > 0:
                errors.append(
                    f"frame has {resampler_resets} resampler resets"
                )
                return False, errors, warnings
    else:
        warnings.append("no metadata file found; proceeding with data validation only")

    # Lead III consistency check
    lead_i = samples[:, 0].astype(np.float64)
    lead_ii = samples[:, 1].astype(np.float64)
    lead_iii = samples[:, 2].astype(np.float64)
    expected_iii = lead_ii - lead_i
    max_error = float(np.max(np.abs(lead_iii - expected_iii)))
    if max_error > CH3_CONSISTENCY_TOLERANCE_MV:
        errors.append(
            f"Lead III max error {max_error:.6g} mV exceeds tolerance "
            f"{CH3_CONSISTENCY_TOLERANCE_MV:.6g}"
        )
        return False, errors, warnings

    # Baseline warning check
    for index, label in enumerate(CHANNEL_ORDER):
        med = float(np.median(samples[:, index].astype(np.float64)))
        if abs(med) > 5.0:  # DEFAULT_BASELINE_WARNING_MV
            warnings.append(
                f"Large DC baseline on {label}: {med:+.2f} mV. "
                f"Dynamic ECG amplitude remains within limit."
            )

    return True, errors, warnings


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""

    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Prediction output
# ---------------------------------------------------------------------------

def build_prediction_json(
    *,
    status: str,
    frame_id: str,
    source_file: str,
    source_sha256: str,
    sampling_rate_hz: float,
    shape: tuple[int, ...],
    dtype: str,
    unit: str,
    prediction: str | None = None,
    confidence_percent: float | None = None,
    probabilities: dict[str, float] | None = None,
    threshold: float = 0.5,
    latency_ms: float | None = None,
    model_path: str | None = None,
    model_sha256: str | None = None,
    runtime: str | None = None,
    warning: str | None = None,
    error: str | None = None,
    input_validation_status: str | None = None,
    input_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build a structured prediction JSON dictionary."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "frame_id": frame_id,
        "source_file": source_file,
        "source_sha256": source_sha256,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sampling_rate_hz": sampling_rate_hz,
        "shape": list(shape),
        "dtype": dtype,
        "unit": unit,
        "prediction": prediction,
        "confidence_percent": confidence_percent,
        "probabilities": probabilities,
        "threshold": threshold,
        "latency_ms": latency_ms,
        "model_path": model_path,
        "model_sha256": model_sha256,
        "runtime": runtime,
        "warning": warning,
        "error": error,
        "input_validation_status": input_validation_status,
        "input_warnings": input_warnings,
    }


def write_prediction_atomic(
    output_dir: Path,
    filename: str,
    data: dict[str, Any],
) -> Path:
    """Write a prediction JSON file atomically using tempfile + os.replace."""

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / filename

    fd, tmp_path = tempfile.mkstemp(
        dir=str(output_dir), suffix=".tmp", prefix=".pred_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return target


# ---------------------------------------------------------------------------
# Engine wrapper
# ---------------------------------------------------------------------------

class EngineContext:
    """Lazy-loaded wrapper around ECGEngine for the worker."""

    def __init__(self, model_dir: Path | None = None) -> None:
        self._model_dir = model_dir
        self._engine: Any = None
        self._model_path: str | None = None
        self._model_sha256: str | None = None
        self._runtime: str | None = None
        self._threshold: float = 0.5
        self._labels: list[str] = []

    def load(self) -> None:
        """Load the ECGEngine and cache model metadata."""

        if self._model_dir is not None:
            os.environ["ECG_MODEL_DIR"] = str(self._model_dir)

        from ecg_edge_ai_engine import ECGEngine  # noqa: E402

        self._engine = ECGEngine()

        # Resolve model path and metadata
        loader = self._engine.model_loader
        from ecg_edge_ai_engine import config as engine_cfg

        model_path = engine_cfg.MODEL_DIR / "model.tflite"
        self._model_path = str(model_path)
        if model_path.is_file():
            self._model_sha256 = sha256_file(model_path)

        metadata = loader.metadata or {}
        self._threshold = float(metadata.get("threshold", 0.5))
        self._labels = list(metadata.get("labels", []))

        # Resolve runtime name
        try:
            from ecg_edge_ai_engine.model_loader import RUNTIME_NAME

            self._runtime = RUNTIME_NAME
        except ImportError:
            self._runtime = "unknown"

        LOGGER.info(
            "Model loaded: %s (labels=%s, threshold=%.2f, runtime=%s)",
            self._model_path,
            self._labels,
            self._threshold,
            self._runtime,
        )

    def predict(
        self, frame: np.ndarray, sampling_rate: float
    ) -> dict[str, Any]:
        """Run inference and return the raw engine result."""

        if self._engine is None:
            raise RuntimeError("Engine not loaded; call load() first")
        return self._engine.predict(frame, sampling_rate=sampling_rate)

    @property
    def model_path(self) -> str | None:
        return self._model_path

    @property
    def model_sha256(self) -> str | None:
        return self._model_sha256

    @property
    def runtime(self) -> str | None:
        return self._runtime

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def labels(self) -> list[str]:
        return list(self._labels)


# ---------------------------------------------------------------------------
# Process a single frame
# ---------------------------------------------------------------------------

def process_frame(
    frame_id: str,
    npy_path: Path,
    output_dir: Path,
    engine: EngineContext,
    sampling_rate: float,
) -> dict[str, Any] | None:
    """Process a single frame: validate, infer, write prediction JSON.

    Returns the prediction dict or None if skipped/failed.
    """

    source_sha256 = sha256_file(npy_path)

    # Load frame
    try:
        samples = np.load(npy_path, allow_pickle=False)
    except Exception as error:
        LOGGER.error("Frame %s: unable to load NPY: %s", frame_id, error)
        fail_data = build_prediction_json(
            status="FAIL",
            frame_id=frame_id,
            source_file=str(npy_path),
            source_sha256=source_sha256,
            sampling_rate_hz=sampling_rate,
            shape=(0, 0),
            dtype="unknown",
            unit="mV",
            error=f"Unable to load NPY: {error}",
        )
        write_prediction_atomic(
            output_dir, f"frame_{frame_id}_prediction.json", fail_data
        )
        write_prediction_atomic(output_dir, "latest_prediction.json", fail_data)
        return fail_data

    # Load metadata
    metadata = load_frame_metadata(npy_path)

    # Validate
    is_valid, errors, warnings = validate_frame(samples, metadata, sampling_rate)
    if not is_valid:
        reason = "; ".join(errors)
        LOGGER.warning("Frame %s rejected: %s", frame_id, reason)
        fail_data = build_prediction_json(
            status="FAIL",
            frame_id=frame_id,
            source_file=str(npy_path),
            source_sha256=source_sha256,
            sampling_rate_hz=sampling_rate,
            shape=tuple(int(d) for d in samples.shape),
            dtype=str(samples.dtype),
            unit="mV",
            error=reason,
            warning="; ".join(warnings) if warnings else None,
        )
        write_prediction_atomic(
            output_dir, f"frame_{frame_id}_prediction.json", fail_data
        )
        write_prediction_atomic(output_dir, "latest_prediction.json", fail_data)
        return fail_data

    # Cast to float32 in memory
    frame = np.asarray(samples, dtype=MODEL_INPUT_DTYPE)

    # Run inference
    try:
        t0 = time.perf_counter()
        result = engine.predict(frame, sampling_rate)
        latency_ms = (time.perf_counter() - t0) * 1000.0
    except Exception as error:
        LOGGER.error("Frame %s: inference failed: %s", frame_id, error)
        fail_data = build_prediction_json(
            status="FAIL",
            frame_id=frame_id,
            source_file=str(npy_path),
            source_sha256=source_sha256,
            sampling_rate_hz=sampling_rate,
            shape=tuple(int(d) for d in frame.shape),
            dtype=str(frame.dtype),
            unit="mV",
            model_path=engine.model_path,
            model_sha256=engine.model_sha256,
            runtime=engine.runtime,
            error=f"Inference failed: {error}",
            warning="; ".join(warnings) if warnings else None,
        )
        write_prediction_atomic(
            output_dir, f"frame_{frame_id}_prediction.json", fail_data
        )
        write_prediction_atomic(output_dir, "latest_prediction.json", fail_data)
        return fail_data

    # Build prediction output
    prediction_label = result.get("prediction", "Unclassified")
    confidence = result.get("confidence", 0.0)
    probabilities = result.get("probabilities", {})

    # Determine input validation status for the prediction JSON
    input_val_status: str | None = None
    input_warns: list[str] | None = None
    if warnings:
        input_val_status = "WARNING"
        input_warns = list(warnings)

    pred_data = build_prediction_json(
        status="PASS",
        frame_id=frame_id,
        source_file=str(npy_path),
        source_sha256=source_sha256,
        sampling_rate_hz=sampling_rate,
        shape=tuple(int(d) for d in frame.shape),
        dtype=str(frame.dtype),
        unit="mV",
        prediction=prediction_label,
        confidence_percent=round(confidence, 2),
        probabilities=probabilities,
        threshold=engine.threshold,
        latency_ms=round(latency_ms, 2),
        model_path=engine.model_path,
        model_sha256=engine.model_sha256,
        runtime=engine.runtime,
        warning="; ".join(warnings) if warnings else None,
        input_validation_status=input_val_status,
        input_warnings=input_warns,
    )

    write_prediction_atomic(
        output_dir, f"frame_{frame_id}_prediction.json", pred_data
    )
    write_prediction_atomic(output_dir, "latest_prediction.json", pred_data)

    LOGGER.info(
        "Frame %s: %s %.1f%%, %.1f ms",
        frame_id,
        prediction_label,
        confidence,
        latency_ms,
    )
    return pred_data


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------

def run_worker(args: argparse.Namespace) -> int:
    """Execute the AI worker with the given arguments."""

    input_dir = args.input_dir
    output_dir = args.output_dir
    follow = args.follow
    poll_interval = args.poll_interval
    sampling_rate = args.sampling_rate

    LOGGER.info("AI worker started")
    LOGGER.info("Input:  %s", input_dir)
    LOGGER.info("Output: %s", output_dir)
    LOGGER.info("Mode:   %s", "follow" if follow else "once")

    if not input_dir.is_dir():
        LOGGER.error("Input directory does not exist: %s", input_dir)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    # Load engine
    engine = EngineContext(model_dir=args.model_dir)
    try:
        engine.load()
    except Exception as error:
        LOGGER.error("Unable to load AI engine: %s", error)
        return 1

    LOGGER.info("Watching: %s", input_dir)

    stability_delay = poll_interval * STABILITY_CHECK_DELAY_FRACTION
    processed_count = 0
    skipped_count = 0
    total_latency = 0.0
    all_results: list[dict[str, Any]] = []

    try:
        while True:
            frames = discover_frames(input_dir, args.allow_calibrated_input)

            new_work = False
            for frame_id, npy_path in frames:
                # Skip existing valid predictions
                if has_valid_prediction(output_dir, frame_id):
                    LOGGER.debug("Frame %s skipped: prediction exists", frame_id)
                    skipped_count += 1
                    continue

                # File stability check
                if not is_file_stable(npy_path, delay=stability_delay):
                    LOGGER.debug(
                        "Frame %s skipped: file not stable", frame_id
                    )
                    continue

                LOGGER.info("Frame %s detected", frame_id)
                result = process_frame(
                    frame_id, npy_path, output_dir, engine, sampling_rate
                )
                if result is not None:
                    all_results.append(result)
                    processed_count += 1
                    if result.get("latency_ms") is not None:
                        total_latency += result["latency_ms"]
                    new_work = True

            if not follow:
                break

            if not new_work:
                time.sleep(poll_interval)

    except KeyboardInterrupt:
        LOGGER.info("AI worker stopped by user")

    avg_latency = total_latency / processed_count if processed_count else 0.0
    LOGGER.info(
        "Summary: processed=%d skipped=%d avg_latency=%.1f ms",
        processed_count,
        skipped_count,
        avg_latency,
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args = resolve_args(args)

    # Configure logging
    log_level = getattr(logging, str(args.log_level).upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    LOGGER.setLevel(log_level)

    # Suppress AI engine verbose logging in follow mode
    if args.follow and log_level > logging.DEBUG:
        for logger_name in (
            "ecg_edge_ai_engine",
            "ecg_edge_ai_engine.engine",
            "ecg_edge_ai_engine.preprocessing",
            "ecg_edge_ai_engine.inference",
            "ecg_edge_ai_engine.postprocessing",
            "ecg_edge_ai_engine.model_loader",
        ):
            logging.getLogger(logger_name).setLevel(logging.WARNING)

    return run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())

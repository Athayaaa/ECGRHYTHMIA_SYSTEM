"""Offline replay for recorded raw ECG frame datasets.

The replay path intentionally uses the same calibration and model-input helper
functions as the live receiver path. It does not talk to hardware and does not
modify the source dataset.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np
import yaml

from ads1293_session import verify_persisted_session_metadata
from calibration_utils import (
    CalibratedExportPaths,
    CalibrationError,
    FRAME_STEM_RE,
    FrameSet,
    convert_raw_to_mv,
    frame_sort_time,
    is_legacy_calibration,
    load_calibration_config,
    load_raw_frame,
    offset_code_range_stats,
    require_calibration_compatible,
    require_corrected_frame_provenance,
    require_frame_output_sample_rate,
    require_frame_session_link,
    write_calibrated_frame,
)
from model_input_utils import (
    DEFAULT_MAX_ABS_MV,
    ModelInputError,
    ModelInputPaths,
    prepare_model_input_file,
    signal_quality_dict,
    validate_model_samples,
)


REALTIME_FRAME_DELAY_SECONDS = 10.0


@dataclass(frozen=True)
class ReplayFrameResult:
    frame_id: str
    status: str
    stage: str
    raw_npy_path: str
    calibrated_npy_path: str | None = None
    calibrated_json_path: str | None = None
    model_ready_npy_path: str | None = None
    model_ready_json_path: str | None = None
    rejected_metadata_path: str | None = None
    signal_quality: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "status": self.status,
            "stage": self.stage,
            "raw_npy_path": self.raw_npy_path,
            "calibrated_npy_path": self.calibrated_npy_path,
            "calibrated_json_path": self.calibrated_json_path,
            "model_ready_npy_path": self.model_ready_npy_path,
            "model_ready_json_path": self.model_ready_json_path,
            "rejected_metadata_path": self.rejected_metadata_path,
            "signal_quality": self.signal_quality,
            "error": self.error,
        }


@dataclass(frozen=True)
class ReplaySummary:
    input_dir: str
    output_dir: str
    calibrated_dir: str
    model_ready_dir: str
    rejected_dir: str
    total_raw_frames: int
    frames_converted: int
    model_ready_frames: int
    failed_frames: int
    rejected_frames: int
    frame_results: list[ReplayFrameResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_dir": self.input_dir,
            "output_dir": self.output_dir,
            "calibrated_dir": self.calibrated_dir,
            "model_ready_dir": self.model_ready_dir,
            "rejected_dir": self.rejected_dir,
            "total_raw_frames": self.total_raw_frames,
            "frames_converted": self.frames_converted,
            "model_ready_frames": self.model_ready_frames,
            "failed_frames": self.failed_frames,
            "rejected_frames": self.rejected_frames,
            "frame_results": [result.to_dict() for result in self.frame_results],
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay recorded raw ECG frames through the Phase 3 pipeline"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory containing raw frame_*.csv/.bin/.npy/.json files",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "Explicit calibration YAML. Legacy datasets require explicitly passing "
            "calibration_config_legacy_640_to_250.yaml."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Replay output directory; calibrated/model-ready/rejected subdirs are created",
    )
    parser.add_argument(
        "--session-metadata",
        type=Path,
        help=(
            "Required verified session JSON for corrected-pipeline input. The "
            "legacy profile is the only mode that may omit this artifact."
        ),
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="Wait 10 seconds between frames to mimic live frame cadence",
    )
    parser.add_argument(
        "--max-abs-mv",
        type=float,
        default=DEFAULT_MAX_ABS_MV,
        help="Maximum sane absolute model-input amplitude in mV",
    )
    return parser.parse_args()


def path_is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def find_raw_frame_sets(input_dir: Path) -> list[FrameSet]:
    frames: list[FrameSet] = []
    for npy_path in input_dir.glob("frame_*.npy"):
        match = FRAME_STEM_RE.match(npy_path.stem)
        if match is None:
            continue

        stem = npy_path.stem
        json_path = input_dir / f"{stem}.json"
        try:
            sort_time = frame_sort_time(json_path) if json_path.exists() else npy_path.stat().st_mtime
        except OSError:
            sort_time = 0.0
        frames.append(
            FrameSet(
                stem=stem,
                csv_path=input_dir / f"{stem}.csv",
                npy_path=npy_path,
                json_path=json_path,
                bin_path=input_dir / f"{stem}.bin",
                frame_index=int(match.group(1)),
                sort_time=sort_time,
            )
        )

    return sorted(frames, key=lambda frame: (frame.frame_index, frame.sort_time))


def source_files_for_frame(frame: FrameSet) -> dict[str, str]:
    return {
        "csv": str(frame.csv_path),
        "bin": str(frame.bin_path),
        "npy": str(frame.npy_path),
        "json": str(frame.json_path),
    }


def require_raw_companion_files(frame: FrameSet) -> None:
    missing = [
        path.name
        for path in (frame.csv_path, frame.bin_path, frame.npy_path, frame.json_path)
        if not path.exists()
    ]
    if missing:
        raise CalibrationError(
            f"{frame.stem} is missing raw companion file(s): {', '.join(missing)}"
        )


def model_paths_as_strings(paths: ModelInputPaths | None) -> tuple[str | None, str | None]:
    if paths is None:
        return None, None
    return str(paths.npy_path), str(paths.metadata_path)


def calibrated_paths_as_strings(
    paths: CalibratedExportPaths | None,
) -> tuple[str | None, str | None]:
    if paths is None:
        return None, None
    return str(paths.npy_path), str(paths.json_path)


def write_rejected_metadata(
    rejected_dir: Path,
    frame: FrameSet,
    stage: str,
    error: str,
    signal_quality: dict[str, Any] | None = None,
    calibrated_paths: CalibratedExportPaths | None = None,
) -> Path:
    rejected_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "created_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "frame_id": frame.stem,
        "rejection_stage": stage,
        "error": error,
        "signal_quality": signal_quality
        or {
            "status": "FAIL",
            "reasons": [error],
        },
        "source_files": source_files_for_frame(frame),
        "calibrated_files": (
            {
                "csv": str(calibrated_paths.csv_path),
                "npy": str(calibrated_paths.npy_path),
                "json": str(calibrated_paths.json_path),
            }
            if calibrated_paths is not None
            else {}
        ),
        "model_ready_exported": False,
        "model_ready_policy": (
            "Corrected frames require PASS acquisition and PASS signal quality; "
            "all frames with signal_quality.status FAIL are excluded from the "
            "final model-ready directory."
        ),
    }
    metadata_path = rejected_dir / f"{frame.stem}_rejected.json"
    if metadata_path.exists() or metadata_path.is_symlink():
        raise CalibrationError(
            f"refusing to overwrite existing replay rejection metadata: {metadata_path}"
        )
    with metadata_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return metadata_path


def replay_frame(
    frame: FrameSet,
    calibration: dict[str, Any],
    calibrated_dir: Path,
    model_ready_dir: Path,
    rejected_dir: Path,
    max_abs_mV: float,
    runtime_metadata: dict[str, Any] | None = None,
) -> ReplayFrameResult:
    calibrated_paths: CalibratedExportPaths | None = None
    signal_quality: dict[str, Any] | None = None
    corrected_provenance: dict[str, Any] | None = None

    try:
        require_raw_companion_files(frame)
        raw_samples, source_metadata = load_raw_frame(frame)
        if not is_legacy_calibration(calibration):
            if runtime_metadata is None:
                raise CalibrationError(
                    "verified external session metadata is required for corrected replay"
                )
            verify_persisted_session_metadata(runtime_metadata)
            require_frame_session_link(source_metadata, runtime_metadata)
            compatibility = require_calibration_compatible(
                calibration,
                runtime_metadata,
            )
            calibration["compatibility"] = compatibility.to_dict()
            corrected_provenance = require_corrected_frame_provenance(
                source_metadata
            )
        range_stats = offset_code_range_stats(
            raw_samples,
            adcmax=calibration["adcmax"],
        )
        if range_stats["out_of_range_count"]:
            raise CalibrationError(
                f"{frame.stem} has {range_stats['out_of_range_count']} out-of-range "
                "Lead I/II offset-code value(s)"
            )
        sample_rate_hz = require_frame_output_sample_rate(
            source_metadata,
            calibration,
        )

        samples_mv = convert_raw_to_mv(raw_samples, calibration)
        calibrated_paths = write_calibrated_frame(
            output_dir=calibrated_dir,
            stem=frame.stem,
            samples_mv=samples_mv,
            sample_rate_hz=sample_rate_hz,
            calibration=calibration,
            source_metadata=source_metadata,
            source_files=source_files_for_frame(frame),
            raw_range_stats=range_stats,
        )

        corrected_warning_reasons: list[str] = []
        if (
            corrected_provenance is not None
            and not corrected_provenance["model_ready"]
        ):
            corrected_warning_reasons.append(
                "source corrected frame signal_quality_status is WARNING"
            )
        if corrected_provenance is not None and range_stats["status"] == "WARNING":
            corrected_warning_reasons.append(
                f"{range_stats['near_rail_count']} resampled Lead I/II code(s) "
                "are near an ADC rail"
            )
        if corrected_warning_reasons:
            signal_quality = {
                "status": "WARNING",
                "reasons": corrected_warning_reasons,
            }
            rejected_path = write_rejected_metadata(
                rejected_dir=rejected_dir,
                frame=frame,
                stage="signal_quality",
                error="; ".join(corrected_warning_reasons),
                signal_quality=signal_quality,
                calibrated_paths=calibrated_paths,
            )
            calibrated_npy, calibrated_json = calibrated_paths_as_strings(
                calibrated_paths
            )
            return ReplayFrameResult(
                frame_id=frame.stem,
                status="WARNING",
                stage="signal_quality",
                raw_npy_path=str(frame.npy_path),
                calibrated_npy_path=calibrated_npy,
                calibrated_json_path=calibrated_json,
                rejected_metadata_path=str(rejected_path),
                signal_quality=signal_quality,
                error="; ".join(corrected_warning_reasons),
            )

        validation = validate_model_samples(
            np.asarray(samples_mv, dtype=np.float32),
            max_abs_mV=max_abs_mV,
        )
        signal_quality = signal_quality_dict(validation)
        if validation.status == "FAIL":
            error = "; ".join(validation.failures)
            rejected_path = write_rejected_metadata(
                rejected_dir=rejected_dir,
                frame=frame,
                stage="signal_quality",
                error=error,
                signal_quality=signal_quality,
                calibrated_paths=calibrated_paths,
            )
            calibrated_npy, calibrated_json = calibrated_paths_as_strings(calibrated_paths)
            return ReplayFrameResult(
                frame_id=frame.stem,
                status="FAIL",
                stage="signal_quality",
                raw_npy_path=str(frame.npy_path),
                calibrated_npy_path=calibrated_npy,
                calibrated_json_path=calibrated_json,
                rejected_metadata_path=str(rejected_path),
                signal_quality=signal_quality,
                error=error,
            )

        prepared = prepare_model_input_file(
            input_path=calibrated_paths.npy_path,
            metadata_path=calibrated_paths.json_path,
            output_dir=model_ready_dir,
            max_abs_mV=max_abs_mV,
            allow_legacy=is_legacy_calibration(calibration),
        )
        calibrated_npy, calibrated_json = calibrated_paths_as_strings(calibrated_paths)
        model_npy, model_json = model_paths_as_strings(prepared.paths)
        return ReplayFrameResult(
            frame_id=frame.stem,
            status=prepared.validation.status,
            stage="model_ready",
            raw_npy_path=str(frame.npy_path),
            calibrated_npy_path=calibrated_npy,
            calibrated_json_path=calibrated_json,
            model_ready_npy_path=model_npy,
            model_ready_json_path=model_json,
            signal_quality=prepared.metadata.get("signal_quality", signal_quality),
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
        CalibrationError,
        ModelInputError,
    ) as error:
        rejected_path = write_rejected_metadata(
            rejected_dir=rejected_dir,
            frame=frame,
            stage="pipeline",
            error=str(error),
            signal_quality=signal_quality,
            calibrated_paths=calibrated_paths,
        )
        calibrated_npy, calibrated_json = calibrated_paths_as_strings(calibrated_paths)
        return ReplayFrameResult(
            frame_id=frame.stem,
            status="FAIL",
            stage="pipeline",
            raw_npy_path=str(frame.npy_path),
            calibrated_npy_path=calibrated_npy,
            calibrated_json_path=calibrated_json,
            rejected_metadata_path=str(rejected_path),
            signal_quality=signal_quality
            or {
                "status": "FAIL",
                "reasons": [str(error)],
            },
            error=str(error),
        )


def write_replay_summary(output_dir: Path, summary: ReplaySummary) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "replay_summary.json"
    if summary_path.exists() or summary_path.is_symlink():
        raise CalibrationError(
            f"refusing to overwrite existing replay summary: {summary_path}"
        )
    with summary_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(summary.to_dict(), indent=2, sort_keys=True) + "\n")
    return summary_path


def replay_dataset(
    input_dir: Path,
    config_path: Path,
    output_dir: Path,
    realtime: bool = False,
    max_abs_mV: float = DEFAULT_MAX_ABS_MV,
    session_metadata: dict[str, Any] | None = None,
) -> ReplaySummary:
    input_dir = input_dir.resolve()
    config_path = config_path.resolve()
    output_dir = output_dir.resolve()

    if path_is_inside(output_dir, input_dir):
        raise CalibrationError(
            "--output-dir must be outside --input-dir to keep raw datasets unchanged"
        )
    if output_dir.exists() or output_dir.is_symlink():
        raise CalibrationError(
            "--output-dir must be a new, non-existing path so prior replay "
            f"artifacts cannot be overwritten: {output_dir}"
        )

    # ``config_path`` is a required API argument and a required CLI option, so
    # allowing legacy here remains an explicit user choice rather than a hidden
    # fallback. Corrected configs are still checked against every frame below.
    calibration = load_calibration_config(config_path, allow_legacy=True)
    legacy = is_legacy_calibration(calibration)
    if not legacy:
        if session_metadata is None:
            raise CalibrationError(
                "--session-metadata is required for corrected-pipeline replay"
            )
        verify_persisted_session_metadata(session_metadata)
    frames = find_raw_frame_sets(input_dir)
    if not frames:
        raise CalibrationError(f"no raw frame_*.npy files found in {input_dir}")

    # Reserve one unique replay root atomically. This prevents a concurrent or
    # repeated replay from entering the per-frame writers for the same path.
    output_dir.mkdir(parents=True, exist_ok=False)
    calibrated_dir = output_dir / "calibrated"
    model_ready_dir = output_dir / "model_ready"
    rejected_dir = output_dir / "rejected"

    results: list[ReplayFrameResult] = []
    for index, frame in enumerate(frames):
        result = replay_frame(
            frame=frame,
            calibration=calibration,
            calibrated_dir=calibrated_dir,
            model_ready_dir=model_ready_dir,
            rejected_dir=rejected_dir,
            max_abs_mV=max_abs_mV,
            runtime_metadata=session_metadata,
        )
        results.append(result)
        print(f"{frame.stem}: {result.status} ({result.stage})")

        if realtime and index < len(frames) - 1:
            time.sleep(REALTIME_FRAME_DELAY_SECONDS)

    frames_converted = sum(1 for result in results if result.calibrated_npy_path)
    model_ready_frames = sum(1 for result in results if result.model_ready_npy_path)
    failed_frames = sum(1 for result in results if result.status == "FAIL")
    rejected_frames = sum(1 for result in results if result.rejected_metadata_path)

    summary = ReplaySummary(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        calibrated_dir=str(calibrated_dir),
        model_ready_dir=str(model_ready_dir),
        rejected_dir=str(rejected_dir),
        total_raw_frames=len(frames),
        frames_converted=frames_converted,
        model_ready_frames=model_ready_frames,
        failed_frames=failed_frames,
        rejected_frames=rejected_frames,
        frame_results=results,
    )
    write_replay_summary(output_dir, summary)
    return summary


def main() -> int:
    args = parse_args()
    try:
        session_metadata = None
        if args.session_metadata is not None:
            session_metadata = json.loads(
                args.session_metadata.resolve().read_text(encoding="utf-8")
            )
            if not isinstance(session_metadata, dict):
                raise CalibrationError("--session-metadata must contain a JSON object")
        summary = replay_dataset(
            input_dir=args.input_dir,
            config_path=args.config,
            output_dir=args.output_dir,
            realtime=bool(args.realtime),
            max_abs_mV=float(args.max_abs_mv),
            session_metadata=session_metadata,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, CalibrationError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("OK: replay complete")
    print(f"total_raw_frames: {summary.total_raw_frames}")
    print(f"frames_converted: {summary.frames_converted}")
    print(f"model_ready_frames: {summary.model_ready_frames}")
    print(f"failed_frames: {summary.failed_frames}")
    print(f"output_dir: {summary.output_dir}")
    return 0 if summary.failed_frames == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

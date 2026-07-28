"""Safe filesystem lifecycle helpers for one ECG recording session."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SESSION_SCHEMA_VERSION = 1
SESSION_PREFIX = "session_"
SESSION_LAYOUT = ("raw_adc", "calibrated", "model_ready", "predictions", "logs")
DEFAULT_SAMPLE_RATE_HZ = 250.0
DEFAULT_FRAME_SECONDS = 10.0
CHANNEL_ORDER = ["Lead I", "Lead II", "Lead III"]


@dataclass(frozen=True)
class SessionPaths:
    session_id: str
    root: Path
    raw_adc: Path
    calibrated: Path
    model_ready: Path
    predictions: Path
    logs: Path
    metadata_path: Path


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def session_id_for(now: datetime) -> str:
    return now.astimezone().strftime("session_%d%m%Y_%H%M%S")


def paths_for_session(root: Path, session_id: str | None = None) -> SessionPaths:
    root = root.expanduser().resolve()
    return SessionPaths(
        session_id=session_id or root.name,
        root=root,
        raw_adc=root / "raw_adc",
        calibrated=root / "calibrated",
        model_ready=root / "model_ready",
        predictions=root / "predictions",
        logs=root / "logs",
        metadata_path=root / "session.json",
    )


def create_recording_session(
    sessions_root: Path,
    *,
    now: datetime | None = None,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    duration_per_frame_s: float = DEFAULT_FRAME_SECONDS,
) -> SessionPaths:
    started_at = (now or datetime.now().astimezone()).astimezone()
    sessions_root = sessions_root.expanduser().resolve()
    sessions_root.mkdir(parents=True, exist_ok=True)

    base_id = session_id_for(started_at)
    suffix = 0
    while True:
        session_id = base_id if suffix == 0 else f"{base_id}_{suffix:02d}"
        paths = paths_for_session(sessions_root / session_id, session_id)
        try:
            paths.root.mkdir()
            break
        except FileExistsError:
            suffix += 1

    try:
        for path in (paths.raw_adc, paths.calibrated, paths.model_ready, paths.predictions, paths.logs):
            path.mkdir()
        write_json_atomic(
            paths.metadata_path,
            {
                "schema_version": SESSION_SCHEMA_VERSION,
                "session_id": paths.session_id,
                "started_at": started_at.isoformat(timespec="seconds"),
                "ended_at": None,
                "status": "RECORDING",
                "sampling_rate_hz": float(sample_rate_hz),
                "duration_per_frame_s": float(duration_per_frame_s),
                "channel_order": CHANNEL_ORDER,
                "unit": "mV",
                "frame_count": 0,
            },
        )
    except BaseException:
        # Do not remove an existing session; only the newly-created empty root.
        try:
            if paths.root.exists() and not any(paths.root.iterdir()):
                paths.root.rmdir()
        except OSError:
            pass
        raise
    return paths


def load_session_metadata(paths: SessionPaths) -> dict[str, Any]:
    with paths.metadata_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{paths.metadata_path} does not contain a JSON object")
    return data


def finish_recording_session(
    paths: SessionPaths,
    frame_count: int,
    *,
    now: datetime | None = None,
    status: str = "STOPPED",
) -> None:
    data = load_session_metadata(paths)
    ended_at = (now or datetime.now().astimezone()).astimezone()
    data.update(
        {
            "ended_at": ended_at.isoformat(timespec="seconds"),
            "status": status,
            "frame_count": max(0, int(frame_count)),
        }
    )
    write_json_atomic(paths.metadata_path, data)


def legacy_output_files(data_root: Path) -> list[Path]:
    """Return only known flat legacy outputs; never traverse sessions or symlinks."""
    data_root = data_root.expanduser().resolve()
    candidates: list[Path] = []
    top_level_names = {
        "raw_ecg.csv",
        "ecg_metadata.sqlite",
        "ecg_metadata.sqlite-journal",
        "ecg_metadata.sqlite-shm",
        "ecg_metadata.sqlite-wal",
    }
    try:
        entries = list(data_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file() and (entry.name in top_level_names or entry.name.startswith("frame_")):
            candidates.append(entry)
            continue
        if not entry.is_dir() or entry.name not in {"calibrated", "model_ready", "predictions"}:
            continue
        try:
            for child in entry.iterdir():
                if child.is_file() and not child.is_symlink() and (
                    child.name.startswith("frame_") or child.name == "latest_prediction.json"
                ):
                    candidates.append(child)
        except OSError:
            continue
    return sorted(candidates)


def clean_legacy_output(data_root: Path) -> list[Path]:
    removed: list[Path] = []
    for path in legacy_output_files(data_root):
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed

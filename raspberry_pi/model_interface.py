"""Placeholder accessors for the latest exported ECG window.

This module intentionally does not run inference. It only exposes the latest
stored Phase 2 window and metadata in the shape expected by a future model path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

MODEL_INPUT_SHAPE = (2500, 3)
DEFAULT_DATA_DIR = Path(__file__).with_name("data")


def get_model_input_shape() -> tuple[int, int]:
    return MODEL_INPUT_SHAPE


def get_latest_metadata(data_dir: Path | str = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    metadata_path = _latest_metadata_path(Path(data_dir))
    if metadata_path is None:
        return None

    with metadata_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_latest_window(data_dir: Path | str = DEFAULT_DATA_DIR) -> np.ndarray | None:
    data_path = Path(data_dir)
    metadata = get_latest_metadata(data_path)
    if metadata is None:
        return None

    npy_path = data_path / metadata["npy_file"]
    samples = np.load(npy_path)
    if samples.shape != MODEL_INPUT_SHAPE:
        raise ValueError(f"expected window shape {MODEL_INPUT_SHAPE}, got {samples.shape}")

    return np.asarray(samples, dtype=np.float32)


def _latest_metadata_path(data_dir: Path) -> Path | None:
    candidates = sorted(data_dir.glob("frame_*.json"))
    if not candidates:
        return None
    return candidates[-1]

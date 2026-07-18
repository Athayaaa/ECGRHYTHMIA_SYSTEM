"""Read-only discovery and navigation for calibrated ECG frames.

Only canonical ``frame_<number>_mv.npy`` files are considered frames.  CSV
exports and temporary files are deliberately ignored: NPY is the viewer's
single source of sample data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterator

import numpy as np


FRAME_FILENAME_PATTERN = re.compile(r"^frame_(?P<frame_id>\d+)_mv\.npy$")


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """Paths belonging to one calibrated ECG frame.

    ``metadata_path`` is ``None`` when a matching JSON file was not present at
    the time of the repository scan.  The record is immutable and contains no
    operation that can write to the source directory.
    """

    frame_id: int
    npy_path: Path
    metadata_path: Path | None = None

    @property
    def json_path(self) -> Path | None:
        """Alias used by callers that refer to metadata as JSON."""

        return self.metadata_path

    @property
    def path(self) -> Path:
        """Return the canonical NPY path."""

        return self.npy_path

    @property
    def filename(self) -> str:
        """Return the NPY filename without its parent directory."""

        return self.npy_path.name

    @property
    def has_metadata(self) -> bool:
        """Whether matching JSON metadata existed during the last scan."""

        return self.metadata_path is not None

    @property
    def display_id(self) -> str:
        """Return a stable, human-friendly frame label."""

        return f"frame_{self.frame_id:06d}"


class FrameRepository:
    """Maintain a numerically sorted, read-only view of an ECG directory."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._frames: tuple[FrameRecord, ...] = ()
        self._by_id: dict[int, FrameRecord] = {}

    @property
    def data_dir(self) -> Path:
        """Directory currently scanned by the repository."""

        return self._data_dir

    def set_data_dir(self, data_dir: str | Path) -> tuple[FrameRecord, ...]:
        """Select ``data_dir`` and immediately refresh the cached frame list."""

        self._data_dir = Path(data_dir).expanduser().resolve()
        self._frames = ()
        self._by_id = {}
        return self.refresh()

    def refresh(self) -> tuple[FrameRecord, ...]:
        """Rescan the directory and return an immutable numerical frame list.

        A missing directory is represented by an empty repository.  Other I/O
        errors are allowed to propagate so the GUI can report them without
        silently presenting stale data.
        """

        if not self._data_dir.is_dir():
            self._frames = ()
            self._by_id = {}
            return self._frames

        records: list[FrameRecord] = []
        for path in self._iter_directory():
            match = FRAME_FILENAME_PATTERN.fullmatch(path.name)
            if match is None or not path.is_file():
                continue

            frame_id = int(match.group("frame_id"))
            candidate_metadata = path.with_suffix(".json")
            metadata_path = (
                candidate_metadata if candidate_metadata.is_file() else None
            )
            records.append(
                FrameRecord(
                    frame_id=frame_id,
                    npy_path=path,
                    metadata_path=metadata_path,
                )
            )

        records.sort(key=lambda record: (record.frame_id, record.filename))
        self._frames = tuple(records)

        # Canonical data uses one filename per numerical ID.  If a directory
        # contains duplicate spellings (for example frame_1 and frame_000001),
        # navigation remains deterministic and get_by_id returns the first.
        self._by_id = {}
        for record in self._frames:
            self._by_id.setdefault(record.frame_id, record)
        return self._frames

    def _iter_directory(self) -> Iterator[Path]:
        """Yield direct children without recursively following directories."""

        return self._data_dir.iterdir()

    def frames(self) -> tuple[FrameRecord, ...]:
        """Return the immutable frame snapshot from the latest scan."""

        return self._frames

    def frame_count(self) -> int:
        """Return the number of discovered NPY frames."""

        return len(self._frames)

    def get_by_index(self, index: int) -> FrameRecord | None:
        """Return a frame by zero-based index, or ``None`` outside the list."""

        if 0 <= index < len(self._frames):
            return self._frames[index]
        return None

    def get_by_id(self, frame_id: int) -> FrameRecord | None:
        """Return the frame with numerical ``frame_id``, if present."""

        return self._by_id.get(int(frame_id))

    def first(self) -> FrameRecord | None:
        """Return the earliest numerical frame, or ``None`` when empty."""

        return self.get_by_index(0)

    def latest(self) -> FrameRecord | None:
        """Return the latest numerical frame, or ``None`` when empty."""

        return self.get_by_index(len(self._frames) - 1)

    def previous(self, current: int | FrameRecord) -> FrameRecord | None:
        """Return the frame before ``current``; boundaries return ``None``.

        Integer arguments are interpreted as frame IDs rather than list
        indices.  Passing a :class:`FrameRecord` is convenient for GUI callers.
        """

        index = self._index_for(current)
        if index is None or index == 0:
            return None
        return self._frames[index - 1]

    def next(self, current: int | FrameRecord) -> FrameRecord | None:
        """Return the frame after ``current``; boundaries return ``None``."""

        index = self._index_for(current)
        if index is None or index >= len(self._frames) - 1:
            return None
        return self._frames[index + 1]

    def _index_for(self, current: int | FrameRecord) -> int | None:
        frame_id = current.frame_id if isinstance(current, FrameRecord) else int(current)
        for index, record in enumerate(self._frames):
            if record.frame_id == frame_id:
                return index
        return None

    @staticmethod
    def load_array(record: FrameRecord) -> np.ndarray:
        """Load an NPY frame without pickle support and without modifying it."""

        return np.load(record.npy_path, allow_pickle=False)

    @staticmethod
    def load_metadata(record: FrameRecord) -> dict[str, Any] | None:
        """Load JSON metadata, returning ``None`` for an NPY-only frame.

        Invalid JSON and non-object JSON deliberately raise exceptions for the
        validator or GUI error boundary to turn into a user-visible warning.
        """

        if record.metadata_path is None:
            return None
        with record.metadata_path.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        if not isinstance(metadata, dict):
            raise ValueError(f"{record.metadata_path.name} must contain a JSON object")
        return metadata


__all__ = [
    "FRAME_FILENAME_PATTERN",
    "FrameRecord",
    "FrameRepository",
]

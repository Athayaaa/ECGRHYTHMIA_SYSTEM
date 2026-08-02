"""Qt directory watching with conservative completed-frame detection.

``QFileSystemWatcher`` supplies prompt notifications while a 500 ms timer
provides a reliable fallback for coalesced or missed directory events.  A file
is never announced as ready on its first observation: its size and identity
must be unchanged on a second scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat as stat_module
from typing import Iterable

from .frame_repository import FrameRecord, FrameRepository

try:  # Keep the pure stability helper testable before GUI dependencies install.
    from .qt_compat import QtCore

    QFileSystemWatcher = QtCore.QFileSystemWatcher
    QObject = QtCore.QObject
    QTimer = QtCore.QTimer
    pyqtSignal = QtCore.pyqtSignal

    QT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without GUI dependencies
    QFileSystemWatcher = QObject = QTimer = pyqtSignal = None  # type: ignore[assignment]
    QT_AVAILABLE = False


@dataclass(frozen=True, slots=True)
class FileObservation:
    """File attributes used to recognize writes and atomic replacement."""

    size: int
    device: int
    inode: int
    modified_ns: int


@dataclass(slots=True)
class _TrackedObservation:
    observation: FileObservation
    unchanged_scans: int


class FrameStabilityTracker:
    """Detect files unchanged across at least two consecutive scans.

    File identity and modification time complement the required size check so
    deletion, same-name replacement, and same-size rewrites reset stability.
    """

    def __init__(self, required_scans: int = 2) -> None:
        if required_scans < 2:
            raise ValueError("required_scans must be at least 2")
        self.required_scans = int(required_scans)
        self._tracked: dict[Path, _TrackedObservation] = {}

    def reset(self) -> None:
        """Forget all observations, for example after changing directory."""

        self._tracked.clear()

    def update(self, paths: Iterable[str | Path]) -> frozenset[Path]:
        """Observe ``paths`` once and return those currently considered stable."""

        normalized = {Path(path).resolve() for path in paths}
        for stale_path in self._tracked.keys() - normalized:
            del self._tracked[stale_path]

        stable: set[Path] = set()
        for path in normalized:
            try:
                observation = self._stat(path)
            except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
                self._tracked.pop(path, None)
                continue

            previous = self._tracked.get(path)
            if previous is not None and previous.observation == observation:
                unchanged_scans = previous.unchanged_scans + 1
            else:
                unchanged_scans = 1
            self._tracked[path] = _TrackedObservation(
                observation=observation,
                unchanged_scans=unchanged_scans,
            )
            if unchanged_scans >= self.required_scans:
                stable.add(path)
        return frozenset(stable)

    def observe(self, path: str | Path) -> bool:
        """Observe one file; useful for focused tests and non-Qt callers."""

        resolved = Path(path).resolve()
        normalized = set(self._tracked)
        normalized.add(resolved)
        return resolved in self.update(normalized)

    def is_stable(self, path: str | Path) -> bool:
        """Return stability from the most recent observation without rescanning."""

        state = self._tracked.get(Path(path).resolve())
        return state is not None and state.unchanged_scans >= self.required_scans

    def unchanged_scans(self, path: str | Path) -> int:
        """Return the current consecutive-observation count for ``path``."""

        state = self._tracked.get(Path(path).resolve())
        return 0 if state is None else state.unchanged_scans

    def observation(self, path: str | Path) -> FileObservation | None:
        """Return the latest signature, used to detect later replacement."""

        state = self._tracked.get(Path(path).resolve())
        return None if state is None else state.observation

    @staticmethod
    def _stat(path: Path) -> FileObservation:
        stat_result = path.stat()
        if not path.is_file():
            raise FileNotFoundError(f"not a regular file: {path}")
        return FileObservation(
            size=int(stat_result.st_size),
            device=int(stat_result.st_dev),
            inode=int(stat_result.st_ino),
            modified_ns=int(stat_result.st_mtime_ns),
        )


if QT_AVAILABLE:

    class FrameWatcher(QObject):
        """Watch a :class:`FrameRepository` and announce stable frame files."""

        frames_changed = pyqtSignal(object)
        stable_frame_available = pyqtSignal(object)
        directory_unavailable = pyqtSignal(str)
        error = pyqtSignal(str)

        # CamelCase/compact aliases ease integration with conventional Qt code.
        framesChanged = pyqtSignal(object)
        frame_ready = pyqtSignal(object)
        frameReady = pyqtSignal(object)

        def __init__(
            self,
            repository: FrameRepository | str | Path,
            parent: QObject | None = None,
            *,
            interval_ms: int = 500,
            metadata_grace_scans: int = 2,
        ) -> None:
            super().__init__(parent)
            if interval_ms <= 0:
                raise ValueError("interval_ms must be positive")
            if metadata_grace_scans < 1:
                raise ValueError("metadata_grace_scans must be at least 1")

            self.repository = (
                repository
                if isinstance(repository, FrameRepository)
                else FrameRepository(repository)
            )
            self.interval_ms = int(interval_ms)
            self.metadata_grace_scans = int(metadata_grace_scans)
            self._file_watcher = QFileSystemWatcher(self)
            self._timer = QTimer(self)
            self._timer.setInterval(self.interval_ms)
            self._timer.setSingleShot(False)
            self._timer.timeout.connect(self.scan_now)
            self._file_watcher.directoryChanged.connect(
                self._on_directory_changed
            )

            self._npy_stability = FrameStabilityTracker()
            self._json_stability = FrameStabilityTracker()
            self._last_frames: tuple[FrameRecord, ...] = ()
            self._missing_metadata_scans: dict[Path, int] = {}
            self._announced: dict[
                Path,
                tuple[FileObservation, FileObservation | None],
            ] = {}
            self._running = False
            self._unavailable_reported = False
            self._watch_retry_path: Path | None = None
            self._watch_retry_scans = 0
            self._watch_error_reported = False
            self._scan_error_reported = False

        @property
        def data_dir(self) -> Path:
            """Return the directory currently being watched."""

            return self.repository.data_dir

        @property
        def is_running(self) -> bool:
            """Whether timer-backed monitoring is active."""

            return self._running

        def start(self) -> None:
            """Start notifications and perform an immediate first scan."""

            if self._running:
                return
            self._running = True
            self._ensure_directory_watch()
            self.scan_now()
            self._timer.start()

        def stop(self) -> None:
            """Stop monitoring without changing repository data."""

            self._running = False
            self._timer.stop()
            watched = self._file_watcher.directories()
            if watched:
                self._file_watcher.removePaths(watched)

        def set_directory(self, data_dir: str | Path) -> None:
            """Switch directories and reset all file-stability history."""

            watched = self._file_watcher.directories()
            if watched:
                self._file_watcher.removePaths(watched)
            try:
                self.repository.set_data_dir(data_dir)
            except OSError as error:
                self.error.emit(f"Could not scan {data_dir}: {error}")
            self._reset_tracking()
            self._last_frames = ()
            self._unavailable_reported = False
            self._watch_retry_path = None
            self._watch_retry_scans = 0
            self._watch_error_reported = False
            self._scan_error_reported = False
            if self._running:
                self._ensure_directory_watch()
                self.scan_now()

        def scan_now(self) -> tuple[FrameRecord, ...]:
            """Rescan once and emit each newly stable frame exactly once.

            A replacement or newly arrived JSON changes the announcement
            signature and therefore emits the record again safely.
            """

            directory = self.repository.data_dir
            if not directory.is_dir():
                self._handle_unavailable(directory)
                return ()

            self._ensure_directory_watch()
            self._unavailable_reported = False
            try:
                frames = self.repository.refresh()
            except OSError as error:
                if not self._scan_error_reported:
                    self.error.emit(f"Could not rescan {directory}: {error}")
                    self._scan_error_reported = True
                return ()
            self._scan_error_reported = False

            if frames != self._last_frames:
                self._last_frames = frames
                self.frames_changed.emit(frames)
                self.framesChanged.emit(frames)

            npy_paths = [record.npy_path for record in frames]
            json_paths = [
                record.metadata_path
                for record in frames
                if record.metadata_path is not None
            ]
            stable_npy = self._npy_stability.update(npy_paths)
            stable_json = self._json_stability.update(json_paths)

            current_paths = {record.npy_path.resolve() for record in frames}
            self._announced = {
                path: signature
                for path, signature in self._announced.items()
                if path in current_paths
            }
            self._missing_metadata_scans = {
                path: count
                for path, count in self._missing_metadata_scans.items()
                if path in current_paths
            }

            ready: list[FrameRecord] = []
            for record in frames:
                npy_path = record.npy_path.resolve()
                if npy_path not in stable_npy:
                    self._missing_metadata_scans.pop(npy_path, None)
                    continue

                json_observation: FileObservation | None = None
                if record.metadata_path is not None:
                    json_path = record.metadata_path.resolve()
                    self._missing_metadata_scans.pop(npy_path, None)
                    if json_path not in stable_json:
                        continue
                    json_observation = self._json_stability.observation(json_path)
                else:
                    grace_count = self._missing_metadata_scans.get(npy_path, 0) + 1
                    self._missing_metadata_scans[npy_path] = grace_count
                    if grace_count < self.metadata_grace_scans:
                        continue

                npy_observation = self._npy_stability.observation(npy_path)
                if npy_observation is None:
                    continue
                signature = (npy_observation, json_observation)
                if self._announced.get(npy_path) == signature:
                    continue

                # Verify existence immediately before handing the record to a
                # loader; disappearance after this remains a handled I/O race.
                if not record.npy_path.is_file():
                    continue
                self._announced[npy_path] = signature
                ready.append(record)
                self.stable_frame_available.emit(record)
                self.frame_ready.emit(record)
                self.frameReady.emit(record)
            return tuple(ready)

        def is_stable(self, record_or_path: FrameRecord | str | Path) -> bool:
            """Return whether the NPY path passed two-scan stability."""

            path = (
                record_or_path.npy_path
                if isinstance(record_or_path, FrameRecord)
                else Path(record_or_path)
            )
            return self._npy_stability.is_stable(path)

        def is_ready(self, record: FrameRecord) -> bool:
            """Return whether the exact NPY/JSON version was announced ready.

            Unlike :meth:`is_stable`, this also enforces JSON stability when a
            sidecar exists and the metadata grace period for NPY-only frames.
            """

            npy_path = record.npy_path.resolve()
            if not self._npy_stability.is_stable(npy_path):
                return False
            npy_observation = self._npy_stability.observation(npy_path)
            if npy_observation is None:
                return False
            try:
                if FrameStabilityTracker._stat(npy_path) != npy_observation:
                    return False
            except OSError:
                return False

            json_observation: FileObservation | None = None
            if record.metadata_path is not None:
                json_path = record.metadata_path.resolve()
                if not self._json_stability.is_stable(json_path):
                    return False
                json_observation = self._json_stability.observation(json_path)
                if json_observation is None:
                    return False
                try:
                    if FrameStabilityTracker._stat(json_path) != json_observation:
                        return False
                except OSError:
                    return False
            else:
                # FrameValidator intentionally discovers a canonical sidecar
                # that arrives after repository discovery. Do not let it open
                # that JSON until the watcher has observed it as a paired,
                # stable file on a later scan.
                canonical_json = record.npy_path.with_suffix(".json")
                try:
                    canonical_stat = canonical_json.stat()
                except FileNotFoundError:
                    pass
                except OSError:
                    return False
                else:
                    if stat_module.S_ISREG(canonical_stat.st_mode):
                        return False

            return self._announced.get(npy_path) == (
                npy_observation,
                json_observation,
            )

        def _on_directory_changed(self, _path: str) -> None:
            # Notifications can be coalesced; they accelerate a scan but never
            # replace the periodic fallback.
            self.scan_now()

        def _ensure_directory_watch(self) -> None:
            directory = self.repository.data_dir
            watched = {Path(path).resolve() for path in self._file_watcher.directories()}
            if directory.is_dir() and directory.resolve() not in watched:
                resolved = directory.resolve()
                if self._watch_retry_path == resolved and self._watch_retry_scans > 0:
                    self._watch_retry_scans -= 1
                    return
                if not self._file_watcher.addPath(str(directory)):
                    # The 500 ms scan remains fully functional if inotify is
                    # exhausted.  Retry periodically without flooding logs.
                    self._watch_retry_path = resolved
                    self._watch_retry_scans = 20
                    if self._watch_error_reported:
                        return
                    self._watch_error_reported = True
                    self.error.emit(f"Could not watch directory: {directory}")
                else:
                    self._watch_retry_path = None
                    self._watch_retry_scans = 0
                    self._watch_error_reported = False

        def _handle_unavailable(self, directory: Path) -> None:
            self._npy_stability.update(())
            self._json_stability.update(())
            self._missing_metadata_scans.clear()
            self._announced.clear()
            if self._last_frames:
                self._last_frames = ()
                self.frames_changed.emit(())
                self.framesChanged.emit(())
            if not self._unavailable_reported:
                self._unavailable_reported = True
                self.directory_unavailable.emit(str(directory))
            self._watch_retry_path = None
            self._watch_retry_scans = 0
            self._watch_error_reported = False

        def _reset_tracking(self) -> None:
            self._npy_stability.reset()
            self._json_stability.reset()
            self._missing_metadata_scans.clear()
            self._announced.clear()


else:

    class FrameWatcher:  # pragma: no cover - only a dependency error boundary
        """Dependency error boundary used when no Qt binding has been installed."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(
                "PyQt6 or PyQt5 is required for FrameWatcher; install "
                "ecg_qt_viewer/requirements.txt in the GUI virtual environment."
            )


__all__ = [
    "FileObservation",
    "FrameStabilityTracker",
    "FrameWatcher",
    "QT_AVAILABLE",
]

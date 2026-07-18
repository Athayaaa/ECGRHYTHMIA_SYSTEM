"""Tests for read-only frame discovery, navigation, and file stability."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ecg_qt_viewer.frame_repository import FrameRecord, FrameRepository
from ecg_qt_viewer.frame_watcher import FrameStabilityTracker


class FrameRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_npy(self, frame_id: int, *, padding: int = 6) -> Path:
        path = self.data_dir / f"frame_{frame_id:0{padding}d}_mv.npy"
        np.save(path, np.zeros((2, 3), dtype=np.float32), allow_pickle=False)
        return path

    def test_frames_are_sorted_by_numerical_id(self) -> None:
        for frame_id in (100, 10, 2, 1):
            self._create_npy(frame_id)

        repository = FrameRepository(self.data_dir)
        records = repository.refresh()

        self.assertEqual([record.frame_id for record in records], [1, 2, 10, 100])
        self.assertEqual(repository.frame_count(), 4)
        self.assertEqual(repository.first().frame_id, 1)  # type: ignore[union-attr]
        self.assertEqual(repository.latest().frame_id, 100)  # type: ignore[union-attr]

    def test_only_canonical_npy_names_are_discovered(self) -> None:
        canonical = self._create_npy(7)
        ignored_names = (
            "frame_000008_mv.csv",
            "frame_000008_mv.json",
            "frame_000008_mv.npy.tmp",
            ".frame_000008_mv.npy",
            "frame_eight_mv.npy",
            "FRAME_000008_MV.NPY",
        )
        for name in ignored_names:
            (self.data_dir / name).write_bytes(b"ignored")

        repository = FrameRepository(self.data_dir)
        records = repository.refresh()

        self.assertEqual(records, (FrameRecord(7, canonical.resolve(), None),))

    def test_metadata_is_paired_but_npy_only_is_allowed(self) -> None:
        npy_with_json = self._create_npy(1)
        metadata_path = npy_with_json.with_suffix(".json")
        metadata_path.write_text(json.dumps({"unit": "mV"}), encoding="utf-8")
        self._create_npy(2)

        repository = FrameRepository(self.data_dir)
        records = repository.refresh()

        self.assertEqual(records[0].metadata_path, metadata_path)
        self.assertTrue(records[0].has_metadata)
        self.assertIsNone(records[1].metadata_path)
        self.assertFalse(records[1].has_metadata)
        self.assertEqual(repository.load_metadata(records[0]), {"unit": "mV"})
        self.assertIsNone(repository.load_metadata(records[1]))

    def test_lookup_and_navigation_boundaries_return_none(self) -> None:
        for frame_id in (3, 11, 50):
            self._create_npy(frame_id)
        repository = FrameRepository(self.data_dir)
        repository.refresh()

        first = repository.first()
        middle = repository.get_by_id(11)
        latest = repository.latest()
        self.assertIsNotNone(first)
        self.assertIsNotNone(middle)
        self.assertIsNotNone(latest)

        self.assertIsNone(repository.previous(first))  # type: ignore[arg-type]
        self.assertEqual(repository.next(first), middle)  # type: ignore[arg-type]
        self.assertEqual(repository.previous(middle), first)  # type: ignore[arg-type]
        self.assertEqual(repository.next(middle), latest)  # type: ignore[arg-type]
        self.assertIsNone(repository.next(latest))  # type: ignore[arg-type]
        self.assertIsNone(repository.previous(9999))
        self.assertIsNone(repository.next(9999))
        self.assertIsNone(repository.get_by_index(-1))
        self.assertIsNone(repository.get_by_index(3))

    def test_missing_directory_refreshes_to_empty(self) -> None:
        repository = FrameRepository(self.data_dir / "not-created")

        self.assertEqual(repository.refresh(), ())
        self.assertEqual(repository.frame_count(), 0)
        self.assertIsNone(repository.first())
        self.assertIsNone(repository.latest())

    def test_loading_is_read_only_and_disables_pickle(self) -> None:
        path = self._create_npy(1)
        before = path.read_bytes()
        repository = FrameRepository(self.data_dir)
        record = repository.refresh()[0]

        loaded = repository.load_array(record)

        self.assertEqual(loaded.dtype, np.float32)
        self.assertEqual(path.read_bytes(), before)


class FrameStabilityTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary_directory.name)
        self.path = self.directory / "frame_000001_mv.npy"
        self.path.write_bytes(b"first")
        self.tracker = FrameStabilityTracker(required_scans=2)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_file_requires_two_unchanged_scans(self) -> None:
        self.assertNotIn(self.path.resolve(), self.tracker.update((self.path,)))
        self.assertIn(self.path.resolve(), self.tracker.update((self.path,)))
        self.assertTrue(self.tracker.is_stable(self.path))

    def test_size_change_resets_stability(self) -> None:
        self.tracker.update((self.path,))
        self.tracker.update((self.path,))
        self.path.write_bytes(b"a larger second payload")

        self.assertNotIn(self.path.resolve(), self.tracker.update((self.path,)))
        self.assertFalse(self.tracker.is_stable(self.path))
        self.assertIn(self.path.resolve(), self.tracker.update((self.path,)))

    def test_atomic_same_name_replacement_resets_stability(self) -> None:
        self.tracker.update((self.path,))
        self.tracker.update((self.path,))
        replacement = self.directory / "replacement.tmp"
        replacement.write_bytes(b"other")  # same byte length as the old file
        os.replace(replacement, self.path)

        self.assertNotIn(self.path.resolve(), self.tracker.update((self.path,)))
        self.assertIn(self.path.resolve(), self.tracker.update((self.path,)))

    def test_deletion_is_removed_safely(self) -> None:
        self.tracker.update((self.path,))
        self.tracker.update((self.path,))
        self.path.unlink()

        self.assertEqual(self.tracker.update((self.path,)), frozenset())
        self.assertFalse(self.tracker.is_stable(self.path))
        self.assertEqual(self.tracker.unchanged_scans(self.path), 0)


if __name__ == "__main__":
    unittest.main()


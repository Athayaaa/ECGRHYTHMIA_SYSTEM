"""Output-collision tests for the ProSim calibration CLI helper."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import yaml

from auto_calibrate_prosim import (
    PROTECTED_CALIBRATION_OUTPUTS,
    save_calibration_config,
)
from calibration_utils import CalibrationError


class AutoCalibrateOutputSafetyTests(unittest.TestCase):
    def test_new_output_is_written_once_and_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new_calibration.yaml"
            first = {"calibration_source": "first"}
            second = {"calibration_source": "second"}

            self.assertEqual(save_calibration_config(first, path), path.resolve())
            original = path.read_bytes()
            self.assertEqual(yaml.safe_load(original), first)

            with self.assertRaises(CalibrationError):
                save_calibration_config(second, path)
            self.assertEqual(path.read_bytes(), original)

    def test_preserved_project_calibrations_are_explicitly_protected(self) -> None:
        for path in PROTECTED_CALIBRATION_OUTPUTS:
            with self.subTest(path=path):
                with self.assertRaises(CalibrationError):
                    save_calibration_config({"unsafe": True}, path)


if __name__ == "__main__":
    unittest.main()

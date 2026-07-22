"""Tests for standard medical ECG-paper spacing."""

from __future__ import annotations

import unittest

from ecg_qt_viewer.ecg_grid_item import (
    calculate_grid_spacing,
    grid_line_values,
)


class GridScalingTests(unittest.TestCase):
    def test_25_mm_s_horizontal_spacing(self) -> None:
        spacing = calculate_grid_spacing(25, 10)
        self.assertAlmostEqual(spacing.minor_horizontal_s, 0.04)
        self.assertAlmostEqual(spacing.major_horizontal_s, 0.20)

    def test_50_mm_s_horizontal_spacing(self) -> None:
        spacing = calculate_grid_spacing(50, 10)
        self.assertAlmostEqual(spacing.minor_horizontal_s, 0.02)
        self.assertAlmostEqual(spacing.major_horizontal_s, 0.10)

    def test_5_mm_mv_vertical_spacing(self) -> None:
        spacing = calculate_grid_spacing(25, 5)
        self.assertAlmostEqual(spacing.minor_vertical_mv, 0.20)
        self.assertAlmostEqual(spacing.major_vertical_mv, 1.00)

    def test_10_mm_mv_vertical_spacing(self) -> None:
        spacing = calculate_grid_spacing(25, 10)
        self.assertAlmostEqual(spacing.minor_vertical_mv, 0.10)
        self.assertAlmostEqual(spacing.major_vertical_mv, 0.50)

    def test_20_mm_mv_vertical_spacing(self) -> None:
        spacing = calculate_grid_spacing(25, 20)
        self.assertAlmostEqual(spacing.minor_vertical_mv, 0.05)
        self.assertAlmostEqual(spacing.major_vertical_mv, 0.25)

    def test_all_speed_and_gain_combinations(self) -> None:
        expected_x = {25: (0.04, 0.20), 50: (0.02, 0.10)}
        expected_y = {
            5: (0.20, 1.00),
            10: (0.10, 0.50),
            20: (0.05, 0.25),
        }
        for speed, x_spacing in expected_x.items():
            for gain, y_spacing in expected_y.items():
                with self.subTest(speed=speed, gain=gain):
                    spacing = calculate_grid_spacing(speed, gain)
                    self.assertEqual(
                        (spacing.minor_x, spacing.major_x), x_spacing
                    )
                    self.assertEqual(
                        (spacing.minor_y, spacing.major_y), y_spacing
                    )

    def test_raw_grid_uses_engineering_time_intervals(self) -> None:
        spacing = calculate_grid_spacing(25, 10, raw_count_spacing=1000)
        self.assertEqual((spacing.minor_x, spacing.major_x), (0.1, 0.5))
        self.assertEqual((spacing.minor_y, spacing.major_y), (1000, 5000))

    def test_raw_vertical_grid_does_not_depend_on_paper_speed_or_gain(self) -> None:
        first = calculate_grid_spacing(25, 5, raw_count_spacing=500)
        second = calculate_grid_spacing(50, 20, raw_count_spacing=500)
        self.assertEqual(first, second)

    def test_visible_line_values_include_boundaries_and_zero(self) -> None:
        values = grid_line_values(-0.1, 0.1, 0.05)
        self.assertEqual(values, (-0.1, -0.05, 0.0, 0.05, 0.1))

    def test_unsupported_speed_or_gain_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calculate_grid_spacing(30, 10)
        with self.assertRaises(ValueError):
            calculate_grid_spacing(25, 15)


if __name__ == "__main__":
    unittest.main()

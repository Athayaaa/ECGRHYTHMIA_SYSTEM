"""Command-line entry point for the standalone ECGRHYTHMIA ECG viewer."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from .main_window import MainWindow


PACKAGE_DIR = Path(__file__).resolve().parent
RASPBERRY_PI_DIR = PACKAGE_DIR.parent
DEFAULT_DATA_DIR = RASPBERRY_PI_DIR / "data" / "calibrated"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="View ECGRHYTHMIA 10-second ECG frames (raw ADC or calibrated mV)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help=f"Directory containing frame files (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--source-mode",
        choices=("raw", "mv"),
        help="Source mode: 'raw' for raw ADC (frame_*.npy), 'mv' for calibrated mV (frame_*_mv.npy)",
    )
    parser.add_argument(
        "--initial-frame",
        help="Initial numeric frame ID or name such as frame_000042_mv or frame_000042",
    )
    parser.add_argument(
        "--follow-live",
        action="store_true",
        default=None,
        help="Follow newly completed frames in the selected directory",
    )
    parser.add_argument("--speed-mm-s", type=float, choices=(25.0, 50.0))
    parser.add_argument("--gain-mm-mv", type=float, choices=(5.0, 10.0, 20.0))
    parser.add_argument("--y-range-mv", type=float, choices=(0.5, 1.0, 2.0, 5.0))
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    configure_logging(args.log_level)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("ECG Qt Viewer")
    app.setApplicationDisplayName("ECGRHYTHMIA ECG Viewer")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("ECGRHYTHMIA")

    window = MainWindow(
        data_dir=args.data_dir,
        default_data_dir=DEFAULT_DATA_DIR,
        initial_frame=args.initial_frame,
        follow_live=args.follow_live,
        paper_speed_mm_s=args.speed_mm_s,
        gain_mm_mv=args.gain_mm_mv,
        y_range_mv=args.y_range_mv,
        source_mode=args.source_mode,
    )
    window.show()

    # The documented offscreen validation command must exercise construction,
    # frame loading, event processing, and shutdown without waiting forever.
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        def finish_smoke_test() -> None:
            current = window.current_frame_path
            print(f"OFFSCREEN_SMOKE_FRAME={current or 'NONE'}", flush=True)
            exit_code = 0 if window.current_frame_is_valid else 1
            window.close()
            app.exit(exit_code)

        QTimer.singleShot(1250, finish_smoke_test)

    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())

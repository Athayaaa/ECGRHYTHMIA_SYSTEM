"""Local Streamlit GUI for ADS1293 raw-count to mV calibration."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, TypeGuard

from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import MultipleLocator, NullFormatter
from matplotlib import pyplot as plt
import numpy as np
import streamlit as st
import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.yaml"
CALIBRATION_CONFIG_PATH = SCRIPT_DIR / "calibration_config.yaml"
EXPECTED_SHAPE = (2500, 3)
EXPECTED_DTYPE = np.dtype("float32")
CHANNELS = [
    ("lead_i", "Lead I", "ch1"),
    ("lead_ii", "Lead II", "ch2"),
    ("lead_iii", "Lead III", "ch3"),
]
FRAME_STEM_RE = re.compile(r"^frame_(\d{6})$")

ADC_NEGATIVE_LIMIT = -8388608.0
ADC_POSITIVE_LIMIT = 8388607.0
ADC_FULL_SCALE_RANGE = ADC_POSITIVE_LIMIT - ADC_NEGATIVE_LIMIT
SATURATION_MARGIN_COUNTS = 8192.0
SUDDEN_JUMP_THRESHOLD_COUNTS = 0.05 * ADC_FULL_SCALE_RANGE
FLOATING_PEAK_TO_PEAK_THRESHOLD_MV = 5.0
FLOATING_SUDDEN_JUMP_THRESHOLD_MV = 1.0
DEFAULT_SCALE_MV_PER_COUNT = 1e-6
DISPLAY_MODES = ["stacked_3_leads", "single_lead"]
PAPER_SPEED_OPTIONS = [25, 50]
GAIN_OPTIONS = [5, 10, 20]
Y_RANGE_MODES = ["auto", "manual"]
MODES = ["Quick View", "Calibration", "Advanced Debug"]
PRESETS = ["Floating/Input Test", "Human Quick Test", "ProSim 1mV 60BPM", "Custom"]
RESULT_PASS = "PASS"
RESULT_WARNING = "WARNING"
RESULT_FAIL = "FAIL"
RESULT_NO_DATA = "NO DATA"
CH3_CONSISTENCY_TOLERANCE_COUNTS = 1.0


class CalibrationError(Exception):
    pass


@dataclass(frozen=True)
class FrameSet:
    stem: str
    csv_path: Path
    npy_path: Path
    json_path: Path
    bin_path: Path
    frame_index: int
    sort_time: float


@dataclass(frozen=True)
class LeadMetrics:
    key: str
    label: str
    minimum: float
    maximum: float
    mean: float
    std: float
    peak_to_peak: float
    saturation_count: int
    sudden_jump_count: int
    max_abs_jump: float


@dataclass(frozen=True)
class DisplaySettings:
    show_medical_grid: bool
    display_mode: str
    selected_lead: str
    paper_speed_mm_per_s: int
    gain_mm_per_mV: int
    time_window_sec: float
    y_range_mode: str
    manual_y_min_mV: float
    manual_y_max_mV: float
    clip_display_to_y_range: bool


@dataclass(frozen=True)
class FrameContext:
    frame_set: FrameSet | None
    raw_samples: np.ndarray | None
    metadata: dict[str, Any]
    pipeline_result: str
    pipeline_issues: list[str]
    ch3_consistency_pass: bool
    ch3_max_error: float
    nan_inf_count: int
    load_error: str | None


@dataclass(frozen=True)
class QualitySummary:
    result: str
    saturation_reasons: list[str]
    jump_reasons: list[str]
    other_reasons: list[str]


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return {}
    return data


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise CalibrationError(f"{path.name} does not contain a JSON object")
    return data


def resolve_data_dir(config_path: Path = DEFAULT_CONFIG_PATH) -> Path:
    config = load_yaml(config_path)
    output_config = config.get("output", {})
    configured_dir = Path(output_config.get("directory", "data"))
    if configured_dir.is_absolute():
        return configured_dir
    return (config_path.parent / configured_dir).resolve()


def configured_source(config_path: Path = DEFAULT_CONFIG_PATH) -> str:
    config = load_yaml(config_path)
    serial_config = config.get("serial", config.get("uart", {}))
    return str(config.get("source", serial_config.get("source", "unknown")))


def ensure_data_directories(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "analysis").mkdir(parents=True, exist_ok=True)
    (data_dir / "calibrated").mkdir(parents=True, exist_ok=True)


def find_latest_complete_frame_set(data_dir: Path) -> FrameSet:
    candidates: list[FrameSet] = []

    for npy_path in data_dir.glob("frame_*.npy"):
        match = FRAME_STEM_RE.match(npy_path.stem)
        if match is None:
            continue

        stem = npy_path.stem
        csv_path = data_dir / f"{stem}.csv"
        json_path = data_dir / f"{stem}.json"
        bin_path = data_dir / f"{stem}.bin"
        if not all(path.exists() for path in (csv_path, json_path, bin_path)):
            continue

        candidates.append(
            FrameSet(
                stem=stem,
                csv_path=csv_path,
                npy_path=npy_path,
                json_path=json_path,
                bin_path=bin_path,
                frame_index=int(match.group(1)),
                sort_time=frame_sort_time(json_path),
            )
        )

    if not candidates:
        raise CalibrationError(f"no complete frame_*.csv/.bin/.npy/.json set in {data_dir}")

    return max(candidates, key=lambda frame: (frame.sort_time, frame.frame_index))


def frame_sort_time(json_path: Path) -> float:
    try:
        metadata = load_json(json_path)
        created_at = metadata.get("created_at_utc")
        if isinstance(created_at, str):
            return datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return json_path.stat().st_mtime


def load_frame(frame_set: FrameSet) -> tuple[np.ndarray, dict[str, Any]]:
    samples = np.load(frame_set.npy_path, allow_pickle=False)
    if samples.shape != EXPECTED_SHAPE:
        raise CalibrationError(
            f"{frame_set.npy_path.name} shape is {samples.shape}, expected {EXPECTED_SHAPE}"
        )
    if samples.dtype != EXPECTED_DTYPE:
        raise CalibrationError(
            f"{frame_set.npy_path.name} dtype is {samples.dtype}, expected {EXPECTED_DTYPE}"
        )

    metadata = load_json(frame_set.json_path)
    return samples, metadata


def load_latest_frame_context(data_dir: Path) -> FrameContext:
    try:
        frame_set = find_latest_complete_frame_set(data_dir)
    except (OSError, CalibrationError) as error:
        return FrameContext(
            frame_set=None,
            raw_samples=None,
            metadata={},
            pipeline_result=RESULT_NO_DATA,
            pipeline_issues=[str(error)],
            ch3_consistency_pass=False,
            ch3_max_error=float("nan"),
            nan_inf_count=0,
            load_error=str(error),
        )

    pipeline_issues: list[str] = []
    metadata: dict[str, Any] = {}
    raw_samples: np.ndarray | None = None
    ch3_consistency_pass = False
    ch3_max_error = float("nan")
    nan_inf_count = 0

    try:
        raw_samples = np.load(frame_set.npy_path, allow_pickle=False)
    except (OSError, ValueError) as error:
        pipeline_issues.append(f"failed to load NPY: {error}")

    try:
        metadata = load_json(frame_set.json_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        pipeline_issues.append(f"failed to load metadata JSON: {error}")

    if raw_samples is not None:
        if raw_samples.shape != EXPECTED_SHAPE:
            pipeline_issues.append(
                f"shape is {raw_samples.shape}, expected {EXPECTED_SHAPE}"
            )
        if raw_samples.dtype != EXPECTED_DTYPE:
            pipeline_issues.append(
                f"dtype is {raw_samples.dtype}, expected {EXPECTED_DTYPE}"
            )

        finite_mask = np.isfinite(raw_samples)
        nan_inf_count = int(raw_samples.size - np.count_nonzero(finite_mask))
        if nan_inf_count > 0:
            pipeline_issues.append(f"{nan_inf_count} NaN/Inf values detected")

        if raw_samples.ndim == 2 and raw_samples.shape[1] == 3:
            expected_ch3 = raw_samples[:, 1] - raw_samples[:, 0]
            ch3_error = np.abs(raw_samples[:, 2] - expected_ch3)
            finite_error = ch3_error[np.isfinite(ch3_error)]
            if finite_error.size:
                ch3_max_error = float(np.max(finite_error))
                ch3_consistency_pass = bool(
                    ch3_max_error <= CH3_CONSISTENCY_TOLERANCE_COUNTS
                )
            else:
                ch3_consistency_pass = False
            if not ch3_consistency_pass:
                pipeline_issues.append(
                    f"ch3 consistency failed; max error {ch3_max_error:.6g}"
                )
        else:
            pipeline_issues.append("ch3 consistency skipped because frame is not Nx3")

    pipeline_result = RESULT_PASS if not pipeline_issues else RESULT_FAIL
    return FrameContext(
        frame_set=frame_set,
        raw_samples=raw_samples,
        metadata=metadata,
        pipeline_result=pipeline_result,
        pipeline_issues=pipeline_issues,
        ch3_consistency_pass=ch3_consistency_pass,
        ch3_max_error=ch3_max_error,
        nan_inf_count=nan_inf_count,
        load_error=None if not pipeline_issues else "; ".join(pipeline_issues),
    )


def default_calibration_config() -> dict[str, Any]:
    return {
        "calibration_source": "manual",
        "expected_mV": None,
        "selected_lead": None,
        "method": None,
        "offset_count": {key: 0.0 for key, _, _ in CHANNELS},
        "scale_mV_per_count": DEFAULT_SCALE_MV_PER_COUNT,
        "invert": {key: False for key, _, _ in CHANNELS},
        "filters": {
            "remove_mean": False,
            "high_pass_cutoff_hz": 0.0,
            "low_pass_cutoff_hz": 0.0,
            "notch_50hz": False,
        },
    }


def merge_calibration_config(saved: dict[str, Any]) -> dict[str, Any]:
    config = default_calibration_config()
    for metadata_key in ("calibration_source", "expected_mV", "selected_lead", "method"):
        if metadata_key in saved:
            config[metadata_key] = saved[metadata_key]

    offset_count = saved.get("offset_count", {})
    if isinstance(offset_count, dict):
        for key, _, _ in CHANNELS:
            config["offset_count"][key] = float(offset_count.get(key, 0.0))

    if "scale_mV_per_count" in saved:
        config["scale_mV_per_count"] = float(saved["scale_mV_per_count"])

    invert = saved.get("invert", {})
    if isinstance(invert, dict):
        for key, _, _ in CHANNELS:
            config["invert"][key] = bool(invert.get(key, False))

    filters = saved.get("filters", saved.get("filter", {}))
    if isinstance(filters, dict):
        config["filters"]["remove_mean"] = bool(filters.get("remove_mean", False))
        config["filters"]["high_pass_cutoff_hz"] = float(
            filters.get("high_pass_cutoff_hz", filters.get("highpass_hz", 0.0))
        )
        config["filters"]["low_pass_cutoff_hz"] = float(
            filters.get("low_pass_cutoff_hz", filters.get("lowpass_hz", 0.0))
        )
        config["filters"]["notch_50hz"] = bool(filters.get("notch_50hz", False))

    metadata = saved.get("metadata", {})
    config["metadata"] = metadata if isinstance(metadata, dict) else {}
    return config


def initialize_ui_state(saved_config: dict[str, Any]) -> None:
    defaults = {
        "display_show_medical_grid": True,
        "display_mode": "stacked_3_leads",
        "display_selected_lead": "lead_i",
        "display_paper_speed_mm_per_s": 25,
        "display_gain_mm_per_mV": 10,
        "display_time_window_sec": 10.0,
        "display_y_range_mode": "manual",
        "display_manual_y_min_mV": -2.0,
        "display_manual_y_max_mV": 2.0,
        "display_clip_display_to_y_range": True,
        "calibration_scale_mV_per_count": float(saved_config["scale_mV_per_count"]),
        "filter_remove_mean": bool(saved_config["filters"]["remove_mean"]),
        "filter_high_pass_cutoff_hz": float(
            saved_config["filters"]["high_pass_cutoff_hz"]
        ),
        "filter_low_pass_cutoff_hz": float(
            saved_config["filters"]["low_pass_cutoff_hz"]
        ),
        "filter_notch_50hz": bool(saved_config["filters"]["notch_50hz"]),
        "conversion_mode": (
            "ProSim calibrated mV"
            if saved_config.get("calibration_source") == "prosim"
            else "Manual calibrated mV"
        ),
        "prosim_expected_mv": 1.0,
        "prosim_lead": "lead_ii",
        "prosim_frames": 6,
        "prosim_method": "robust_pp",
        "prosim_calibration_status": "",
        "prosim_auto_calibration_output": "",
    }
    for key, _, _ in CHANNELS:
        defaults[f"calibration_offset_{key}"] = float(
            saved_config["offset_count"][key]
        )
        defaults[f"calibration_invert_{key}"] = bool(saved_config["invert"][key])

    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def apply_calibration_config_to_state(saved: dict[str, Any]) -> dict[str, Any]:
    config = merge_calibration_config(saved)
    st.session_state["calibration_scale_mV_per_count"] = float(
        config["scale_mV_per_count"]
    )
    st.session_state["filter_remove_mean"] = bool(config["filters"]["remove_mean"])
    st.session_state["filter_high_pass_cutoff_hz"] = float(
        config["filters"]["high_pass_cutoff_hz"]
    )
    st.session_state["filter_low_pass_cutoff_hz"] = float(
        config["filters"]["low_pass_cutoff_hz"]
    )
    st.session_state["filter_notch_50hz"] = bool(config["filters"]["notch_50hz"])
    st.session_state["conversion_mode"] = (
        "ProSim calibrated mV"
        if config.get("calibration_source") == "prosim"
        else "Manual calibrated mV"
    )
    for key, _, _ in CHANNELS:
        st.session_state[f"calibration_offset_{key}"] = float(
            config["offset_count"][key]
        )
        st.session_state[f"calibration_invert_{key}"] = bool(config["invert"][key])
    return config


def apply_preset_defaults(preset: str) -> None:
    if st.session_state.get("active_preset") == preset:
        return

    if preset == "Floating/Input Test":
        st.session_state["filter_remove_mean"] = False
        st.session_state["display_paper_speed_mm_per_s"] = 25
        st.session_state["display_gain_mm_per_mV"] = 10
        st.session_state["display_y_range_mode"] = "auto"
        st.session_state["display_clip_display_to_y_range"] = True
    elif preset in {"Human Quick Test", "ProSim 1mV 60BPM"}:
        st.session_state["filter_remove_mean"] = True
        st.session_state["display_paper_speed_mm_per_s"] = 25
        st.session_state["display_gain_mm_per_mV"] = 10
        st.session_state["display_y_range_mode"] = "manual"
        st.session_state["display_manual_y_min_mV"] = -2.0
        st.session_state["display_manual_y_max_mV"] = 2.0
        st.session_state["display_clip_display_to_y_range"] = True

    st.session_state["active_preset"] = preset


def current_calibration_from_state() -> dict[str, Any]:
    return {
        "offset_count": {
            key: float(st.session_state[f"calibration_offset_{key}"])
            for key, _, _ in CHANNELS
        },
        "scale_mV_per_count": float(
            st.session_state["calibration_scale_mV_per_count"]
        ),
        "invert": {
            key: bool(st.session_state[f"calibration_invert_{key}"])
            for key, _, _ in CHANNELS
        },
        "filters": {
            "remove_mean": bool(st.session_state["filter_remove_mean"]),
            "high_pass_cutoff_hz": float(
                st.session_state["filter_high_pass_cutoff_hz"]
            ),
            "low_pass_cutoff_hz": float(st.session_state["filter_low_pass_cutoff_hz"]),
            "notch_50hz": bool(st.session_state["filter_notch_50hz"]),
        },
    }


def current_display_settings_from_state() -> DisplaySettings:
    return DisplaySettings(
        show_medical_grid=bool(st.session_state["display_show_medical_grid"]),
        display_mode=str(st.session_state["display_mode"]),
        selected_lead=str(st.session_state["display_selected_lead"]),
        paper_speed_mm_per_s=int(st.session_state["display_paper_speed_mm_per_s"]),
        gain_mm_per_mV=int(st.session_state["display_gain_mm_per_mV"]),
        time_window_sec=float(st.session_state["display_time_window_sec"]),
        y_range_mode=str(st.session_state["display_y_range_mode"]),
        manual_y_min_mV=float(st.session_state["display_manual_y_min_mV"]),
        manual_y_max_mV=float(st.session_state["display_manual_y_max_mV"]),
        clip_display_to_y_range=bool(
            st.session_state["display_clip_display_to_y_range"]
        ),
    )


def calibration_controls(saved_config: dict[str, Any]) -> dict[str, Any]:
    st.sidebar.header("Calibration")

    offset_count: dict[str, float] = {}
    invert: dict[str, bool] = {}
    for key, label, _ in CHANNELS:
        offset_count[key] = float(
            st.sidebar.number_input(
                f"{label} offset_count",
                value=float(saved_config["offset_count"][key]),
                step=1.0,
                format="%.6f",
            )
        )
        invert[key] = bool(
            st.sidebar.checkbox(
                f"Invert {label}",
                value=bool(saved_config["invert"][key]),
            )
        )

    scale_mV_per_count = float(
        st.sidebar.number_input(
            "scale_mV_per_count",
            value=float(saved_config["scale_mV_per_count"]),
            step=0.000001,
            format="%.12f",
        )
    )

    st.sidebar.header("Signal Processing")
    filters = {
        "remove_mean": bool(
            st.sidebar.checkbox(
                "Remove mean / baseline correction",
                value=bool(saved_config["filters"]["remove_mean"]),
            )
        ),
        "high_pass_cutoff_hz": float(
            st.sidebar.number_input(
                "High-pass cutoff (Hz)",
                min_value=0.0,
                value=float(saved_config["filters"]["high_pass_cutoff_hz"]),
                step=0.1,
                format="%.3f",
            )
        ),
        "low_pass_cutoff_hz": float(
            st.sidebar.number_input(
                "Low-pass cutoff (Hz)",
                min_value=0.0,
                value=float(saved_config["filters"]["low_pass_cutoff_hz"]),
                step=0.1,
                format="%.3f",
            )
        ),
        "notch_50hz": bool(
            st.sidebar.checkbox(
                "Notch 50 Hz",
                value=bool(saved_config["filters"]["notch_50hz"]),
            )
        ),
    }

    return {
        "offset_count": offset_count,
        "scale_mV_per_count": scale_mV_per_count,
        "invert": invert,
        "filters": filters,
    }


def display_controls() -> DisplaySettings:
    st.sidebar.header("ECG Display")
    show_medical_grid = bool(
        st.sidebar.checkbox("show_medical_grid", value=True)
    )
    display_mode = str(
        st.sidebar.selectbox(
            "display_mode",
            DISPLAY_MODES,
            index=0,
        )
    )
    selected_label = str(
        st.sidebar.selectbox(
            "selected_lead",
            [label for _, label, _ in CHANNELS],
            index=0,
        )
    )
    selected_lead = next(
        key for key, label, _ in CHANNELS if label == selected_label
    )
    paper_speed_mm_per_s = int(
        st.sidebar.selectbox(
            "paper_speed_mm_per_s",
            PAPER_SPEED_OPTIONS,
            index=PAPER_SPEED_OPTIONS.index(25),
        )
    )
    gain_mm_per_mV = int(
        st.sidebar.selectbox(
            "gain_mm_per_mV",
            GAIN_OPTIONS,
            index=GAIN_OPTIONS.index(10),
        )
    )
    time_window_sec = float(
        st.sidebar.number_input(
            "time_window_sec",
            min_value=0.1,
            value=10.0,
            step=0.5,
            format="%.3f",
        )
    )
    y_range_mode = str(
        st.sidebar.selectbox(
            "y_range_mode",
            Y_RANGE_MODES,
            index=Y_RANGE_MODES.index("manual"),
        )
    )
    manual_y_min_mV = float(
        st.sidebar.number_input(
            "manual_y_min_mV",
            value=-2.0,
            step=0.1,
            format="%.3f",
        )
    )
    manual_y_max_mV = float(
        st.sidebar.number_input(
            "manual_y_max_mV",
            value=2.0,
            step=0.1,
            format="%.3f",
        )
    )
    clip_display_to_y_range = bool(
        st.sidebar.checkbox(
            "Clip display to y-range",
            value=True,
        )
    )

    return DisplaySettings(
        show_medical_grid=show_medical_grid,
        display_mode=display_mode,
        selected_lead=selected_lead,
        paper_speed_mm_per_s=paper_speed_mm_per_s,
        gain_mm_per_mV=gain_mm_per_mV,
        time_window_sec=time_window_sec,
        y_range_mode=y_range_mode,
        manual_y_min_mV=manual_y_min_mV,
        manual_y_max_mV=manual_y_max_mV,
        clip_display_to_y_range=clip_display_to_y_range,
    )


def convert_to_mv(raw_samples: np.ndarray, calibration: dict[str, Any]) -> np.ndarray:
    converted = np.empty_like(raw_samples, dtype=np.float32)
    scale = float(calibration["scale_mV_per_count"])

    for index, (key, _, _) in enumerate(CHANNELS):
        values = (raw_samples[:, index].astype(np.float32) - float(
            calibration["offset_count"][key]
        )) * scale
        if bool(calibration["invert"][key]):
            values *= -1.0
        converted[:, index] = values

    if bool(calibration["filters"]["remove_mean"]):
        converted -= np.mean(converted, axis=0, keepdims=True).astype(np.float32)

    return converted


def compute_metrics(samples: np.ndarray, raw_counts: bool) -> list[LeadMetrics]:
    metrics: list[LeadMetrics] = []

    for index, (key, label, _) in enumerate(CHANNELS):
        lead = samples[:, index]
        finite = lead[np.isfinite(lead)]
        if finite.size == 0:
            minimum = maximum = mean = std = peak_to_peak = float("nan")
            saturation_count = 0
        else:
            minimum = float(np.min(finite))
            maximum = float(np.max(finite))
            mean = float(np.mean(finite))
            std = float(np.std(finite))
            peak_to_peak = float(np.ptp(finite))
            saturation_count = (
                int(
                    np.count_nonzero(
                        (np.abs(finite - ADC_NEGATIVE_LIMIT) <= SATURATION_MARGIN_COUNTS)
                        | (np.abs(finite - ADC_POSITIVE_LIMIT) <= SATURATION_MARGIN_COUNTS)
                    )
                )
                if raw_counts
                else 0
            )

        jumps = np.abs(np.diff(lead))
        finite_jumps = jumps[np.isfinite(jumps)]
        if finite_jumps.size == 0:
            sudden_jump_count = 0
            max_abs_jump = float("nan")
        else:
            threshold = SUDDEN_JUMP_THRESHOLD_COUNTS if raw_counts else float("inf")
            sudden_jump_count = int(np.count_nonzero(finite_jumps > threshold))
            max_abs_jump = float(np.max(finite_jumps))

        metrics.append(
            LeadMetrics(
                key=key,
                label=label,
                minimum=minimum,
                maximum=maximum,
                mean=mean,
                std=std,
                peak_to_peak=peak_to_peak,
                saturation_count=saturation_count,
                sudden_jump_count=sudden_jump_count,
                max_abs_jump=max_abs_jump,
            )
        )

    return metrics


def plot_leads(
    samples: np.ndarray,
    sample_rate_hz: int,
    ylabel: str,
    title: str,
) -> Figure:
    time_axis = np.arange(samples.shape[0], dtype=np.float32) / float(sample_rate_hz)
    fig, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True)
    for index, (_, label, _) in enumerate(CHANNELS):
        axes[index].plot(time_axis, samples[:, index], linewidth=0.8)
        axes[index].set_ylabel(ylabel)
        axes[index].set_title(label)
        axes[index].grid(True, linewidth=0.4, alpha=0.35)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle(title)
    fig.tight_layout()
    return fig


def plot_medical_ecg(
    samples_mv: np.ndarray,
    sample_rate_hz: int,
    settings: DisplaySettings,
) -> Figure:
    window_samples, time_axis = windowed_samples(
        samples_mv,
        sample_rate_hz,
        settings.time_window_sec,
    )
    channel_indices = medical_channel_indices(settings)
    lead_count = len(channel_indices)
    fig_height = 7 if lead_count == 3 else 4
    fig, axes = plt.subplots(
        lead_count,
        1,
        figsize=(12, fig_height),
        sharex=True,
        squeeze=False,
    )
    axes_list = list(axes[:, 0])

    for axis, channel_index in zip(axes_list, channel_indices):
        _, label, _ = CHANNELS[channel_index]
        values = window_samples[:, channel_index]
        y_min, y_max = resolve_medical_ylim(values, settings)
        plot_values = (
            np.clip(values, y_min, y_max)
            if settings.clip_display_to_y_range
            else values
        )
        axis.plot(
            time_axis,
            plot_values,
            color="#111111",
            linewidth=0.6,
            alpha=0.8,
        )
        axis.set_ylabel("mV")
        axis.set_title(label, loc="left", fontsize=11, fontweight="bold")
        axis.set_xlim(0.0, float(time_axis[-1]) if time_axis.size else 0.0)
        axis.set_ylim(y_min, y_max)
        axis.text(
            0.995,
            0.90,
            f"{settings.paper_speed_mm_per_s} mm/s, "
            f"{settings.gain_mm_per_mV} mm/mV",
            transform=axis.transAxes,
            ha="right",
            va="top",
            fontsize=9,
            color="#444444",
        )
        if settings.show_medical_grid:
            apply_medical_grid(axis, settings)
        else:
            axis.grid(True, linewidth=0.4, alpha=0.35)

    axes_list[-1].set_xlabel("Time (s)")
    fig.suptitle("Medical ECG View")
    fig.tight_layout()
    return fig


def windowed_samples(
    samples: np.ndarray,
    sample_rate_hz: int,
    time_window_sec: float,
) -> tuple[np.ndarray, np.ndarray]:
    sample_count = max(1, min(samples.shape[0], int(round(time_window_sec * sample_rate_hz))))
    window = samples[:sample_count]
    time_axis = np.arange(sample_count, dtype=np.float32) / float(sample_rate_hz)
    return window, time_axis


def medical_channel_indices(settings: DisplaySettings) -> list[int]:
    if settings.display_mode == "single_lead":
        for index, (key, _, _) in enumerate(CHANNELS):
            if key == settings.selected_lead:
                return [index]
    return [0, 1, 2]


def resolve_medical_ylim(
    values: np.ndarray,
    settings: DisplaySettings,
) -> tuple[float, float]:
    if (
        settings.y_range_mode == "manual"
        and settings.manual_y_min_mV < settings.manual_y_max_mV
    ):
        return settings.manual_y_min_mV, settings.manual_y_max_mV

    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return -1.0, 1.0

    minimum = float(np.min(finite))
    maximum = float(np.max(finite))
    if minimum == maximum:
        padding = max(0.5, abs(minimum) * 0.1)
    else:
        padding = max(0.1, (maximum - minimum) * 0.10)
    return minimum - padding, maximum + padding


def apply_medical_grid(axis: Axes, settings: DisplaySettings) -> None:
    small_time_sec = 1.0 / float(settings.paper_speed_mm_per_s)
    major_time_sec = 5.0 / float(settings.paper_speed_mm_per_s)
    small_mV = 1.0 / float(settings.gain_mm_per_mV)
    major_mV = 5.0 / float(settings.gain_mm_per_mV)

    axis.set_facecolor("#fffafa")
    axis.xaxis.set_minor_locator(MultipleLocator(small_time_sec))
    axis.xaxis.set_major_locator(MultipleLocator(major_time_sec))
    axis.yaxis.set_minor_locator(MultipleLocator(small_mV))
    axis.yaxis.set_major_locator(MultipleLocator(major_mV))
    axis.xaxis.set_minor_formatter(NullFormatter())
    axis.yaxis.set_minor_formatter(NullFormatter())
    axis.tick_params(axis="both", which="minor", labelbottom=False, labelleft=False)
    axis.tick_params(axis="both", which="major", labelsize=8)
    axis.grid(which="minor", color="#f4b8b8", linewidth=0.35, alpha=0.85)
    axis.grid(which="major", color="#d96f6f", linewidth=0.85, alpha=0.95)


def show_metrics(metrics: list[LeadMetrics], unit: str) -> None:
    columns = st.columns(3)
    for column, metric in zip(columns, metrics):
        with column:
            st.subheader(metric.label)
            st.metric(f"Min ({unit})", f"{metric.minimum:.6g}")
            st.metric(f"Max ({unit})", f"{metric.maximum:.6g}")
            st.metric(f"Mean ({unit})", f"{metric.mean:.6g}")
            st.metric(f"Std ({unit})", f"{metric.std:.6g}")
            st.metric(f"Peak-to-peak ({unit})", f"{metric.peak_to_peak:.6g}")


def show_quality_warnings(raw_metrics: list[LeadMetrics]) -> None:
    warnings: list[str] = []
    for metric in raw_metrics:
        if metric.saturation_count > 0:
            warnings.append(
                f"{metric.label}: {metric.saturation_count} samples near signed 24-bit limits"
            )
        if metric.sudden_jump_count > 0:
            warnings.append(
                f"{metric.label}: {metric.sudden_jump_count} sudden jumps detected; "
                f"max jump {metric.max_abs_jump:.6g} counts"
            )

    if not warnings:
        st.success("No saturation or sudden jump warning in the latest raw frame.")
        return

    for warning in warnings:
        st.warning(warning)


def medical_signal_warning_reasons(
    raw_metrics: list[LeadMetrics],
    mv_metrics: list[LeadMetrics],
) -> list[str]:
    reasons: list[str] = []

    for metric in raw_metrics:
        if metric.saturation_count > 0:
            reasons.append(
                f"{metric.label}: {metric.saturation_count} raw samples near saturation"
            )

    for metric in mv_metrics:
        if (
            np.isfinite(metric.peak_to_peak)
            and metric.peak_to_peak > FLOATING_PEAK_TO_PEAK_THRESHOLD_MV
        ):
            reasons.append(
                f"{metric.label}: peak-to-peak {metric.peak_to_peak:.3g} mV"
            )
        if (
            np.isfinite(metric.max_abs_jump)
            and metric.max_abs_jump > FLOATING_SUDDEN_JUMP_THRESHOLD_MV
        ):
            reasons.append(
                f"{metric.label}: max jump {metric.max_abs_jump:.3g} mV/sample"
            )

    return reasons


def show_medical_signal_warning_panel(reasons: list[str]) -> None:
    if not reasons:
        return

    st.warning(
        "Input appears floating or saturated. Connect RA, LA, LL, and "
        "RLD/electrodes before evaluating ECG morphology."
    )
    with st.expander("Signal warning details"):
        for reason in reasons:
            st.write(f"- {reason}")


def sidebar_workflow_controls(data_dir: Path, default_preset: str) -> tuple[str, str, bool]:
    st.sidebar.header("Workflow")
    mode = str(st.sidebar.radio("mode", MODES, index=0))
    preset = str(
        st.sidebar.selectbox(
            "preset",
            PRESETS,
            index=PRESETS.index(default_preset),
        )
    )
    auto_refresh = bool(
        st.sidebar.checkbox("Auto-refresh every 1 second", value=False)
    )
    st.sidebar.text_input("Data directory", value=str(data_dir), disabled=True)
    return mode, preset, auto_refresh


def render_header() -> None:
    st.title("ECGRHYTHMIA ADS1293 Calibration Tool")
    st.caption(
        "View latest ADS1293 frame, calibrate raw count to mV, and inspect ECG "
        "on medical grid."
    )


def render_quick_steps() -> None:
    with st.expander("Quick steps", expanded=True):
        st.markdown(
            """
1. Run `receiver.py` in Terminal 1.
2. Wait until frames are exported.
3. Open this GUI.
4. Use Quick View to inspect ECG.
5. Use Calibration mode to adjust mV conversion.
6. Export calibrated data when ready.
            """.strip()
        )


def frame_modified_timestamp(frame_set: FrameSet | None) -> float | None:
    if frame_set is None:
        return None
    paths = (
        frame_set.csv_path,
        frame_set.npy_path,
        frame_set.json_path,
        frame_set.bin_path,
    )
    try:
        return max(path.stat().st_mtime for path in paths)
    except OSError:
        return frame_set.sort_time


def format_frame_age(timestamp: float | None) -> str:
    if timestamp is None:
        return "NO DATA"
    age_sec = max(0.0, time.time() - timestamp)
    if age_sec < 60:
        return f"{age_sec:.0f}s ago"
    if age_sec < 3600:
        return f"{age_sec / 60.0:.1f}m ago"
    return f"{age_sec / 3600.0:.1f}h ago"


def format_modified_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def result_delta(result: str) -> str:
    if result == RESULT_PASS:
        return "ready"
    if result == RESULT_WARNING:
        return "check signal"
    if result == RESULT_FAIL:
        return "needs fix"
    return "waiting"


def render_status_dashboard(
    frame_context: FrameContext,
    quality_summary: QualitySummary,
    source: str,
    sample_rate_hz: int | None,
    duration_sec: float | None,
) -> None:
    frame_set = frame_context.frame_set
    modified_timestamp = frame_modified_timestamp(frame_set)
    shape_text = (
        str(tuple(frame_context.raw_samples.shape))
        if frame_context.raw_samples is not None
        else "-"
    )

    row_1 = st.columns(4)
    row_1[0].metric("Latest frame", frame_set.stem if frame_set else "NO DATA")
    row_1[1].metric("Frame age", format_frame_age(modified_timestamp))
    row_1[2].metric("Source", source)
    row_1[3].metric("Last modified", format_modified_time(modified_timestamp))

    row_2 = st.columns(4)
    row_2[0].metric(
        "Pipeline result",
        frame_context.pipeline_result,
        delta=result_delta(frame_context.pipeline_result),
    )
    row_2[1].metric(
        "Signal quality result",
        quality_summary.result,
        delta=result_delta(quality_summary.result),
    )
    row_2[2].metric("Shape", shape_text)
    if sample_rate_hz is None or duration_sec is None:
        row_2[3].metric("Sample rate / Duration", "-")
    else:
        row_2[3].metric(
            "Sample rate / Duration",
            f"{sample_rate_hz} Hz / {duration_sec:.1f}s",
        )


def display_settings_controls(compact: bool) -> DisplaySettings:
    if compact:
        cols = st.columns(4)
        with cols[0]:
            st.selectbox(
                "paper speed",
                PAPER_SPEED_OPTIONS,
                key="display_paper_speed_mm_per_s",
            )
        with cols[1]:
            st.selectbox("gain", GAIN_OPTIONS, key="display_gain_mm_per_mV")
        with cols[2]:
            st.selectbox("display mode", DISPLAY_MODES, key="display_mode")
        with cols[3]:
            st.selectbox(
                "selected lead",
                [key for key, _, _ in CHANNELS],
                key="display_selected_lead",
                format_func=lead_label_for_key,
            )
    else:
        cols = st.columns(3)
        with cols[0]:
            st.checkbox("show medical grid", key="display_show_medical_grid")
            st.selectbox(
                "paper speed",
                PAPER_SPEED_OPTIONS,
                key="display_paper_speed_mm_per_s",
            )
        with cols[1]:
            st.selectbox("gain", GAIN_OPTIONS, key="display_gain_mm_per_mV")
            st.selectbox("display mode", DISPLAY_MODES, key="display_mode")
        with cols[2]:
            st.selectbox(
                "selected lead",
                [key for key, _, _ in CHANNELS],
                key="display_selected_lead",
                format_func=lead_label_for_key,
            )
            st.number_input(
                "time window (s)",
                min_value=0.1,
                step=0.5,
                format="%.3f",
                key="display_time_window_sec",
            )

    y_cols = st.columns(4)
    with y_cols[0]:
        st.selectbox("y range", Y_RANGE_MODES, key="display_y_range_mode")
    with y_cols[1]:
        st.number_input(
            "y min (mV)",
            step=0.1,
            format="%.3f",
            key="display_manual_y_min_mV",
        )
    with y_cols[2]:
        st.number_input(
            "y max (mV)",
            step=0.1,
            format="%.3f",
            key="display_manual_y_max_mV",
        )
    with y_cols[3]:
        st.checkbox("Clip display to y-range", key="display_clip_display_to_y_range")

    return current_display_settings_from_state()


def lead_label_for_key(key: str) -> str:
    for channel_key, label, _ in CHANNELS:
        if channel_key == key:
            return label
    return key


def metrics_rows(metrics: list[LeadMetrics], unit: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metric in metrics:
        rows.append(
            {
                "lead": metric.label,
                f"min ({unit})": metric.minimum,
                f"max ({unit})": metric.maximum,
                f"mean ({unit})": metric.mean,
                f"std ({unit})": metric.std,
                f"peak_to_peak ({unit})": metric.peak_to_peak,
                "saturation_count": metric.saturation_count,
                "sudden_jump_count": metric.sudden_jump_count,
                f"max_abs_jump ({unit})": metric.max_abs_jump,
            }
        )
    return rows


def build_quality_summary(
    frame_context: FrameContext,
    raw_metrics: list[LeadMetrics],
    mv_metrics: list[LeadMetrics],
) -> QualitySummary:
    if frame_context.pipeline_result == RESULT_NO_DATA:
        return QualitySummary(RESULT_NO_DATA, [], [], [])
    if frame_context.pipeline_result == RESULT_FAIL:
        return QualitySummary(RESULT_FAIL, [], [], list(frame_context.pipeline_issues))

    saturation_reasons: list[str] = []
    jump_reasons: list[str] = []
    other_reasons: list[str] = []

    for metric in raw_metrics:
        if metric.saturation_count > 0:
            saturation_reasons.append(
                f"{metric.label}: {metric.saturation_count} samples near 24-bit limit"
            )

    for metric in mv_metrics:
        if (
            np.isfinite(metric.peak_to_peak)
            and metric.peak_to_peak > FLOATING_PEAK_TO_PEAK_THRESHOLD_MV
        ):
            saturation_reasons.append(
                f"{metric.label}: peak-to-peak {metric.peak_to_peak:.3g} mV"
            )
        if (
            np.isfinite(metric.max_abs_jump)
            and metric.max_abs_jump > FLOATING_SUDDEN_JUMP_THRESHOLD_MV
        ):
            jump_reasons.append(
                f"{metric.label}: max jump {metric.max_abs_jump:.3g} mV/sample"
            )

    result = (
        RESULT_WARNING
        if saturation_reasons or jump_reasons or other_reasons
        else RESULT_PASS
    )
    return QualitySummary(result, saturation_reasons, jump_reasons, other_reasons)


def render_warning_panels(
    frame_context: FrameContext,
    quality_summary: QualitySummary,
    preset: str,
) -> None:
    if frame_context.pipeline_result == RESULT_NO_DATA:
        st.warning("No ECG frame found. Run receiver.py first.")
        return

    if preset == "Floating/Input Test":
        st.info("Floating input is useful for wiring tests, but it is not ECG.")
    if preset == "Human Quick Test":
        st.warning(
            "Use battery/power bank only. Do not connect charger/PLN while "
            "electrodes are attached."
        )
    if quality_summary.saturation_reasons:
        st.warning("Input appears saturated/floating. Check RA, LA, LL, and RLD.")
        with st.expander("Saturation/floating details"):
            for reason in quality_summary.saturation_reasons:
                st.write(f"- {reason}")
    if quality_summary.jump_reasons:
        st.warning("Signal is unstable. Check electrodes, wiring, and power source.")
        with st.expander("Sudden jump details"):
            for reason in quality_summary.jump_reasons:
                st.write(f"- {reason}")


def render_result_summary(
    frame_context: FrameContext,
    quality_summary: QualitySummary,
) -> None:
    if frame_context.pipeline_result == RESULT_PASS:
        st.success("PIPELINE RESULT: PASS")
    elif frame_context.pipeline_result == RESULT_NO_DATA:
        st.warning("PIPELINE RESULT: NO DATA")
    else:
        st.error("PIPELINE RESULT: FAIL")

    if quality_summary.result == RESULT_PASS:
        st.success("SIGNAL QUALITY RESULT: PASS")
    elif quality_summary.result == RESULT_WARNING:
        st.warning("SIGNAL QUALITY RESULT: WARNING")
    elif quality_summary.result == RESULT_NO_DATA:
        st.warning("SIGNAL QUALITY RESULT: NO DATA")
    else:
        st.error("SIGNAL QUALITY RESULT: FAIL")


def render_file_details(frame_set: FrameSet | None, metadata: dict[str, Any]) -> None:
    with st.expander("Show file details", expanded=False):
        if frame_set is None:
            st.write("No complete frame file set is available yet.")
            return
        rows = [
            {"type": "CSV", "path": str(frame_set.csv_path)},
            {"type": "NPY", "path": str(frame_set.npy_path)},
            {"type": "JSON", "path": str(frame_set.json_path)},
            {"type": "BIN", "path": str(frame_set.bin_path)},
        ]
        st.dataframe(rows, width="stretch", hide_index=True)
        st.json(metadata)


def render_raw_metrics_expander(metrics: list[LeadMetrics]) -> None:
    with st.expander("Show raw metrics", expanded=False):
        st.dataframe(metrics_rows(metrics, "count"), width="stretch")


def render_advanced_calibration_values() -> None:
    with st.expander("Show advanced calibration values", expanded=False):
        cols = st.columns(3)
        with cols[0]:
            st.number_input(
                "high-pass cutoff (Hz)",
                min_value=0.0,
                step=0.1,
                format="%.3f",
                key="filter_high_pass_cutoff_hz",
            )
        with cols[1]:
            st.number_input(
                "low-pass cutoff (Hz)",
                min_value=0.0,
                step=0.1,
                format="%.3f",
                key="filter_low_pass_cutoff_hz",
            )
        with cols[2]:
            st.checkbox("notch 50 Hz", key="filter_notch_50hz")
        st.caption(
            "These filter settings are saved with calibration config. In this "
            "Phase 3E GUI, only mean removal is applied to plotted/exported mV."
        )


def metadata_value(config: dict[str, Any], key: str, default: str = "-") -> str:
    value = config.get(key)
    if value is None:
        return default
    return str(value)


def calibration_offset_rows() -> list[dict[str, Any]]:
    return [
        {
            "lead": label,
            "offset_count": float(st.session_state[f"calibration_offset_{key}"]),
            "invert": bool(st.session_state[f"calibration_invert_{key}"]),
        }
        for key, label, _ in CHANNELS
    ]


def render_calibration_config_summary(saved_raw_config: dict[str, Any]) -> None:
    merged = merge_calibration_config(saved_raw_config)
    metadata = merged.get("metadata", {})
    frames_used = metadata.get("frames_used") if isinstance(metadata, dict) else None

    metric_cols = st.columns(5)
    metric_cols[0].metric(
        "Calibration source",
        metadata_value(merged, "calibration_source", "manual"),
    )
    metric_cols[1].metric("Expected mV", metadata_value(merged, "expected_mV"))
    metric_cols[2].metric("Selected lead", metadata_value(merged, "selected_lead"))
    metric_cols[3].metric("Conversion mode", str(st.session_state["conversion_mode"]))
    metric_cols[4].metric(
        "scale_mV_per_count",
        f"{float(st.session_state['calibration_scale_mV_per_count']):.12g}",
    )

    st.dataframe(calibration_offset_rows(), width="stretch", hide_index=True)
    if isinstance(frames_used, list) and frames_used:
        st.caption(f"Frames used: {', '.join(str(frame) for frame in frames_used)}")


def run_prosim_auto_calibration(
    expected_mv: float,
    lead: str,
    frames: int,
    method: str,
) -> subprocess.CompletedProcess[str]:
    script_path = SCRIPT_DIR / "auto_calibrate_prosim.py"
    return subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--expected-mv",
            f"{expected_mv:g}",
            "--lead",
            lead,
            "--frames",
            str(frames),
            "--method",
            method,
            "--output",
            str(CALIBRATION_CONFIG_PATH),
        ],
        cwd=SCRIPT_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def render_prosim_auto_calibration_section(
    frame_context: FrameContext,
    saved_raw_config: dict[str, Any],
) -> None:
    st.markdown("#### ProSim Calibration Wizard")
    with st.container(border=True):
        st.markdown(
            """
1. Connect ProSim RA, LA, LL, RL/RLD.
2. Set ProSim to 60 BPM, 1 mV.
3. Run `receiver.py` until at least 6 frames exist.
4. Click `Run Auto Calibration`.
5. Load and apply `calibration_config.yaml`.
            """.strip()
        )
        st.warning(
            "Lead II is only used as the calibration reference. The resulting "
            "scale is applied to all leads."
        )

        render_calibration_config_summary(saved_raw_config)

        if st.session_state.get("prosim_calibration_status") == "success":
            st.success("Calibration applied to Lead I, Lead II, and Lead III.")
        elif st.session_state.get("prosim_calibration_status") == "failed":
            st.error("ProSim auto calibration failed.")

        last_output = st.session_state.get("prosim_auto_calibration_output")
        if isinstance(last_output, str) and last_output:
            st.code(last_output, language="text")

        control_cols = st.columns(4)
        with control_cols[0]:
            expected_mv = float(
                st.number_input(
                    "expected_mV",
                    min_value=0.001,
                    value=float(st.session_state.get("prosim_expected_mv", 1.0)),
                    step=0.1,
                    format="%.3f",
                    key="prosim_expected_mv",
                )
            )
        with control_cols[1]:
            lead = str(
                st.selectbox(
                    "calibration_reference_lead",
                    [key for key, _, _ in CHANNELS],
                    key="prosim_lead",
                    format_func=lead_label_for_key,
                )
            )
        with control_cols[2]:
            frames = int(
                st.number_input(
                    "frames_to_use",
                    min_value=1,
                    value=int(st.session_state.get("prosim_frames", 6)),
                    step=1,
                    key="prosim_frames",
                )
            )
        with control_cols[3]:
            method = str(
                st.selectbox(
                    "method",
                    ["robust_pp"],
                    key="prosim_method",
                )
            )

        button_cols = st.columns(2)
        with button_cols[0]:
            if st.button("Load and Apply calibration_config.yaml", width="stretch"):
                if not CALIBRATION_CONFIG_PATH.exists():
                    st.error(f"No calibration config found at {CALIBRATION_CONFIG_PATH}")
                else:
                    apply_calibration_config_to_state(load_yaml(CALIBRATION_CONFIG_PATH))
                    st.session_state["prosim_auto_calibration_output"] = (
                        f"Loaded {CALIBRATION_CONFIG_PATH}"
                    )
                    st.session_state["prosim_calibration_status"] = "success"
                    rerun_app()

        with button_cols[1]:
            can_run = frame_context.frame_set is not None
            if st.button(
                "Run Auto Calibration",
                width="stretch",
                disabled=not can_run,
            ):
                with st.spinner("Running ProSim auto calibration from saved frames..."):
                    result = run_prosim_auto_calibration(
                        expected_mv,
                        lead,
                        frames,
                        method,
                    )
                output = "\n".join(
                    part for part in (result.stdout.strip(), result.stderr.strip()) if part
                )
                st.session_state["prosim_auto_calibration_output"] = output
                if result.returncode == 0:
                    apply_calibration_config_to_state(load_yaml(CALIBRATION_CONFIG_PATH))
                    st.session_state["prosim_calibration_status"] = "success"
                    rerun_app()
                else:
                    st.session_state["prosim_calibration_status"] = "failed"
                    st.error("ProSim auto calibration failed.")
                    if output:
                        st.code(output, language="text")

        st.caption(
            "This tool reads exported frame files only. It does not open the serial "
            "port and does not change receiver packet, CRC, or frame export format."
        )


def valid_three_lead_frame(samples: object) -> TypeGuard[np.ndarray]:
    return (
        isinstance(samples, np.ndarray)
        and samples.ndim == 2
        and samples.shape[1] == 3
    )


def render_medical_ecg_panel(
    data_dir: Path,
    frame_set: FrameSet,
    converted_mv: np.ndarray,
    sample_rate_hz: int,
) -> None:
    settings = current_display_settings_from_state()
    if (
        settings.y_range_mode == "manual"
        and settings.manual_y_min_mV >= settings.manual_y_max_mV
    ):
        st.warning("Manual Y range is invalid; using auto range for this view.")

    medical_fig = plot_medical_ecg(converted_mv, sample_rate_hz, settings)
    st.pyplot(medical_fig, clear_figure=False)
    if st.button("Export medical ECG PNG", width="stretch"):
        png_path = export_medical_ecg_figure(data_dir, frame_set, medical_fig)
        st.success(f"Exported {png_path}")
    plt.close(medical_fig)


def render_quick_view(
    data_dir: Path,
    frame_context: FrameContext,
    converted_mv: np.ndarray | None,
    sample_rate_hz: int,
    quality_summary: QualitySummary,
    preset: str,
) -> None:
    st.subheader("Quick View")
    render_warning_panels(frame_context, quality_summary, preset)

    if frame_context.frame_set is None or converted_mv is None:
        render_file_details(frame_context.frame_set, frame_context.metadata)
        return

    st.caption(
        f"Viewing {frame_context.frame_set.stem} at {sample_rate_hz} Hz. "
        "Use the controls below only for display scale."
    )
    with st.container(border=True):
        display_settings_controls(compact=True)
    render_medical_ecg_panel(
        data_dir=data_dir,
        frame_set=frame_context.frame_set,
        converted_mv=converted_mv,
        sample_rate_hz=sample_rate_hz,
    )
    render_file_details(frame_context.frame_set, frame_context.metadata)


def set_offsets_from_current_frame(raw_samples: np.ndarray | None) -> None:
    if not valid_three_lead_frame(raw_samples):
        st.warning("Cannot calculate baseline because no valid 3-lead frame is loaded.")
        return

    for index, (key, _, _) in enumerate(CHANNELS):
        lead = raw_samples[:, index]
        finite = lead[np.isfinite(lead)]
        if finite.size:
            st.session_state[f"calibration_offset_{key}"] = float(np.mean(finite))
    rerun_app()


def render_calibration_mode(
    data_dir: Path,
    frame_context: FrameContext,
    converted_mv: np.ndarray | None,
    sample_rate_hz: int,
    preset: str,
    saved_raw_config: dict[str, Any],
) -> None:
    st.subheader("Calibration")
    if preset == "Floating/Input Test":
        st.warning("Floating input is not ECG. Use this preset only for wiring checks.")
    if preset == "Human Quick Test":
        st.warning(
            "Use battery/power bank only. Do not connect charger/PLN while "
            "electrodes are attached."
        )

    render_prosim_auto_calibration_section(frame_context, saved_raw_config)

    with st.expander("Advanced manual calibration", expanded=False):
        st.markdown("#### Raw count to mV")
        offset_cols = st.columns(3)
        for col, (key, label, _) in zip(offset_cols, CHANNELS):
            with col:
                st.number_input(
                    f"{label} offset_count",
                    step=1.0,
                    format="%.6f",
                    key=f"calibration_offset_{key}",
                )
                st.checkbox(f"Invert {label}", key=f"calibration_invert_{key}")

        cal_cols = st.columns(3)
        with cal_cols[0]:
            st.number_input(
                "scale_mV_per_count",
                step=0.000001,
                format="%.12f",
                key="calibration_scale_mV_per_count",
            )
        with cal_cols[1]:
            st.checkbox("Remove mean / baseline correction", key="filter_remove_mean")
        with cal_cols[2]:
            if st.button("Auto baseline from current frame", width="stretch"):
                set_offsets_from_current_frame(frame_context.raw_samples)

        render_advanced_calibration_values()
        if st.button("Save calibration config", width="stretch"):
            saved_path = save_calibration_config(current_calibration_from_state())
            st.success(f"Saved {saved_path}")

    can_export = (
        frame_context.frame_set is not None
        and converted_mv is not None
        and valid_three_lead_frame(converted_mv)
    )
    if st.button(
        "Export latest converted frame",
        width="stretch",
        disabled=not can_export,
    ):
        assert frame_context.frame_set is not None
        assert converted_mv is not None
        csv_path, npy_path = export_converted_frame(
            data_dir=data_dir,
            frame_set=frame_context.frame_set,
            converted_mv=converted_mv,
            sample_rate_hz=sample_rate_hz,
        )
        st.success(f"Exported {csv_path} and {npy_path}")

    if converted_mv is None or frame_context.raw_samples is None:
        st.warning("No valid ECG frame found. Run receiver.py first.")
        return

    st.markdown("#### Raw plot")
    raw_fig = plot_leads(
        frame_context.raw_samples,
        sample_rate_hz,
        "count",
        "Raw ADS1293 frame",
    )
    st.pyplot(raw_fig, clear_figure=True)

    st.markdown("#### Converted mV plot")
    mv_fig = plot_leads(converted_mv, sample_rate_hz, "mV", "Converted mV frame")
    st.pyplot(mv_fig, clear_figure=True)


def render_advanced_debug_mode(
    frame_context: FrameContext,
    raw_metrics: list[LeadMetrics],
    mv_metrics: list[LeadMetrics],
    converted_mv: np.ndarray | None,
    sample_rate_hz: int,
) -> None:
    st.subheader("Advanced Debug")
    if frame_context.frame_set is None or frame_context.raw_samples is None:
        st.warning("No ECG frame found. Run receiver.py first.")
        render_file_details(frame_context.frame_set, frame_context.metadata)
        return

    raw_fig = plot_leads(
        frame_context.raw_samples,
        sample_rate_hz,
        "count",
        "Raw ADS1293 frame",
    )
    st.pyplot(raw_fig, clear_figure=True)

    debug_cols = st.columns(4)
    debug_cols[0].metric(
        "ch3 consistency",
        "PASS" if frame_context.ch3_consistency_pass else "FAIL",
    )
    debug_cols[1].metric("ch3 max error", f"{frame_context.ch3_max_error:.6g}")
    debug_cols[2].metric("NaN/Inf count", frame_context.nan_inf_count)
    total_saturation = sum(metric.saturation_count for metric in raw_metrics)
    debug_cols[3].metric("saturation count", total_saturation)

    jump_cols = st.columns(2)
    total_jumps = sum(metric.sudden_jump_count for metric in raw_metrics)
    max_raw_jump = max(
        (
            metric.max_abs_jump
            for metric in raw_metrics
            if np.isfinite(metric.max_abs_jump)
        ),
        default=float("nan"),
    )
    jump_cols[0].metric("sudden jump count", total_jumps)
    jump_cols[1].metric("max absolute jump", f"{max_raw_jump:.6g} count")

    render_raw_metrics_expander(raw_metrics)
    with st.expander("Show converted metrics", expanded=False):
        st.dataframe(metrics_rows(mv_metrics, "mV"), width="stretch")
    render_file_details(frame_context.frame_set, frame_context.metadata)

    with st.expander("Show advanced calibration values", expanded=False):
        st.json(current_calibration_from_state())

    if converted_mv is not None:
        with st.expander("Show converted debug plot", expanded=False):
            mv_fig = plot_leads(converted_mv, sample_rate_hz, "mV", "Converted mV frame")
            st.pyplot(mv_fig, clear_figure=True)


def save_calibration_config(calibration: dict[str, Any]) -> Path:
    data = {
        "offset_count": calibration["offset_count"],
        "scale_mV_per_count": calibration["scale_mV_per_count"],
        "invert": calibration["invert"],
        "filters": calibration["filters"],
        "saved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    CALIBRATION_CONFIG_PATH.write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    return CALIBRATION_CONFIG_PATH


def export_converted_frame(
    data_dir: Path,
    frame_set: FrameSet,
    converted_mv: np.ndarray,
    sample_rate_hz: int,
) -> tuple[Path, Path]:
    export_dir = data_dir / "calibrated"
    export_dir.mkdir(parents=True, exist_ok=True)

    csv_path = export_dir / f"{frame_set.stem}_mv.csv"
    npy_path = export_dir / f"{frame_set.stem}_mv.npy"
    time_axis = np.arange(converted_mv.shape[0], dtype=np.float32) / float(sample_rate_hz)

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("time", "ch1", "ch2", "ch3"))
        for sample_time, row in zip(time_axis, converted_mv):
            writer.writerow(
                (
                    f"{float(sample_time):.3f}",
                    f"{float(row[0]):.9g}",
                    f"{float(row[1]):.9g}",
                    f"{float(row[2]):.9g}",
                )
            )

    np.save(npy_path, np.asarray(converted_mv, dtype=np.float32))
    return csv_path, npy_path


def export_medical_ecg_figure(
    data_dir: Path,
    frame_set: FrameSet,
    figure: Figure,
) -> Path:
    export_dir = data_dir / "calibrated"
    export_dir.mkdir(parents=True, exist_ok=True)
    png_path = export_dir / f"{frame_set.stem}_medical_ecg.png"
    figure.savefig(png_path, dpi=200)
    return png_path


def rerun_app() -> None:
    rerun = getattr(st, "rerun", None)
    if rerun is not None:
        rerun()
        return
    experimental_rerun = getattr(st, "experimental_rerun", None)
    if experimental_rerun is not None:
        experimental_rerun()


def main() -> None:
    st.set_page_config(
        page_title="ECGRHYTHMIA ADS1293 Calibration",
        layout="wide",
    )
    render_header()
    data_dir = resolve_data_dir()
    ensure_data_directories(data_dir)
    source = configured_source()
    saved_raw_config = load_yaml(CALIBRATION_CONFIG_PATH)
    saved_config = merge_calibration_config(saved_raw_config)
    initialize_ui_state(saved_config)

    default_preset = (
        "ProSim 1mV 60BPM"
        if saved_config.get("calibration_source") == "prosim"
        else "Floating/Input Test"
    )
    mode, preset, auto_refresh = sidebar_workflow_controls(data_dir, default_preset)
    apply_preset_defaults(preset)

    frame_context = load_latest_frame_context(data_dir)
    sample_rate_hz: int | None = None
    duration_sec: float | None = None
    converted_mv: np.ndarray | None = None
    raw_metrics: list[LeadMetrics] = []
    mv_metrics: list[LeadMetrics] = []

    if valid_three_lead_frame(frame_context.raw_samples):
        metadata_rate = frame_context.metadata.get("sample_rate_hz", 250)
        try:
            sample_rate_hz = int(metadata_rate)
        except (TypeError, ValueError):
            sample_rate_hz = 250
        if sample_rate_hz <= 0:
            sample_rate_hz = 250
        duration_sec = float(frame_context.raw_samples.shape[0]) / float(sample_rate_hz)
        converted_mv = convert_to_mv(
            frame_context.raw_samples,
            current_calibration_from_state(),
        )
        raw_metrics = compute_metrics(frame_context.raw_samples, raw_counts=True)
        mv_metrics = compute_metrics(converted_mv, raw_counts=False)

    quality_summary = build_quality_summary(frame_context, raw_metrics, mv_metrics)

    render_quick_steps()
    render_status_dashboard(
        frame_context=frame_context,
        quality_summary=quality_summary,
        source=source,
        sample_rate_hz=sample_rate_hz,
        duration_sec=duration_sec,
    )
    render_result_summary(frame_context, quality_summary)

    if mode == "Quick View":
        render_quick_view(
            data_dir=data_dir,
            frame_context=frame_context,
            converted_mv=converted_mv,
            sample_rate_hz=sample_rate_hz or 250,
            quality_summary=quality_summary,
            preset=preset,
        )
    elif mode == "Calibration":
        render_calibration_mode(
            data_dir=data_dir,
            frame_context=frame_context,
            converted_mv=converted_mv,
            sample_rate_hz=sample_rate_hz or 250,
            preset=preset,
            saved_raw_config=saved_raw_config,
        )
    else:
        render_advanced_debug_mode(
            frame_context=frame_context,
            raw_metrics=raw_metrics,
            mv_metrics=mv_metrics,
            converted_mv=converted_mv,
            sample_rate_hz=sample_rate_hz or 250,
        )

    if auto_refresh:
        time.sleep(1.0)
        rerun_app()


if __name__ == "__main__":
    main()

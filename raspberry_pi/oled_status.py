#!/usr/bin/env python3
"""ECGRHYTHMIA OLED I2C 128x64 status display (read-only).

Standalone status app for the Raspberry Pi. It ONLY reads existing system
state (files + process list) and renders a small rotating status view on a
128x64 I2C OLED, or to the console for laptop testing.

Safety contract (enforced):
  * No `import serial`, never opens /dev/tty*.
  * No `import receiver`, never launches or signals the receiver.
  * Poll interval floor 500 ms (<= 2 reads/sec).
  * Every read is wrapped: failures degrade to UNKNOWN / N/A, never crash.

See OLED_STATUS_GUIDE.md for wiring, install and usage.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:  # psutil is required for real status; guarded so --self-test docs still load
    import psutil
except Exception:  # pragma: no cover - environment dependent
    psutil = None  # type: ignore

LOG = logging.getLogger("oled_status")

SCRIPT_DIR = Path(__file__).resolve().parent

# --- constants ------------------------------------------------------------
DEFAULT_DATA_ROOT = "data/corrected_sessions"
FALLBACK_DATA_ROOT = "data"
STALE_THRESHOLD_S = 15.0
REFRESH_FLOOR_S = 0.5
DISK_FULL_PCT = 90.0
TEMP_HIGH_C = 75.0
FRAME_GLOB = "frame_*.json"
CAL_GLOB = "frame_*_mv.json"
FRAME_NUM_RE = re.compile(r"frame_(\d+)")
PREDICTION_STALE_THRESHOLD_S = 30.0

# Availability sentinels rendered on screen
NA = "N/A"
UNKNOWN = "UNKNOWN"


# ==========================================================================
# Data-root resolution
# ==========================================================================
def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = (SCRIPT_DIR / p).resolve()
    return p


def _dir_has_frames(d: Path) -> bool:
    try:
        return d.is_dir() and any(d.glob(FRAME_GLOB))
    except Exception:
        return False


def find_data_root(args) -> Path:
    """Resolve the directory scanned for frame_*.json.

    Priority: --session-dir > --data-root (if it exists & has frames) >
    fallback `data/`. Always re-resolvable each cycle (folder may move).
    """
    if getattr(args, "session_dir", None):
        return _resolve(args.session_dir)
    primary = _resolve(getattr(args, "data_root", DEFAULT_DATA_ROOT))
    if _dir_has_frames(primary) or primary.is_dir():
        # Use primary only if it actually exists; else fall back.
        if primary.is_dir() and (_dir_has_frames(primary) or True):
            if _dir_has_frames(primary):
                return primary
    fallback = _resolve(FALLBACK_DATA_ROOT)
    if _dir_has_frames(fallback) or fallback.is_dir():
        return fallback
    # Nothing usable; return primary so callers report WAITING gracefully.
    return primary


# ==========================================================================
# Metadata readers (all failure-tolerant)
# ==========================================================================
def _newest_by_index(files) -> Optional[Path]:
    best = None
    best_key = None
    for f in files:
        m = FRAME_NUM_RE.search(Path(f).name)
        idx = int(m.group(1)) if m else -1
        try:
            mtime = Path(f).stat().st_mtime
        except Exception:
            mtime = 0.0
        key = (idx, mtime)
        if best_key is None or key > best_key:
            best_key = key
            best = Path(f)
    return best


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    except Exception:  # pragma: no cover - defensive
        return None


def _sorted_by_index_desc(files):
    def key(f):
        m = FRAME_NUM_RE.search(Path(f).name)
        idx = int(m.group(1)) if m else -1
        try:
            mtime = Path(f).stat().st_mtime
        except Exception:
            mtime = 0.0
        return (idx, mtime)
    return sorted((Path(f) for f in files), key=key, reverse=True)


def find_latest_frame_meta(root: Path) -> Optional[dict]:
    try:
        files = list(root.glob(FRAME_GLOB))
    except Exception:
        return None
    for candidate in _sorted_by_index_desc(files):
        meta = _read_json(candidate)
        if meta is None:
            continue  # partial/incomplete JSON -> try next newest
        try:
            meta["_mtime"] = candidate.stat().st_mtime
            meta["_path"] = str(candidate)
        except Exception:
            pass
        return meta
    return None


def find_latest_calibrated_meta(root: Path) -> Optional[dict]:
    cal_dir = root / "calibrated"
    if not cal_dir.is_dir():
        return None
    try:
        files = list(cal_dir.glob(CAL_GLOB))
    except Exception:
        return None
    newest = _newest_by_index(files)
    if newest is None:
        return None
    return _read_json(newest)


# ==========================================================================
# Process / system readers
# ==========================================================================
def get_receiver_status() -> dict:
    """Return {running, start_time} by scanning for receiver.py cmdline."""
    result = {"running": False, "start_time": None}
    if psutil is None:
        return result
    for proc in psutil.process_iter(["cmdline", "create_time"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if any("receiver.py" in str(part) for part in cmd):
                result["running"] = True
                result["start_time"] = proc.info.get("create_time")
                return result
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return result


def get_ai_status(root: Path) -> str:
    """OK if an AI engine process is detected, else WAIT. UNKNOWN on error."""
    if psutil is None:
        return UNKNOWN
    try:
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
            except Exception:
                continue
            if "ecg-edge-ai-engine" in cmd or "ecg_edge_ai" in cmd or "ai_worker" in cmd:
                return "OK"
        return "WAIT"
    except Exception:
        return UNKNOWN


def read_latest_prediction(root: Path) -> Optional[dict]:
    """Read predictions/latest_prediction.json relative to the data root.

    Returns the parsed dict or None on any error. Checks for staleness
    against the most recent frame mtime.
    """
    # Try predictions/ as sibling of root
    candidates = [
        root / "predictions" / "latest_prediction.json",
        root.parent / "predictions" / "latest_prediction.json",
    ]
    for pred_path in candidates:
        if pred_path.is_file():
            data = _read_json(pred_path)
            if data is not None and isinstance(data, dict):
                try:
                    data["_pred_mtime"] = pred_path.stat().st_mtime
                except Exception:
                    data["_pred_mtime"] = 0.0
                return data
    return None


def get_cpu_temp() -> Optional[float]:
    # Try psutil sensors first.
    if psutil is not None and hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures()
            for _name, entries in (temps or {}).items():
                for e in entries:
                    if e.current is not None:
                        return float(e.current)
        except Exception:
            pass
    # Fallback: /sys/class/thermal.
    try:
        for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            with open(zone, "r") as fh:
                raw = fh.read().strip()
            if raw:
                val = float(raw)
                return val / 1000.0 if val > 1000 else val
    except Exception:
        pass
    return None


def get_disk_usage(root: Path) -> Optional[float]:
    if psutil is None:
        return None
    target = str(root if root.exists() else SCRIPT_DIR)
    try:
        return float(psutil.disk_usage(target).percent)
    except Exception:
        return None


def get_net_status() -> str:
    """OK if a non-loopback interface is up with an IPv4 address."""
    if psutil is None:
        return "--"
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        for iface, st in stats.items():
            if iface == "lo" or not getattr(st, "isup", False):
                continue
            for a in addrs.get(iface, []):
                fam = getattr(a, "family", None)
                if fam is not None and int(fam) == int(getattr(__import__("socket"), "AF_INET")):
                    if a.address and not a.address.startswith("127."):
                        return "OK"
        return "--"
    except Exception:
        return "--"


def get_config_verified(cal_meta: Optional[dict]) -> str:
    """OK/FAIL only if metadata explicitly states it; else UNKNOWN.

    No producer emits `config_verified` today, so this practically returns
    UNKNOWN. Hook kept for future pipeline output.
    """
    if not cal_meta:
        return UNKNOWN
    val = cal_meta.get("config_verified")
    if val is None:
        cal = cal_meta.get("calibration") or {}
        val = cal.get("config_verified")
    if val is True:
        return "OK"
    if val is False:
        return "FAIL"
    return UNKNOWN


def derive_signal_quality(meta: Optional[dict]) -> str:
    """Return signal quality from metadata if present, else N/A.

    v1 does NOT compute signal quality; only surfaces an explicit field.
    """
    if not meta:
        return NA
    for key in ("signal_quality", "signal_quality_label", "quality"):
        if key in meta and meta[key] not in (None, ""):
            return str(meta[key])
    return NA


# ==========================================================================
# Snapshot
# ==========================================================================
@dataclass
class StatusSnapshot:
    state: str = "BOOTING"
    rec_seconds: Optional[int] = None
    frame_index: Optional[int] = None
    signal: str = NA
    ads_config: str = UNKNOWN
    in_rate: str = NA
    out_rate: str = NA
    loss: Optional[int] = None
    crc_errors: Optional[int] = None
    out_of_order: Optional[int] = None
    temp_c: Optional[float] = None
    disk_pct: Optional[float] = None
    ai: str = "WAIT"
    ai_prediction: str = ""
    ai_confidence: str = ""
    ai_frame: str = ""
    net: str = "--"
    error_code: Optional[str] = None
    error_cause: str = ""
    error_hint1: str = ""
    error_hint2: str = ""
    parser_stats: dict = field(default_factory=dict)


# ==========================================================================
# Snapshot builder + state machine + error detection
# ==========================================================================
def build_snapshot(args, prev: Optional[StatusSnapshot], booted: bool) -> StatusSnapshot:
    snap = StatusSnapshot()
    now = time.time()

    root = find_data_root(args)
    frame_meta = find_latest_frame_meta(root)
    cal_meta = find_latest_calibrated_meta(root)
    recv = get_receiver_status()

    # Frame index + parser stats.
    frame_recent = False
    if frame_meta:
        idx = frame_meta.get("frame_index")
        if isinstance(idx, int):
            snap.frame_index = idx
        pstats = frame_meta.get("parser_stats") or {}
        if isinstance(pstats, dict):
            snap.parser_stats = pstats
            snap.loss = pstats.get("packet_loss_count")
            snap.crc_errors = pstats.get("crc_errors")
            snap.out_of_order = pstats.get("out_of_order_count")
        sr = frame_meta.get("sample_rate_hz")
        if sr is not None:
            snap.out_rate = f"{sr} Hz"
        mtime = frame_meta.get("_mtime")
        if isinstance(mtime, (int, float)):
            frame_recent = (now - mtime) < STALE_THRESHOLD_S
        snap.signal = derive_signal_quality(frame_meta)

    # native input rate: no producer -> N/A. Hook for future.
    if frame_meta and frame_meta.get("native_sample_rate_hz"):
        snap.in_rate = f"{frame_meta['native_sample_rate_hz']} Hz"

    snap.ads_config = get_config_verified(cal_meta)
    snap.ai = get_ai_status(root)
    snap.temp_c = get_cpu_temp()
    snap.disk_pct = get_disk_usage(root)
    snap.net = get_net_status()

    # AI prediction from latest_prediction.json
    pred = read_latest_prediction(root)
    if pred is not None:
        pred_status = pred.get("status", "")
        prediction = pred.get("prediction", "")
        confidence = pred.get("confidence_percent")
        pred_frame_id = pred.get("frame_id", "")
        pred_mtime = pred.get("_pred_mtime", 0.0)

        # Staleness check: prediction older than latest frame?
        frame_mtime = 0.0
        if frame_meta:
            frame_mtime = frame_meta.get("_mtime", 0.0) or 0.0
        is_stale = (
            frame_mtime > 0
            and pred_mtime > 0
            and (frame_mtime - pred_mtime) > PREDICTION_STALE_THRESHOLD_S
        )

        if is_stale:
            snap.ai_prediction = "STALE"
            snap.ai_confidence = ""
            snap.ai_frame = pred_frame_id[-4:] if pred_frame_id else ""
        elif pred_status == "FAIL":
            snap.ai_prediction = "ERROR"
            snap.ai_confidence = ""
            snap.ai_frame = pred_frame_id[-4:] if pred_frame_id else ""
        elif prediction == "Unclassified":
            snap.ai_prediction = "UNCLASS"
            if confidence is not None:
                snap.ai_confidence = f"{confidence:.1f}%"
            snap.ai_frame = pred_frame_id[-4:] if pred_frame_id else ""
        elif prediction:
            snap.ai_prediction = prediction.upper()[:8]
            if confidence is not None:
                snap.ai_confidence = f"{confidence:.1f}%"
            snap.ai_frame = pred_frame_id[-4:] if pred_frame_id else ""
        snap.ai = snap.ai_prediction if snap.ai_prediction else "WAIT"

    # REC duration.
    if recv["running"] and recv["start_time"]:
        snap.rec_seconds = int(max(0, now - recv["start_time"]))

    # State machine.
    if not booted:
        snap.state = "BOOTING"
    elif not recv["running"]:
        snap.state = "READY"
    elif recv["running"] and not frame_recent:
        snap.state = "WAITING"
    else:
        snap.state = "RECORDING"

    # Error detection (may override state to ERROR).
    code, cause, h1, h2 = detect_error(snap, prev, recv, frame_recent)
    if code:
        snap.state = "ERROR"
        snap.error_code = code
        snap.error_cause = cause
        snap.error_hint1 = h1
        snap.error_hint2 = h2
    return snap


def detect_error(snap, prev, recv, frame_recent):
    """Return (code, cause, hint1, hint2) or (None, ...)."""
    # E02 ADS config explicitly FAIL.
    if snap.ads_config == "FAIL":
        return ("E02", "ADS1293 config", "not verified", "check calibration")
    # E03 receiver running but no recent frame.
    if recv["running"] and not frame_recent:
        return ("E03", "no new frame", "receiver up,", "no data <15s")
    # E04 packet loss / out-of-order increase vs previous.
    if prev is not None and prev.parser_stats and snap.parser_stats:
        p_loss = prev.parser_stats.get("packet_loss_count", 0) or 0
        c_loss = snap.parser_stats.get("packet_loss_count", 0) or 0
        p_ooo = prev.parser_stats.get("out_of_order_count", 0) or 0
        c_ooo = snap.parser_stats.get("out_of_order_count", 0) or 0
        if c_loss > p_loss or c_ooo > p_ooo:
            return ("E04", "packet loss", f"loss={c_loss}", f"ooo={c_ooo}")
        p_crc = prev.parser_stats.get("crc_errors", 0) or 0
        c_crc = snap.parser_stats.get("crc_errors", 0) or 0
        if c_crc > p_crc:
            return ("E05", "CRC errors", f"crc={c_crc}", "check link")
    # E06 disk almost full.
    if snap.disk_pct is not None and snap.disk_pct > DISK_FULL_PCT:
        return ("E06", "disk full", f"{snap.disk_pct:.0f}% used", "free space")
    # E07 CPU temp too high.
    if snap.temp_c is not None and snap.temp_c > TEMP_HIGH_C:
        return ("E07", "temp high", f"{snap.temp_c:.0f} C", "add cooling")
    return (None, "", "", "")


# ==========================================================================
# Rendering (page text)
# ==========================================================================
def _fmt_mmss(seconds: Optional[int]) -> str:
    if seconds is None:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _fmt_int(v) -> str:
    return str(v) if isinstance(v, int) else NA


def _fmt_frame(v) -> str:
    return f"{v:03d}" if isinstance(v, int) else NA


def render_page_lines(snap: StatusSnapshot, page: int) -> list:
    if snap.error_code:
        return [
            f"ERROR {snap.error_code}",
            snap.error_cause[:16],
            "",
            snap.error_hint1[:16],
            snap.error_hint2[:16],
        ]
    if page == 0:
        return [
            "ECGRHYTHMIA",
            f"{snap.state:<9}{_fmt_mmss(snap.rec_seconds):>7}",
            f"FRAME     {_fmt_frame(snap.frame_index):>6}",
            f"SIGNAL {snap.signal:>9}",
        ]
    if page == 1:
        return [
            "ACQUISITION",
            f"ADS1293 {snap.ads_config:>8}",
            f"IN {snap.in_rate:>13}",
            f"OUT {snap.out_rate:>12}",
            f"LOSS {_fmt_int(snap.loss):>11}",
        ]
    # page 2
    temp = f"{snap.temp_c:.0f} C" if snap.temp_c is not None else NA
    disk = f"{snap.disk_pct:.0f}%" if snap.disk_pct is not None else NA
    # AI line: show prediction + confidence if available
    if snap.ai_prediction and snap.ai_prediction not in ("WAIT", ""):
        ai_line = f"AI: {snap.ai_prediction}"
        lines = [
            "RASPBERRY PI",
            f"TEMP {temp:>11}",
            f"DISK {disk:>11}",
            ai_line[:16],
        ]
        if snap.ai_confidence:
            lines.append(f"CONF: {snap.ai_confidence}")
        elif snap.ai_frame:
            lines.append(f"F: {snap.ai_frame}")
    else:
        lines = [
            "RASPBERRY PI",
            f"TEMP {temp:>11}",
            f"DISK {disk:>11}",
            "AI: WAIT",
            f"NET {snap.net:>12}",
        ]
    return lines


def render_console(snap: StatusSnapshot, page: int) -> str:
    lines = render_page_lines(snap, page)
    border = "+" + "-" * 22 + "+"
    body = "\n".join("| " + ln.ljust(20) + " |" for ln in lines)
    return f"{border}\n{body}\n{border}"


# ==========================================================================
# OLED backend (imported lazily; guarded for laptop use)
# ==========================================================================
class OledRenderer:
    def __init__(self, args):
        from luma.core.interface.serial import i2c  # type: ignore
        from luma.oled import device as oled_device  # type: ignore
        from PIL import ImageFont  # type: ignore

        serial_iface = i2c(port=args.i2c_bus, address=args.address)
        driver_cls = getattr(oled_device, args.driver)
        self.device = driver_cls(serial_iface)
        self.font = ImageFont.load_default()

    def render(self, snap: StatusSnapshot, page: int) -> None:
        from luma.core.render import canvas  # type: ignore

        lines = render_page_lines(snap, page)
        with canvas(self.device) as draw:
            y = 0
            for ln in lines:
                draw.text((0, y), ln, font=self.font, fill=255)
                y += 12


# ==========================================================================
# Safety self-guard
# ==========================================================================
SAFETY_MARKER = "__SAFETY_GUARD_REGION__"


def assert_safe_source() -> None:
    """Static guard: source must not import serial or open tty.

    The check ignores comments, docstrings, and the guard region itself so
    that the enumeration of forbidden tokens does not trip the check.
    """
    src_lines = Path(__file__).read_text().splitlines()
    # __SAFETY_GUARD_REGION__
    forbidden = ("i" + "mport serial", "i" + "mport receiver",
                 "/dev/" + "tty", "f" + "rom serial")
    # __SAFETY_GUARD_REGION__
    in_guard = False
    offenders = []
    for ln in src_lines:
        if SAFETY_MARKER in ln:
            in_guard = not in_guard
            continue
        if in_guard:
            continue
        stripped = ln.strip()
        if stripped.startswith("#") or stripped.startswith("*") or stripped.startswith('"'):
            continue
        for token in forbidden:
            if token in ln:
                offenders.append((token, ln.strip()))
    if offenders:
        raise AssertionError(f"unsafe token(s) in source: {offenders}")


# ==========================================================================
# CLI + main loop
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ECGRHYTHMIA read-only OLED status display")
    p.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    p.add_argument("--session-dir", default=None)
    p.add_argument("--i2c-bus", type=int, default=1)
    p.add_argument("--address", type=lambda x: int(x, 0), default=0x3C)
    p.add_argument("--driver", default="ssd1306")
    p.add_argument("--refresh", type=float, default=0.5)
    p.add_argument("--page-seconds", type=float, default=4.0)
    p.add_argument("--console", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--log-level", default="INFO")
    return p


def run(args) -> int:
    refresh = max(REFRESH_FLOOR_S, float(args.refresh))
    if float(args.refresh) < REFRESH_FLOOR_S:
        LOG.warning("refresh %.3f below floor; clamped to %.3f", args.refresh, refresh)

    renderer = None
    if not args.console:
        try:
            renderer = OledRenderer(args)
        except Exception as exc:  # E01 OLED/I2C init failed
            LOG.error("E01 OLED/I2C init failed: %s", exc)
            print("E01 OLED/I2C init failed:", exc, file=sys.stderr)
            if not args.once:
                print("Use --console for text mode without hardware.", file=sys.stderr)
            return 1

    prev: Optional[StatusSnapshot] = None
    booted = False
    n_pages = 3
    start = time.monotonic()
    try:
        while True:
            snap = build_snapshot(args, prev, booted)
            booted = True
            elapsed = time.monotonic() - start
            page = int(elapsed / max(0.5, args.page_seconds)) % n_pages
            if args.console:
                sys.stdout.write("\n" + render_console(snap, page) + "\n")
                sys.stdout.flush()
            elif renderer is not None:
                try:
                    renderer.render(snap, page)
                except Exception as exc:
                    LOG.error("OLED render error: %s", exc)
            prev = snap
            if args.once:
                return 0
            time.sleep(refresh)
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    assert_safe_source()
    if args.self_test:
        return _self_test()
    return run(args)


# ==========================================================================
# Inline self-test (no external test files)
# ==========================================================================
def _self_test() -> int:
    import tempfile

    failures = []

    def check(name, cond):
        if cond:
            print(f"PASS {name}")
        else:
            print(f"FAIL {name}")
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # metadata present -> parsed
        (root / "frame_000005.json").write_text(json.dumps({
            "frame_index": 5, "sample_rate_hz": 250,
            "parser_stats": {"packet_loss_count": 2, "crc_errors": 1,
                             "out_of_order_count": 0},
        }))
        (root / "frame_000012.json").write_text(json.dumps({
            "frame_index": 12, "sample_rate_hz": 250,
            "parser_stats": {"packet_loss_count": 3, "crc_errors": 1,
                             "out_of_order_count": 0},
        }))
        meta = find_latest_frame_meta(root)
        check("metadata_present_parsed", meta is not None and meta.get("frame_index") == 12)
        check("latest_frame_numeric_order", meta.get("frame_index") == 12)
        check("packet_loss_surfaced", meta["parser_stats"]["packet_loss_count"] == 3)

        # partial/incomplete JSON -> None but no crash
        (root / "frame_000013.json").write_text("{ not valid json")
        meta2 = find_latest_frame_meta(root)
        check("partial_json_no_crash", meta2 is not None)  # falls back to 12

        # metadata missing dir
        empty = Path(td) / "empty"
        empty.mkdir()
        check("metadata_missing_none", find_latest_frame_meta(empty) is None)

        # calibrated meta
        cal_dir = root / "calibrated"
        cal_dir.mkdir()
        (cal_dir / "frame_000012_mv.json").write_text(json.dumps({
            "unit": "mV", "calibration": {"calibration_source": "prosim"},
        }))
        cal = find_latest_calibrated_meta(root)
        check("calibrated_meta_parsed", cal is not None and cal.get("unit") == "mV")

        # config_verified mapping
        check("config_unknown", get_config_verified(cal) == UNKNOWN)
        check("config_ok", get_config_verified({"config_verified": True}) == "OK")
        check("config_fail", get_config_verified({"config_verified": False}) == "FAIL")

        # signal quality N/A when absent
        check("signal_na", derive_signal_quality(meta) == NA)

        # data-root fallback: nonexistent primary -> fallback with frames
        class A:
            session_dir = None
            data_root = str(Path(td) / "does_not_exist")
        # place a frame under fallback 'data' relative to script dir is unsafe;
        # instead test explicit session-dir override
        class B:
            session_dir = str(root)
            data_root = DEFAULT_DATA_ROOT
        check("session_dir_override", find_data_root(B()) == root)

    # cpu temp: never crashes, returns float or None
    t = get_cpu_temp()
    check("cpu_temp_no_crash", t is None or isinstance(t, float))

    # disk usage percent
    d = get_disk_usage(SCRIPT_DIR)
    check("disk_usage_percent", d is None or (0.0 <= d <= 100.0))

    # net status
    check("net_status_value", get_net_status() in ("OK", "--"))

    # console render without OLED
    snap = StatusSnapshot(state="WAITING", frame_index=118, loss=0)
    out = render_console(snap, 0)
    check("console_renders", "ECGRHYTHMIA" in out)
    check("console_page2", "ACQUISITION" in render_console(snap, 1))
    check("console_page3", "RASPBERRY PI" in render_console(snap, 2))

    # error overlay render
    esnap = StatusSnapshot(error_code="E03", error_cause="no new frame")
    check("error_overlay", "ERROR E03" in render_console(esnap, 0))

    # safety: source has no serial/tty usage
    try:
        assert_safe_source()
        check("safe_source_guard", True)
    except AssertionError:
        check("safe_source_guard", False)

    print(f"\n{len(failures)} failure(s)" if failures else "\nALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

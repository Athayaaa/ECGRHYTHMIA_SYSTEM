#!/usr/bin/env python3
"""ECGRHYTHMIA OLED I2C 128x64 status display with rotary menu control.

The OLED process renders status and controls systemd user services. It never
opens the ECG transport; only receiver.py is allowed to do that. GPIO callbacks
only enqueue small input events, and the main OLED loop handles menu actions.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    import psutil
except Exception:  # pragma: no cover - environment dependent
    psutil = None  # type: ignore

LOG = logging.getLogger("oled_status")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
RUNTIME_DIR = SCRIPT_DIR / "runtime"
CURRENT_SESSION_ENV = RUNTIME_DIR / "current_session.env"
OLED_SETTINGS_PATH = RUNTIME_DIR / "oled_settings.json"
DEFAULT_SESSION_ROOT = SCRIPT_DIR / "data" / "sessions"

DEFAULT_DATA_ROOT = "data/corrected_sessions"
FALLBACK_DATA_ROOT = "data"
STALE_THRESHOLD_S = 15.0
REFRESH_FLOOR_S = 0.5
INPUT_POLL_S = 0.03
DEFAULT_GREETING_SECONDS = 3.0
OLED_TEXT_CHARS = 21
OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_MARGIN = 3
MENU_ANIMATION_S = 0.15
TOAST_SECONDS = 2.0
DISK_FULL_PCT = 90.0
TEMP_HIGH_C = 75.0
FRAME_GLOB = "frame_*.json"
CAL_GLOB = "frame_*_mv.json"
FRAME_NUM_RE = re.compile(r"frame_(\d+)")

NA = "N/A"
UNKNOWN = "UNKNOWN"

ENCODER_A_GPIO = 13
ENCODER_B_GPIO = 19
ENCODER_BUTTON_GPIO = 26
ENCODER_BOUNCE_TIME_S = 0.05
ENCODER_HOLD_TIME_S = 1.5
ROTARY_DEBOUNCE_S = 0.06
MENU_TIMEOUT_S = 30.0

RECEIVER_SERVICE = "ecgrhythmia-receiver.service"
AI_SERVICE = "ecgrhythmia-ai.service"
MQTT_SERVICE = "ecgrhythmia-mqtt.service"
CONTROLLED_SERVICES = {
    "receiver": RECEIVER_SERVICE,
    "ai": AI_SERVICE,
    "mqtt": MQTT_SERVICE,
}


# ==========================================================================
# Atomic files
# ==========================================================================
def write_text_atomic(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, data: dict) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_key_value_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"')
    except OSError:
        return {}
    return result


def shell_quote_env(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ==========================================================================
# Settings
# ==========================================================================
@dataclass
class OledSettings:
    brightness: int = 100
    timeout_seconds: int = 0
    invert: bool = False
    encoder_reverse: bool = False
    animations: bool = True

    @classmethod
    def load(cls, path: Path = OLED_SETTINGS_PATH) -> "OledSettings":
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        brightness = int(data.get("brightness", 100))
        if brightness not in (25, 50, 75, 100):
            brightness = 100
        timeout_seconds = int(data.get("timeout_seconds", 0))
        if timeout_seconds not in (0, 30, 60, 300):
            timeout_seconds = 0
        return cls(
            brightness=brightness,
            timeout_seconds=timeout_seconds,
            invert=bool(data.get("invert", False)),
            encoder_reverse=bool(data.get("encoder_reverse", False)),
            animations=bool(data.get("animations", True)),
        )

    def save(self, path: Path = OLED_SETTINGS_PATH) -> None:
        write_json_atomic(
            path,
            {
                "brightness": self.brightness,
                "timeout_seconds": self.timeout_seconds,
                "invert": self.invert,
                "encoder_reverse": self.encoder_reverse,
                "animations": self.animations,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )


# ==========================================================================
# Data-root and metadata readers
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
    if getattr(args, "session_dir", None):
        return _resolve(args.session_dir)

    env = read_key_value_env(CURRENT_SESSION_ENV)
    active_session = env.get("SESSION_DIR")
    if active_session:
        active_path = Path(active_session)
        if active_path.is_dir():
            return active_path

    primary = _resolve(getattr(args, "data_root", DEFAULT_DATA_ROOT))
    if _dir_has_frames(primary) or primary.is_dir():
        return primary
    fallback = _resolve(FALLBACK_DATA_ROOT)
    if _dir_has_frames(fallback) or fallback.is_dir():
        return fallback
    return primary


def _read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError, ValueError):
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


def _newest_by_index(files) -> Optional[Path]:
    files = _sorted_by_index_desc(files)
    return files[0] if files else None


def find_latest_frame_meta(root: Path) -> Optional[dict]:
    try:
        files = list(root.glob(FRAME_GLOB))
    except Exception:
        return None
    for candidate in _sorted_by_index_desc(files):
        meta = _read_json(candidate)
        if meta is None:
            continue
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
        newest = _newest_by_index(list(cal_dir.glob(CAL_GLOB)))
    except Exception:
        return None
    if newest is None:
        return None
    return _read_json(newest)


def count_frame_files(root: Path) -> int:
    try:
        return len(list(root.glob("frame_*.json")))
    except Exception:
        return 0


def directory_size_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except Exception:
        return total
    return total


# ==========================================================================
# Process and system readers
# ==========================================================================
def _process_has(token: str) -> bool:
    if psutil is None:
        return False
    try:
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
            except Exception:
                continue
            if token in cmd:
                return True
    except Exception:
        return False
    return False


def get_receiver_status() -> dict:
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
        except Exception:
            continue
    return result


def get_ai_status() -> str:
    return "RUNNING" if _process_has("ai_worker.py") else "STOPPED"


def get_mqtt_status() -> str:
    return "RUNNING" if _process_has("mqtt_publisher.py") else "STOPPED"


def get_cpu_temp() -> Optional[float]:
    if psutil is not None and hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures()
            for _name, entries in (temps or {}).items():
                for entry in entries:
                    if entry.current is not None:
                        return float(entry.current)
        except Exception:
            pass
    try:
        for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            raw = Path(zone).read_text(encoding="utf-8").strip()
            if raw:
                value = float(raw)
                return value / 1000.0 if value > 1000 else value
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


def get_disk_free(root: Path) -> Optional[int]:
    if psutil is None:
        return None
    target = str(root if root.exists() else SCRIPT_DIR)
    try:
        return int(psutil.disk_usage(target).free)
    except Exception:
        return None


def get_uptime_seconds() -> Optional[int]:
    if psutil is None:
        return None
    try:
        return int(time.time() - psutil.boot_time())
    except Exception:
        return None


def get_net_status() -> str:
    return "OK" if get_ip_address() != NA else "--"


def get_ip_address() -> str:
    if psutil is None:
        return NA
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        for iface, stat in stats.items():
            if iface == "lo" or not getattr(stat, "isup", False):
                continue
            for addr in addrs.get(iface, []):
                if getattr(addr, "family", None) == socket.AF_INET:
                    if addr.address and not addr.address.startswith("127."):
                        return str(addr.address)
    except Exception:
        return NA
    return NA


def get_config_verified(cal_meta: Optional[dict]) -> str:
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
    if not meta:
        return NA
    value = meta.get("signal_quality")
    if isinstance(value, dict):
        status = value.get("status")
        return str(status) if status not in (None, "") else NA
    for key in ("signal_quality_label", "quality"):
        if meta.get(key) not in (None, ""):
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
    resampler_reset: Optional[int] = None
    temp_c: Optional[float] = None
    disk_pct: Optional[float] = None
    disk_free_bytes: Optional[int] = None
    uptime_seconds: Optional[int] = None
    ai: str = "STOPPED"
    mqtt: str = "STOPPED"
    receiver: str = "OFF"
    net: str = "--"
    ip_address: str = NA
    session_id: str = NA
    session_dir: Optional[Path] = None
    frame_count: int = 0
    session_size_bytes: int = 0
    error_code: Optional[str] = None
    error_cause: str = ""
    error_hint1: str = ""
    error_hint2: str = ""
    parser_stats: dict = field(default_factory=dict)


def build_snapshot(args, prev: Optional[StatusSnapshot], booted: bool) -> StatusSnapshot:
    snap = StatusSnapshot()
    now = time.time()

    root = find_data_root(args)
    frame_meta = find_latest_frame_meta(root)
    cal_meta = find_latest_calibrated_meta(root)
    recv = get_receiver_status()
    env = read_key_value_env(CURRENT_SESSION_ENV)

    snap.session_id = env.get("SESSION_ID", NA)
    if env.get("SESSION_DIR"):
        snap.session_dir = Path(env["SESSION_DIR"])
    elif root.is_dir():
        snap.session_dir = root

    frame_recent = False
    if frame_meta:
        idx = frame_meta.get("frame_index")
        if isinstance(idx, int):
            snap.frame_index = idx
        else:
            m = FRAME_NUM_RE.search(str(frame_meta.get("_path", "")))
            snap.frame_index = int(m.group(1)) if m else None

        pstats = frame_meta.get("parser_stats") or {}
        if isinstance(pstats, dict):
            snap.parser_stats = pstats
            snap.loss = pstats.get("packet_loss_count")
            snap.crc_errors = pstats.get("crc_errors")
            snap.out_of_order = pstats.get("out_of_order_count")
            snap.resampler_reset = pstats.get("resampler_reset_count")
        sr = frame_meta.get("sample_rate_hz")
        if sr is not None:
            snap.out_rate = f"{sr} Hz"
        native = frame_meta.get("native_sample_rate_hz")
        if native is not None:
            snap.in_rate = f"{native} Hz"
        mtime = frame_meta.get("_mtime")
        if isinstance(mtime, (int, float)):
            frame_recent = (now - mtime) < STALE_THRESHOLD_S
        snap.signal = derive_signal_quality(frame_meta)

    snap.ads_config = get_config_verified(cal_meta)
    snap.temp_c = get_cpu_temp()
    snap.disk_pct = get_disk_usage(root)
    snap.disk_free_bytes = get_disk_free(root)
    snap.uptime_seconds = get_uptime_seconds()
    snap.net = get_net_status()
    snap.ip_address = get_ip_address()
    snap.ai = get_ai_status()
    snap.mqtt = get_mqtt_status()
    snap.receiver = "ON" if recv["running"] else "OFF"

    if snap.session_dir is not None:
        snap.frame_count = count_frame_files(snap.session_dir)
        snap.session_size_bytes = directory_size_bytes(snap.session_dir)

    if recv["running"] and recv["start_time"]:
        snap.rec_seconds = int(max(0, now - recv["start_time"]))

    if not booted:
        snap.state = "BOOTING"
    elif not recv["running"]:
        snap.state = "READY"
    elif recv["running"] and not frame_recent:
        snap.state = "WAITING FRAME"
    else:
        snap.state = "RECORDING"

    code, cause, h1, h2 = detect_error(snap, prev, recv, frame_recent)
    if code:
        snap.state = "ERROR"
        snap.error_code = code
        snap.error_cause = cause
        snap.error_hint1 = h1
        snap.error_hint2 = h2
    return snap


def detect_error(snap, prev, recv, frame_recent):
    if snap.ads_config == "FAIL":
        return ("E02", "ADS CONFIG FAIL", "check sensor", "calibration")
    if recv["running"] and not frame_recent:
        return ("E03", "NO NEW FRAME", "receiver ON", "no data <15s")
    if prev is not None and prev.parser_stats and snap.parser_stats:
        p_loss = prev.parser_stats.get("packet_loss_count", 0) or 0
        c_loss = snap.parser_stats.get("packet_loss_count", 0) or 0
        p_ooo = prev.parser_stats.get("out_of_order_count", 0) or 0
        c_ooo = snap.parser_stats.get("out_of_order_count", 0) or 0
        if c_loss > p_loss or c_ooo > p_ooo:
            return ("E04", "PACKET LOSS", f"loss={c_loss}", f"ooo={c_ooo}")
        p_crc = prev.parser_stats.get("crc_errors", 0) or 0
        c_crc = snap.parser_stats.get("crc_errors", 0) or 0
        if c_crc > p_crc:
            return ("E05", "CRC ERRORS", f"crc={c_crc}", "check link")
    if snap.disk_pct is not None and snap.disk_pct > DISK_FULL_PCT:
        return ("E06", "DISK LOW", f"{snap.disk_pct:.0f}% used", "free space")
    if snap.temp_c is not None and snap.temp_c > TEMP_HIGH_C:
        return ("E07", "TEMP HIGH", f"{snap.temp_c:.0f} C", "add cooling")
    return (None, "", "", "")


# ==========================================================================
# Services and session control
# ==========================================================================
@dataclass
class ServiceResult:
    ok: bool
    status: str
    message: str = ""


class SystemServiceManager:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        timeout: float = 4.0,
    ) -> None:
        self.runner = runner or subprocess.run
        self.timeout = timeout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return self.runner(
            ["systemctl", "--user", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.timeout,
            check=False,
        )

    def exists(self, service: str) -> bool:
        try:
            result = self._run(["status", service])
        except Exception:
            return False
        text = (result.stdout or "") + (result.stderr or "")
        if "could not be found" in text.lower() or "not-found" in text.lower():
            return False
        return result.returncode in (0, 3)

    def status(self, service: str) -> str:
        if not self.exists(service):
            return "N/A"
        try:
            active = self._run(["is-active", service])
        except Exception:
            return "UNKNOWN"
        state = (active.stdout or "").strip()
        if active.returncode == 0 and state == "active":
            return "RUNNING"
        if state in ("inactive", "failed", "activating", "deactivating"):
            return state.upper()
        return "STOPPED"

    def is_active(self, service: str) -> bool:
        return self.status(service) == "RUNNING"

    def start(self, service: str) -> ServiceResult:
        if not self.exists(service):
            return ServiceResult(False, "N/A", f"{service} N/A")
        try:
            result = self._run(["start", service])
        except Exception as exc:
            return ServiceResult(False, "ERROR", str(exc))
        if result.returncode != 0:
            return ServiceResult(False, "ERROR", (result.stderr or "").strip())
        status = self.status(service)
        if status != "RUNNING":
            return ServiceResult(False, status, f"{service} {status}")
        return ServiceResult(True, status, "OK")

    def stop(self, service: str) -> ServiceResult:
        if not self.exists(service):
            return ServiceResult(True, "N/A", f"{service} N/A")
        try:
            result = self._run(["stop", service])
        except Exception as exc:
            return ServiceResult(False, "ERROR", str(exc))
        if result.returncode != 0:
            return ServiceResult(False, "ERROR", (result.stderr or "").strip())
        status = self.status(service)
        if status != "RUNNING":
            return ServiceResult(False, status, f"{service} {status}")
        return ServiceResult(True, status, "OK")

    def restart(self, service: str) -> ServiceResult:
        if not self.exists(service):
            return ServiceResult(False, "N/A", f"{service} N/A")
        try:
            result = self._run(["restart", service])
        except Exception as exc:
            return ServiceResult(False, "ERROR", str(exc))
        if result.returncode != 0:
            return ServiceResult(False, "ERROR", (result.stderr or "").strip())
        return ServiceResult(True, self.status(service), "OK")


@dataclass
class ActionResult:
    ok: bool
    title: str
    lines: list[str]


class SessionController:
    def __init__(
        self,
        service_manager: SystemServiceManager,
        session_root: Path = DEFAULT_SESSION_ROOT,
        env_path: Path = CURRENT_SESSION_ENV,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self.services = service_manager
        self.session_root = session_root
        self.env_path = env_path
        self.now_func = now_func or datetime.now

    def _unique_session_id(self) -> str:
        base = self.now_func().strftime("session_%Y%m%d_%H%M%S")
        candidate = base
        counter = 2
        while (self.session_root / candidate).exists():
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    def _write_session_env(self, session_id: str, session_dir: Path) -> dict[str, Path]:
        calibrated_dir = session_dir / "calibrated"
        model_ready_dir = session_dir / "model_ready"
        prediction_dir = session_dir / "predictions"
        for path in (session_dir, calibrated_dir, model_ready_dir, prediction_dir):
            path.mkdir(parents=True, exist_ok=True)
        values = {
            "SESSION_ID": session_id,
            "SESSION_DIR": str(session_dir),
            "CALIBRATED_DIR": str(calibrated_dir),
            "MODEL_READY_DIR": str(model_ready_dir),
            "PREDICTION_DIR": str(prediction_dir),
        }
        text = "".join(f"{key}={shell_quote_env(value)}\n" for key, value in values.items())
        write_text_atomic(self.env_path, text, mode=0o600)
        return {
            "session": session_dir,
            "calibrated": calibrated_dir,
            "model_ready": model_ready_dir,
            "prediction": prediction_dir,
        }

    def start_new_session(self) -> ActionResult:
        if self.services.is_active(RECEIVER_SERVICE):
            return ActionResult(False, "SERVICE ERROR", ["Receiver already", "RUNNING"])

        session_id = self._unique_session_id()
        session_dir = self.session_root / session_id
        self._write_session_env(session_id, session_dir)

        feedback = ["STARTING", session_id[-13:]]
        start_order = [
            ("RECEIVER", RECEIVER_SERVICE),
            ("AI", AI_SERVICE),
            ("MQTT", MQTT_SERVICE),
        ]
        for label, service in start_order:
            result = self.services.start(service)
            if not result.ok:
                feedback.append(f"{label} ERROR")
                feedback.append(result.message[:OLED_TEXT_CHARS] or result.status)
                return ActionResult(False, "SERVICE ERROR", feedback[-4:])
            feedback.append(f"{label} OK" if result.status != "N/A" else f"{label} N/A")

        feedback.append("RECORDING")
        return ActionResult(True, "RECORDING", feedback[-5:])

    def stop_recording(self) -> ActionResult:
        feedback = ["STOPPING"]
        for label, service in [
            ("MQTT", MQTT_SERVICE),
            ("AI", AI_SERVICE),
            ("RECEIVER", RECEIVER_SERVICE),
        ]:
            result = self.services.stop(service)
            if not result.ok:
                feedback.append(f"{label} ERROR")
                feedback.append(result.message[:OLED_TEXT_CHARS] or result.status)
                return ActionResult(False, "SERVICE ERROR", feedback[-4:])
            feedback.append(f"{label} OFF" if result.status != "N/A" else f"{label} N/A")
        feedback.append("SESSION SAVED")
        feedback.append("READY")
        return ActionResult(True, "READY", feedback[-5:])

    def restart_service(self, service: str, label: str) -> ActionResult:
        result = self.services.restart(service)
        if not result.ok:
            return ActionResult(False, "SERVICE ERROR", [label, result.message[:OLED_TEXT_CHARS] or result.status])
        return ActionResult(True, "SAVED", [label, "RESTARTED"])

    def reboot(self) -> ActionResult:
        try:
            result = subprocess.run(
                ["systemctl", "reboot"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3.0,
                check=False,
            )
        except Exception as exc:
            return ActionResult(False, "SERVICE ERROR", ["REBOOT ERROR", str(exc)[:OLED_TEXT_CHARS]])
        if result.returncode != 0:
            return ActionResult(False, "SERVICE ERROR", ["REBOOT ERROR", (result.stderr or "")[:OLED_TEXT_CHARS]])
        return ActionResult(True, "REBOOT", ["REBOOTING"])

    def shutdown(self) -> ActionResult:
        try:
            result = subprocess.run(
                ["systemctl", "poweroff"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3.0,
                check=False,
            )
        except Exception as exc:
            return ActionResult(False, "SERVICE ERROR", ["SHUTDOWN ERROR", str(exc)[:OLED_TEXT_CHARS]])
        if result.returncode != 0:
            return ActionResult(False, "SERVICE ERROR", ["SHUTDOWN ERROR", (result.stderr or "")[:OLED_TEXT_CHARS]])
        return ActionResult(True, "SHUTDOWN", ["POWERING OFF"])


# ==========================================================================
# Rotary input
# ==========================================================================
@dataclass
class InputEvent:
    kind: str
    delta: int = 0


class RotaryInput:
    def __init__(
        self,
        event_queue: "queue.Queue[InputEvent]",
        *,
        pin_a: int = ENCODER_A_GPIO,
        pin_b: int = ENCODER_B_GPIO,
        button_pin: int = ENCODER_BUTTON_GPIO,
        reverse: bool = False,
        rotary_cls=None,
        button_cls=None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.queue = event_queue
        self.reverse = reverse
        self.clock = clock or time.monotonic
        self.last_rotate_at = 0.0
        self.short_suppressed = False

        if rotary_cls is None or button_cls is None:
            from gpiozero import Button, RotaryEncoder  # type: ignore

            rotary_cls = rotary_cls or RotaryEncoder
            button_cls = button_cls or Button

        self.encoder = rotary_cls(pin_a, pin_b, max_steps=0, wrap=True)
        self.button = button_cls(
            button_pin,
            pull_up=True,
            bounce_time=ENCODER_BOUNCE_TIME_S,
            hold_time=ENCODER_HOLD_TIME_S,
        )
        self.encoder.when_rotated_clockwise = self._clockwise
        self.encoder.when_rotated_counter_clockwise = self._counter_clockwise
        self.button.when_pressed = self._button_pressed
        self.button.when_held = self._button_held
        self.button.when_released = self._button_released

    def _enqueue_rotate(self, delta: int) -> None:
        now = self.clock()
        if now - self.last_rotate_at < ROTARY_DEBOUNCE_S:
            return
        self.last_rotate_at = now
        if self.reverse:
            delta *= -1
        self.queue.put(InputEvent("rotate", delta))

    def _clockwise(self) -> None:
        self._enqueue_rotate(1)

    def _counter_clockwise(self) -> None:
        self._enqueue_rotate(-1)

    def _button_pressed(self) -> None:
        self.short_suppressed = False

    def _button_held(self) -> None:
        self.short_suppressed = True
        self.queue.put(InputEvent("back"))

    def _button_released(self) -> None:
        if not self.short_suppressed:
            self.queue.put(InputEvent("select"))
        self.short_suppressed = False

    def close(self) -> None:
        for item in (getattr(self, "encoder", None), getattr(self, "button", None)):
            try:
                item.close()
            except Exception:
                pass


# ==========================================================================
# Menu state
# ==========================================================================
@dataclass
class MenuItem:
    label: str
    submenu: str | None = None
    action: str | None = None


MENUS: dict[str, list[MenuItem]] = {
    "main": [
        MenuItem("Recording", "recording"),
        MenuItem("Services", "services"),
        MenuItem("Status", "status"),
        MenuItem("Display", "display"),
        MenuItem("System", "system"),
        MenuItem("Back", action="back"),
    ],
    "recording": [
        MenuItem("Start New Session", action="confirm_start"),
        MenuItem("Stop Recording", action="confirm_stop"),
        MenuItem("Session Info", action="session_info"),
        MenuItem("Back", action="back"),
    ],
    "services": [
        MenuItem("Receiver Status", action="receiver_status"),
        MenuItem("Restart Receiver", action="confirm_restart_receiver"),
        MenuItem("AI Worker Status", action="ai_status"),
        MenuItem("MQTT Status", action="mqtt_status"),
        MenuItem("Restart MQTT", action="confirm_restart_mqtt"),
        MenuItem("Back", action="back"),
    ],
    "status": [
        MenuItem("Acquisition", action="detail_acquisition"),
        MenuItem("Network", action="detail_network"),
        MenuItem("Raspberry Pi", action="detail_pi"),
        MenuItem("Storage", action="detail_storage"),
        MenuItem("Back", action="back"),
    ],
    "display": [
        MenuItem("Brightness", action="brightness"),
        MenuItem("Screen Timeout", action="screen_timeout"),
        MenuItem("Animations", action="animations"),
        MenuItem("Invert Display", action="invert_display"),
        MenuItem("Back", action="back"),
    ],
    "system": [
        MenuItem("Reboot", action="confirm_reboot"),
        MenuItem("Shutdown", action="confirm_shutdown"),
        MenuItem("Back", action="back"),
    ],
}


class MenuController:
    def __init__(
        self,
        session_controller: SessionController,
        settings: OledSettings,
        service_manager: SystemServiceManager,
        menu_timeout_s: float = MENU_TIMEOUT_S,
    ) -> None:
        self.session_controller = session_controller
        self.settings = settings
        self.service_manager = service_manager
        self.menu_timeout_s = menu_timeout_s
        self.mode = "status"
        self.menu_stack = ["main"]
        self.selected = 0
        self.last_input_at = time.monotonic()
        self.message_title = ""
        self.message_lines: list[str] = []
        self.detail_kind = ""
        self.confirm_prompt = ""
        self.confirm_action = ""
        self.confirm_selected = 0
        self.hold_action = ""
        self.adjust_kind = ""
        self.adjust_options: list[tuple[str, object]] = []
        self.adjust_selected = 0
        self.message_until = 0.0
        self.animation_started_at = 0.0
        self.animation_direction = 0

    @property
    def current_menu(self) -> str:
        return self.menu_stack[-1]

    def wake(self) -> None:
        self.last_input_at = time.monotonic()

    def animate(self, direction: int = 1) -> None:
        if self.settings.animations:
            self.animation_started_at = time.monotonic()
            self.animation_direction = 1 if direction >= 0 else -1

    def animation_active(self, now: Optional[float] = None) -> bool:
        if not self.settings.animations or not self.animation_started_at:
            return False
        elapsed = (now if now is not None else time.monotonic()) - self.animation_started_at
        return 0.0 <= elapsed < MENU_ANIMATION_S

    def animation_offset(self, now: Optional[float] = None) -> int:
        if not self.animation_active(now):
            return 0
        elapsed = (now if now is not None else time.monotonic()) - self.animation_started_at
        progress = min(1.0, elapsed / MENU_ANIMATION_S)
        return int((1.0 - progress) * 10 * self.animation_direction)

    def handle(self, event: InputEvent) -> None:
        self.wake()
        if self.mode == "status":
            if event.kind == "select":
                self.open_menu()
            return
        if event.kind == "back":
            self.back()
            return
        if self.mode == "menu":
            self._handle_menu(event)
        elif self.mode == "confirm":
            self._handle_confirm(event)
        elif self.mode == "hold_confirm":
            self._handle_hold_confirm(event)
        elif self.mode == "adjust":
            self._handle_adjust(event)
        elif self.mode in ("message", "detail"):
            if event.kind == "select":
                self.back()

    def tick(self) -> None:
        if self.mode == "message" and self.message_until and time.monotonic() >= self.message_until:
            self.mode = "menu"
            self.message_until = 0.0
        if self.mode != "status" and time.monotonic() - self.last_input_at > self.menu_timeout_s:
            self.mode = "status"
            self.menu_stack = ["main"]
            self.selected = 0

    def open_menu(self) -> None:
        self.mode = "menu"
        self.menu_stack = ["main"]
        self.selected = 0
        self.animate(1)

    def back(self) -> None:
        if self.mode == "hold_confirm":
            self._execute_hold_action()
            return
        if self.mode in ("message", "detail", "confirm", "adjust"):
            self.mode = "menu"
            self.animate(-1)
            return
        if self.mode == "menu" and len(self.menu_stack) > 1:
            self.menu_stack.pop()
            self.selected = 0
            self.animate(-1)
            return
        self.mode = "status"

    def _handle_menu(self, event: InputEvent) -> None:
        items = MENUS[self.current_menu]
        if event.kind == "rotate":
            self.selected = (self.selected + event.delta) % len(items)
            self.animate(event.delta)
            return
        if event.kind != "select":
            return
        item = items[self.selected]
        if item.submenu:
            self.menu_stack.append(item.submenu)
            self.selected = 0
            self.animate(1)
            return
        self._run_menu_action(item.action or "")

    def _handle_confirm(self, event: InputEvent) -> None:
        if event.kind == "rotate":
            self.confirm_selected = (self.confirm_selected + event.delta) % 2
            self.animate(event.delta)
            return
        if event.kind != "select":
            return
        if self.confirm_selected == 0:
            self.show_message("CANCELLED", ["NO ACTION"])
            return
        if self.confirm_action in ("reboot", "shutdown"):
            self.mode = "hold_confirm"
            self.hold_action = self.confirm_action
            return
        self._execute_action(self.confirm_action)

    def _handle_hold_confirm(self, event: InputEvent) -> None:
        if event.kind == "select":
            return

    def _handle_adjust(self, event: InputEvent) -> None:
        if event.kind == "rotate":
            self.adjust_selected = (self.adjust_selected + event.delta) % len(self.adjust_options)
            self.animate(event.delta)
            return
        if event.kind != "select":
            return
        label, value = self.adjust_options[self.adjust_selected]
        if self.adjust_kind == "brightness":
            self.settings.brightness = int(value)
        elif self.adjust_kind == "timeout":
            self.settings.timeout_seconds = int(value)
        self.settings.save()
        self.show_message("SAVED", [self.adjust_kind.upper(), label])

    def _run_menu_action(self, action: str) -> None:
        if action == "back":
            self.back()
        elif action == "confirm_start":
            self.confirm("START SESSION?", "start")
        elif action == "confirm_stop":
            self.confirm("STOP RECORDING?", "stop")
        elif action == "confirm_restart_receiver":
            self.confirm("RESTART RCVR?", "restart_receiver")
        elif action == "confirm_restart_mqtt":
            self.confirm("RESTART MQTT?", "restart_mqtt")
        elif action == "confirm_reboot":
            self.confirm("REBOOT?", "reboot")
        elif action == "confirm_shutdown":
            self.confirm("SHUTDOWN?", "shutdown")
        elif action == "brightness":
            self.adjust("brightness", [("25%", 25), ("50%", 50), ("75%", 75), ("100%", 100)])
        elif action == "screen_timeout":
            self.adjust("timeout", [("Off", 0), ("30 sec", 30), ("60 sec", 60), ("5 min", 300)])
        elif action == "animations":
            self.settings.animations = not self.settings.animations
            self.settings.save()
            self.show_message("SAVED", ["ANIMATIONS", "ON" if self.settings.animations else "OFF"])
        elif action == "invert_display":
            self.settings.invert = not self.settings.invert
            self.settings.save()
            self.show_message("SAVED", ["INVERT", "ON" if self.settings.invert else "OFF"])
        elif action.startswith("detail_"):
            self.mode = "detail"
            self.detail_kind = action.removeprefix("detail_")
        elif action == "session_info":
            self.mode = "detail"
            self.detail_kind = "storage"
        elif action.endswith("_status"):
            self.mode = "detail"
            self.detail_kind = action

    def confirm(self, prompt: str, action: str) -> None:
        self.mode = "confirm"
        self.confirm_prompt = prompt
        self.confirm_action = action
        self.confirm_selected = 0

    def adjust(self, kind: str, options: list[tuple[str, object]]) -> None:
        self.mode = "adjust"
        self.adjust_kind = kind
        self.adjust_options = options
        self.adjust_selected = 0

    def show_message(self, title: str, lines: list[str]) -> None:
        self.mode = "message"
        self.message_title = title
        self.message_lines = lines[:4]
        self.message_until = time.monotonic() + TOAST_SECONDS

    def _execute_action(self, action: str) -> None:
        if action == "start":
            result = self.session_controller.start_new_session()
        elif action == "stop":
            result = self.session_controller.stop_recording()
        elif action == "restart_receiver":
            result = self.session_controller.restart_service(RECEIVER_SERVICE, "RECEIVER")
        elif action == "restart_mqtt":
            result = self.session_controller.restart_service(MQTT_SERVICE, "MQTT")
        else:
            result = ActionResult(False, "SERVICE ERROR", ["UNKNOWN ACTION"])
        self.show_message(result.title, result.lines)

    def _execute_hold_action(self) -> None:
        if self.hold_action == "reboot":
            result = self.session_controller.reboot()
        elif self.hold_action == "shutdown":
            result = self.session_controller.shutdown()
        else:
            result = ActionResult(False, "CANCELLED", ["NO ACTION"])
        self.show_message(result.title, result.lines)

    def render(self, snap: StatusSnapshot) -> list[str]:
        if self.mode == "status":
            return render_main_lines(snap)
        if self.mode == "menu":
            return render_menu_lines(self.current_menu, self.selected)
        if self.mode == "confirm":
            return render_confirm_lines(self.confirm_prompt, self.confirm_selected)
        if self.mode == "hold_confirm":
            title = "HOLD TO " + self.hold_action.upper()
            return [title[:OLED_TEXT_CHARS], "", "Long press", "to confirm", "Short: cancel"]
        if self.mode == "adjust":
            return render_adjust_lines(self.adjust_kind, self.adjust_options, self.adjust_selected)
        if self.mode == "detail":
            return render_detail_lines(self.detail_kind, snap, self.service_manager)
        if self.mode == "message":
            return [
                self.message_title[:OLED_TEXT_CHARS],
                *[line[:OLED_TEXT_CHARS] for line in self.message_lines],
            ]
        return render_main_lines(snap)


# ==========================================================================
# Rendering
# ==========================================================================
def _fmt_mmss(seconds: Optional[int]) -> str:
    if seconds is None:
        return "--:--"
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes:02d}:{sec:02d}"


def _fmt_hms(seconds: Optional[int]) -> str:
    if seconds is None:
        return NA
    hours, rem = divmod(int(seconds), 3600)
    minutes, sec = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{sec:02d}"


def _fmt_int(value) -> str:
    return str(value) if isinstance(value, int) else NA


def _fmt_frame(value) -> str:
    return f"F{value:04d}" if isinstance(value, int) else "F----"


def _fmt_bytes(value: Optional[int]) -> str:
    if value is None:
        return NA
    units = ("B", "KB", "MB", "GB")
    amount = float(value)
    idx = 0
    while amount >= 1024 and idx < len(units) - 1:
        amount /= 1024
        idx += 1
    return f"{amount:.1f}{units[idx]}" if idx else f"{int(amount)}B"


def render_main_lines(snap: StatusSnapshot) -> list[str]:
    if snap.error_code:
        return [
            "ECGRHYTHMIA",
            f"ERROR {snap.error_code}",
            snap.error_cause[:OLED_TEXT_CHARS],
            "Press: Menu",
        ]
    if snap.state == "RECORDING":
        return [
            "ECGRHYTHMIA",
            "RECORDING",
            f"{_fmt_mmss(snap.rec_seconds)}  {_fmt_frame(snap.frame_index)}",
            "Press: Menu",
        ]
    if snap.state == "WAITING FRAME":
        return [
            "ECGRHYTHMIA",
            "WAITING FRAME",
            f"Receiver: {snap.receiver}",
            "Press: Menu",
        ]
    return [
        "ECGRHYTHMIA",
        snap.state[:OLED_TEXT_CHARS],
        f"Last: {_fmt_frame(snap.frame_index)}",
        "Press: Menu",
    ]


def render_greeting_lines() -> list[str]:
    return [
        "ECGRHYTHMIA",
        "Edge AI ECG",
        "Starting...",
        "Press: Menu",
    ]


def render_page_lines(snap: StatusSnapshot, page: int) -> list[str]:
    if page == 0:
        return render_main_lines(snap)
    if page == 1:
        return render_detail_lines("acquisition", snap, None)
    return render_detail_lines("pi", snap, None)


def render_menu_lines(menu_name: str, selected: int) -> list[str]:
    items = MENUS[menu_name]
    title = menu_name.upper()[:OLED_TEXT_CHARS]
    start = max(0, min(selected - 1, max(0, len(items) - 4)))
    visible = items[start : start + 4]
    lines = [title]
    for idx, item in enumerate(visible, start=start):
        prefix = ">" if idx == selected else " "
        lines.append(f"{prefix}{item.label}"[:OLED_TEXT_CHARS])
    return lines


def render_confirm_lines(prompt: str, selected: int) -> list[str]:
    return [
        prompt[:OLED_TEXT_CHARS],
        f"{'>' if selected == 0 else ' '}NO",
        f"{'>' if selected == 1 else ' '}YES",
    ]


def render_adjust_lines(kind: str, options: list[tuple[str, object]], selected: int) -> list[str]:
    lines = [kind.upper()[:OLED_TEXT_CHARS]]
    for idx, (label, _value) in enumerate(options[:4]):
        prefix = ">" if idx == selected else " "
        lines.append(f"{prefix}{label}"[:OLED_TEXT_CHARS])
    return lines


def render_detail_lines(
    kind: str,
    snap: StatusSnapshot,
    services: SystemServiceManager | None,
) -> list[str]:
    if kind in ("receiver_status",):
        status = services.status(RECEIVER_SERVICE) if services else snap.receiver
        return ["RECEIVER", f"STATUS {status}"[:OLED_TEXT_CHARS], "Back: hold"]
    if kind in ("ai_status",):
        status = services.status(AI_SERVICE) if services else snap.ai
        return ["AI WORKER", f"STATUS {status}"[:OLED_TEXT_CHARS], "Back: hold"]
    if kind in ("mqtt_status",):
        status = services.status(MQTT_SERVICE) if services else snap.mqtt
        return ["MQTT", f"STATUS {status}"[:OLED_TEXT_CHARS], "Back: hold"]
    if kind == "acquisition":
        return [
            "ACQUISITION",
            f"RCV {snap.receiver}",
            f"ADS {snap.ads_config}",
            f"SR {snap.out_rate}",
            f"{_fmt_frame(snap.frame_index)} L{_fmt_int(snap.loss)} C{_fmt_int(snap.crc_errors)}",
        ]
    if kind == "network":
        return [
            "NETWORK",
            f"IP {snap.ip_address}"[:OLED_TEXT_CHARS],
            f"MQTT {snap.mqtt}",
            f"NET {snap.net}",
        ]
    if kind == "pi":
        temp = f"{snap.temp_c:.0f}C" if snap.temp_c is not None else NA
        disk = f"{snap.disk_pct:.0f}%" if snap.disk_pct is not None else NA
        return [
            "RASPBERRY PI",
            f"TEMP {temp}",
            f"DISK {disk}",
            f"UP {_fmt_hms(snap.uptime_seconds)}"[:OLED_TEXT_CHARS],
        ]
    if kind == "storage":
        return [
            "STORAGE",
            snap.session_id[-16:],
            f"FRAMES {snap.frame_count}",
            f"SIZE {_fmt_bytes(snap.session_size_bytes)}",
            f"FREE {_fmt_bytes(snap.disk_free_bytes)}",
        ]
    return ["STATUS", UNKNOWN]


def render_console(lines: list[str]) -> str:
    border = "+" + "-" * 22 + "+"
    body = "\n".join("| " + line.ljust(20)[:20] + " |" for line in lines)
    return f"{border}\n{body}\n{border}"


# ==========================================================================
# Pixel UI drawing
# ==========================================================================
def _text_width(draw, text: str, font) -> int:
    try:
        return int(draw.textbbox((0, 0), text, font=font)[2])
    except AttributeError:  # pragma: no cover - legacy Pillow
        return int(draw.textsize(text, font=font)[0])


def truncate_text(draw, text: object, font, max_width: int) -> str:
    """Return text that fits in max_width pixels, adding an ellipsis if needed."""
    value = str(text if text not in (None, "") else NA)
    if _text_width(draw, value, font) <= max_width:
        return value
    suffix = "..."
    while value and _text_width(draw, value + suffix, font) > max_width:
        value = value[:-1]
    return (value + suffix) if value else suffix


def _rounded_rectangle(draw, box, radius: int, **kwargs) -> None:
    try:
        draw.rounded_rectangle(box, radius=radius, **kwargs)
    except AttributeError:  # pragma: no cover - old Pillow on older Pi images
        draw.rectangle(box, **kwargs)


def draw_icon(draw, name: str, x: int, y: int, size: int = 12, fill: int = 255) -> None:
    """Draw compact, deliberately simple icons without external image assets."""
    s = max(8, size)
    x2, y2 = x + s - 1, y + s - 1
    mid_x, mid_y = x + s // 2, y + s // 2
    width = max(1, s // 7)
    if name in ("recording", "stop"):
        if name == "recording":
            draw.ellipse((x + 2, y + 2, x2 - 2, y2 - 2), outline=fill, fill=fill)
        else:
            draw.rectangle((x + 2, y + 2, x2 - 2, y2 - 2), outline=fill, fill=fill)
    elif name in ("heart", "session"):
        draw.polygon([(mid_x, y2 - 1), (x + 1, y + s // 2), (x + 2, y + 2),
                      (mid_x, y + 4), (x2 - 2, y + 2), (x2 - 1, y + s // 2)], fill=fill)
    elif name == "services":
        for yy in (y + 2, y + s // 2, y2 - 2):
            draw.rectangle((x + 2, yy, x2 - 2, yy + width), fill=fill)
    elif name == "receiver":
        draw.rectangle((x + 2, y + 4, x2 - 2, y2 - 2), outline=fill)
        draw.line((x + 4, y + 2, x + 4, y + 4), fill=fill)
        draw.line((x2 - 4, y + 2, x2 - 4, y + 4), fill=fill)
        draw.line((x + 4, mid_y, x + 6, mid_y - 2, x + 8, mid_y + 2, x2 - 3, mid_y - 2), fill=fill)
    elif name == "ai":
        draw.rectangle((x + 2, y + 2, x2 - 2, y2 - 2), outline=fill)
        for px, py in ((x + 4, y + 4), (x2 - 4, y + 4), (mid_x, y2 - 4)):
            draw.ellipse((px - 1, py - 1, px + 1, py + 1), fill=fill)
    elif name == "mqtt":
        draw.arc((x + 1, y + 2, x2 - 1, y2 + 4), 200, 340, fill=fill)
        draw.arc((x + 3, y + 4, x2 - 3, y2 + 1), 200, 340, fill=fill)
        draw.ellipse((mid_x - 1, mid_y - 1, mid_x + 1, mid_y + 1), fill=fill)
    elif name == "acquisition":
        draw.line((x, mid_y, x + 2, mid_y, x + 4, y + 3, x + 6, y2 - 2,
                   x + 8, y + 5, x + 10, mid_y, x2, mid_y), fill=fill, width=width)
    elif name == "network":
        for inset in (0, 2, 4):
            draw.arc((x + inset, y + inset, x2 - inset, y2 - inset), 205, 335, fill=fill)
        draw.ellipse((mid_x - 1, y2 - 3, mid_x + 1, y2 - 1), fill=fill)
    elif name == "pi":
        draw.ellipse((x + 3, y + 1, x2 - 3, y2 - 2), outline=fill)
        for px, py in ((x + 1, y + 3), (x2 - 1, y + 3), (x + 1, y2 - 3), (x2 - 1, y2 - 3)):
            draw.line((mid_x, mid_y, px, py), fill=fill)
        draw.ellipse((mid_x - 1, mid_y - 1, mid_x + 1, mid_y + 1), fill=fill)
    elif name == "storage":
        draw.rectangle((x + 2, y + 1, x2 - 2, y2 - 1), outline=fill)
        draw.rectangle((x + 4, y + 3, x2 - 4, y + 5), fill=fill)
        draw.line((x + 3, y2 - 4, x2 - 3, y2 - 4), fill=fill)
    elif name in ("display", "settings"):
        if name == "display":
            draw.rectangle((x + 1, y + 2, x2 - 1, y2 - 3), outline=fill)
            draw.line((x + 4, y2 - 1, x2 - 4, y2 - 1), fill=fill)
        else:
            draw.ellipse((x + 3, y + 3, x2 - 3, y2 - 3), outline=fill)
            for px, py in ((mid_x, y + 1), (mid_x, y2 - 1), (x + 1, mid_y), (x2 - 1, mid_y)):
                draw.line((mid_x, mid_y, px, py), fill=fill)
    elif name in ("reboot", "back"):
        draw.arc((x + 2, y + 2, x2 - 1, y2 - 1), 45, 315, fill=fill)
        draw.polygon([(x + 1, y + 4), (x + 6, y + 3), (x + 4, y + 8)], fill=fill)
    elif name == "shutdown":
        draw.arc((x + 2, y + 2, x2 - 2, y2 - 1), 35, 325, fill=fill)
        draw.line((mid_x, y, mid_x, mid_y), fill=fill, width=width)
    elif name in ("warning", "error"):
        draw.polygon([(mid_x, y + 1), (x2 - 1, y2 - 1), (x + 1, y2 - 1)], outline=fill)
        draw.line((mid_x, y + 4, mid_x, y2 - 5), fill=fill, width=width)
        draw.point((mid_x, y2 - 3), fill=fill)
    elif name == "success":
        draw.ellipse((x + 1, y + 1, x2 - 1, y2 - 1), outline=fill)
        draw.line((x + 3, mid_y, x + 5, y2 - 4, x2 - 3, y + 3), fill=fill, width=width)
    else:
        draw.rectangle((x + 2, y + 2, x2 - 2, y2 - 2), outline=fill)


def draw_status_badge(draw, label: str, x_right: int, y: int, font) -> int:
    text = truncate_text(draw, label, font, 38)
    width = _text_width(draw, text, font) + 8
    x = x_right - width
    _rounded_rectangle(draw, (x, y, x_right, y + 10), 2, fill=255, outline=255)
    draw.text((x + 4, y + 1), text, font=font, fill=0)
    return x


def draw_header(draw, title: str, badge: str, fonts, icon: str = "heart") -> None:
    small, _large = fonts
    draw_icon(draw, icon, OLED_MARGIN, 1, 9)
    badge_x = draw_status_badge(draw, badge, OLED_WIDTH - OLED_MARGIN, 1, small)
    draw.text((15, 2), truncate_text(draw, title, small, max(12, badge_x - 18)), font=small, fill=255)
    draw.line((OLED_MARGIN, 12, OLED_WIDTH - OLED_MARGIN, 12), fill=255)


def draw_progress_bar(draw, x: int, y: int, width: int, value: Optional[float], label: str, font) -> None:
    draw.rectangle((x, y, x + width, y + 6), outline=255)
    if value is not None:
        fill_w = max(0, min(width - 2, round((width - 2) * value / 100.0)))
        if fill_w:
            draw.rectangle((x + 1, y + 1, x + fill_w, y + 5), fill=255)
    draw.text((x + width + 3, y - 2), truncate_text(draw, label, font, OLED_WIDTH - x - width - 6), font=font, fill=255)


def draw_scrollbar(draw, item_count: int, selected: int, top: int = 16, bottom: int = 59) -> None:
    height = bottom - top
    draw.rectangle((122, top, 124, bottom), outline=255)
    if item_count <= 1:
        thumb_h, thumb_y = height - 2, top + 1
    else:
        thumb_h = max(6, round((height - 2) / item_count))
        thumb_y = top + 1 + round((height - 2 - thumb_h) * selected / (item_count - 1))
    draw.rectangle((123, thumb_y, 123, thumb_y + thumb_h), fill=255)


def _item_icon(item: MenuItem) -> str:
    lookup = {
        "Recording": "recording", "Services": "services", "Status": "acquisition", "Display": "display", "System": "settings", "Back": "back",
        "Start New Session": "recording", "Stop Recording": "stop", "Session Info": "session", "Receiver Status": "receiver", "Restart Receiver": "receiver",
        "AI Worker Status": "ai", "MQTT Status": "mqtt", "Restart MQTT": "mqtt", "Acquisition": "acquisition", "Network": "network",
        "Raspberry Pi": "pi", "Storage": "storage", "Brightness": "display", "Screen Timeout": "display", "Animations": "display",
        "Invert Display": "display", "Reboot": "reboot", "Shutdown": "shutdown",
    }
    return lookup.get(item.label, "settings")


def draw_menu_card(draw, item: MenuItem, selected: bool, y: int, fonts, x_offset: int = 0, compact: bool = False) -> None:
    small, large = fonts
    x = OLED_MARGIN + x_offset
    right = 119 + x_offset
    height = 21 if selected and not compact else 11
    if selected:
        _rounded_rectangle(draw, (x, y, right, y + height), 2, fill=255, outline=255)
        fill, font = 0, (large if not compact else small)
    else:
        fill, font = 255, small
    icon_size = 12 if selected and not compact else 9
    draw_icon(draw, _item_icon(item), x + 4, y + max(1, (height - icon_size) // 2), icon_size, fill)
    text_x = x + 19
    draw.text((text_x, y + max(1, (height - 9) // 2)), truncate_text(draw, item.label, font, right - text_x - 10), font=font, fill=fill)
    if selected:
        draw.polygon([(right - 6, y + height // 2 - 3), (right - 3, y + height // 2), (right - 6, y + height // 2 + 3)], fill=fill)


def draw_menu(draw, menu: MenuController, fonts, now: float) -> None:
    items = MENUS[menu.current_menu]
    title = "MAIN MENU" if menu.current_menu == "main" else menu.current_menu.upper()
    draw_header(draw, title, f"{menu.selected + 1}/{len(items)}", fonts, "services")
    offset = menu.animation_offset(now)
    if menu.selected > 0:
        draw_menu_card(draw, items[menu.selected - 1], False, 14, fonts, x_offset=offset, compact=True)
    draw_menu_card(draw, items[menu.selected], True, 25, fonts, x_offset=offset)
    if menu.selected + 1 < len(items):
        draw_menu_card(draw, items[menu.selected + 1], False, 49, fonts, x_offset=offset, compact=True)
    draw_scrollbar(draw, len(items), menu.selected)


def draw_confirmation(draw, prompt: str, selected: int, fonts, hold: bool = False) -> None:
    small, large = fonts
    draw_header(draw, "CONFIRM", "HOLD" if hold else "CHECK", fonts, "warning")
    draw_icon(draw, "warning", 8, 19, 16)
    draw.text((29, 21), truncate_text(draw, prompt, large, 92), font=large, fill=255)
    if hold:
        _rounded_rectangle(draw, (14, 41, 113, 55), 2, outline=255)
        draw.text((26, 44), "HOLD TO CONFIRM", font=small, fill=255)
        return
    for index, label in enumerate(("NO", "YES")):
        x = 28 + index * 40
        if selected == index:
            _rounded_rectangle(draw, (x - 6, 42, x + 26, 56), 2, fill=255)
            fill = 0
        else:
            draw.rectangle((x - 6, 42, x + 26, 56), outline=255)
            fill = 255
        draw.text((x + 2, 45), label, font=small, fill=fill)


def draw_toast(draw, title: str, lines: list[str], fonts) -> None:
    small, large = fonts
    success = title not in ("SERVICE ERROR", "ERROR")
    draw_header(draw, "ECGRHYTHMIA", "DONE" if success else "ERROR", fonts, "heart")
    _rounded_rectangle(draw, (5, 18, 122, 57), 3, outline=255)
    draw_icon(draw, "success" if success else "error", 11, 29, 16)
    draw.text((34, 23), truncate_text(draw, title, large, 80), font=large, fill=255)
    for index, line in enumerate(lines[:2]):
        draw.text((34, 39 + index * 9), truncate_text(draw, line, small, 80), font=small, fill=255)


def draw_main_screen(draw, snap: StatusSnapshot, fonts, now: float) -> None:
    small, large = fonts
    state = "REC" if snap.state == "RECORDING" else "WAIT" if snap.state == "WAITING FRAME" else snap.state
    draw_header(draw, "ECG", state, fonts, "heart")
    if snap.error_code:
        draw_icon(draw, "error", 7, 22, 19)
        draw.text((31, 19), f"ERROR {snap.error_code}", font=large, fill=255)
        draw.text((31, 35), truncate_text(draw, snap.error_cause, small, 89), font=small, fill=255)
        draw.text((7, 53), "PRESS: MENU", font=small, fill=255)
        return
    if snap.state == "RECORDING":
        _rounded_rectangle(draw, (6, 17, 121, 42), 3, outline=255)
        draw_icon(draw, "recording", 12, 24, 11)
        draw.text((30, 20), _fmt_mmss(snap.rec_seconds), font=large, fill=255)
        draw.text((30, 33), f"FRAME {_fmt_frame(snap.frame_index)}", font=small, fill=255)
        pulse = 36 + int(4 * (1 + math.sin(now * 8)))
        draw_progress_bar(draw, 7, 49, pulse, 100, f"MQTT {snap.mqtt}", small)
        return
    if snap.state == "WAITING FRAME":
        draw_icon(draw, "acquisition", 8, 22, 21)
        draw.text((34, 20), "WAITING FRAME", font=large, fill=255)
        draw.text((34, 36), f"RECEIVER {snap.receiver}", font=small, fill=255)
    else:
        draw_icon(draw, "heart", 8, 22, 21)
        draw.text((35, 21), "READY", font=large, fill=255)
        draw.text((35, 36), f"LAST {_fmt_frame(snap.frame_index)}", font=small, fill=255)
    draw.text((37, 53), "PRESS MENU", font=small, fill=255)


def draw_detail(draw, kind: str, snap: StatusSnapshot, services, fonts) -> None:
    small, large = fonts
    service_data = {
        "receiver_status": ("RECEIVER", "receiver", RECEIVER_SERVICE, snap.receiver),
        "ai_status": ("AI WORKER", "ai", AI_SERVICE, snap.ai),
        "mqtt_status": ("MQTT", "mqtt", MQTT_SERVICE, snap.mqtt),
    }
    if kind in service_data:
        title, icon, service, fallback = service_data[kind]
        status = services.status(service) if services else fallback
        draw_header(draw, title, status, fonts, icon)
        draw_icon(draw, icon, 12, 22, 22)
        draw.text((42, 25), "SERVICE", font=small, fill=255)
        draw.text((42, 37), truncate_text(draw, status, large, 72), font=large, fill=255)
        return
    detail = "storage" if kind == "session_info" else kind
    if detail == "acquisition":
        draw_header(draw, "ACQUISITION", snap.receiver, fonts, "acquisition")
        draw_icon(draw, "acquisition", 7, 19, 17)
        draw.text((29, 18), truncate_text(draw, snap.out_rate, large, 62), font=large, fill=255)
        draw.text((29, 34), f"FRAME {_fmt_frame(snap.frame_index)}", font=small, fill=255)
        draw.text((7, 51), f"LOSS {_fmt_int(snap.loss)}  CRC {_fmt_int(snap.crc_errors)}", font=small, fill=255)
    elif detail == "network":
        draw_header(draw, "NETWORK", snap.mqtt, fonts, "network")
        draw_icon(draw, "network", 7, 20, 18)
        draw.text((31, 19), truncate_text(draw, snap.ip_address, large, 89), font=large, fill=255)
        draw.text((31, 37), f"MQTT {snap.mqtt}", font=small, fill=255)
        draw.text((31, 49), f"NET {snap.net}", font=small, fill=255)
    elif detail == "pi":
        temp = f"{snap.temp_c:.0f}C" if snap.temp_c is not None else NA
        draw_header(draw, "RASPBERRY PI", temp, fonts, "pi")
        draw_icon(draw, "pi", 7, 20, 18)
        draw.text((31, 19), f"UP {_fmt_hms(snap.uptime_seconds)}", font=large, fill=255)
        label = f"DISK {snap.disk_pct:.0f}%" if snap.disk_pct is not None else "DISK N/A"
        draw_progress_bar(draw, 7, 47, 78, snap.disk_pct, label, small)
    else:
        label = snap.session_id[-16:] if snap.session_id != NA else NA
        draw_header(draw, "STORAGE", "DATA", fonts, "storage")
        draw_icon(draw, "storage", 7, 20, 18)
        draw.text((31, 19), truncate_text(draw, label, small, 90), font=small, fill=255)
        draw.text((31, 32), f"FRAMES {snap.frame_count}", font=large, fill=255)
        draw.text((31, 47), f"SIZE {_fmt_bytes(snap.session_size_bytes)}", font=small, fill=255)


def draw_adjust(draw, menu: MenuController, fonts) -> None:
    items = [MenuItem(label) for label, _value in menu.adjust_options]
    draw_header(draw, menu.adjust_kind.upper(), "SET", fonts, "display")
    for index, item in enumerate(items):
        draw_menu_card(draw, item, index == menu.adjust_selected, 17 + index * 11, fonts, compact=True)
    draw_scrollbar(draw, len(items), menu.adjust_selected)


def draw_greeting(draw, fonts) -> None:
    small, large = fonts
    _rounded_rectangle(draw, (3, 3, 124, 60), 3, outline=255)
    draw_icon(draw, "heart", 12, 19, 24)
    draw.text((43, 20), "ECGRHYTHMIA", font=small, fill=255)
    draw.text((43, 32), "EDGE AI ECG", font=large, fill=255)
    draw.text((43, 48), "STARTING...", font=small, fill=255)


def draw_screen(draw, snap: StatusSnapshot, menu: MenuController, fonts, now: float, greeting: bool = False) -> None:
    draw.rectangle((0, 0, OLED_WIDTH - 1, OLED_HEIGHT - 1), fill=0)
    if greeting:
        draw_greeting(draw, fonts)
    elif menu.mode == "status":
        draw_main_screen(draw, snap, fonts, now)
    elif menu.mode == "menu":
        draw_menu(draw, menu, fonts, now)
    elif menu.mode == "confirm":
        draw_confirmation(draw, menu.confirm_prompt, menu.confirm_selected, fonts)
    elif menu.mode == "hold_confirm":
        draw_confirmation(draw, menu.hold_action.upper(), 0, fonts, hold=True)
    elif menu.mode == "adjust":
        draw_adjust(draw, menu, fonts)
    elif menu.mode == "detail":
        draw_detail(draw, menu.detail_kind, snap, menu.service_manager, fonts)
    elif menu.mode == "message":
        draw_toast(draw, menu.message_title, menu.message_lines, fonts)
    else:
        draw_main_screen(draw, snap, fonts, now)


# ==========================================================================
# OLED backend
# ==========================================================================
class OledRenderer:
    def __init__(self, args, settings: OledSettings):
        from luma.core.interface.serial import i2c  # type: ignore
        from luma.oled import device as oled_device  # type: ignore
        from PIL import ImageFont  # type: ignore

        serial_iface = i2c(port=args.i2c_bus, address=args.address)
        driver_cls = getattr(oled_device, args.driver)
        self.device = driver_cls(serial_iface)
        self.fonts = load_ui_fonts(ImageFont)
        self.blank = False
        self.apply_settings(settings)

    def apply_settings(self, settings: OledSettings) -> None:
        try:
            self.device.contrast(int(255 * settings.brightness / 100))
        except Exception:
            pass
        try:
            self.device.command(0xA7 if settings.invert else 0xA6)
        except Exception:
            pass

    def clear(self) -> None:
        try:
            self.device.clear()
        except Exception:
            pass
        self.blank = True

    def render_screen(
        self,
        snap: StatusSnapshot,
        menu: MenuController,
        now: float,
        greeting: bool = False,
    ) -> None:
        from luma.core.render import canvas  # type: ignore

        with canvas(self.device) as draw:
            draw_screen(draw, snap, menu, self.fonts, now, greeting)
        self.blank = False


def load_ui_fonts(image_font):
    """Use installed fonts only; the default bitmap font remains a safe fallback."""
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    )
    for path in candidates:
        try:
            return image_font.truetype(path, 9), image_font.truetype(path, 13)
        except OSError:
            continue
    fallback = image_font.load_default()
    return fallback, fallback


def save_preview_frames(preview_dir: Path) -> list[Path]:
    """Render representative screens to PNG without I2C, GPIO, or service actions."""
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    preview_dir.mkdir(parents=True, exist_ok=True)
    fonts = load_ui_fonts(ImageFont)
    services = SystemServiceManager()
    menu = MenuController(SessionController(services), OledSettings(), services)
    base = StatusSnapshot(
        state="READY",
        frame_index=15,
        receiver="OFF",
        mqtt="STOPPED",
        ai="STOPPED",
        out_rate="250 Hz",
        ip_address="192.168.1.42",
        temp_c=48.0,
        disk_pct=37.0,
        uptime_seconds=3723,
        session_id="session_20260724_110355",
        frame_count=15,
        session_size_bytes=1_572_864,
    )
    screens: list[tuple[str, StatusSnapshot, str, str, int]] = []
    screens.append(("01_ready", base, "status", "", 0))
    recording = StatusSnapshot(**{**base.__dict__, "state": "RECORDING", "receiver": "ON", "mqtt": "RUNNING", "rec_seconds": 150})
    screens.append(("02_recording", recording, "status", "", 0))
    error = StatusSnapshot(**{**base.__dict__, "state": "ERROR", "error_code": "E03", "error_cause": "NO NEW FRAME"})
    screens.append(("03_error", error, "status", "", 0))
    screens.extend([
        ("04_main_menu", base, "menu", "main", 0),
        ("05_recording_menu", base, "menu", "recording", 0),
        ("06_status_menu", base, "menu", "status", 1),
        ("07_confirmation", base, "confirm", "", 0),
        ("08_toast_success", base, "message", "", 0),
    ])
    written: list[Path] = []
    for name, snap, mode, menu_name, selected in screens:
        menu.mode = mode
        menu.selected = selected
        menu.menu_stack = [menu_name or "main"]
        if mode == "confirm":
            menu.confirm("START SESSION?", "start")
        elif mode == "message":
            menu.message_title = "RECORDING"
            menu.message_lines = ["RECEIVER OK", "MQTT CONNECTED"]
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT), 0)
        draw_screen(ImageDraw.Draw(image), snap, menu, fonts, now=1.0)
        output = preview_dir / f"{name}.png"
        image.save(output)
        written.append(output)
    return written


# ==========================================================================
# Safety self-guard
# ==========================================================================
SAFETY_MARKER = "__SAFETY_GUARD_REGION__"


def assert_safe_source() -> None:
    src_lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    # __SAFETY_GUARD_REGION__
    forbidden = ("i" + "mport serial", "i" + "mport receiver",
                 "/dev/" + "tty", "f" + "rom serial")
    # __SAFETY_GUARD_REGION__
    in_guard = False
    offenders = []
    for line in src_lines:
        if SAFETY_MARKER in line:
            in_guard = not in_guard
            continue
        if in_guard:
            continue
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("*") or stripped.startswith('"'):
            continue
        for token in forbidden:
            if token in line:
                offenders.append((token, stripped))
    if offenders:
        raise AssertionError(f"unsafe token(s) in source: {offenders}")


# ==========================================================================
# CLI + loop
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ECGRHYTHMIA OLED status display")
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    parser.add_argument("--session-dir", default=None)
    parser.add_argument("--i2c-bus", type=int, default=1)
    parser.add_argument("--address", type=lambda x: int(x, 0), default=0x3C)
    parser.add_argument("--driver", default="ssd1306")
    parser.add_argument("--refresh", type=float, default=0.5)
    parser.add_argument("--page-seconds", type=float, default=4.0)
    parser.add_argument("--greeting-seconds", type=float, default=DEFAULT_GREETING_SECONDS)
    parser.add_argument("--menu-timeout", type=float, default=MENU_TIMEOUT_S)
    parser.add_argument("--console", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--preview-dir", type=Path, default=None)
    parser.add_argument("--no-gpio", action="store_true")
    parser.add_argument("--reverse-encoder", action="store_true")
    parser.add_argument("--encoder-a-gpio", type=int, default=ENCODER_A_GPIO)
    parser.add_argument("--encoder-b-gpio", type=int, default=ENCODER_B_GPIO)
    parser.add_argument("--encoder-button-gpio", type=int, default=ENCODER_BUTTON_GPIO)
    parser.add_argument("--log-level", default="INFO")
    return parser


def run(args) -> int:
    refresh = max(REFRESH_FLOOR_S, float(args.refresh))
    if float(args.refresh) < REFRESH_FLOOR_S:
        LOG.warning("refresh %.3f below floor; clamped to %.3f", args.refresh, refresh)

    settings = OledSettings.load()
    if args.reverse_encoder:
        settings.encoder_reverse = True
    service_manager = SystemServiceManager()
    session_controller = SessionController(service_manager)
    menu = MenuController(session_controller, settings, service_manager, args.menu_timeout)

    events: "queue.Queue[InputEvent]" = queue.Queue()
    rotary = None
    if not args.console and not args.no_gpio:
        try:
            rotary = RotaryInput(
                events,
                pin_a=args.encoder_a_gpio,
                pin_b=args.encoder_b_gpio,
                button_pin=args.encoder_button_gpio,
                reverse=settings.encoder_reverse,
            )
        except Exception as exc:
            LOG.error("Rotary init failed: %s", exc)

    renderer = None
    if not args.console:
        try:
            renderer = OledRenderer(args, settings)
        except Exception as exc:
            LOG.error("E01 OLED/I2C init failed: %s", exc)
            print("E01 OLED/I2C init failed:", exc, file=sys.stderr)
            return 1

    prev: Optional[StatusSnapshot] = None
    snap: Optional[StatusSnapshot] = None
    booted = False
    start = time.monotonic()
    last_settings = OledSettings(**settings.__dict__)
    next_snapshot_at = 0.0
    last_render_key = None

    try:
        while True:
            event_seen = False
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                menu.handle(event)
                event_seen = True

            menu.tick()
            now_mono = time.monotonic()
            if snap is None or now_mono >= next_snapshot_at:
                snap = build_snapshot(args, prev, booted)
                booted = True
                prev = snap
                next_snapshot_at = now_mono + refresh

            show_greeting = (
                menu.mode == "status"
                and not snap.error_code
                and args.greeting_seconds > 0
                and now_mono - start < args.greeting_seconds
            )
            if show_greeting:
                lines = render_greeting_lines()
            elif menu.mode == "status" and not snap.error_code:
                elapsed = now_mono - start
                page = int(elapsed / max(0.5, args.page_seconds)) % 3
                lines = render_page_lines(snap, page)
            else:
                lines = menu.render(snap)

            render_key = (
                tuple(lines),
                menu.mode,
                settings.brightness,
                settings.invert,
                settings.timeout_seconds,
                settings.animations,
            )
            should_render = event_seen or render_key != last_render_key or menu.animation_active(now_mono)
            if args.console:
                if should_render:
                    sys.stdout.write("\n" + render_console(lines) + "\n")
                    sys.stdout.flush()
            elif renderer is not None:
                if settings != last_settings:
                    renderer.apply_settings(settings)
                    last_settings = OledSettings(**settings.__dict__)
                    should_render = True
                idle_for = now_mono - menu.last_input_at
                if settings.timeout_seconds and idle_for > settings.timeout_seconds:
                    if not renderer.blank:
                        renderer.clear()
                        should_render = False
                else:
                    if should_render or renderer.blank:
                        renderer.render_screen(snap, menu, now_mono, greeting=show_greeting)

            last_render_key = render_key
            if args.once:
                return 0
            time.sleep(INPUT_POLL_S)
    except KeyboardInterrupt:
        return 0
    finally:
        if rotary is not None:
            rotary.close()


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
    if args.preview_dir is not None:
        written = save_preview_frames(args.preview_dir)
        print(f"Saved {len(written)} OLED previews to {args.preview_dir}")
        return 0
    return run(args)


# ==========================================================================
# Inline self-test
# ==========================================================================
def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        print(("PASS " if condition else "FAIL ") + name)
        if not condition:
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "frame_000005.json").write_text(json.dumps({
            "frame_index": 5,
            "sample_rate_hz": 250,
            "parser_stats": {"packet_loss_count": 2, "crc_errors": 1},
        }), encoding="utf-8")
        (root / "frame_000012.json").write_text(json.dumps({
            "frame_index": 12,
            "sample_rate_hz": 250,
            "parser_stats": {"packet_loss_count": 3, "crc_errors": 1},
        }), encoding="utf-8")
        meta = find_latest_frame_meta(root)
        check("latest_frame_numeric_order", meta is not None and meta.get("frame_index") == 12)
        check("signal_na", derive_signal_quality(meta) == NA)
        (root / "calibrated").mkdir()
        (root / "calibrated" / "frame_000012_mv.json").write_text(
            json.dumps({"config_verified": True}),
            encoding="utf-8",
        )
        check("config_ok", get_config_verified(find_latest_calibrated_meta(root)) == "OK")

        events: "queue.Queue[InputEvent]" = queue.Queue()

        class FakeRotary:
            def __init__(self, a, b, **kwargs):
                self.a = a
                self.b = b
                self.kwargs = kwargs

            def close(self):
                pass

        class FakeButton:
            def __init__(self, pin, **kwargs):
                self.pin = pin
                self.kwargs = kwargs

            def close(self):
                pass

        now = [0.0]
        rotary = RotaryInput(
            events,
            rotary_cls=FakeRotary,
            button_cls=FakeButton,
            clock=lambda: now[0],
        )
        check("gpio_bcm_a", rotary.encoder.a == ENCODER_A_GPIO)
        check("gpio_bcm_b", rotary.encoder.b == ENCODER_B_GPIO)
        check("gpio_button", rotary.button.pin == ENCODER_BUTTON_GPIO)
        check("button_pullup", rotary.button.kwargs.get("pull_up") is True)
        now[0] = 1.0
        rotary._clockwise()
        check("rotate_right_event", events.get_nowait().delta == 1)
        rotary._counter_clockwise()
        check("debounce_blocks_double", events.empty())
        now[0] = 2.0
        rotary._counter_clockwise()
        check("rotate_left_event", events.get_nowait().delta == -1)
        rotary._button_pressed()
        rotary._button_held()
        rotary._button_released()
        check("long_no_short", [events.get_nowait().kind] == ["back"])

        class FakeServices(SystemServiceManager):
            def __init__(self):
                self.active: set[str] = set()
                self.calls: list[tuple[str, str]] = []

            def exists(self, service: str) -> bool:
                return True

            def status(self, service: str) -> str:
                return "RUNNING" if service in self.active else "STOPPED"

            def is_active(self, service: str) -> bool:
                return service in self.active

            def start(self, service: str) -> ServiceResult:
                self.calls.append(("start", service))
                self.active.add(service)
                return ServiceResult(True, "RUNNING", "OK")

            def stop(self, service: str) -> ServiceResult:
                self.calls.append(("stop", service))
                self.active.discard(service)
                return ServiceResult(True, "STOPPED", "OK")

            def restart(self, service: str) -> ServiceResult:
                self.calls.append(("restart", service))
                self.active.add(service)
                return ServiceResult(True, "RUNNING", "OK")

        fake = FakeServices()
        controller = SessionController(fake, session_root=root / "sessions", env_path=root / "current.env")
        result = controller.start_new_session()
        check("start_ok", result.ok)
        check("start_unique_env", (root / "current.env").is_file())
        check("start_order", fake.calls == [
            ("start", RECEIVER_SERVICE),
            ("start", AI_SERVICE),
            ("start", MQTT_SERVICE),
        ])
        duplicate = controller.start_new_session()
        check("no_receiver_double", not duplicate.ok)
        stop_result = controller.stop_recording()
        check("stop_ok", stop_result.ok)
        check("stop_order", fake.calls[-3:] == [
            ("stop", MQTT_SERVICE),
            ("stop", AI_SERVICE),
            ("stop", RECEIVER_SERVICE),
        ])
        check("stop_keeps_session", any((root / "sessions").iterdir()))

    check("console_renders", "ECGRHYTHMIA" in render_console(render_main_lines(StatusSnapshot(state="READY"))))
    check("greeting_renders", "Starting" in render_console(render_greeting_lines()))
    try:
        assert_safe_source()
        check("safe_source_guard", True)
    except AssertionError:
        check("safe_source_guard", False)

    print(f"\n{len(failures)} failure(s)" if failures else "\nALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

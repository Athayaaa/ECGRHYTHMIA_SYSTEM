"""Verified ADS1293 protocol-V2 configuration and session metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any
import uuid


PROTOCOL_VERSION = 2
AFE_RES_HZ = 102_400
R1 = 4
R2 = 6
R3_CH1 = 16
R3_CH2 = 16
NATIVE_RATE_NUMERATOR = 800
NATIVE_RATE_DENOMINATOR = 3
NATIVE_SAMPLE_RATE_HZ = NATIVE_RATE_NUMERATOR / NATIVE_RATE_DENOMINATOR
OUTPUT_SAMPLE_RATE_HZ = 250
RESAMPLE_UP = 15
RESAMPLE_DOWN = 16
ADCMAX = 0xF30000
ZERO_CODE = ADCMAX // 2
ADC_ENCODING = "offset_binary_uint24"
VREF_NOMINAL_V = 2.4
ANALOG_GAIN = 3.5
PACKET_SIZE = 32
CRC_TYPE = "CRC16-CCITT-FALSE"
EXPECTED_DEVICE_ID = 0x01
CONFIGURATION_PROFILE = "ADS1293_NATIVE_266P667_SPS"
PLACEHOLDER_PROVENANCE_VALUES = frozenset(
    {
        "",
        "-",
        "unknown",
        "n/a",
        "na",
        "none",
        "null",
        "not available",
        "not_available",
        "unset",
    }
)
NOMINAL_SCALE_MV_PER_COUNT = (
    2.0 * VREF_NOMINAL_V * 1000.0 / (ANALOG_GAIN * ADCMAX)
)
TARGET_STATIC_REGISTERS = {
    0x01: 0x11,  # FLEX_CH1_CN: Lead I = LA - RA, self-test off
    0x02: 0x19,  # FLEX_CH2_CN: Lead II = LL - RA, self-test off
    0x05: 0x00,  # FLEX_VBAT_CN: battery monitor disabled
    0x06: 0x08,  # LOD_CN: lead-off block shut down
    0x07: 0x00,  # LOD_EN: lead-off current injection disabled
    0x08: 0x00,  # LOD_CURRENT: no DC lead-off current
    0x09: 0x00,  # LOD_AC_CN: no AC lead-off excitation
    0x0A: 0x07,
    0x0C: 0x04,
    0x11: 0x00,  # REF_CN: internal references enabled
    0x12: 0x04,
    0x13: 0x00,  # AFE_RES: 102.4 kHz
    0x21: 0x04,  # R2 = 6
    0x22: 0x10,  # R3 CH1 = 16
    0x23: 0x10,  # R3 CH2 = 16
    0x25: 0x00,  # R1 CH1/CH2 = 4
    0x26: 0x00,
    0x27: 0x08,
    0x28: 0x40,  # SYNCB_CN: default synchronous DRDY behavior
    0x29: 0x00,  # MASK_DRDYB: DRDY output is not masked
    0x2F: 0x30,
}


class FirmwareCompatibilityError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_config_dump_response(response: str) -> dict[str, Any]:
    """Extract exactly one protocol configuration JSON object from text mode."""

    candidates: list[dict[str, Any]] = []
    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "protocol_version" in value:
            candidates.append(value)

    if len(candidates) != 1:
        raise FirmwareCompatibilityError(
            "ads config dump must return exactly one configuration JSON object; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _integer(config: dict[str, Any], field: str) -> int | None:
    value = config.get(field)
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            return int(value, 0)
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or value != parsed):
        return None
    return parsed


def _is_missing_or_placeholder(value: Any) -> bool:
    return not isinstance(value, str) or (
        value.strip().casefold() in PLACEHOLDER_PROVENANCE_VALUES
    )


def _registers_by_address(
    config: dict[str, Any],
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    registers = config.get("registers")
    if not isinstance(registers, dict) or not registers:
        return {}, ["verified register write/readback map is missing"]

    registers_by_address: dict[int, dict[str, Any]] = {}
    trust_failures: list[str] = []
    for name, details in registers.items():
        if not isinstance(details, dict):
            trust_failures.append(f"register {name} details are not an object")
            continue
        if details.get("written") != details.get("readback"):
            trust_failures.append(
                f"register {name} write/readback mismatch: "
                f"{details.get('written')!r} != {details.get('readback')!r}"
            )
        if details.get("match") is not True:
            trust_failures.append(f"register {name} did not report match=true")
        address = _integer(details, "address")
        if address is None:
            trust_failures.append(f"register {name} has an invalid address")
            continue
        registers_by_address[address] = details
    return registers_by_address, trust_failures


def firmware_critical_compatibility_failures(
    config: dict[str, Any],
) -> list[str]:
    """Return known wire-format, acquisition, and DSP incompatibilities.

    Development overrides must never bypass these failures.  Missing register
    evidence is handled separately as a trust failure, while a present and
    parseable register value that is known to be wrong is critical.
    """

    failures: list[str] = []
    expected_integer_fields = {
        "protocol_version": PROTOCOL_VERSION,
        "afe_res_hz": AFE_RES_HZ,
        "r1": R1,
        "r2": R2,
        "r3_ch1": R3_CH1,
        "r3_ch2": R3_CH2,
        "native_sample_rate_numerator": NATIVE_RATE_NUMERATOR,
        "native_sample_rate_denominator": NATIVE_RATE_DENOMINATOR,
        "adcmax": ADCMAX,
        "zero_code": ZERO_CODE,
        "packet_size": PACKET_SIZE,
    }
    for field, expected in expected_integer_fields.items():
        actual = _integer(config, field)
        if actual != expected:
            failures.append(f"{field}={config.get(field)!r}, expected {expected}")

    try:
        native_rate = float(config.get("native_sample_rate_hz"))
    except (TypeError, ValueError):
        native_rate = float("nan")
    if not math.isclose(
        native_rate, NATIVE_SAMPLE_RATE_HZ, rel_tol=0.0, abs_tol=1e-6
    ):
        failures.append(
            f"native_sample_rate_hz={config.get('native_sample_rate_hz')!r}, "
            f"expected {NATIVE_SAMPLE_RATE_HZ:.12g}"
        )

    if config.get("adc_encoding") != ADC_ENCODING:
        failures.append(
            f"adc_encoding={config.get('adc_encoding')!r}, expected {ADC_ENCODING!r}"
        )
    if config.get("crc_type") != CRC_TYPE:
        failures.append(f"crc_type={config.get('crc_type')!r}, expected {CRC_TYPE!r}")
    if config.get("selftest_mode") != "off":
        failures.append("internal self-test must be off for normal acquisition")

    for field, expected in (
        ("vref_nominal_v", VREF_NOMINAL_V),
        ("analog_gain", ANALOG_GAIN),
    ):
        try:
            actual = float(config.get(field))
        except (TypeError, ValueError):
            actual = float("nan")
        if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-6):
            failures.append(f"{field}={config.get(field)!r}, expected {expected}")

    device_id = _integer(config, "device_id")
    if device_id is not None and device_id != EXPECTED_DEVICE_ID:
        failures.append(
            f"device_id={config.get('device_id')!r}, expected {EXPECTED_DEVICE_ID}"
        )

    registers_by_address, _ = _registers_by_address(config)
    for address, expected_value in TARGET_STATIC_REGISTERS.items():
        details = registers_by_address.get(address)
        if details is None:
            continue
        readback = _integer(details, "readback")
        if readback is not None and readback != expected_value:
            failures.append(
                f"register 0x{address:02X} readback={details.get('readback')!r}, "
                f"expected {expected_value}"
            )

    return failures


def firmware_trust_failures(config: dict[str, Any]) -> list[str]:
    """Return absent or untrusted provenance/readback evidence."""

    failures: list[str] = []
    if config.get("config_verified") is not True:
        failures.append("firmware did not report config_verified=true")
    if config.get("configuration_profile") != CONFIGURATION_PROFILE:
        failures.append(f"configuration_profile is not {CONFIGURATION_PROFILE}")

    if _integer(config, "device_id") is None:
        failures.append(
            "required firmware metadata device_id is missing or invalid"
        )
    for field in (
        "firmware_commit",
        "firmware_build_time_utc",
        "firmware_build_environment",
    ):
        if _is_missing_or_placeholder(config.get(field)):
            failures.append(
                f"required firmware metadata {field} is missing or a placeholder"
            )
    if not isinstance(config.get("firmware_dirty"), bool):
        failures.append("required firmware metadata firmware_dirty is not boolean")

    registers_by_address, register_failures = _registers_by_address(config)
    failures.extend(register_failures)
    for address in TARGET_STATIC_REGISTERS:
        details = registers_by_address.get(address)
        if details is None:
            failures.append(f"required register 0x{address:02X} is missing")
        elif _integer(details, "readback") is None:
            failures.append(f"register 0x{address:02X} readback is invalid")

    return failures


def firmware_compatibility_failures(config: dict[str, Any]) -> list[str]:
    return (
        firmware_critical_compatibility_failures(config)
        + firmware_trust_failures(config)
    )


def verify_firmware_config(
    config: dict[str, Any], *, allow_unverified: bool = False
) -> list[str]:
    critical_failures = firmware_critical_compatibility_failures(config)
    trust_failures = firmware_trust_failures(config)
    if critical_failures:
        detail = "\n  - ".join(critical_failures)
        raise FirmwareCompatibilityError(
            "wire/DSP-critical ADS1293 firmware incompatibility; "
            "--allow-unverified-firmware cannot override it:\n  - " + detail
        )
    if trust_failures and not allow_unverified:
        detail = "\n  - ".join(trust_failures)
        raise FirmwareCompatibilityError(
            "incompatible or unverified ADS1293 firmware:\n  - " + detail
        )
    return trust_failures


def verify_persisted_session_metadata(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Strictly verify an external corrected-acquisition session artifact.

    Offline conversion and replay may only use a persisted session that carries
    the complete firmware write/readback evidence and records a verified normal
    acquisition.  Frame metadata is linked to this artifact separately through
    ``require_frame_session_link`` before calibration compatibility is checked.
    """

    if not isinstance(metadata, dict):
        raise FirmwareCompatibilityError(
            "persisted session metadata must be a JSON object"
        )

    verify_firmware_config(metadata, allow_unverified=False)

    session_id = metadata.get("session_id")
    if _is_missing_or_placeholder(session_id):
        raise FirmwareCompatibilityError(
            "persisted session metadata is missing a valid session_id"
        )
    assert isinstance(session_id, str)
    if session_id != session_id.strip():
        raise FirmwareCompatibilityError(
            "persisted session metadata session_id contains whitespace"
        )

    acquisition_status = str(metadata.get("acquisition_status", "")).strip().upper()
    if acquisition_status != "PASS":
        raise FirmwareCompatibilityError(
            "persisted session acquisition_status must be PASS; "
            f"got {acquisition_status or 'MISSING'}"
        )

    acquisition_reasons = metadata.get("acquisition_reasons", [])
    if acquisition_reasons is None:
        acquisition_reasons = []
    if not isinstance(acquisition_reasons, list):
        raise FirmwareCompatibilityError(
            "persisted session acquisition_reasons must be a list"
        )
    if acquisition_reasons:
        raise FirmwareCompatibilityError(
            "persisted session reports acquisition failure reason(s): "
            + "; ".join(str(reason) for reason in acquisition_reasons)
        )

    expected_integer_fields = {
        "output_sample_rate_hz": OUTPUT_SAMPLE_RATE_HZ,
        "resample_up": RESAMPLE_UP,
        "resample_down": RESAMPLE_DOWN,
    }
    failures: list[str] = []
    for field, expected in expected_integer_fields.items():
        actual = _integer(metadata, field)
        if actual != expected:
            failures.append(f"{field}={metadata.get(field)!r}, expected {expected}")
    if failures:
        raise FirmwareCompatibilityError(
            "persisted session has an incompatible output/resampling contract: "
            + "; ".join(failures)
        )

    return metadata


def serial_utilization(
    *, packet_size: int = PACKET_SIZE, baudrate: int, native_rate_hz: float
) -> float:
    """Return serial line utilization for 8N1 framing (10 bits/byte)."""

    if packet_size <= 0 or baudrate <= 0 or native_rate_hz <= 0:
        raise ValueError("packet size, baudrate, and rate must be positive")
    return packet_size * 10.0 * native_rate_hz / float(baudrate)


def expected_selftest_codes(adcmax: int) -> dict[str, int]:
    if adcmax <= 0:
        raise ValueError("adcmax must be positive")
    # Datasheet Eq. 2 with VTEST=VREF/12 and Eq. 3.
    zero = adcmax / 2.0
    delta = adcmax * (ANALOG_GAIN / 24.0)
    return {
        "zero": int(round(zero)),
        "positive": int(round(zero + delta)),
        "negative": int(round(zero - delta)),
    }


def file_sha256(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_session_metadata(
    *,
    firmware_config: dict[str, Any],
    serial_port: str,
    baudrate: int,
    calibration_config_path: Path | None,
    calibration_compatible: bool,
    calibration_status: str,
    calibration_source: str | None = None,
    compatibility_failures: list[str] | None = None,
) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    native_rate = float(firmware_config.get("native_sample_rate_hz", 0.0))
    created_at = utc_now_iso()
    source = (
        str(calibration_source).strip()
        if calibration_source is not None and str(calibration_source).strip()
        else "unavailable"
    )
    return {
        "session_id": session_id,
        "created_at": created_at,
        "created_at_utc": created_at,
        "serial_port": serial_port,
        "baud_rate": int(baudrate),
        "serial_utilization_fraction": serial_utilization(
            packet_size=int(firmware_config.get("packet_size", PACKET_SIZE)),
            baudrate=baudrate,
            native_rate_hz=native_rate,
        ),
        "minimum_serial_baud_8n1": int(
            math.ceil(
                int(firmware_config.get("packet_size", PACKET_SIZE))
                * 10.0
                * native_rate
            )
        ),
        "firmware_commit": firmware_config.get("firmware_commit"),
        "firmware_dirty": firmware_config.get("firmware_dirty"),
        "firmware_build_time_utc": firmware_config.get("firmware_build_time_utc"),
        "firmware_build_environment": firmware_config.get(
            "firmware_build_environment"
        ),
        "protocol_version": firmware_config.get("protocol_version"),
        "configuration_profile": firmware_config.get("configuration_profile"),
        "selftest_mode": firmware_config.get("selftest_mode"),
        "device_id": firmware_config.get("device_id"),
        "ads1293_device_id": firmware_config.get("device_id"),
        "registers": firmware_config.get("registers", {}),
        "config_verified": firmware_config.get("config_verified", False),
        "afe_res_hz": firmware_config.get("afe_res_hz"),
        "r1": firmware_config.get("r1"),
        "r2": firmware_config.get("r2"),
        "r3_ch1": firmware_config.get("r3_ch1"),
        "r3_ch2": firmware_config.get("r3_ch2"),
        "native_sample_rate_numerator": firmware_config.get(
            "native_sample_rate_numerator"
        ),
        "native_sample_rate_denominator": firmware_config.get(
            "native_sample_rate_denominator"
        ),
        "native_sample_rate_hz": native_rate,
        "output_sample_rate_hz": OUTPUT_SAMPLE_RATE_HZ,
        "resample_up": RESAMPLE_UP,
        "resample_down": RESAMPLE_DOWN,
        "adc_encoding": firmware_config.get("adc_encoding"),
        "adcmax": firmware_config.get("adcmax"),
        "zero_code": firmware_config.get("zero_code"),
        "vref_nominal_v": firmware_config.get("vref_nominal_v", VREF_NOMINAL_V),
        "analog_gain": firmware_config.get("analog_gain", ANALOG_GAIN),
        # This describes the calibration snapshot selected for this session. It
        # must not silently remain datasheet_nominal after a compatible ProSim
        # calibration is selected by the caller.
        "conversion_source": source,
        "calibration_status": str(calibration_status),
        "calibration_config_path": (
            str(calibration_config_path.resolve())
            if calibration_config_path is not None
            else None
        ),
        "calibration_config_sha256": file_sha256(calibration_config_path),
        "calibration_compatibility": {
            "compatible": bool(calibration_compatible),
            "status": calibration_status,
            "failures": list(compatibility_failures or []),
        },
        "packet_size": firmware_config.get("packet_size"),
        "crc_type": firmware_config.get("crc_type"),
        "channel_mapping": {
            "packet_lead_i": "ADS1293 CH1 offset-code; LA - RA",
            "packet_lead_ii": "ADS1293 CH2 offset-code; LL - RA",
            "output_lead_iii": "resampled Lead II - resampled Lead I",
        },
    }


def write_session_metadata(output_dir: Path, metadata: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    created = str(metadata.get("created_at_utc", utc_now_iso()))
    safe_timestamp = created.replace(":", "").replace("-", "")
    safe_timestamp = safe_timestamp.replace(".", "").replace("Z", "Z")
    contents = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    suffix = 0
    while True:
        extra = "" if suffix == 0 else f"_{suffix:02d}"
        path = output_dir / f"session_{safe_timestamp}{extra}.json"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(contents)
            return path
        except FileExistsError:
            suffix += 1

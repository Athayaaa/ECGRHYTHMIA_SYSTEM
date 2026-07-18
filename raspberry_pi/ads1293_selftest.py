"""Run ADS1293 internal zero/positive/negative signal-path self-tests.

Internal ADS1293 self-test verifies the ADC signal path and configuration.  It
is not a replacement for external ProSim gain calibration.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable

import numpy as np
import serial

from ads1293_serial import (
    collect_v2_packets,
    command_response,
    start_v2_binary_stream,
    stop_text_and_binary_output,
    verified_config_handshake,
)
from ads1293_session import expected_selftest_codes
from packet_parser import (
    VERSION_V2,
    V2_FLAG_ADS_ERROR,
    V2_FLAG_BACKLOG_OVERRUN,
    V2_FLAG_CODE_OUT_OF_RANGE,
    V2_FLAG_CONFIG_VERIFIED,
    V2_FLAG_PREVIOUS_TX_FAILURE,
    V2_FLAG_SELFTEST_ACTIVE,
    V2_NATIVE_RATE_NUMERATOR,
)


DEFAULT_OUTPUT_DIR = Path(__file__).with_name("data") / "selftest_reports"
SELFTEST_MODES = ("zero", "positive", "negative")
SELFTEST_STATEMENT = (
    "Internal ADS1293 self-test verifies the ADC signal path and "
    "configuration. It is not a replacement for external ProSim gain "
    "calibration."
)
V2_KNOWN_FLAGS = (
    V2_FLAG_CONFIG_VERIFIED
    | V2_FLAG_ADS_ERROR
    | V2_FLAG_BACKLOG_OVERRUN
    | V2_FLAG_CODE_OUT_OF_RANGE
    | V2_FLAG_PREVIOUS_TX_FAILURE
    | V2_FLAG_SELFTEST_ACTIVE
)
V2_SELFTEST_REJECT_FLAGS = (
    V2_FLAG_ADS_ERROR
    | V2_FLAG_BACKLOG_OVERRUN
    | V2_FLAG_CODE_OUT_OF_RANGE
    | V2_FLAG_PREVIOUS_TX_FAILURE
)


class SelftestRestorationError(RuntimeError):
    """Restoration failure with JSON-safe evidence from completed steps."""

    def __init__(self, message: str, *, evidence: list[str], failures: list[str]):
        super().__init__(message)
        self.evidence = list(evidence)
        self.failures = list(failures)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--boot-delay", type=float, default=3.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--tolerance-percent-full-scale",
        type=float,
        default=2.0,
        help="Maximum absolute mean-code error as percent ADCMAX",
    )
    parser.add_argument(
        "--max-std-counts",
        type=float,
        default=100000.0,
        help="Maximum allowed standard deviation for each channel",
    )
    parser.add_argument(
        "--allow-unverified-firmware",
        action="store_true",
        help="Development-only firmware compatibility override",
    )
    return parser.parse_args()


def _parse_status_json(raw: bytes) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "selftest_mode" in value:
            objects.append(value)
    if len(objects) != 1:
        raise RuntimeError(
            f"self-test command must return exactly one status JSON object; "
            f"found {len(objects)}"
        )
    return objects[0]


def channel_statistics(
    values: np.ndarray,
    *,
    expected_code: int,
    adcmax: int,
    tolerance_percent_full_scale: float,
    max_std_counts: float,
) -> dict[str, Any]:
    samples = np.asarray(values, dtype=np.float64)
    mean = float(np.mean(samples))
    median = float(np.median(samples))
    standard_deviation = float(np.std(samples))
    absolute_error = abs(mean - float(expected_code))
    percent_error = 100.0 * absolute_error / float(adcmax)
    range_violations = int(np.count_nonzero((samples < 0) | (samples > adcmax)))
    passed = (
        percent_error <= tolerance_percent_full_scale
        and standard_deviation <= max_std_counts
        and range_violations == 0
    )
    return {
        "sample_count": int(samples.size),
        "mean_code": mean,
        "median_code": median,
        "standard_deviation_code": standard_deviation,
        "expected_code": int(expected_code),
        "absolute_code_error": absolute_error,
        "percent_full_scale_error": percent_error,
        "range_violation_count": range_violations,
        "status": "PASS" if passed else "FAIL",
    }


def validate_selftest_packets(
    packets: list,
    *,
    expected_count: int,
    native_rate_numerator: int,
) -> dict[str, Any]:
    """Reject a numerically plausible self-test with an invalid acquisition."""

    failures: list[str] = []
    if len(packets) != expected_count:
        failures.append(f"packet count {len(packets)}, expected {expected_count}")

    previous_sequence: int | None = None
    for index, packet in enumerate(packets):
        prefix = f"packet[{index}]"
        if int(getattr(packet, "protocol_version", -1)) != VERSION_V2:
            failures.append(f"{prefix} is not protocol V2")
        if int(getattr(packet, "sample_count", -1)) != 1:
            failures.append(f"{prefix} sample_count is not 1")
        if int(getattr(packet, "native_rate_numerator", -1)) != native_rate_numerator:
            failures.append(
                f"{prefix} native-rate numerator is not {native_rate_numerator}"
            )

        flags = int(getattr(packet, "flags", 0))
        if flags & ~V2_KNOWN_FLAGS:
            failures.append(f"{prefix} has reserved flag bits 0x{flags & ~V2_KNOWN_FLAGS:02X}")
        if not flags & V2_FLAG_CONFIG_VERIFIED:
            failures.append(f"{prefix} lacks CONFIG_VERIFIED")
        if not flags & V2_FLAG_SELFTEST_ACTIVE:
            failures.append(f"{prefix} lacks SELFTEST_ACTIVE")
        if flags & V2_SELFTEST_REJECT_FLAGS:
            failures.append(
                f"{prefix} has acquisition-error flags 0x{flags & V2_SELFTEST_REJECT_FLAGS:02X}"
            )
        if int(getattr(packet, "error_status", 0)) != 0:
            failures.append(f"{prefix} reports ADS1293 error status")

        sequence = int(getattr(packet, "sequence", -1))
        if previous_sequence is not None:
            expected_sequence = (previous_sequence + 1) & 0xFFFFFFFF
            if sequence != expected_sequence:
                failures.append(
                    f"{prefix} sequence={sequence}, expected {expected_sequence}"
                )
        previous_sequence = sequence

    if failures:
        preview = "; ".join(failures[:8])
        if len(failures) > 8:
            preview += f"; ... ({len(failures)} failures total)"
        raise RuntimeError(f"invalid self-test acquisition: {preview}")
    return {
        "status": "PASS",
        "packet_count": len(packets),
        "first_sequence": int(packets[0].sequence) if packets else None,
        "last_sequence": int(packets[-1].sequence) if packets else None,
        "native_rate_numerator": native_rate_numerator,
        "sample_count_per_packet": 1,
    }


def restore_selftest_off(
    serial_port: serial.Serial,
    *,
    read_size: int,
    command_fn: Callable[..., bytes],
    stop_fn: Callable[..., None],
) -> list[str]:
    """Stop acquisition and prove that the firmware returned to normal inputs."""

    failures: list[str] = []
    evidence: list[str] = []
    try:
        stop_fn(serial_port, response_timeout_sec=0.8, read_size=read_size)
        evidence.append("binary/text stream stop completed")
    except Exception as error:
        failures.append(f"stream stop failed: {error}")
    try:
        command_fn(
            serial_port,
            "ads stop",
            timeout_sec=0.8,
            read_size=read_size,
        )
        evidence.append("ads stop command completed")
    except Exception as error:
        failures.append(f"conversion stop failed: {error}")

    for command in ("ads selftest off", "ads selftest status"):
        try:
            raw = command_fn(
                serial_port,
                command,
                timeout_sec=1.0,
                read_size=read_size,
            )
            status = _parse_status_json(raw)
            if str(status.get("selftest_mode", "")).lower() != "off":
                failures.append(f"{command!r} reported {status!r}")
            else:
                evidence.append(f"{command} reported selftest_mode=off")
        except Exception as error:
            failures.append(f"{command!r} verification failed: {error}")

    if failures:
        raise SelftestRestorationError(
            "could not verify self-test restoration: " + "; ".join(failures),
            evidence=evidence,
            failures=failures,
        )
    return evidence


def _restoration_result(
    *,
    evidence: list[str] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe restoration result without inferring success."""

    if error is None:
        return {
            "attempted": True,
            "status": "PASS",
            "evidence": list(evidence or []),
        }
    return {
        "attempted": True,
        "status": "FAIL",
        "evidence": list(getattr(error, "evidence", evidence or [])),
        "failures": list(getattr(error, "failures", [str(error)])),
        "error": str(error),
    }


def _attach_sequence_diagnostics(
    error: BaseException,
    *,
    restoration: dict[str, Any],
    modes: dict[str, Any],
) -> None:
    """Attach audit evidence for the CLI failure-report path."""

    try:
        error.selftest_restoration = restoration  # type: ignore[attr-defined]
        error.selftest_partial_modes = modes  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        # Built-in exceptions normally accept attributes.  Do not mask the
        # acquisition/restoration error if a foreign exception type does not.
        pass


def execute_selftest_sequence(
    serial_port: serial.Serial,
    *,
    firmware_config: dict[str, Any],
    sample_count: int,
    timeout_sec: float,
    tolerance_percent_full_scale: float,
    max_std_counts: float,
    read_size: int = 1024,
    command_fn: Callable[..., bytes] = command_response,
    start_stream_fn: Callable[..., bytes] = start_v2_binary_stream,
    collect_fn: Callable[..., list] = collect_v2_packets,
    stop_fn: Callable[..., None] = stop_text_and_binary_output,
) -> dict[str, Any]:
    """Execute all modes and always restore ``ads selftest off``."""

    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    adcmax = int(firmware_config["adcmax"])
    expected = expected_selftest_codes(adcmax)
    modes: dict[str, Any] = {}

    primary_error: BaseException | None = None
    try:
        for mode in SELFTEST_MODES:
            stop_fn(
                serial_port,
                response_timeout_sec=0.8,
                read_size=read_size,
            )
            command_fn(
                serial_port,
                "ads stop",
                timeout_sec=0.8,
                read_size=read_size,
            )
            status_raw = command_fn(
                serial_port,
                f"ads selftest {mode}",
                timeout_sec=1.0,
                read_size=read_size,
            )
            status = _parse_status_json(status_raw)
            if str(status.get("selftest_mode", "")).lower() != mode:
                raise RuntimeError(
                    f"firmware did not enter self-test mode {mode!r}: {status}"
                )

            initial = start_stream_fn(
                serial_port,
                response_timeout_sec=0.8,
                detect_timeout_sec=timeout_sec,
                read_size=read_size,
            )
            if not initial:
                raise TimeoutError(f"no V2 stream detected for self-test {mode}")
            packets = collect_fn(
                serial_port,
                sample_count=sample_count,
                timeout_sec=timeout_sec,
                initial_data=initial,
                read_size=read_size,
            )
            integrity = validate_selftest_packets(
                packets,
                expected_count=sample_count,
                native_rate_numerator=int(
                    firmware_config.get(
                        "native_sample_rate_numerator",
                        V2_NATIVE_RATE_NUMERATOR,
                    )
                ),
            )
            lead_i = np.asarray([packet.lead_i_code for packet in packets])
            lead_ii = np.asarray([packet.lead_ii_code for packet in packets])
            mode_result = {
                "firmware_status": status,
                "acquisition_integrity": integrity,
                "expected_code": expected[mode],
                "lead_i": channel_statistics(
                    lead_i,
                    expected_code=expected[mode],
                    adcmax=adcmax,
                    tolerance_percent_full_scale=tolerance_percent_full_scale,
                    max_std_counts=max_std_counts,
                ),
                "lead_ii": channel_statistics(
                    lead_ii,
                    expected_code=expected[mode],
                    adcmax=adcmax,
                    tolerance_percent_full_scale=tolerance_percent_full_scale,
                    max_std_counts=max_std_counts,
                ),
            }
            mode_result["status"] = (
                "PASS"
                if all(
                    mode_result[lead]["status"] == "PASS"
                    for lead in ("lead_i", "lead_ii")
                )
                else "FAIL"
            )
            modes[mode] = mode_result
    except BaseException as error:
        primary_error = error

    restoration_error: BaseException | None = None
    restoration_evidence: list[str] = []
    try:
        restoration_evidence = restore_selftest_off(
            serial_port,
            read_size=read_size,
            command_fn=command_fn,
            stop_fn=stop_fn,
        )
    except BaseException as error:
        restoration_error = error

    restoration = _restoration_result(
        evidence=restoration_evidence,
        error=restoration_error,
    )

    if primary_error is not None:
        if restoration_error is not None:
            combined_error = RuntimeError(
                f"self-test failed ({primary_error}); additionally, "
                f"restoration failed ({restoration_error})"
            )
            _attach_sequence_diagnostics(
                combined_error,
                restoration=restoration,
                modes=modes,
            )
            raise combined_error from primary_error
        _attach_sequence_diagnostics(
            primary_error,
            restoration=restoration,
            modes=modes,
        )
        raise primary_error.with_traceback(primary_error.__traceback__)
    if restoration_error is not None:
        _attach_sequence_diagnostics(
            restoration_error,
            restoration=restoration,
            modes=modes,
        )
        raise restoration_error

    overall = "PASS" if all(row["status"] == "PASS" for row in modes.values()) else "FAIL"
    return {
        "created_at_utc": utc_now_iso(),
        "test_type": "ADS1293_internal_selftest",
        "statement": SELFTEST_STATEMENT,
        "adcmax": adcmax,
        "expected_codes": expected,
        "sample_count_per_mode": sample_count,
        "tolerances": {
            "percent_full_scale": tolerance_percent_full_scale,
            "max_std_counts": max_std_counts,
        },
        "modes": modes,
        "restoration": restoration,
        "status": overall,
    }


def serial_settings(args: argparse.Namespace) -> dict[str, Any]:
    """Return the serial/acquisition settings persisted in every CLI report."""

    return {
        "port": str(args.port),
        "baud_rate": int(args.baudrate),
        "serial_read_timeout_seconds": 0.1,
        "boot_delay_seconds": float(args.boot_delay),
        "collection_timeout_seconds": float(args.timeout),
        "samples_per_mode": int(args.samples),
    }


def build_failure_report(
    args: argparse.Namespace,
    error: Exception,
    *,
    firmware_config: dict[str, Any] | None = None,
    compatibility_failures: list[str] | None = None,
) -> dict[str, Any]:
    """Create an auditable report for handshake or sequence exceptions."""

    restoration = getattr(error, "selftest_restoration", None)
    if not isinstance(restoration, dict):
        restoration = {
            "attempted": False,
            "status": "UNKNOWN",
            "evidence": [
                "self-test restoration evidence is unavailable; firmware state "
                "was not claimed as restored"
            ],
        }
    report: dict[str, Any] = {
        "created_at_utc": utc_now_iso(),
        "test_type": "ADS1293_internal_selftest",
        "statement": SELFTEST_STATEMENT,
        "status": "FAIL",
        "error": str(error),
        "error_type": type(error).__name__,
        "serial_settings": serial_settings(args),
        # Retain the established scalar fields for existing report consumers.
        "serial_port": str(args.port),
        "baud_rate": int(args.baudrate),
        "restoration": restoration,
    }
    partial_modes = getattr(error, "selftest_partial_modes", None)
    if isinstance(partial_modes, dict):
        report["modes"] = partial_modes
    if firmware_config is not None:
        report["firmware_config"] = firmware_config
    if compatibility_failures is not None:
        report["firmware_compatibility_failures"] = list(
            compatibility_failures
        )
    return report


def apply_firmware_verification_status(
    report: dict[str, Any],
    compatibility_failures: list[str] | tuple[str, ...],
    *,
    override_used: bool,
) -> None:
    """Make an override diagnosable without converting it into a PASS."""

    failures = list(compatibility_failures)
    report["firmware_compatibility_failures"] = failures
    report["firmware_verification"] = {
        "status": "FAIL_UNVERIFIED" if failures else "PASS",
        "override_used": bool(override_used and failures),
        "compatibility_failures": failures,
    }
    if failures:
        report["numeric_selftest_status"] = report.get("status", "UNKNOWN")
        report["status"] = "FAIL_UNVERIFIED"


def write_report(output_dir: Path, report: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(report["created_at_utc"]).replace(":", "").replace("-", "")
    path = output_dir / f"selftest_{stamp}.json"
    suffix = 1
    while path.exists():
        path = output_dir / f"selftest_{stamp}_{suffix:02d}.json"
        suffix += 1
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path


def main() -> int:
    args = parse_args()
    if args.samples <= 0:
        print("error: --samples must be positive", file=sys.stderr)
        return 2
    print(SELFTEST_STATEMENT)

    handshake = None
    try:
        with serial.Serial(
            port=args.port,
            baudrate=args.baudrate,
            timeout=0.1,
        ) as serial_port:
            for attribute in ("dtr", "rts"):
                try:
                    setattr(serial_port, attribute, False)
                except (AttributeError, OSError, ValueError, serial.SerialException):
                    pass
            if args.boot_delay > 0:
                time.sleep(args.boot_delay)
            serial_port.reset_input_buffer()
            handshake = verified_config_handshake(
                serial_port,
                allow_unverified=args.allow_unverified_firmware,
                response_timeout_sec=1.5,
            )
            report = execute_selftest_sequence(
                serial_port,
                firmware_config=handshake.config,
                sample_count=args.samples,
                timeout_sec=args.timeout,
                tolerance_percent_full_scale=args.tolerance_percent_full_scale,
                max_std_counts=args.max_std_counts,
            )
            report["serial_port"] = args.port
            report["baud_rate"] = args.baudrate
            report["serial_settings"] = serial_settings(args)
            report["firmware_config"] = handshake.config
            apply_firmware_verification_status(
                report,
                handshake.compatibility_failures,
                override_used=args.allow_unverified_firmware,
            )
    except Exception as error:
        firmware_config = None if handshake is None else handshake.config
        compatibility_failures = (
            None if handshake is None else list(handshake.compatibility_failures)
        )
        report = build_failure_report(
            args,
            error,
            firmware_config=firmware_config,
            compatibility_failures=compatibility_failures,
        )
        try:
            report_path = write_report(args.output_dir, report)
            print(f"report: {report_path}")
        except Exception as report_error:
            print(
                f"error: could not write self-test failure report: {report_error}",
                file=sys.stderr,
            )
        print(f"error: self-test failed: {error}", file=sys.stderr)
        return 2

    report_path = write_report(args.output_dir, report)
    for mode, row in report["modes"].items():
        for lead in ("lead_i", "lead_ii"):
            stats = row[lead]
            print(
                f"{mode:8s} {lead}: mean={stats['mean_code']:.3f} "
                f"median={stats['median_code']:.3f} "
                f"std={stats['standard_deviation_code']:.3f} "
                f"expected={stats['expected_code']} "
                f"abs_error={stats['absolute_code_error']:.3f} "
                f"full_scale_error={stats['percent_full_scale_error']:.6f}% "
                f"{stats['status']}"
            )
    print(f"report: {report_path}")
    print(f"overall: {report['status']}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Validation for calibrated ECG frames before they reach a plot.

Validation never changes a source file.  Non-float32 numerical arrays are
converted to a separate in-memory float32 array and reported as a warning.
Only sample-data failures block plotting; metadata and Lead III consistency
issues are warnings so an operator can still inspect the stored signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .frame_repository import FrameRecord


EXPECTED_SHAPE = (2500, 3)
EXPECTED_DTYPE = np.dtype(np.float32)
EXPECTED_SAMPLE_RATE_HZ = 250
EXPECTED_UNIT = "mV"
EXPECTED_CHANNEL_ORDER = ("Lead I", "Lead II", "Lead III")
DEFAULT_MAX_ABS_MV = 10.0
DEFAULT_LEAD_III_TOLERANCE_MV = 1e-5


class ValidationStatus(str, Enum):
    """Severity used by the viewer's PASS/WARNING/FAIL badge."""

    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class LeadStatistics:
    """Summary statistics for one lead, calculated without altering samples."""

    name: str
    minimum_mv: float
    maximum_mv: float
    mean_mv: float
    std_mv: float

    @property
    def min_mv(self) -> float:
        """Short alias for display code."""

        return self.minimum_mv

    @property
    def max_mv(self) -> float:
        """Short alias for display code."""

        return self.maximum_mv


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Complete validation report consumed by the GUI.

    ``samples`` is always an in-memory float32 array when conversion succeeds.
    Callers must check :attr:`can_plot` before displaying it.
    """

    status: ValidationStatus
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    samples: np.ndarray | None
    metadata: Mapping[str, Any] | None
    source_shape: tuple[int, ...] | None
    source_dtype: str | None
    max_abs_mv: float | None
    lead_iii_max_error_mv: float | None
    per_lead_stats: tuple[LeadStatistics, ...]

    @property
    def data(self) -> np.ndarray | None:
        """Alias for GUI code that calls the plot-ready array ``data``."""

        return self.samples

    @property
    def failures(self) -> tuple[str, ...]:
        """Alias for blocking validation errors."""

        return self.errors

    @property
    def shape(self) -> tuple[int, ...] | None:
        """Original array shape."""

        return self.source_shape

    @property
    def dtype(self) -> str | None:
        """Original array dtype, before any in-memory conversion."""

        return self.source_dtype

    @property
    def lead3_error(self) -> float | None:
        """Compact alias for the Lead III maximum absolute equation error."""

        return self.lead_iii_max_error_mv

    @property
    def can_plot(self) -> bool:
        """Whether sample data passed every plot-blocking check."""

        return self.status is not ValidationStatus.FAIL and self.samples is not None

    @property
    def is_valid(self) -> bool:
        """Whether the frame may be plotted (warnings are still valid)."""

        return self.can_plot

    @property
    def is_pass(self) -> bool:
        """Whether the frame passed without warnings."""

        return self.status is ValidationStatus.PASS


class FrameValidator:
    """Load and validate one canonical 10-second, three-lead ECG frame."""

    def __init__(
        self,
        *,
        max_abs_mv: float = DEFAULT_MAX_ABS_MV,
        lead_iii_tolerance_mv: float = DEFAULT_LEAD_III_TOLERANCE_MV,
    ) -> None:
        if max_abs_mv <= 0:
            raise ValueError("max_abs_mv must be positive")
        if lead_iii_tolerance_mv < 0:
            raise ValueError("lead_iii_tolerance_mv must not be negative")
        self.max_abs_limit_mv = float(max_abs_mv)
        self.lead_iii_tolerance_mv = float(lead_iii_tolerance_mv)

    def validate(
        self,
        source: FrameRecord | str | Path | np.ndarray,
        metadata: Mapping[str, Any] | None = None,
        *,
        metadata_path: str | Path | None = None,
    ) -> ValidationResult:
        """Validate a repository record, NPY path, or in-memory array.

        For array input, pass a metadata mapping to avoid the expected
        missing-metadata warning.  For file input, adjacent ``.json`` metadata
        is inferred unless an explicit ``metadata_path`` is supplied.
        """

        if isinstance(source, np.ndarray):
            return self.validate_array(
                source,
                metadata=metadata,
                metadata_available=metadata is not None,
            )

        if isinstance(source, FrameRecord):
            npy_path = source.npy_path
            inferred_metadata_path = source.metadata_path
            if inferred_metadata_path is None:
                # A watcher may observe JSON after the repository record was
                # created; checking the canonical neighbor is race-safe.
                candidate = source.npy_path.with_suffix(".json")
                if candidate.is_file():
                    inferred_metadata_path = candidate
        else:
            npy_path = Path(source)
            candidate = npy_path.with_suffix(".json")
            inferred_metadata_path = candidate if candidate.is_file() else None

        if metadata_path is not None:
            inferred_metadata_path = Path(metadata_path)

        loaded_metadata = metadata
        metadata_warning: str | None = None
        if loaded_metadata is None and inferred_metadata_path is not None:
            try:
                loaded_metadata = self._load_metadata(inferred_metadata_path)
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                MemoryError,
                RecursionError,
            ) as error:
                metadata_warning = (
                    f"Metadata could not be read from "
                    f"{inferred_metadata_path.name}: {error}."
                )

        mapped: Any = None
        try:
            # Parse and map the NPY before materializing it. A corrupt header
            # can claim a huge shape; mmap rejects truncated payloads without
            # attempting a correspondingly huge heap allocation. Wrong shapes
            # are returned to validate_array, which now fails before casting.
            mapped = np.load(
                npy_path,
                allow_pickle=False,
                mmap_mode="r",
            )
            if not isinstance(mapped, np.ndarray):
                raise ValueError("NPY source did not contain one NumPy array")
            mapped_dtype = mapped.dtype
            mapped_is_supported_number = (
                np.issubdtype(mapped_dtype, np.number)
                and not np.issubdtype(mapped_dtype, np.complexfloating)
                and not np.issubdtype(mapped_dtype, np.bool_)
            )
            if (
                tuple(int(value) for value in mapped.shape) != EXPECTED_SHAPE
                or not mapped_is_supported_number
            ):
                return self.validate_array(
                    mapped,
                    metadata=loaded_metadata,
                    metadata_available=(loaded_metadata is not None),
                    additional_warnings=(
                        () if metadata_warning is None else (metadata_warning,)
                    ),
                )
            # Keep plotting independent of later in-place file changes. Only
            # the canonical 7,500-element frame reaches this bounded copy.
            samples = np.array(mapped, copy=True, subok=False)
        except (
            OSError,
            ValueError,
            TypeError,
            EOFError,
            MemoryError,
            OverflowError,
        ) as error:
            warnings = self._metadata_warnings(
                loaded_metadata,
                available=(loaded_metadata is not None),
            )
            if metadata_warning is not None:
                warnings.insert(0, metadata_warning)
            failure = f"NPY frame could not be read from {npy_path}: {error}."
            return self._result(
                errors=[failure],
                warnings=warnings,
                samples=None,
                metadata=loaded_metadata,
                source_shape=None,
                source_dtype=None,
                max_abs_mv=None,
                lead_iii_error=None,
                statistics=(),
            )
        finally:
            mmap_handle = getattr(mapped, "_mmap", None)
            if mmap_handle is not None:
                try:
                    mmap_handle.close()
                except (OSError, ValueError, BufferError):
                    pass
            close = getattr(mapped, "close", None)
            if callable(close) and not isinstance(mapped, np.ndarray):
                try:
                    close()
                except (OSError, ValueError, RuntimeError):
                    pass

        return self.validate_array(
            samples,
            metadata=loaded_metadata,
            metadata_available=(loaded_metadata is not None),
            additional_warnings=(() if metadata_warning is None else (metadata_warning,)),
        )

    def validate_array(
        self,
        samples: np.ndarray,
        metadata: Mapping[str, Any] | None = None,
        *,
        metadata_available: bool | None = None,
        additional_warnings: tuple[str, ...] = (),
    ) -> ValidationResult:
        """Validate an array and return a non-mutating plot-ready report."""

        errors: list[str] = []
        warnings = list(additional_warnings)
        if metadata_available is None:
            metadata_available = metadata is not None
        warnings.extend(self._metadata_warnings(metadata, available=metadata_available))

        try:
            array = np.asarray(samples)
        except Exception as error:  # defensive for unusual array-like subclasses
            return self._result(
                errors=[f"Samples could not be interpreted as an array: {error}."],
                warnings=warnings,
                samples=None,
                metadata=metadata,
                source_shape=None,
                source_dtype=None,
                max_abs_mv=None,
                lead_iii_error=None,
                statistics=(),
            )

        source_shape = tuple(int(value) for value in array.shape)
        source_dtype = str(array.dtype)
        if source_shape != EXPECTED_SHAPE:
            errors.append(
                f"Shape is {source_shape}; expected exactly {EXPECTED_SHAPE}."
            )

        if not (
            np.issubdtype(array.dtype, np.number)
            and not np.issubdtype(array.dtype, np.complexfloating)
            and not np.issubdtype(array.dtype, np.bool_)
        ):
            errors.append(
                f"Dtype {source_dtype} is not a supported real numerical ECG dtype."
            )
            return self._result(
                errors=errors,
                warnings=warnings,
                samples=None,
                metadata=metadata,
                source_shape=source_shape,
                source_dtype=source_dtype,
                max_abs_mv=None,
                lead_iii_error=None,
                statistics=(),
            )

        if source_shape != EXPECTED_SHAPE:
            # Shape is plot-blocking, so avoid any cast, finite scan, or
            # statistic over an arbitrarily large malformed array.
            return self._result(
                errors=errors,
                warnings=warnings,
                samples=None,
                metadata=metadata,
                source_shape=source_shape,
                source_dtype=source_dtype,
                max_abs_mv=None,
                lead_iii_error=None,
                statistics=(),
            )

        if array.dtype != EXPECTED_DTYPE:
            warnings.append(
                f"Dtype is {source_dtype}; converted to float32 in memory only."
            )

        try:
            # Float64 diagnostics preserve a value just above the clinical
            # threshold before the required plot conversion rounds it.
            diagnostic = array.astype(np.float64, copy=False)
            plot_samples = array.astype(np.float32, copy=False)
        except (TypeError, ValueError, OverflowError, MemoryError) as error:
            errors.append(f"Samples could not be converted to float32: {error}.")
            return self._result(
                errors=errors,
                warnings=warnings,
                samples=None,
                metadata=metadata,
                source_shape=source_shape,
                source_dtype=source_dtype,
                max_abs_mv=None,
                lead_iii_error=None,
                statistics=(),
            )

        finite = bool(np.all(np.isfinite(diagnostic)))
        if not finite:
            nan_count = int(np.count_nonzero(np.isnan(diagnostic)))
            inf_count = int(np.count_nonzero(np.isinf(diagnostic)))
            errors.append(
                f"Samples contain non-finite values "
                f"({nan_count} NaN, {inf_count} Inf)."
            )

        max_abs_mv: float | None = None
        lead_iii_error: float | None = None
        statistics: tuple[LeadStatistics, ...] = ()
        if diagnostic.size and finite:
            max_abs_mv = float(np.max(np.abs(diagnostic)))
            if max_abs_mv > self.max_abs_limit_mv:
                errors.append(
                    f"Maximum absolute amplitude is {max_abs_mv:.6g} mV; "
                    f"limit is {self.max_abs_limit_mv:.6g} mV."
                )

            if source_shape == EXPECTED_SHAPE:
                lead_iii_error = float(
                    np.max(
                        np.abs(
                            diagnostic[:, 2]
                            - (diagnostic[:, 1] - diagnostic[:, 0])
                        )
                    )
                )
                if lead_iii_error > self.lead_iii_tolerance_mv:
                    warnings.append(
                        f"Lead III maximum equation error is "
                        f"{lead_iii_error:.6g} mV; tolerance is "
                        f"{self.lead_iii_tolerance_mv:.6g} mV."
                    )
                statistics = self._calculate_statistics(diagnostic)

        return self._result(
            errors=errors,
            warnings=warnings,
            samples=plot_samples,
            metadata=metadata,
            source_shape=source_shape,
            source_dtype=source_dtype,
            max_abs_mv=max_abs_mv,
            lead_iii_error=lead_iii_error,
            statistics=statistics,
        )

    @staticmethod
    def _load_metadata(path: Path) -> Mapping[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        if not isinstance(metadata, dict):
            raise ValueError("metadata root must be a JSON object")
        return metadata

    @staticmethod
    def _metadata_warnings(
        metadata: Mapping[str, Any] | None,
        *,
        available: bool,
    ) -> list[str]:
        if not available or metadata is None:
            return ["Matching JSON metadata is missing; NPY samples can still be plotted."]

        warnings: list[str] = []
        sample_rate = metadata.get("sample_rate_hz")
        if sample_rate != EXPECTED_SAMPLE_RATE_HZ:
            warnings.append(
                f"Metadata sample_rate_hz is {sample_rate!r}; expected "
                f"{EXPECTED_SAMPLE_RATE_HZ}."
            )

        unit = metadata.get("unit")
        if unit != EXPECTED_UNIT:
            warnings.append(
                f"Metadata unit is {unit!r}; expected {EXPECTED_UNIT!r}."
            )

        channel_order = metadata.get("channel_order")
        normalized_channel_order = (
            tuple(channel_order)
            if isinstance(channel_order, (list, tuple))
            else None
        )
        if normalized_channel_order != EXPECTED_CHANNEL_ORDER:
            # Parentheses avoid displaying a generator or another surprising
            # metadata type while retaining the exact stored value in details.
            warnings.append(
                f"Metadata channel_order is {channel_order!r}; expected "
                f"{list(EXPECTED_CHANNEL_ORDER)!r}."
            )

        optional_checks: tuple[tuple[str, Any], ...] = (
            ("sample_count", EXPECTED_SHAPE[0]),
            ("shape", list(EXPECTED_SHAPE)),
            ("dtype", "float32"),
            ("duration_seconds", EXPECTED_SHAPE[0] / EXPECTED_SAMPLE_RATE_HZ),
        )
        for key, expected in optional_checks:
            if key in metadata and metadata[key] != expected:
                warnings.append(
                    f"Metadata {key} is {metadata[key]!r}; expected {expected!r}."
                )
        return warnings

    @staticmethod
    def _calculate_statistics(samples: np.ndarray) -> tuple[LeadStatistics, ...]:
        stats: list[LeadStatistics] = []
        for index, name in enumerate(EXPECTED_CHANNEL_ORDER):
            lead = samples[:, index]
            stats.append(
                LeadStatistics(
                    name=name,
                    minimum_mv=float(np.min(lead)),
                    maximum_mv=float(np.max(lead)),
                    mean_mv=float(np.mean(lead)),
                    std_mv=float(np.std(lead)),
                )
            )
        return tuple(stats)

    @staticmethod
    def _result(
        *,
        errors: list[str],
        warnings: list[str],
        samples: np.ndarray | None,
        metadata: Mapping[str, Any] | None,
        source_shape: tuple[int, ...] | None,
        source_dtype: str | None,
        max_abs_mv: float | None,
        lead_iii_error: float | None,
        statistics: tuple[LeadStatistics, ...],
    ) -> ValidationResult:
        if errors:
            status = ValidationStatus.FAIL
            reasons = tuple(errors + warnings)
        elif warnings:
            status = ValidationStatus.WARNING
            reasons = tuple(warnings)
        else:
            status = ValidationStatus.PASS
            reasons = ("All frame validation checks passed.",)
        return ValidationResult(
            status=status,
            reasons=reasons,
            warnings=tuple(warnings),
            errors=tuple(errors),
            samples=samples,
            metadata=metadata,
            source_shape=source_shape,
            source_dtype=source_dtype,
            max_abs_mv=max_abs_mv,
            lead_iii_max_error_mv=lead_iii_error,
            per_lead_stats=statistics,
        )


def validate_frame(
    source: FrameRecord | str | Path | np.ndarray,
    metadata: Mapping[str, Any] | None = None,
    *,
    max_abs_mv: float = DEFAULT_MAX_ABS_MV,
    lead_iii_tolerance_mv: float = DEFAULT_LEAD_III_TOLERANCE_MV,
) -> ValidationResult:
    """Convenience wrapper around :class:`FrameValidator`."""

    validator = FrameValidator(
        max_abs_mv=max_abs_mv,
        lead_iii_tolerance_mv=lead_iii_tolerance_mv,
    )
    return validator.validate(source, metadata=metadata)


__all__ = [
    "DEFAULT_LEAD_III_TOLERANCE_MV",
    "DEFAULT_MAX_ABS_MV",
    "EXPECTED_CHANNEL_ORDER",
    "EXPECTED_DTYPE",
    "EXPECTED_SAMPLE_RATE_HZ",
    "EXPECTED_SHAPE",
    "EXPECTED_UNIT",
    "FrameValidator",
    "LeadStatistics",
    "ValidationResult",
    "ValidationStatus",
    "validate_frame",
]

"""Stateful ADS1293 native-to-250 Hz rational resampling.

The corrected ADS1293 configuration produces ``800/3`` samples/second.  The
canonical downstream rate is 250 Hz, so the exact rational rate conversion is
``up=15, down=16``.  This module deliberately keeps the resampler independent
from serial parsing and file export.

The FIR is the same Kaiser-windowed low-pass design used by SciPy's default
``resample_poly`` implementation.  The verified offset-binary zero code is
removed before filtering and restored afterward, so the representation bias
cannot create a large startup transient or a phase-dependent DC ripple.
Filtering is performed causally while preserving both FIR state and decimation
phase between calls.  The default filter has a 10-output-sample (40 ms) group
delay.  Those 10 causal warm-up outputs are held internally; :meth:`finalize`
supplies the right-hand centered-zero padding needed for a finite stream.
Consequently, ``process(...)`` followed by ``finalize()`` matches
``resample_poly(x - zero_code, 15, 16) + zero_code`` and has exactly
``ceil(N * 15 / 16)`` samples.

For a live stream, ``finalize`` is not called: output simply trails acquisition
by the documented 40 ms filter delay.  A sequence loss must call
``StreamingECGFramePipeline`` (or ``reset_after_sequence_loss`` together with
``ResampledFrameAccumulator``); no missing native packet is interpolated.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

import numpy as np
from scipy import signal


NATIVE_SAMPLE_RATE_HZ = 800.0 / 3.0
OUTPUT_SAMPLE_RATE_HZ = 250.0
RESAMPLE_UP = 15
RESAMPLE_DOWN = 16
DEFAULT_FRAME_SIZE = 2500
UINT32_MODULUS = 1 << 32
ADS1293_ZERO_CODE = 0x798000


@dataclass(frozen=True)
class ResamplerStats:
    """Immutable snapshot of cumulative streaming-resampler statistics."""

    native_samples_consumed: int
    output_samples_produced: int
    current_phase: int
    resampler_reset_count: int
    filter_warmup_discard_count: int
    sequence_gap_count: int
    sequence_discontinuity_count: int
    missing_native_samples: int
    filter_delay_output_samples: int
    finalized: bool


@dataclass(frozen=True)
class FrameAccumulatorStats:
    """Immutable snapshot of canonical-frame assembly statistics."""

    output_samples_received: int
    completed_frames: int
    partial_samples: int
    discarded_partial_samples: int
    loss_affected_partial_frames: int


@dataclass(frozen=True)
class SequenceLossEvent:
    """Details of one detected native sequence discontinuity."""

    expected_sequence: int
    received_sequence: int
    missing_native_samples: int
    discarded_partial_output_samples: int
    kind: str = "gap"


@dataclass(frozen=True)
class PipelineChunkResult:
    """DSP output, complete frames, and losses from one native input chunk.

    ``output_samples`` contains every newly published resampled sample exactly
    once, including samples also copied into a completed frame or retained in
    the partial-frame accumulator.  It is intended for continuous diagnostics
    such as ``raw_ecg.csv``; only ``frames`` have the canonical frame contract.
    """

    frames: tuple[np.ndarray, ...]
    loss_events: tuple[SequenceLossEvent, ...]
    output_samples: np.ndarray


def expected_output_count(
    native_sample_count: int,
    up: int = RESAMPLE_UP,
    down: int = RESAMPLE_DOWN,
) -> int:
    """Return SciPy-compatible output length ``ceil(N * up / down)``."""

    if native_sample_count < 0:
        raise ValueError("native_sample_count must be non-negative")
    if up <= 0 or down <= 0:
        raise ValueError("up and down must be positive")
    return (int(native_sample_count) * int(up) + int(down) - 1) // int(down)


def _design_default_taps(
    up: int,
    down: int,
    taps_per_phase: int,
) -> np.ndarray:
    """Create the default SciPy-compatible, DC-normalized polyphase FIR."""

    if taps_per_phase <= 0:
        raise ValueError("taps_per_phase must be positive")
    half_len = taps_per_phase * max(up, down)
    taps = signal.firwin(
        2 * half_len + 1,
        1.0 / max(up, down),
        window=("kaiser", 5.0),
    )
    # Upsampling inserts up-1 zeros, so a DC-preserving interpolation filter
    # has gain ``up``.  scipy.signal.resample_poly applies the same scaling.
    return np.asarray(taps * up, dtype=np.float64)


class StatefulPolyphaseResampler:
    """Stateful anti-aliased rational resampler for two acquired ECG leads.

    Input is an arbitrary-size ``(N, 2)`` array containing Lead I and Lead II
    ADS1293 offset-code counts.  Returned data is floating-point ``(M, 3)`` in
    canonical order: Lead I, Lead II, and the *derived* Lead III.

    The implementation uses a persistent FIR delay line and rational
    decimation phase.  It never independently resamples individual chunks or
    10-second frames.
    """

    def __init__(
        self,
        *,
        up: int = RESAMPLE_UP,
        down: int = RESAMPLE_DOWN,
        channels: int = 2,
        taps_per_phase: int = 10,
        fir_taps: np.ndarray | None = None,
        zero_code: float = ADS1293_ZERO_CODE,
    ) -> None:
        up = int(up)
        down = int(down)
        channels = int(channels)
        if up <= 0 or down <= 0:
            raise ValueError("up and down must be positive")
        if math.gcd(up, down) != 1:
            raise ValueError("up and down must be reduced to coprime integers")
        if channels != 2:
            raise ValueError("ADS1293 ECG resampling requires exactly two leads")

        self.up = up
        self.down = down
        self.channels = channels
        self.zero_code = float(zero_code)
        if not np.isfinite(self.zero_code):
            raise ValueError("zero_code must be finite")
        if fir_taps is None:
            taps = _design_default_taps(up, down, taps_per_phase)
        else:
            taps = np.asarray(fir_taps, dtype=np.float64)
            if taps.ndim != 1 or taps.size == 0:
                raise ValueError("fir_taps must be a non-empty one-dimensional array")
            if not np.isfinite(taps).all():
                raise ValueError("fir_taps contains NaN or Inf")

        if taps.size % 2 != 1:
            raise ValueError("linear-phase FIR must contain an odd number of taps")
        half_len = (int(taps.size) - 1) // 2
        if half_len % down != 0:
            raise ValueError(
                "FIR group delay must be an integer number of output samples"
            )

        taps.setflags(write=False)
        self._taps = taps
        self.filter_delay_output_samples = half_len // down
        self.filter_delay_native_samples = half_len / up

        self._native_samples_consumed = 0
        self._output_samples_produced = 0
        self._reset_count = 0
        self._warmup_discard_count = 0
        self._sequence_gap_count = 0
        self._sequence_discontinuity_count = 0
        self._missing_native_samples = 0
        self._begin_epoch()

    @property
    def filter_taps(self) -> np.ndarray:
        """Read-only FIR coefficients, including interpolation gain."""

        return self._taps

    @property
    def stats(self) -> ResamplerStats:
        """Return a consistent statistics snapshot."""

        return ResamplerStats(
            native_samples_consumed=self._native_samples_consumed,
            output_samples_produced=self._output_samples_produced,
            current_phase=self._native_phase,
            resampler_reset_count=self._reset_count,
            filter_warmup_discard_count=self._warmup_discard_count,
            sequence_gap_count=self._sequence_gap_count,
            sequence_discontinuity_count=self._sequence_discontinuity_count,
            missing_native_samples=self._missing_native_samples,
            filter_delay_output_samples=self.filter_delay_output_samples,
            finalized=self._finalized,
        )

    def _begin_epoch(self) -> None:
        self._zi = np.zeros(
            (self._taps.size - 1, self.channels),
            dtype=np.float64,
        )
        # Number of high-rate samples already consumed, modulo ``down``.
        self._highrate_phase = 0
        # Exact rational phase resulting only from real native samples.
        self._native_phase = 0
        self._epoch_native_samples = 0
        self._epoch_output_samples = 0
        self._warmup_remaining = self.filter_delay_output_samples
        self._finalized = False

    def process(self, measured_leads: np.ndarray | Sequence[Sequence[float]]) -> np.ndarray:
        """Consume an arbitrary native chunk and return available samples.

        FIR state, rational phase, and the 40 ms alignment delay remain live
        across calls.  Call :meth:`finalize` exactly once only for a finite
        offline stream; a running receiver must leave the stream open.
        """

        if self._finalized:
            raise RuntimeError("cannot process after finalize(); reset first")
        samples = self._validate_input(measured_leads)
        native_count = int(samples.shape[0])
        if native_count == 0:
            return self._empty_output()

        upsampled = np.zeros(
            (native_count * self.up, self.channels),
            dtype=np.float64,
        )
        # Offset-binary zero is a representation bias, not an ECG signal.
        # Filtering the ~8-million-count bias directly creates a large edge
        # transient and phase-dependent DC ripple.  Resample differential
        # deviations, then restore the verified zero code at publication.
        upsampled[:: self.up, :] = samples - self.zero_code
        measured_output = self._filter_highrate(upsampled)

        self._native_samples_consumed += native_count
        self._epoch_native_samples += native_count
        self._native_phase = (
            self._native_phase + native_count * self.up
        ) % self.down
        return self._publish(measured_output)

    def finalize(self) -> np.ndarray:
        """Flush a finite epoch and return its remaining delay-compensated data.

        Right-hand zero padding is applied in the centered signal domain,
        equivalent to padding the original offset-code stream with
        ``zero_code``. No padded value is counted as a consumed native sample.
        The object becomes closed until reset.
        """

        if self._finalized:
            return self._empty_output()

        target_count = expected_output_count(
            self._epoch_native_samples,
            self.up,
            self.down,
        )
        public_needed = target_count - self._epoch_output_samples
        if public_needed < 0:
            raise RuntimeError("internal output accounting exceeded target count")
        candidates_needed = public_needed + self._warmup_remaining

        if candidates_needed:
            first_decimated_offset = (-self._highrate_phase) % self.down
            highrate_count = (
                first_decimated_offset
                + (candidates_needed - 1) * self.down
                + 1
            )
            zeros = np.zeros((highrate_count, self.channels), dtype=np.float64)
            measured_output = self._filter_highrate(zeros)
            output = self._publish(measured_output)
        else:
            output = self._empty_output()

        if self._epoch_output_samples != target_count:
            raise RuntimeError(
                "internal resampler flush count mismatch: "
                f"expected {target_count}, produced {self._epoch_output_samples}"
            )
        self._finalized = True
        return output

    def reset(self) -> None:
        """Abandon the current epoch and clear filter/phase state.

        Cumulative statistics are retained.  Use
        :meth:`reset_after_sequence_loss` when the cause is missing packets so
        exact loss accounting is preserved.
        """

        self._reset_count += 1
        self._begin_epoch()

    def reset_after_sequence_loss(self, missing_native_samples: int) -> None:
        """Reset without interpolation and record an exact native loss count."""

        missing = int(missing_native_samples)
        if missing <= 0:
            raise ValueError("missing_native_samples must be positive")
        self.reset_after_discontinuity(missing)

    def reset_after_discontinuity(self, missing_native_samples: int = 0) -> None:
        """Reset after any unsafe sequence discontinuity.

        ``missing_native_samples`` is exact and positive for a forward gap.
        Duplicate or out-of-order packets use zero: the offending packet must
        be dropped and the caller must establish a new sequence baseline.
        """

        missing = int(missing_native_samples)
        if missing < 0:
            raise ValueError("missing_native_samples must be non-negative")
        self._sequence_discontinuity_count += 1
        if missing:
            self._sequence_gap_count += 1
            self._missing_native_samples += missing
        self.reset()

    def _validate_input(
        self,
        measured_leads: np.ndarray | Sequence[Sequence[float]],
    ) -> np.ndarray:
        samples = np.asarray(measured_leads, dtype=np.float64)
        if samples.ndim != 2 or samples.shape[1] != self.channels:
            raise ValueError(
                f"measured_leads must have shape (N, {self.channels}), "
                f"got {samples.shape}"
            )
        if not np.isfinite(samples).all():
            raise ValueError("measured_leads contains NaN or Inf")
        return samples

    def _filter_highrate(self, highrate_input: np.ndarray) -> np.ndarray:
        if highrate_input.shape[0] == 0:
            return np.empty((0, self.channels), dtype=np.float64)
        filtered, self._zi = signal.lfilter(
            self._taps,
            [1.0],
            highrate_input,
            axis=0,
            zi=self._zi,
        )
        first = (-self._highrate_phase) % self.down
        decimated = filtered[first:: self.down, :]
        self._highrate_phase = (
            self._highrate_phase + int(highrate_input.shape[0])
        ) % self.down
        return decimated

    def _publish(self, measured_output: np.ndarray) -> np.ndarray:
        discard = min(self._warmup_remaining, int(measured_output.shape[0]))
        if discard:
            measured_output = measured_output[discard:, :]
            self._warmup_remaining -= discard
            self._warmup_discard_count += discard

        count = int(measured_output.shape[0])
        if count == 0:
            return self._empty_output()
        measured_output = measured_output + self.zero_code
        lead_iii = measured_output[:, 1] - measured_output[:, 0]
        output = np.column_stack(
            (measured_output[:, 0], measured_output[:, 1], lead_iii)
        )
        self._output_samples_produced += count
        self._epoch_output_samples += count
        return np.asarray(output, dtype=np.float64)

    @staticmethod
    def _empty_output() -> np.ndarray:
        return np.empty((0, 3), dtype=np.float64)


class ResampledFrameAccumulator:
    """Accumulate continuous 250 Hz output into exact canonical frames."""

    def __init__(
        self,
        frame_size: int = DEFAULT_FRAME_SIZE,
        *,
        output_dtype: np.dtype | type = np.float32,
    ) -> None:
        frame_size = int(frame_size)
        if frame_size <= 0:
            raise ValueError("frame_size must be positive")
        self.frame_size = frame_size
        self.output_dtype = np.dtype(output_dtype)
        if not np.issubdtype(self.output_dtype, np.floating):
            raise ValueError("output_dtype must be floating point")
        self._partial = np.empty((0, 3), dtype=np.float64)
        self._received = 0
        self._completed = 0
        self._discarded = 0
        self._loss_affected = 0

    @property
    def stats(self) -> FrameAccumulatorStats:
        return FrameAccumulatorStats(
            output_samples_received=self._received,
            completed_frames=self._completed,
            partial_samples=int(self._partial.shape[0]),
            discarded_partial_samples=self._discarded,
            loss_affected_partial_frames=self._loss_affected,
        )

    def append(self, output_samples: np.ndarray) -> list[np.ndarray]:
        samples = np.asarray(output_samples, dtype=np.float64)
        if samples.ndim != 2 or samples.shape[1] != 3:
            raise ValueError(f"output_samples must have shape (N, 3), got {samples.shape}")
        if not np.isfinite(samples).all():
            raise ValueError("output_samples contains NaN or Inf")
        if samples.shape[0] == 0:
            return []

        self._received += int(samples.shape[0])
        if self._partial.shape[0]:
            samples = np.concatenate((self._partial, samples), axis=0)

        frames: list[np.ndarray] = []
        complete_count = int(samples.shape[0]) // self.frame_size
        for index in range(complete_count):
            start = index * self.frame_size
            stop = start + self.frame_size
            frame = samples[start:stop, :2].astype(self.output_dtype, copy=True)
            # Derive Lead III *after* casting the measured leads to the
            # canonical dtype.  This keeps the float32 identity exact instead
            # of independently rounding a float64 difference.
            lead_iii = frame[:, 1] - frame[:, 0]
            frames.append(np.column_stack((frame, lead_iii)))
        consumed = complete_count * self.frame_size
        self._partial = samples[consumed:, :].copy()
        self._completed += complete_count
        return frames

    def discard_partial(self) -> int:
        """Discard an incomplete frame, returning its exact sample count."""

        discarded = int(self._partial.shape[0])
        if discarded:
            self._discarded += discarded
            self._loss_affected += 1
            self._partial = np.empty((0, 3), dtype=np.float64)
        return discarded

    def pending_snapshot(self) -> np.ndarray:
        """Copy currently buffered samples without marking a frame complete."""

        return self._partial.copy()


class StreamingECGFramePipeline:
    """Sequence-aware resampling plus loss-safe 2500-sample frame assembly."""

    def __init__(
        self,
        *,
        resampler: StatefulPolyphaseResampler | None = None,
        frame_size: int = DEFAULT_FRAME_SIZE,
        frame_dtype: np.dtype | type = np.float32,
    ) -> None:
        self.resampler = resampler or StatefulPolyphaseResampler()
        self.frames = ResampledFrameAccumulator(
            frame_size=frame_size,
            output_dtype=frame_dtype,
        )
        self._last_sequence: int | None = None

    @property
    def last_sequence(self) -> int | None:
        return self._last_sequence

    def process(
        self,
        measured_leads: np.ndarray,
        sequences: Iterable[int],
    ) -> PipelineChunkResult:
        samples = np.asarray(measured_leads)
        if samples.ndim != 2 or samples.shape[1] != 2:
            raise ValueError(f"measured_leads must have shape (N, 2), got {samples.shape}")
        sequence_array = np.asarray(list(sequences), dtype=np.uint64)
        if sequence_array.ndim != 1 or sequence_array.size != samples.shape[0]:
            raise ValueError("sequences must contain exactly one value per native sample")
        if np.any(sequence_array >= UINT32_MODULUS):
            raise ValueError("sequence values must fit uint32")
        if samples.shape[0] == 0:
            return PipelineChunkResult(
                (),
                (),
                np.empty((0, 3), dtype=np.float64),
            )

        completed: list[np.ndarray] = []
        losses: list[SequenceLossEvent] = []
        published: list[np.ndarray] = []
        run_start = 0

        for index, raw_sequence in enumerate(sequence_array):
            received = int(raw_sequence)
            if self._last_sequence is not None:
                expected = (self._last_sequence + 1) % UINT32_MODULUS
                if received != expected:
                    forward = (received - expected) % UINT32_MODULUS
                    if index > run_start:
                        output = self.resampler.process(samples[run_start:index, :2])
                        published.append(output)
                        completed.extend(self.frames.append(output))
                    discarded = self.frames.discard_partial()
                    is_forward_gap = 0 < forward < UINT32_MODULUS // 2
                    if is_forward_gap:
                        missing = forward
                        kind = "gap"
                        self.resampler.reset_after_sequence_loss(missing)
                    else:
                        missing = 0
                        kind = (
                            "duplicate"
                            if received == self._last_sequence
                            else "out_of_order"
                        )
                        self.resampler.reset_after_discontinuity()
                    losses.append(
                        SequenceLossEvent(
                            expected_sequence=expected,
                            received_sequence=received,
                            missing_native_samples=missing,
                            discarded_partial_output_samples=discarded,
                            kind=kind,
                        )
                    )
                    if is_forward_gap:
                        # The first packet after a known forward gap is valid.
                        run_start = index
                        self._last_sequence = received
                    else:
                        # Drop stale/duplicate input.  The next packet starts a
                        # fresh sequence epoch rather than causing reset loops.
                        run_start = index + 1
                        self._last_sequence = None
                    continue
            self._last_sequence = received

        if run_start < samples.shape[0]:
            output = self.resampler.process(samples[run_start:, :2])
            published.append(output)
            completed.extend(self.frames.append(output))
        output_samples = (
            np.concatenate(published, axis=0)
            if published
            else np.empty((0, 3), dtype=np.float64)
        )
        return PipelineChunkResult(
            tuple(completed),
            tuple(losses),
            output_samples,
        )

    def finalize(self) -> tuple[np.ndarray, ...]:
        """Finalize a finite input stream and return any newly completed frames."""

        return tuple(self.frames.append(self.resampler.finalize()))

    def reset_after_discontinuity(self, missing_native_samples: int = 0) -> int:
        """Explicitly abandon partial output and reset sequence/filter state.

        This is safe for receiver-side CRC/protocol discontinuities where no
        usable sequence value exists.  It returns the exact number of partial
        250 Hz samples discarded; a subsequent packet establishes the new
        native sequence baseline.
        """

        discarded = self.frames.discard_partial()
        self.resampler.reset_after_discontinuity(missing_native_samples)
        self._last_sequence = None
        return discarded

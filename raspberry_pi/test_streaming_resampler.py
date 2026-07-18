"""Regression tests for the corrected stateful 15/16 ECG resampler."""

from __future__ import annotations

import unittest

import numpy as np
from scipy import signal

from streaming_resampler import (
    ADS1293_ZERO_CODE,
    NATIVE_SAMPLE_RATE_HZ,
    OUTPUT_SAMPLE_RATE_HZ,
    ResampledFrameAccumulator,
    StatefulPolyphaseResampler,
    StreamingECGFramePipeline,
    expected_output_count,
)


class StatefulPolyphaseResamplerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = np.random.default_rng(1293)

    def random_signal(self, count: int) -> np.ndarray:
        return (
            ADS1293_ZERO_CODE
            + self.rng.normal(size=(count, 2)).astype(np.float64)
        )

    def process_random_chunks(
        self,
        samples: np.ndarray,
        *,
        finalize: bool = True,
    ) -> tuple[np.ndarray, StatefulPolyphaseResampler]:
        resampler = StatefulPolyphaseResampler()
        output = []
        offset = 0
        while offset < samples.shape[0]:
            size = min(
                samples.shape[0] - offset,
                int(self.rng.integers(1, 211)),
            )
            output.append(resampler.process(samples[offset : offset + size]))
            offset += size
        if finalize:
            output.append(resampler.finalize())
        return np.concatenate(output, axis=0), resampler

    def test_exact_ratio_and_30_second_count(self) -> None:
        self.assertEqual(expected_output_count(8000), 7500)
        self.assertEqual(expected_output_count(16), 15)
        self.assertEqual(expected_output_count(1), 1)

        output, resampler = self.process_random_chunks(self.random_signal(8000))
        self.assertEqual(output.shape, (7500, 3))
        self.assertEqual(resampler.stats.native_samples_consumed, 8000)
        self.assertEqual(resampler.stats.output_samples_produced, 7500)
        self.assertEqual(resampler.stats.current_phase, 0)

    def test_random_chunks_equal_one_chunk(self) -> None:
        samples = self.random_signal(4241)
        chunked, _ = self.process_random_chunks(samples)

        one = StatefulPolyphaseResampler()
        whole = np.concatenate((one.process(samples), one.finalize()), axis=0)
        np.testing.assert_allclose(chunked, whole, rtol=0.0, atol=2e-15)

    def test_matches_trusted_scipy_reference_after_documented_delay(self) -> None:
        samples = self.random_signal(4097)
        resampler = StatefulPolyphaseResampler()

        # Before finalization the live stream trails by the documented FIR
        # delay, rather than inventing future samples.
        live = resampler.process(samples)
        reference = (
            signal.resample_poly(
                samples - ADS1293_ZERO_CODE,
                15,
                16,
                axis=0,
            )
            + ADS1293_ZERO_CODE
        )
        delay = resampler.filter_delay_output_samples
        self.assertEqual(delay, 10)
        self.assertEqual(live.shape[0], reference.shape[0] - delay)
        np.testing.assert_allclose(live[:, :2], reference[:-delay], rtol=0.0, atol=2e-15)

        tail = resampler.finalize()
        finished = np.concatenate((live, tail), axis=0)
        np.testing.assert_allclose(finished[:, :2], reference, rtol=0.0, atol=2e-15)
        self.assertEqual(
            resampler.stats.filter_warmup_discard_count,
            delay,
        )

    def test_lead_iii_is_derived_after_measured_lead_resampling(self) -> None:
        output, _ = self.process_random_chunks(self.random_signal(1000))
        np.testing.assert_array_equal(output[:, 2], output[:, 1] - output[:, 0])

    def test_zero_code_dc_is_exact_from_first_sample(self) -> None:
        samples = np.full((4000, 2), ADS1293_ZERO_CODE, dtype=np.float64)
        output, _ = self.process_random_chunks(samples)
        np.testing.assert_array_equal(
            output[:, :2],
            np.full_like(output[:, :2], ADS1293_ZERO_CODE),
        )
        np.testing.assert_array_equal(output[:, 2], np.zeros(output.shape[0]))

    def test_low_frequency_and_40_hz_amplitudes_are_preserved(self) -> None:
        native_count = 8000
        time_native = np.arange(native_count) / NATIVE_SAMPLE_RATE_HZ
        amplitudes = (12_000.0, 5_000.0)
        frequencies = (1.25, 40.0)
        lead_i = sum(
            amplitude * np.sin(2.0 * np.pi * frequency * time_native)
            for amplitude, frequency in zip(amplitudes, frequencies)
        )
        samples = ADS1293_ZERO_CODE + np.column_stack((lead_i, 0.7 * lead_i))
        output, _ = self.process_random_chunks(samples)

        time_output = np.arange(output.shape[0]) / OUTPUT_SAMPLE_RATE_HZ
        interior = slice(100, -100)
        for amplitude, frequency in zip(amplitudes, frequencies):
            basis = np.exp(-2j * np.pi * frequency * time_output[interior])
            estimate = 2.0 * abs(
                np.mean((output[interior, 0] - ADS1293_ZERO_CODE) * basis)
            )
            self.assertAlmostEqual(estimate / amplitude, 1.0, delta=0.01)

    def test_rejects_nonfinite_input(self) -> None:
        resampler = StatefulPolyphaseResampler()
        samples = np.full((2, 2), ADS1293_ZERO_CODE, dtype=np.float64)
        samples[1, 0] = np.nan
        with self.assertRaisesRegex(ValueError, "NaN or Inf"):
            resampler.process(samples)

    def test_finalize_is_idempotent_and_process_requires_reset(self) -> None:
        resampler = StatefulPolyphaseResampler()
        resampler.process(np.full((16, 2), ADS1293_ZERO_CODE))
        self.assertEqual(resampler.finalize().shape[1], 3)
        self.assertEqual(resampler.finalize().shape, (0, 3))
        with self.assertRaises(RuntimeError):
            resampler.process(np.zeros((1, 2)))
        resampler.reset()
        self.assertEqual(
            resampler.process(np.full((1, 2), ADS1293_ZERO_CODE)).shape,
            (0, 3),
        )


class FrameAndLossSemanticsTests(unittest.TestCase):
    def test_8000_samples_make_exactly_three_float32_frames(self) -> None:
        native_count = 8000
        samples = np.column_stack(
            (
                np.arange(native_count, dtype=np.float64) + 7_962_624.0,
                np.arange(native_count, dtype=np.float64) + 7_962_700.0,
            )
        )
        sequences = np.arange(native_count, dtype=np.uint32)
        pipeline = StreamingECGFramePipeline()
        frames: list[np.ndarray] = []
        for start in range(0, native_count, 137):
            result = pipeline.process(
                samples[start : start + 137],
                sequences[start : start + 137],
            )
            frames.extend(result.frames)
        frames.extend(pipeline.finalize())

        self.assertEqual(len(frames), 3)
        for frame in frames:
            self.assertEqual(frame.shape, (2500, 3))
            self.assertEqual(frame.dtype, np.dtype("float32"))
            np.testing.assert_array_equal(frame[:, 2], frame[:, 1] - frame[:, 0])
        self.assertEqual(pipeline.frames.stats.partial_samples, 0)

        concatenated = np.concatenate(frames, axis=0)
        reference_measured = (
            signal.resample_poly(
                samples - ADS1293_ZERO_CODE,
                15,
                16,
                axis=0,
            )
            + ADS1293_ZERO_CODE
        ).astype(np.float32)
        reference = np.column_stack(
            (
                reference_measured,
                reference_measured[:, 1] - reference_measured[:, 0],
            )
        )
        # Includes both joins at output indexes 2500 and 5000.
        np.testing.assert_array_equal(concatenated, reference)

    def test_chunk_result_exposes_each_output_sample_without_resampling(self) -> None:
        pipeline = StreamingECGFramePipeline()
        samples = np.column_stack(
            (
                np.arange(3000, dtype=np.float64) + 7_962_624.0,
                np.arange(3000, dtype=np.float64) + 7_962_700.0,
            )
        )
        result = pipeline.process(samples, np.arange(3000, dtype=np.uint32))
        self.assertEqual(result.output_samples.ndim, 2)
        self.assertEqual(result.output_samples.shape[1], 3)
        self.assertEqual(
            result.output_samples.shape[0],
            2500 + pipeline.frames.stats.partial_samples,
        )
        np.testing.assert_array_equal(
            result.output_samples[:, 2],
            result.output_samples[:, 1] - result.output_samples[:, 0],
        )

    def test_forward_gap_discards_partial_frame_and_resets(self) -> None:
        first_count = 1000
        second_count = 3000
        samples_a = np.full((first_count, 2), 7_962_624.0)
        samples_b = np.full((second_count, 2), 7_962_624.0)
        pipeline = StreamingECGFramePipeline()

        first = pipeline.process(samples_a, np.arange(first_count, dtype=np.uint32))
        self.assertFalse(first.frames)
        partial_before = pipeline.frames.stats.partial_samples
        self.assertGreater(partial_before, 0)

        # Expected next sequence is 1000; receiving 1005 means exactly five
        # missing native packets.  The received sample itself remains valid.
        second_sequences = np.arange(1005, 1005 + second_count, dtype=np.uint32)
        second = pipeline.process(samples_b, second_sequences)
        self.assertEqual(len(second.loss_events), 1)
        event = second.loss_events[0]
        self.assertEqual(event.kind, "gap")
        self.assertEqual(event.missing_native_samples, 5)
        self.assertEqual(event.discarded_partial_output_samples, partial_before)
        self.assertEqual(pipeline.resampler.stats.resampler_reset_count, 1)
        self.assertEqual(pipeline.resampler.stats.sequence_gap_count, 1)
        self.assertEqual(pipeline.resampler.stats.missing_native_samples, 5)
        self.assertEqual(pipeline.frames.stats.loss_affected_partial_frames, 1)
        self.assertGreaterEqual(len(second.frames), 1)
        np.testing.assert_array_equal(
            second.output_samples[:, :2],
            np.full_like(second.output_samples[:, :2], ADS1293_ZERO_CODE),
        )
        np.testing.assert_array_equal(
            second.output_samples[:, 2],
            np.zeros(second.output_samples.shape[0]),
        )

    def test_duplicate_is_dropped_without_crashing_and_baseline_restarts(self) -> None:
        pipeline = StreamingECGFramePipeline()
        samples = np.full((32, 2), 7_962_624.0)
        pipeline.process(samples, np.arange(32, dtype=np.uint32))

        duplicate = pipeline.process(samples[:1], [31])
        self.assertEqual(len(duplicate.loss_events), 1)
        self.assertEqual(duplicate.loss_events[0].kind, "duplicate")
        self.assertEqual(duplicate.loss_events[0].missing_native_samples, 0)
        self.assertIsNone(pipeline.last_sequence)

        # Any valid uint32 sequence may establish the next epoch baseline.
        restarted = pipeline.process(samples[:2], [900, 901])
        self.assertFalse(restarted.loss_events)
        self.assertEqual(pipeline.last_sequence, 901)
        stats = pipeline.resampler.stats
        self.assertEqual(stats.sequence_gap_count, 0)
        self.assertEqual(stats.sequence_discontinuity_count, 1)
        self.assertEqual(stats.resampler_reset_count, 1)

    def test_out_of_order_is_dropped_without_crashing(self) -> None:
        pipeline = StreamingECGFramePipeline()
        samples = np.full((4, 2), 7_962_624.0)
        pipeline.process(samples[:3], [100, 101, 102])
        result = pipeline.process(samples[3:], [90])
        self.assertEqual(result.loss_events[0].kind, "out_of_order")
        self.assertIsNone(pipeline.last_sequence)

    def test_uint32_sequence_wrap_is_contiguous(self) -> None:
        pipeline = StreamingECGFramePipeline()
        samples = np.full((4, 2), 7_962_624.0)
        result = pipeline.process(
            samples,
            [0xFFFFFFFE, 0xFFFFFFFF, 0, 1],
        )
        self.assertFalse(result.loss_events)
        self.assertEqual(pipeline.last_sequence, 1)

    def test_explicit_discontinuity_discards_partial_and_resets_baseline(self) -> None:
        pipeline = StreamingECGFramePipeline()
        samples = np.full((100, 2), 7_962_624.0)
        pipeline.process(samples, np.arange(100, dtype=np.uint32))
        partial = pipeline.frames.stats.partial_samples
        discarded = pipeline.reset_after_discontinuity()
        self.assertEqual(discarded, partial)
        self.assertEqual(pipeline.frames.stats.partial_samples, 0)
        self.assertIsNone(pipeline.last_sequence)

    def test_frame_accumulator_never_emits_an_incomplete_frame(self) -> None:
        accumulator = ResampledFrameAccumulator(frame_size=2500)
        self.assertFalse(accumulator.append(np.zeros((2499, 3))))
        frames = accumulator.append(np.zeros((1, 3)))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].shape, (2500, 3))


if __name__ == "__main__":
    unittest.main()

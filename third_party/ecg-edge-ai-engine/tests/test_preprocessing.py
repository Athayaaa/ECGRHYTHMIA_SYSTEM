"""
Unit tests for the preprocessing module.
"""

import unittest
import numpy as np
from ecg_edge_ai_engine.preprocessing import (
    sanitize_signal,
    validate_signal,
    ensure_length,
    wavelet_denoising,
    median_baseline,
    bandpass_filter,
    zscore_normalization,
    advanced_cleaning_pipeline
)
from ecg_edge_ai_engine.utils import InvalidSignalShape

class TestPreprocessing(unittest.TestCase):
    
    def test_sanitize_signal(self):
        # Array with NaNs and Infs
        arr = np.array([[1.0, np.nan, np.inf], [-np.inf, 2.5, 3.0]], dtype=np.float32)
        sanitized = sanitize_signal(arr)
        
        # Check that NaN and Infs are replaced by 0.0
        self.assertEqual(sanitized[0, 1], 0.0)
        self.assertEqual(sanitized[0, 2], 0.0)
        self.assertEqual(sanitized[1, 0], 0.0)
        self.assertEqual(sanitized[1, 1], 2.5)
        self.assertEqual(sanitized[1, 2], 3.0)

    def test_validate_signal(self):
        # Valid 3-lead signal
        valid_signal = np.random.normal(size=(100, 3))
        validated = validate_signal(valid_signal)
        self.assertTrue(np.array_equal(valid_signal, validated))
        
        # Invalid channel count (2 channels instead of 3)
        invalid_channels = np.random.normal(size=(100, 2))
        with self.assertRaises(InvalidSignalShape):
            validate_signal(invalid_channels)
            
        # Invalid dimensions (1D array)
        invalid_dim = np.random.normal(size=(100,))
        with self.assertRaises(InvalidSignalShape):
            validate_signal(invalid_dim)

        # Signal too short
        too_short = np.random.normal(size=(10, 3))
        with self.assertRaises(InvalidSignalShape):
            validate_signal(too_short)

    def test_ensure_length_crop(self):
        # Input signal of shape (100, 3), target length 60
        signal = np.zeros((100, 3))
        signal[20:80, :] = 1.0 # Middle is 1.0
        
        cropped = ensure_length(signal, 60)
        self.assertEqual(cropped.shape, (60, 3))
        # Center cropping should yield the 60 samples from index 20 to 80 (exclusive)
        self.assertTrue(np.all(cropped == 1.0))

    def test_ensure_length_pad(self):
        # Input signal of shape (40, 3), target length 60
        signal = np.ones((40, 3))
        
        padded = ensure_length(signal, 60)
        self.assertEqual(padded.shape, (60, 3))
        # Zero padding should pad at the end with 0
        self.assertTrue(np.all(padded[:40, :] == 1.0))
        self.assertTrue(np.all(padded[40:, :] == 0.0))

    def test_dsp_operations(self):
        # Generate clean synthetic sine signal
        fs = 250.0
        t = np.linspace(0, 1, int(fs), endpoint=False)
        signal = np.stack([np.sin(2 * np.pi * 10 * t)] * 3, axis=1) # 10 Hz signal
        
        # 1. Wavelet denoising
        denoised = wavelet_denoising(signal)
        self.assertEqual(denoised.shape, signal.shape)
        
        # 2. Median baseline wander removal
        corrected = median_baseline(signal)
        self.assertEqual(corrected.shape, signal.shape)
        
        # 3. Bandpass filter
        filtered = bandpass_filter(signal, fs=fs, lowcut=0.5, highcut=45.0)
        self.assertEqual(filtered.shape, signal.shape)
        
        # 4. Z-score normalization
        normalized = zscore_normalization(signal)
        self.assertEqual(normalized.shape, signal.shape)
        # Standard deviation should be approximately 1.0 (or clipped to default max/min)
        for ch in range(normalized.shape[1]):
            self.assertTrue(np.max(normalized[:, ch]) <= 5.0)
            self.assertTrue(np.min(normalized[:, ch]) >= -5.0)

    def test_advanced_cleaning_pipeline(self):
        # Check pipeline end-to-end
        signal = np.random.normal(size=(500, 3))
        cleaned = advanced_cleaning_pipeline(signal, src_fs=250.0, target_fs=250.0)
        self.assertEqual(cleaned.shape, (500, 3))

if __name__ == '__main__':
    unittest.main()

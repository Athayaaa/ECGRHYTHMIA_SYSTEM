"""
Unit tests for the ECGEngine class.
"""

import unittest
import numpy as np
from ecg_edge_ai_engine import ECGEngine
from ecg_edge_ai_engine.utils import InvalidSignalShape, InvalidSamplingRate

class TestEngine(unittest.TestCase):
    
    def setUp(self):
        self.engine = ECGEngine()
        
    def test_predict_success_exact_length(self):
        # 10 seconds of 250 Hz signal (exactly 2500 samples)
        raw_signal = np.random.normal(size=(2500, 3))
        result = self.engine.predict(raw_signal, sampling_rate=250.0)
        
        # Verify result format and key constraints
        self.assertIsInstance(result, dict)
        self.assertIn("prediction", result)
        self.assertIn("confidence", result)
        self.assertIn("probabilities", result)
        self.assertIn("metadata", result)
        
        self.assertEqual(result["metadata"]["input_length"], 2500)
        self.assertEqual(result["metadata"]["sampling_rate"], 250.0)
        
    def test_predict_success_variable_length(self):
        # 8 seconds of 250 Hz signal (requires padding to 2500)
        raw_signal_short = np.random.normal(size=(2000, 3))
        result_short = self.engine.predict(raw_signal_short, sampling_rate=250.0)
        self.assertEqual(result_short["metadata"]["input_length"], 2500)
        
        # 12 seconds of 250 Hz signal (requires center cropping to 2500)
        raw_signal_long = np.random.normal(size=(3000, 3))
        result_long = self.engine.predict(raw_signal_long, sampling_rate=250.0)
        self.assertEqual(result_long["metadata"]["input_length"], 2500)

    def test_predict_invalid_signal_shape(self):
        # 1D array should raise InvalidSignalShape
        signal_1d = np.random.normal(size=(2500,))
        with self.assertRaises(InvalidSignalShape):
            self.engine.predict(signal_1d)
            
        # 2 channels instead of 3 should raise InvalidSignalShape
        signal_2ch = np.random.normal(size=(2500, 2))
        with self.assertRaises(InvalidSignalShape):
            self.engine.predict(signal_2ch)
            
    def test_predict_invalid_sampling_rate(self):
        raw_signal = np.random.normal(size=(2500, 3))
        
        # Zero and negative rates should raise InvalidSamplingRate
        with self.assertRaises(InvalidSamplingRate):
            self.engine.predict(raw_signal, sampling_rate=0.0)
        with self.assertRaises(InvalidSamplingRate):
            self.engine.predict(raw_signal, sampling_rate=-10.0)

if __name__ == '__main__':
    unittest.main()

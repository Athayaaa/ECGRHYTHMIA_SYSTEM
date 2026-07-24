"""
Unit tests for the inference module.
"""

import unittest
import numpy as np
from ecg_edge_ai_engine.model_loader import ModelLoader
from ecg_edge_ai_engine.inference import run_inference

class TestInference(unittest.TestCase):
    
    def test_run_inference(self):
        # Load the real interpreter from cache
        loader = ModelLoader()
        interpreter = loader.interpreter
        
        # Generate clean zero-signal matching the expected length and shape
        dummy_signal = np.zeros((2500, 3), dtype=np.float32)
        
        # Run inference
        probabilities = run_inference(interpreter, dummy_signal)
        
        # Verify output properties
        self.assertIsInstance(probabilities, np.ndarray)
        self.assertEqual(probabilities.ndim, 1)
        self.assertEqual(len(probabilities), 4)
        # Softmax outputs sum to approximately 1.0
        self.assertAlmostEqual(float(np.sum(probabilities)), 1.0, places=2)

if __name__ == '__main__':
    unittest.main()

"""
Unit tests for the postprocessing module.
"""

import unittest
import numpy as np
from ecg_edge_ai_engine.postprocessing import postprocess_prediction

class TestPostprocessing(unittest.TestCase):
    
    def setUp(self):
        self.labels = ["Normal", "AF", "Takikardia", "Bradikardia"]
        self.probs_normal = np.array([0.8, 0.1, 0.05, 0.05])
        self.probs_low = np.array([0.3, 0.25, 0.25, 0.2])  # Max is 0.3, which is below 0.5 threshold
        
    def test_postprocess_format(self):
        metadata = {"model_name": "TestModel", "version": "1.0.0"}
        result = postprocess_prediction(
            probabilities=self.probs_normal,
            labels=self.labels,
            threshold=0.5,
            model_metadata=metadata
        )
        
        # Verify structure
        self.assertIn("prediction", result)
        self.assertIn("confidence", result)
        self.assertIn("probabilities", result)
        self.assertIn("metadata", result)
        
        # Verify correctness
        self.assertEqual(result["prediction"], "Normal")
        self.assertEqual(result["confidence"], 80.0)
        self.assertEqual(result["probabilities"]["Normal"], 80.0)
        self.assertEqual(result["probabilities"]["AF"], 10.0)
        self.assertEqual(result["metadata"], metadata)
        
    def test_postprocess_threshold(self):
        result = postprocess_prediction(
            probabilities=self.probs_low,
            labels=self.labels,
            threshold=0.5,
            model_metadata=None
        )
        
        # Max prob is 0.3 (30%) which is below 0.5 threshold. Output prediction should be "Unclassified".
        self.assertEqual(result["prediction"], "Unclassified")
        self.assertEqual(result["confidence"], 30.0)
        self.assertEqual(result["probabilities"]["Normal"], 30.0)

if __name__ == '__main__':
    unittest.main()

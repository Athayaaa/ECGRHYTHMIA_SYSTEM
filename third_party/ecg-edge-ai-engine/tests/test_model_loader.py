"""
Unit tests for the model loader module.
"""

import unittest
from ecg_edge_ai_engine.model_loader import ModelLoader

class TestModelLoader(unittest.TestCase):
    
    def test_singleton(self):
        # Instantiate model loader multiple times
        loader1 = ModelLoader()
        loader2 = ModelLoader()
        
        # Verify that both refer to the exact same instance in memory (Singleton)
        self.assertIs(loader1, loader2)
        
    def test_load_interpreter_and_metadata(self):
        loader = ModelLoader()
        
        # Load model properties
        loader.load_model()
        
        # Verify cached structures
        self.assertIsNotNone(loader.interpreter)
        self.assertIsNotNone(loader.metadata)
        self.assertIn("model_name", loader.metadata)
        self.assertIn("labels", loader.metadata)
        self.assertIn("input_length", loader.metadata)
        self.assertIn("sampling_rate", loader.metadata)

if __name__ == '__main__':
    unittest.main()

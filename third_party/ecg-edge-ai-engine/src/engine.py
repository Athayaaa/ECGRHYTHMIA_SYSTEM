"""
Main engine module for the ECG Edge AI Engine.
Exposes the ECGEngine class as the main library interface.
"""

import numpy as np
from .utils import (
    setup_logger,
    InvalidSignalShape,
    InvalidSamplingRate,
    ModelNotLoaded,
    ECGEngineError
)
from . import config as cfg
from .model_loader import ModelLoader
from .preprocessing import advanced_cleaning_pipeline, ensure_length
from .inference import run_inference
from .postprocessing import postprocess_prediction

logger = setup_logger(__name__)

class ECGEngine:
    """
    ECGEngine is the principal class for the ECG AI inference engine.
    It encapsulates signal verification, cleaning/DSP, TFLite inference, 
    and structured JSON-compatible dictionary output.
    """
    
    def __init__(self):
        logger.info("Initializing ECGEngine startup.")
        try:
            self.model_loader = ModelLoader()
            # Eagerly load the model to cache the interpreter on startup
            self.model_loader.load_model()
            logger.info("ECGEngine successfully started and TFLite model loaded.")
        except Exception as e:
            logger.error(f"Failed to load TFLite model during ECGEngine startup: {e}")
            
    def predict(self, raw_signal: np.ndarray, sampling_rate: float = 250.0) -> dict:
        """
        Runs the complete prediction pipeline on the input ECG raw signal.
        
        Args:
            raw_signal: Numpy ndarray of shape (N, 3) representing 3-lead ECG.
            sampling_rate: Input signal sampling frequency (Hz), default is 250.0.
            
        Returns:
            dict: Structured prediction dictionary containing label, confidence, 
                  and probabilities.
        """
        logger.info("Prediction request received.")
        
        try:
            # 1. Validation of inputs
            if not isinstance(raw_signal, np.ndarray):
                raise InvalidSignalShape("Input signal must be a numpy ndarray.")
            if sampling_rate <= 0:
                raise InvalidSamplingRate(f"Sampling rate must be positive, got {sampling_rate}")
                
            # Check model loading status
            interpreter = self.model_loader.interpreter
            metadata = self.model_loader.metadata
            if interpreter is None:
                raise ModelNotLoaded("TFLite interpreter is not loaded.")
                
            # 2. Preprocessing
            logger.info("Starting preprocessing pipeline.")
            target_fs = metadata.get("sampling_rate", cfg.TARGET_FS)
            
            cleaned_signal = advanced_cleaning_pipeline(
                raw_signal=raw_signal,
                src_fs=sampling_rate,
                target_fs=target_fs
            )
            logger.info("Preprocessing pipeline completed.")
            
            # 3. Ensure input length (Prepare tensor shape)
            input_length = int(metadata.get("input_length", cfg.MODEL_INPUT_LENGTH))
            logger.info(f"Conditioning signal length to model input length: {input_length}.")
            prepared_signal = ensure_length(cleaned_signal, input_length)
            
            # 4. Inference
            logger.info("Starting TFLite inference.")
            raw_probabilities = run_inference(interpreter, prepared_signal)
            logger.info("TFLite inference completed.")
            
            # 5. Postprocessing
            logger.info("Starting postprocessing.")
            labels = metadata.get("labels", cfg.DEFAULT_CLASSES)
            threshold = float(metadata.get("threshold", cfg.DEFAULT_THRESHOLD))
            
            result = postprocess_prediction(
                probabilities=raw_probabilities,
                labels=labels,
                threshold=threshold,
                model_metadata={
                    "model_name": metadata.get("model_name", "Unknown model"),
                    "version": metadata.get("version", "Unknown version"),
                    "preprocessing_version": metadata.get("preprocessing_version", "Unknown version"),
                    "input_length": input_length,
                    "sampling_rate": target_fs
                }
            )
            logger.info(f"Prediction successful. Result: {result['prediction']}")
            return result
            
        except ECGEngineError as e:
            logger.error(f"ECGEngine operational error: {e}")
            raise e
        except Exception as e:
            msg = f"An unexpected error occurred during prediction: {e}"
            logger.error(msg)
            raise ECGEngineError(msg) from e

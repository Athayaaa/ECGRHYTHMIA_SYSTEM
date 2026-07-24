"""
Utilities and custom exceptions for the ECG Edge AI Engine.
"""

import logging
from . import config as cfg

class ECGEngineError(Exception):
    """Base exception for all ECG Edge AI Engine errors."""
    pass

class InvalidSignalShape(ECGEngineError):
    """Raised when the input signal shape is not (N, 3)."""
    pass

class InvalidSamplingRate(ECGEngineError):
    """Raised when the sampling rate is invalid."""
    pass

class ModelNotLoaded(ECGEngineError):
    """Raised when the TFLite model is not loaded or initialization fails."""
    pass

class ModelLoadError(ModelNotLoaded):
    """Raised when the model loader fails to read or load the model file."""
    pass

class InferenceError(ECGEngineError):
    """Raised when TFLite inference execution fails."""
    pass

def setup_logger(name: str) -> logging.Logger:
    """
    Sets up and returns a logger configured according to config.py.
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # Check config to enable/disable logging
        logging_enabled = getattr(cfg, "LOGGING_ENABLED", True)
        if logging_enabled:
            level_str = getattr(cfg, "LOGGING_LEVEL", "INFO").upper()
            level = getattr(logging, level_str, logging.INFO)
            logger.setLevel(level)
            logger.disabled = False
        else:
            logger.disabled = True
            
    return logger

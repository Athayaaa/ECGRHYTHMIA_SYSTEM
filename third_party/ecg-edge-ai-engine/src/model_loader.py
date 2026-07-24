"""
Model loader module for the ECG Edge AI Engine.
Handles loading and caching the TFLite interpreter and model metadata.
"""

import json
import threading
from pathlib import Path
from typing import Dict, Any

from . import config as cfg
from .utils import ModelLoadError, setup_logger

logger = setup_logger(__name__)

# Prefer the modern LiteRT runtime, while retaining legacy fallbacks.
try:
    from ai_edge_litert.interpreter import Interpreter
    RUNTIME_NAME = "ai-edge-litert"
except ImportError:
    try:
        from tflite_runtime.interpreter import Interpreter
        RUNTIME_NAME = "tflite-runtime"
    except ImportError:
        try:
            import tensorflow as tf
            Interpreter = tf.lite.Interpreter
            RUNTIME_NAME = "tensorflow"
        except ImportError as exc:
            raise ImportError(
                "No compatible LiteRT/TFLite runtime is installed. "
                "Install ai-edge-litert==2.1.6."
            ) from exc

class ModelLoader:
    """
    Thread-safe Singleton class to load and cache the TFLite model and its metadata.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ModelLoader, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._interpreter = None
        self._metadata = {}
        self._load_lock = threading.Lock()
        self._initialized = True

    def load_model(self) -> None:
        """
        Loads the TFLite interpreter and model metadata.
        Uses lazy loading under a thread-safe lock.
        """
        with self._load_lock:
            if self._interpreter is not None:
                return

            model_path = cfg.MODEL_DIR / "model.tflite"
            metadata_path = cfg.MODEL_DIR / "metadata.json"

            logger.info(f"Loading TFLite model from: {model_path}")
            if not model_path.exists():
                raise ModelLoadError(f"Model file not found at: {model_path}")

            try:
                logger.info(f"Using TFLite runtime: {RUNTIME_NAME}")
                self._interpreter = Interpreter(model_path=str(model_path))
                self._interpreter.allocate_tensors()
                logger.info("TFLite model loaded and tensors allocated successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize TFLite interpreter: {e}")
                self._interpreter = None
                raise ModelLoadError(f"Failed to load TFLite model: {e}") from e

            logger.info(f"Loading model metadata from: {metadata_path}")
            if metadata_path.exists():
                try:
                    with open(metadata_path, 'r', encoding='utf-8') as f:
                        self._metadata = json.load(f)
                    logger.info("Model metadata loaded successfully.")
                except Exception as e:
                    logger.error(f"Failed to parse metadata file: {e}")
                    raise ModelLoadError(f"Failed to parse metadata: {e}") from e
            else:
                logger.warning(f"Metadata file not found at {metadata_path}. Using empty metadata.")
                self._metadata = {}

    @property
    def interpreter(self):
        """Returns the cached TFLite interpreter, loading it if necessary."""
        if self._interpreter is None:
            self.load_model()
        return self._interpreter

    @property
    def metadata(self) -> Dict[str, Any]:
        """Returns the cached model metadata, loading it if necessary."""
        if self._interpreter is None:
            self.load_model()
        return self._metadata

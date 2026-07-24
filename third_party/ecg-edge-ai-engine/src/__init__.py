"""
ECG Edge AI Engine Package.
A black-box inference engine library for 3-lead 250 Hz ECG signals.
"""

from .engine import ECGEngine
from .version import __version__
from .utils import (
    ECGEngineError,
    InvalidSignalShape,
    InvalidSamplingRate,
    ModelNotLoaded,
    InferenceError
)

__all__ = [
    "ECGEngine",
    "__version__",
    "ECGEngineError",
    "InvalidSignalShape",
    "InvalidSamplingRate",
    "ModelNotLoaded",
    "InferenceError"
]

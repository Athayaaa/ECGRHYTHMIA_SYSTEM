"""
src/config.py

Global configuration for the ECG Edge AI Engine.
"""

import os
from pathlib import Path

# =============================================================================
# PROJECT ROOT & PATHS
# =============================================================================

# src/config.py -> src -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Dynamic model directory resolution
ENV_MODEL_DIR = os.environ.get("ECG_MODEL_DIR")
if ENV_MODEL_DIR:
    MODEL_DIR = Path(ENV_MODEL_DIR).resolve()
else:
    # Sibling directory (Repository mode)
    repo_models = PROJECT_ROOT / "models"
    # Subdirectory (Installed mode if packaged)
    pkg_models = Path(__file__).resolve().parent / "models"
    
    if repo_models.exists() and (repo_models / "model.tflite").exists():
        MODEL_DIR = repo_models
    elif pkg_models.exists() and (pkg_models / "model.tflite").exists():
        MODEL_DIR = pkg_models
    else:
        MODEL_DIR = repo_models

# =============================================================================
# DSP CONFIGURATION
# =============================================================================

TARGET_FS = 250.0
MODEL_INPUT_LENGTH = 2500

DEFAULT_WAVELET = "db4"
DEFAULT_WAVELET_LEVEL = 4
DEFAULT_MEDIAN_KERNEL = 51
DEFAULT_LOWCUT = 0.5
DEFAULT_HIGHCUT = 45.0
DEFAULT_FILTER_ORDER = 4

DEFAULT_CLIP_MIN = -5.0
DEFAULT_CLIP_MAX = 5.0
EPSILON = 1e-8
DEFAULT_CLASSES = ["Normal", "AF", "Takikardia", "Bradikardia"]
DEFAULT_THRESHOLD = 0.5

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

LOGGING_ENABLED = True
LOGGING_LEVEL = "INFO"
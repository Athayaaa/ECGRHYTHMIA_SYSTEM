#!/usr/bin/env bash
# Build a reproducible AI virtual environment and verify LiteRT before service use.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SCRIPT_DIR/venv_ai"
PYTHON_BIN="$VENV_DIR/bin/python"
REQUIREMENTS="$REPO_DIR/third_party/ecg-edge-ai-engine/requirements.txt"
ENGINE_DIR="$REPO_DIR/third_party/ecg-edge-ai-engine"
MODEL_PATH="$ENGINE_DIR/models/model.tflite"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 tidak ditemukan." >&2
    exit 1
fi

if [[ ! -f "$MODEL_PATH" ]]; then
    echo "Model TFLite tidak ditemukan: $MODEL_PATH" >&2
    exit 1
fi

python3 -m venv "$VENV_DIR"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install --no-cache-dir --force-reinstall -r "$REQUIREMENTS"
"$PYTHON_BIN" -m pip install --no-deps -e "$ENGINE_DIR"

MODEL_PATH="$MODEL_PATH" "$PYTHON_BIN" - <<'PY'
import os

import numpy
import scipy
from ai_edge_litert.interpreter import Interpreter
from ecg_edge_ai_engine.engine import ECGEngine

model_path = os.environ["MODEL_PATH"]
interpreter = Interpreter(model_path=model_path)
interpreter.allocate_tensors()
result = ECGEngine().predict(numpy.zeros((2500, 3), dtype=numpy.float32))
if not isinstance(result, dict):
    raise RuntimeError("ECG engine did not return a prediction dictionary")

print(f"NumPy: {numpy.__version__}")
print(f"SciPy: {scipy.__version__}")
print("LiteRT, SciPy, dan inferensi model: OK")
PY

"$PYTHON_BIN" -m pip check
echo "Environment AI siap: $VENV_DIR"

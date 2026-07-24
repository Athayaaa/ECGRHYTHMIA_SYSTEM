"""
Postprocessing module for the ECG Edge AI Engine.
Converts raw probability vectors into human-readable prediction structures.
"""

from typing import Dict, Any, List
import numpy as np
from .utils import setup_logger

logger = setup_logger(__name__)

def postprocess_prediction(
    probabilities: np.ndarray,
    labels: List[str],
    threshold: float = 0.5,
    model_metadata: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Translates raw model output probabilities into structured predictions.
    
    Args:
        probabilities: 1D numpy array of predicted class probabilities.
        labels: List of string labels mapping to class indexes.
        threshold: Classification confidence threshold. If max probability is below
                   this threshold, the prediction will be categorized as "Unclassified".
        model_metadata: Additional model information dictionary to pass to output.
        
    Returns:
        Dict[str, Any]: Structured output dictionary.
    """
    logger.info("Postprocessing raw probabilities.")
    
    # Validation of inputs
    if len(probabilities) != len(labels):
        logger.warning(
            f"Mismatch between probability count ({len(probabilities)}) and label count ({len(labels)}). "
            "Truncating to match."
        )
        min_len = min(len(probabilities), len(labels))
        probabilities = probabilities[:min_len]
        labels = labels[:min_len]

    # Convert to python native floats and format as percentages
    probabilities_dict = {
        label: round(float(probabilities[i]) * 100.0, 2)
        for i, label in enumerate(labels)
    }

    max_idx = int(np.argmax(probabilities))
    max_prob = float(probabilities[max_idx])
    confidence = round(max_prob * 100.0, 2)

    # Classify based on threshold
    if max_prob >= threshold:
        prediction = labels[max_idx]
    else:
        logger.info(
            f"Highest class probability ({max_prob:.4f}) is below threshold ({threshold:.4f}). "
            "Flagging prediction as 'Unclassified'."
        )
        prediction = "Unclassified"

    result = {
        "prediction": prediction,
        "confidence": confidence,
        "probabilities": probabilities_dict,
        "metadata": model_metadata if model_metadata is not None else {}
    }

    logger.info(f"Prediction result: {prediction} ({confidence}%)")
    return result

"""
Inference module for the ECG Edge AI Engine.
Prepares tensors, runs the TFLite interpreter, and returns raw probability vectors.
"""

import numpy as np
from .utils import InferenceError, setup_logger

logger = setup_logger(__name__)

def run_inference(interpreter, signal: np.ndarray) -> np.ndarray:
    """
    Prepares the input tensor, runs the TFLite interpreter, and returns the raw output probability vector.
    
    Args:
        interpreter: The loaded TFLite interpreter.
        signal: Preprocessed ECG signal of shape (samples, channels), typically (2500, 3).
        
    Returns:
        np.ndarray: Raw class probabilities of shape (num_classes,).
    """
    logger.info("Preparing tensor for inference.")
    
    try:
        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()
        
        # Get expected input properties
        input_shape = input_details[0]['shape']
        input_type = input_details[0]['dtype']
        input_index = input_details[0]['index']
        
        # Format input signal: add batch dimension if model expects 3D/4D
        # For instance, if model expects (1, 2500, 3) and input is (2500, 3)
        if len(input_shape) == 3:
            if signal.ndim == 2:
                signal_tensor = np.expand_dims(signal, axis=0)
            else:
                signal_tensor = signal
        elif len(input_shape) == 4:
            # Some models might expect (1, 2500, 3, 1) or similar
            if signal.ndim == 2:
                signal_tensor = np.expand_dims(np.expand_dims(signal, axis=0), axis=-1)
            elif signal.ndim == 3:
                signal_tensor = np.expand_dims(signal, axis=-1)
            else:
                signal_tensor = signal
        else:
            signal_tensor = signal

        # Cast to model's expected type (usually float32)
        signal_tensor = signal_tensor.astype(input_type)
        
        # Verify that the shapes match
        if not np.array_equal(signal_tensor.shape, input_shape):
            logger.warning(
                f"Prepared tensor shape {signal_tensor.shape} does not match model input shape {input_shape}. "
                "Attempting to set tensor anyway."
            )
            
        interpreter.set_tensor(input_index, signal_tensor)
        
        logger.info("Invoking TFLite interpreter.")
        interpreter.invoke()
        
        # Get raw probability output
        output_index = output_details[0]['index']
        output_tensor = interpreter.get_tensor(output_index)
        
        # Squeeze batch dimension to return a 1D probability array
        probabilities = np.squeeze(output_tensor)
        
        # Handle cases where probabilities might be a 0-d array or single value
        if probabilities.ndim == 0:
            probabilities = np.array([probabilities.item()])
            
        logger.info("Inference completed successfully.")
        return probabilities
        
    except Exception as e:
        logger.error(f"Inference execution failed: {e}")
        raise InferenceError(f"Failed to execute TFLite model: {e}") from e

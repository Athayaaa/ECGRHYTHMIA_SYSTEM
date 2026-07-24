"""
example_predict.py

Demonstration of using the ECGEngine library to perform inference
on raw numpy arrays (3-lead, 250 Hz).
"""

import numpy as np
from ecg_edge_ai_engine import ECGEngine

def main():
    print("=== Initialize ECGEngine ===")
    try:
        engine = ECGEngine()
    except Exception as e:
        print(f"Failed to start engine: {e}")
        return

    print("\n=== Generate Synthetic ECG Signal ===")
    fs = 250.0  # 250 Hz
    duration = 10.0  # 10 seconds
    n_samples = int(fs * duration)
    
    # Create simple waveforms for Lead I, II, and calculate Lead III
    t = np.linspace(0, duration, n_samples, endpoint=False)
    lead_i = np.sin(2 * np.pi * 1.2 * t) + 0.05 * np.random.normal(size=n_samples)
    lead_ii = np.sin(2 * np.pi * 1.2 * t + 0.3) + 0.05 * np.random.normal(size=n_samples)
    lead_iii = lead_ii - lead_i
    
    # Stack signals into (samples, 3) shape
    raw_signal = np.stack([lead_i, lead_ii, lead_iii], axis=1)
    print(f"Signal shape: {raw_signal.shape} (N_samples, N_leads)")
    print(f"Sampling frequency: {fs} Hz")

    print("\n=== Run Inference ===")
    try:
        result = engine.predict(raw_signal, sampling_rate=fs)
        
        print("\n=== Inference Result ===")
        print(f"Prediction : {result['prediction']}")
        print(f"Confidence : {result['confidence']}%")
        
        print("\n=== Probabilities ===")
        for label, prob in result['probabilities'].items():
            print(f"  {label}: {prob}%")
            
        print("\n=== Metadata ===")
        for key, val in result['metadata'].items():
            print(f"  {key}: {val}")
            
    except Exception as e:
        print(f"Error during prediction: {e}")

if __name__ == "__main__":
    main()

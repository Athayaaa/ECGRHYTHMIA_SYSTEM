"""
example_csv.py

Demonstration of using the ECGEngine library to perform inference
on raw signals loaded from a CSV file.
"""

import os
import pandas as pd
import numpy as np
from ecg_edge_ai_engine import ECGEngine

def generate_dummy_csv(filepath: str):
    """Generates a dummy raw_ecg.csv file for testing."""
    print(f"Creating a dummy raw ECG CSV file at: {filepath}")
    fs = 250.0
    duration = 10.0
    n_samples = int(fs * duration)
    
    t = np.linspace(0, duration, n_samples, endpoint=False)
    lead_i = np.sin(2 * np.pi * 1.2 * t) + 0.05 * np.random.normal(size=n_samples)
    lead_ii = np.sin(2 * np.pi * 1.2 * t + 0.3) + 0.05 * np.random.normal(size=n_samples)
    lead_iii = lead_ii - lead_i
    
    df = pd.DataFrame({
        "lead_I": lead_i,
        "lead_II": lead_ii,
        "lead_III": lead_iii
    })
    df.to_csv(filepath, index=False)
    print("Dummy CSV file generated.")

def main():
    csv_path = "raw_ecg.csv"
    
    # If file doesn't exist, generate a dummy one
    if not os.path.exists(csv_path):
        generate_dummy_csv(csv_path)
        
    print("\n=== Reading ECG data from CSV ===")
    try:
        # Load CSV using pandas
        df = pd.read_csv(csv_path)
        print(f"Loaded DataFrame shape: {df.shape}")
        print("First 5 rows:")
        print(df.head())
        
        # Take first 3 columns as the 3-lead signals and convert to numpy array
        raw_signal = df.iloc[:, :3].to_numpy()
        print(f"Extracted numpy array of shape: {raw_signal.shape}")
    except Exception as e:
        print(f"Failed to read or parse CSV: {e}")
        return

    print("\n=== Initialize ECGEngine ===")
    try:
        engine = ECGEngine()
    except Exception as e:
        print(f"Failed to start engine: {e}")
        return

    print("\n=== Run Inference ===")
    try:
        result = engine.predict(raw_signal, sampling_rate=250.0)
        
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

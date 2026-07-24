from __future__ import annotations

import csv
import time
from collections import Counter
from pathlib import Path

import numpy as np
from ecg_edge_ai_engine import ECGEngine


DATASETS = {
    "60BPM_0p5mV": Path(
        "../../raspberry_pi/data/replay/60BPM_0p5mV/model_ready"
    ),
    "60BPM_1mV": Path(
        "../../raspberry_pi/data/model_ready_prosim_test"
    ),
    "60BPM_2mV": Path(
        "../../raspberry_pi/data/replay/60BPM_2mV/model_ready"
    ),
    "100BPM_1mV": Path(
        "../../raspberry_pi/data/replay/100BPM_1mV/model_ready"
    ),
    "120BPM_1mV": Path(
        "../../raspberry_pi/data/replay/120BPM_1mV/model_ready"
    ),
}

OUTPUT = Path("batch_inference_results.csv")


def main() -> None:
    engine = ECGEngine()
    rows: list[dict[str, object]] = []

    for dataset_name, directory in DATASETS.items():
        directory = directory.resolve()
        files = sorted(directory.glob("frame_*_input.npy"))

        if not files:
            print(f"{dataset_name}: SKIP, tidak ada file di {directory}")
            continue

        predictions: Counter[str] = Counter()
        latencies: list[float] = []

        for file_path in files:
            frame = np.load(file_path)

            if frame.shape != (2500, 3):
                print(f"{file_path.name}: FAIL shape={frame.shape}")
                continue

            if not np.isfinite(frame).all():
                print(f"{file_path.name}: FAIL NaN/Inf")
                continue

            start = time.perf_counter()
            result = engine.predict(frame, sampling_rate=250.0)
            latency_ms = (time.perf_counter() - start) * 1000.0

            prediction = str(result["prediction"])
            confidence = float(result["confidence"])

            predictions[prediction] += 1
            latencies.append(latency_ms)

            rows.append(
                {
                    "dataset": dataset_name,
                    "frame": file_path.name,
                    "prediction": prediction,
                    "confidence_percent": confidence,
                    "latency_ms": latency_ms,
                    "max_abs_mV": float(np.max(np.abs(frame))),
                }
            )

        values = np.asarray(latencies, dtype=np.float64)

        print(f"\n{dataset_name}")
        print(f"  processed : {len(latencies)}")
        print(f"  predictions: {dict(predictions)}")

        if len(values):
            print(f"  mean_ms   : {values.mean():.3f}")
            print(f"  p95_ms    : {np.percentile(values, 95):.3f}")

    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "dataset",
            "frame",
            "prediction",
            "confidence_percent",
            "latency_ms",
            "max_abs_mV",
        ]

        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved: {OUTPUT.resolve()}")


if __name__ == "__main__":
    main()

# Phase 3H Validation Report

- Calibration config: `/home/athaya/Projects/ECGRHYTHMIA_SYSTEM/raspberry_pi/calibration_config.yaml`
- Canonical frame: `(2500, 3)` float32, 250 Hz, 10 seconds, mV
- Acceptance: no NaN/Inf, no saturation, Lead III error <= 1e-5 mV, BPM error <= 2, amplitude error <= 5%, max abs <= 10 mV

| Dataset | Raw | Converted | Model-ready | Failed | Amp mV | Amp Err % | BPM | BPM Err | Max abs mV | Lead III err mV | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 60BPM_0p5mV | 14 | 14 | 14 | 0 | 0.501705 | 0.341025 | 60 | 0 | 0.602923 | 0 | PASS |
| 60BPM_1mV | 10 | 10 | 10 | 0 | 0.977222 | 2.27784 | 60 | 0 | 1.03749 | 0 | PASS |
| 60BPM_2mV | 25 | 25 | 25 | 0 | 1.98461 | 0.769658 | 60 | 0 | 2.09415 | 0 | PASS |
| 100BPM_1mV | 16 | 16 | 16 | 0 | 0.994232 | 0.576753 | 100 | 0 | 1.03352 | 0 | PASS |
| 120BPM_1mV | 18 | 18 | 18 | 0 | 0.995285 | 0.471524 | 120 | 0 | 1.12366 | 0 | PASS |

## Reasons

- 60BPM_0p5mV: PASS
- 60BPM_1mV: PASS
- 60BPM_2mV: PASS
- 100BPM_1mV: PASS
- 120BPM_1mV: PASS

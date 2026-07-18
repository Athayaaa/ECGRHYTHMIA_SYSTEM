# ECGRHYTHMIA Pipeline Handoff

> **LEGACY 640-TO-250 PIPELINE ONLY:** This handoff records the historical
> sample-skipping pipeline and its archived ProSim calibration. The word
> "final" below applies only to that legacy dataset; it is incompatible with
> corrected protocol V2 firmware. For current acquisition, use
> `ADS1293_CORRECTED_PIPELINE.md`, `CALIBRATION_MIGRATION.md`, and
> `calibration_config_ads1293_266p667_nominal.yaml` (preliminary).

## Calibration

- Calibration file: `calibration_config.yaml`
- Calibration version: Phase 3F final multipoint ProSim calibration
- Calibration method: `multipoint_rpeak`
- Created at: `2026-07-15T05:53:36.803325Z`
- Scale: `0.00011691351316834283 mV/count`
- Offsets:
  - Lead I: `6399663.0`
  - Lead II: `6399426.0`
  - Lead III: `Lead II offset - Lead I offset`

## Canonical Input

- Shape: `(2500, 3)`
- Dtype: `float32`
- Sample rate: `250 Hz`
- Duration: `10 seconds`
- Unit: `mV`
- Channel order: `Lead I`, `Lead II`, `Lead III`
- Lead III rule: `Lead III = Lead II - Lead I`
- Model-ready file: `frame_xxxxxx_input.npy`
- Model-ready metadata: `frame_xxxxxx_input.json`

## Directories

- Raw recorded datasets: `../data_kalibrasi/<dataset>/`
- Calibrated replay output: `data/replay/<dataset>/calibrated/`
- Model-ready replay output: `data/replay/<dataset>/model_ready/`
- Rejected replay output: `data/replay/<dataset>/rejected/`
- Validation reports: `data/validation_reports/`

Raw datasets remain immutable input records. Replay output is generated data and
can be regenerated from raw frames plus `calibration_config.yaml`.

## Rejected Frames

Frames with `signal_quality.status == FAIL` are not exported to the final
model-ready directory. The replay pipeline writes rejection metadata under:

```text
data/replay/<dataset>/rejected/frame_xxxxxx_rejected.json
```

The rejection metadata records the stage, reason, source files, calibrated files
when available, and the signal quality result.

## Commands

Replay one dataset:

```bash
python3 replay_dataset.py \
  --input-dir ../data_kalibrasi/60BPM_1mV \
  --config calibration_config.yaml \
  --output-dir data/replay/60BPM_1mV
```

Replay with live-like 10 second frame cadence:

```bash
python3 replay_dataset.py \
  --input-dir ../data_kalibrasi/60BPM_1mV \
  --config calibration_config.yaml \
  --output-dir data/replay/60BPM_1mV \
  --realtime
```

Validate all recorded ProSim datasets and write JSON/CSV/Markdown reports:

```bash
python3 validate_all_recorded_datasets.py \
  --config calibration_config.yaml
```

Run Phase 3H regression tests:

```bash
python3 -m unittest test_phase3h_pipeline.py
```

## Known Limitation

The Phase 3H pipeline is validated against recorded ProSim datasets only.
Human/live ECG signal quality has not yet been validated.

# ECGRHYTHMIA Model Input Specification

This document defines the stable calibrated ECG frame interface for future
1D-CNN model integration. No model, inference runtime, or training workflow is
part of this phase.

## Canonical Input

- Shape: `(2500, 3)`
- Dtype: `float32`
- Unit: `mV`
- Sample rate: `250 Hz`
- Duration: `10 seconds`
- Samples per channel: `2500`
- Channel count: `3`
- Channel order:
  - `Lead I`
  - `Lead II`
  - `Lead III`
- Lead III derivation: `Lead III = Lead II - Lead I`
- Memory layout: `samples x channels`

## File Formats

The canonical model input file is NPY:

```text
frame_xxxxxx_input.npy
```

The NPY file stores a contiguous `float32` NumPy array with shape `(2500, 3)`.
Values remain in physical `mV` units. No z-score, min-max normalization,
clipping, filtering, resampling, or baseline removal is applied by default.

CSV and JSON files are supporting formats only. CSV is useful for inspection,
and JSON records provenance, calibration metadata, validation results, and any
explicit preprocessing steps. The future model interface should consume the NPY
file as the stable input.

## Provenance

Model-ready frames are derived from calibrated mV frames:

```text
data/calibrated/frame_xxxxxx_mv.npy
data/calibrated/frame_xxxxxx_mv.json
```

Raw ADC frames remain the preserved acquisition record. Model-ready frames must
never replace or modify raw ADC exports or calibrated mV source files.

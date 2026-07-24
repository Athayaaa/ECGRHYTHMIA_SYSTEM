"""
Preprocessing module for the ECG Edge AI Engine.
Provides functions for filtering, denoising, normalization, and resizing of ECG signals.
"""

import numpy as np
from scipy.signal import (
    resample_poly,
    butter,
    filtfilt,
    medfilt
)
import pywt

from . import config as cfg
from .utils import InvalidSignalShape, setup_logger

logger = setup_logger(__name__)

def sanitize_signal(signal: np.ndarray) -> np.ndarray:
    """
    Cleans NaN / Inf values before digital signal processing.
    """
    signal = np.asarray(signal, dtype=np.float32)
    return np.nan_to_num(
        signal,
        nan=0.0,
        posinf=0.0,
        neginf=0.0
    )

def validate_signal(signal: np.ndarray) -> np.ndarray:
    """
    Ensures signal is 2D with shape [timesteps, 3] representing 3-lead ECG.
    Raises InvalidSignalShape if requirements are not met.
    """
    if signal.ndim != 2:
        raise InvalidSignalShape(
            f"Signal must be 2D [timesteps, channels], got shape={signal.shape}"
        )
    if signal.shape[1] != 3:
        raise InvalidSignalShape(
            f"Signal must be 3-lead ECG, got shape={signal.shape}"
        )
    if signal.shape[0] < 32:
        raise InvalidSignalShape(
            f"Signal is too short to process, got length={signal.shape[0]}"
        )
    return signal

def ensure_length(signal: np.ndarray, target_len: int) -> np.ndarray:
    """
    Ensures the signal matches the model input length.
    Uses center cropping if too long, or zero padding (at the end) if too short.
    """
    signal = sanitize_signal(signal)
    current_len = signal.shape[0]

    if current_len == target_len:
        return signal
    elif current_len > target_len:
        # Center cropping
        start = (current_len - target_len) // 2
        return signal[start:start + target_len, :]
    else:
        # Zero padding
        pad_len = target_len - current_len
        return np.pad(
            signal,
            ((0, pad_len), (0, 0)),
            mode='constant',
            constant_values=0.0
        )

def apply_poly_resample(signal: np.ndarray, src_fs: float, target_fs: float) -> np.ndarray:
    """
    Polyphase FIR Resampling for signal resampling.
    """
    signal = sanitize_signal(signal)
    if src_fs == target_fs:
        return signal

    gcd = np.gcd(int(src_fs), int(target_fs))
    up = int(target_fs // gcd)
    down = int(src_fs // gcd)

    resampled = resample_poly(
        signal,
        up,
        down,
        axis=0
    )
    return sanitize_signal(resampled)

def wavelet_denoising(
    data: np.ndarray,
    wavelet: str = None,
    level: int = None
) -> np.ndarray:
    """
    Adaptive Wavelet Denoising per channel to suppress high-frequency noise.
    """
    data = sanitize_signal(data)
    
    # Resolve parameters from config
    wavelet = wavelet if wavelet is not None else cfg.DEFAULT_WAVELET
    level = level if level is not None else cfg.DEFAULT_WAVELET_LEVEL

    out = np.zeros_like(data)
    for i in range(data.shape[1]):
        max_lvl = pywt.dwt_max_level(
            data.shape[0],
            pywt.Wavelet(wavelet).dec_len
        )
        safe_level = min(level, max_lvl)
        
        coeffs = pywt.wavedec(
            data[:, i],
            wavelet,
            level=safe_level
        )
        threshold = np.std(coeffs[-1]) / 2.0
        coeffs[1:] = [
            pywt.threshold(
                c,
                value=threshold,
                mode='soft'
            )
            for c in coeffs[1:]
        ]
        reconstructed = pywt.waverec(
            coeffs,
            wavelet
        )
        out[:, i] = reconstructed[:data.shape[0]]

    return sanitize_signal(out)

def median_baseline(
    data: np.ndarray,
    kernel_size: int = None
) -> np.ndarray:
    """
    Removes baseline wander using a median filter.
    """
    data = sanitize_signal(data)
    kernel_size = kernel_size if kernel_size is not None else cfg.DEFAULT_MEDIAN_KERNEL

    # Median kernel size must be odd
    if kernel_size % 2 == 0:
        kernel_size += 1

    out = np.zeros_like(data)
    for i in range(data.shape[1]):
        baseline = medfilt(
            data[:, i],
            kernel_size=kernel_size
        )
        out[:, i] = data[:, i] - baseline

    return sanitize_signal(out)

def bandpass_filter(
    signal: np.ndarray,
    fs: float,
    lowcut: float = None,
    highcut: float = None,
    order: int = None
) -> np.ndarray:
    """
    Applies Butterworth Bandpass filter to extract clean ECG band.
    """
    signal = sanitize_signal(signal)
    
    lowcut = lowcut if lowcut is not None else cfg.DEFAULT_LOWCUT
    highcut = highcut if highcut is not None else cfg.DEFAULT_HIGHCUT
    order = order if order is not None else cfg.DEFAULT_FILTER_ORDER

    nyq = 0.5 * fs
    low = max(lowcut / nyq, 0.001)
    high = min(highcut / nyq, 0.99)

    if low >= high:
        return signal

    try:
        b, a = butter(
            order,
            [low, high],
            btype='band'
        )
        filtered = filtfilt(
            b,
            a,
            signal,
            axis=0
        )
        return sanitize_signal(filtered)
    except Exception as e:
        logger.warning(f"Error applying bandpass filter: {e}")
        return signal

def zscore_normalization(
    data: np.ndarray,
    epsilon: float = None,
    clip_min: float = None,
    clip_max: float = None
) -> np.ndarray:
    """
    Z-score normalizes the signal per channel, then clips outliers.
    """
    data = sanitize_signal(data)
    
    epsilon = epsilon if epsilon is not None else cfg.EPSILON
    clip_min = clip_min if clip_min is not None else cfg.DEFAULT_CLIP_MIN
    clip_max = clip_max if clip_max is not None else cfg.DEFAULT_CLIP_MAX

    mean = np.mean(data, axis=0)
    std = np.std(data, axis=0)
    
    norm = (data - mean) / (std + epsilon)
    norm = np.clip(norm, clip_min, clip_max)
    
    return sanitize_signal(norm)

def advanced_cleaning_pipeline(
    raw_signal: np.ndarray,
    src_fs: float,
    target_fs: float = None,
    wavelet: str = None,
    level: int = None,
    median_kernel: int = None,
    lowcut: float = None,
    highcut: float = None,
    filter_order: int = None,
    clip_min: float = None,
    clip_max: float = None,
    epsilon: float = None
) -> np.ndarray:
    """
    Executes the modular ECG preprocessing pipeline in sequence:
    sanitize -> validate -> wavelet -> median baseline -> bandpass -> (resample if needed) -> zscore.
    """
    # 1. Sanitize
    x = sanitize_signal(raw_signal)
    
    # 2. Validate
    x = validate_signal(x)
    
    # Resolve target_fs
    target_fs = target_fs if target_fs is not None else cfg.TARGET_FS

    # If upsampling is needed, resample early before wavelet/filtering
    if src_fs < target_fs:
        x = apply_poly_resample(x, src_fs, target_fs)
        current_fs = target_fs
    else:
        current_fs = src_fs

    # 3. Wavelet Denoising
    x = wavelet_denoising(x, wavelet=wavelet, level=level)

    # 4. Median Baseline Correction
    x = median_baseline(x, kernel_size=median_kernel)

    # 5. Bandpass Filter
    x = bandpass_filter(
        x,
        fs=current_fs,
        lowcut=lowcut,
        highcut=highcut,
        order=filter_order
    )

    # If downsampling is needed, resample after filtering (acts as anti-aliasing)
    if src_fs > target_fs:
        x = apply_poly_resample(x, src_fs, target_fs)

    # 6. Z-score normalization & clipping
    x = zscore_normalization(
        x,
        epsilon=epsilon,
        clip_min=clip_min,
        clip_max=clip_max
    )

    return x
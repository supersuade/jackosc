"""Magnitude spectrum, band sums, and peak picking (numpy rfft).

Runs in analysis workers; numpy's FFT (pocketfft) releases the GIL, so
the CPU-heavy work here does not contend with the JACK realtime thread.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "hz_to_bin",
    "bin_to_hz",
    "magnitude_spectrum",
    "band_power",
    "band_sum",
    "parabolic_peak",
]


def hz_to_bin(hz: float, window: int, sample_rate: float) -> float:
    return hz * window / sample_rate


def bin_to_hz(bin_: float, window: int, sample_rate: float) -> float:
    return bin_ * sample_rate / window


def magnitude_spectrum(samples: np.ndarray, window: np.ndarray | None = None) -> np.ndarray:
    """|rfft| magnitude, float32, length window//2+1. Analysis thread only."""
    x = samples * window if window is not None else samples
    return np.abs(np.fft.rfft(x)).astype(np.float32)


def band_power(mag: np.ndarray, i0: int, i1: int) -> float:
    """Sum of squared magnitudes over bins [i0, i1] (energy)."""
    return float(np.sum(mag[i0 : i1 + 1] ** 2))


def band_sum(mag: np.ndarray, i0: int, i1: int) -> float:
    """Sum of magnitudes over bins [i0, i1]."""
    return float(np.sum(mag[i0 : i1 + 1]))


def parabolic_peak(mag: np.ndarray, i0: int, i1: int) -> tuple[float, float]:
    """Sub-bin peak (bin_float, magnitude) over bins [i0, i1]."""
    if i1 < i0:
        return 0.0, 0.0
    seg = mag[i0 : i1 + 1]
    k = int(np.argmax(seg)) + i0
    y1 = float(seg[k - i0 - 1]) if k > i0 else 0.0
    y2 = float(seg[k - i0]) if k <= i1 else 0.0
    y3 = float(seg[k - i0 + 1]) if k < i1 else 0.0
    denom = y1 - 2.0 * y2 + y3
    if denom == 0.0:
        return float(k), y2
    delta = 0.5 * (y1 - y3) / denom
    return float(k) + delta, y2


def energy_flux(mag: np.ndarray, prev: np.ndarray, i0: int, i1: int) -> float:
    """Onset strength: positive log-domain change of band energy.

    Band energy (sum of squared magnitudes) is stable for low tones
    where per-bin magnitudes slosh between adjacent bins window to
    window; the log half-wave difference keeps onsets large (0 → tone
    ≈ 10) while steady-tone wobble stays ≈ 0.2.
    """
    if prev is None:
        return 0.0
    cur = np.log1p(float(np.sum(mag[i0 : i1 + 1] ** 2)))
    old = np.log1p(float(np.sum(prev[i0 : i1 + 1] ** 2)))
    return max(0.0, cur - old)


def spectral_centroid(mag: np.ndarray, i0: int, i1: int) -> float:
    """Frequency-weighted center of mass over bins [i0, i1], as a bin index."""
    seg = mag[i0 : i1 + 1]
    den = float(np.sum(seg))
    if den <= 0.0:
        return 0.0
    k = np.arange(i0, i1 + 1, dtype=np.float64)
    return float(np.sum(k * seg)) / den


def spectral_rolloff(mag: np.ndarray, i0: int, i1: int, percent: float = 85.0) -> float:
    """Bin index below which `percent` of the band energy sits."""
    seg = mag[i0 : i1 + 1]
    total = float(np.sum(seg))
    if total <= 0.0:
        return 0.0
    target = total * percent / 100.0
    acc = 0.0
    for idx, m in enumerate(seg):
        acc += float(m)
        if acc >= target:
            return float(i0 + idx)
    return float(i1)

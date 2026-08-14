"""YIN fundamental-frequency estimation (de Cheveigné & Kawahara, 2002).

FFT-autocorrelation trick for the difference function, cumulative mean
normalized difference, absolute threshold with local-minimum refinement
and parabolic sub-sample interpolation. Accurate for low frequencies
where FFT-bin peak picking fails (46.9 Hz bins at the default window).
"""

from __future__ import annotations

import numpy as np

__all__ = ["yin_pitch"]


def yin_pitch(x, sample_rate: float, fmin: float, fmax: float, threshold: float = 0.1):
    """Estimate the fundamental frequency of 1-D signal `x`.

    Returns f0 in Hz, or None when no pitch is detected (unvoiced or
    aperiodic frames). `x` should span at least ~2 periods of the
    lowest expected frequency (the caller's buffer size enforces this).
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    tau_max = min(n - 1, max(2, int(sample_rate / fmin)))
    tau_min = max(2, min(tau_max, int(sample_rate / fmax)))
    if tau_max < tau_min or np.dot(x, x) < 1e-6:
        return None

    # difference function via autocorrelation:
    #   d(tau) = sum (x[j] - x[j+tau])^2 = E0(tau) + E_tau(tau) - 2 r(tau)
    f = np.fft.rfft(x, 2 * n)
    r = np.fft.irfft(f * np.conj(f))[:n]  # linear autocorrelation
    c = np.cumsum(x * x)
    d = np.empty(tau_max + 1)
    d[0] = 0.0
    for tau in range(1, tau_max + 1):
        e0 = c[n - tau - 1]
        e_tau = c[n - 1] - c[tau - 1]
        d[tau] = e0 + e_tau - 2.0 * r[tau]

    # cumulative mean normalized difference: d'(tau) = d(tau) * tau / sum d(1..tau)
    cmnd = np.empty(tau_max + 1)
    cmnd[0] = 1.0
    total = 0.0
    for tau in range(1, tau_max + 1):
        total += d[tau]
        cmnd[tau] = d[tau] * tau / total if total > 0 else 1.0

    # absolute threshold, then walk to the local minimum
    tau = None
    for t in range(tau_min, tau_max + 1):
        if cmnd[t] < threshold:
            tau = t
            break
    if tau is None:
        return None
    while tau + 1 <= tau_max and cmnd[tau + 1] < cmnd[tau]:
        tau += 1
    # parabolic interpolation for sub-sample accuracy
    if 0 < tau < tau_max:
        denom = cmnd[tau - 1] - 2.0 * cmnd[tau] + cmnd[tau + 1]
        if denom != 0.0:
            tau += 0.5 * (cmnd[tau - 1] - cmnd[tau + 1]) / denom
    if tau <= 0:
        return None
    return sample_rate / tau

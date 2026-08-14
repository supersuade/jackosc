"""Single-bin DFT via the Goertzel algorithm, evaluated per window.

Evaluates the DFT at the exact continuous frequency (no bin
quantization / scalloping loss). Reset per analysis window: the filter
is stateless across windows so overlapping windows never double-count
samples. Thread-affine — owned by one analysis worker.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["Goertzel"]


class Goertzel:
    def __init__(self, freq: float, sample_rate: float):
        if not (0.0 < freq < sample_rate / 2.0):
            raise ValueError("freq must be in (0, Nyquist)")
        self.freq = freq
        self.sample_rate = sample_rate
        self._coef = 2.0 * math.cos(2.0 * math.pi * freq / sample_rate)
        self._s1 = 0.0
        self._s2 = 0.0
        self._n = 0

    def feed(self, samples) -> None:
        coef = self._coef
        s1 = self._s1
        s2 = self._s2
        for x in np.asarray(samples):
            s0 = x + coef * s1 - s2
            s2 = s1
            s1 = s0
        self._s1 = s1
        self._s2 = s2
        self._n += len(samples)

    def amplitude(self) -> float:
        """Current magnitude per sample (≈ 0.5 × sine amplitude); 0 if no data."""
        if self._n == 0:
            return 0.0
        s1, s2, coef = self._s1, self._s2, self._coef
        mag = math.sqrt(max(s1 * s1 + s2 * s2 - coef * s1 * s2, 0.0))
        return mag / self._n

    def reset(self) -> None:
        self._s1 = self._s2 = 0.0
        self._n = 0

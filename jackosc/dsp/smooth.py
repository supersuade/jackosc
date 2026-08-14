"""One-pole smoothers for jitter-free output values."""

from __future__ import annotations

import math

__all__ = ["AttackRelease"]


class AttackRelease:
    """One-pole smoother with separate attack/release time constants.

    ``process(x, step)`` advances by ``step`` samples at the configured
    sample rate, so it can be called once per analysis window. NaN
    passes through without corrupting state (uncalibrated rules stay
    silent and recover cleanly once calibrated).
    """

    def __init__(
        self,
        attack_ms: float = 5.0,
        release_ms: float = 150.0,
        sample_rate: float = 48000.0,
    ):
        self._y = 0.0
        self._a_att = math.exp(-1.0 / (attack_ms * 1e-3 * sample_rate)) if attack_ms > 0 else 0.0
        self._a_rel = math.exp(-1.0 / (release_ms * 1e-3 * sample_rate)) if release_ms > 0 else 0.0

    def process(self, x: float, step: int = 1) -> float:
        if x != x:  # NaN: pass through, do not corrupt state
            return x
        a = self._a_rel if x < self._y else self._a_att
        if step != 1:
            a **= step
        self._y = x + a * (self._y - x)
        if abs(self._y) < 1e-9:
            self._y = 0.0  # silence decays to exactly 0, never denormal garbage
        return self._y

    def reset(self, value: float = 0.0) -> None:
        self._y = value

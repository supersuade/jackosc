"""Frame-to-window accumulation with overlap (worker-owned, thread-affine)."""

from __future__ import annotations

import numpy as np

__all__ = ["WindowAccumulator"]


class WindowAccumulator:
    """Accumulate sample frames into fixed-size windows, advancing `hop`.

    Emits complete windows only; partial frames stay buffered. Each
    emitted window is a fresh float32 array of length `window`.
    """

    def __init__(self, window: int, hop: int):
        if window <= 0 or not (0 < hop <= window):
            raise ValueError("need 0 < hop <= window")
        self.window = window
        self.hop = hop
        self._buf = np.empty(window, dtype=np.float32)
        self._n = 0

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        """Append samples; return the list of complete windows produced."""
        out: list[np.ndarray] = []
        buf = self._buf
        n = self._n
        i = 0
        total = len(samples)
        while i < total:
            take = min(self.window - n, total - i)
            buf[n : n + take] = samples[i : i + take]
            n += take
            i += take
            if n == self.window:
                out.append(buf.copy())
                keep = self.window - self.hop
                buf[:keep] = buf[self.hop :]
                n = keep
        self._n = n
        return out

    def clear(self) -> None:
        self._n = 0

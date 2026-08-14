"""Shared analysis state between audio workers and consumers.

Writers: one analysis worker per channel. Readers: the OSC sender and
the web socket broadcaster. All reads/writes are lock-free under the
GIL: scalar values live in a preallocated float matrix (a single float
store is atomic), spectra are swapped by reference (immutable
snapshots, never torn), and metadata is replaced wholesale on
reconfiguration.

Rule identity is positional: (channel_index, rule_index); string ids
like ``"0:2"`` are exposed for the UI.
"""

from __future__ import annotations

import numpy as np

__all__ = ["ValueStore"]

_NAN = float("nan")
MAX_RULES_PER_CHANNEL = 16


class ValueStore:
    def __init__(self) -> None:
        self._values: np.ndarray = np.zeros((0, 0), dtype=np.float64)
        self._rule_ids: dict[str, str] = {}
        self._spectra: dict[int, np.ndarray] = {}
        self._multi: dict[tuple[int, int], np.ndarray] = {}

    # -- configuration (control plane) --------------------------------

    def reconfigure(self, channels) -> None:
        """Rebuild the value matrix and rule-id table for current channels."""
        n = len(channels)
        self._values = np.full((n, MAX_RULES_PER_CHANNEL), _NAN)
        self._rule_ids = {
            f"{i}:{j}": f"{ch.name}:{j}"
            for i, ch in enumerate(channels)
            for j in range(len(ch.rules))
        }
        self._spectra = {}
        self._multi = {}

    # -- writers (analysis workers) ------------------------------------

    def set_value(self, channel: int, rule: int, value: float) -> None:
        self._values[channel, rule] = value  # GIL-atomic single float store

    def set_multi(self, channel: int, rule: int, arr: np.ndarray) -> None:
        self._multi[(channel, rule)] = arr  # immutable snapshot, ref-swap

    def set_spectrum(self, channel: int, mag: np.ndarray) -> None:
        self._spectra[channel] = mag  # ref swap: readers see old or new, never torn

    # -- readers (OSC sender, web broadcaster) -------------------------

    def value(self, channel: int, rule: int) -> float:
        return float(self._values[channel, rule])

    def multi(self, channel: int, rule: int) -> np.ndarray | None:
        return self._multi.get((channel, rule))

    def spectrum(self, channel: int) -> np.ndarray | None:
        return self._spectra.get(channel)

    def snapshot(self) -> dict:
        """Copy of values + refs to spectra/ids; cheap (small matrix)."""
        return {
            "values": self._values.copy(),
            "rule_ids": dict(self._rule_ids),
            "spectra": dict(self._spectra),
            "multi": dict(self._multi),
        }

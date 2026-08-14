"""Lock-free single-producer/single-consumer ring buffer (float32).

The producer is the JACK realtime thread; the consumer is a per-channel
analysis worker. The write path performs no allocation and takes no
locks: it copies samples into a preallocated buffer, then publishes the
write index as a single int attribute. Under CPython the GIL makes that
store atomic and orders it after the data copies, so the consumer never
observes a torn buffer.

This contract is CPython-specific (GIL). A free-threaded build would
need real atomics here; the class isolates that concern.
"""

from __future__ import annotations

import numpy as np

__all__ = ["RingBuffer"]


class RingBuffer:
    """SPSC ring of float32 samples.

    Producer: :meth:`write` — non-blocking, allocation-free.
    Consumer: :meth:`read_into` (preallocated out) / :meth:`read`.

    When full, the newest samples are dropped and counted in
    ``dropped`` — analysis is latest-wins, never blocking the audio
    thread.
    """

    __slots__ = ("_buf", "_size", "_mask", "_write", "_read", "dropped")

    def __init__(self, capacity: int, dtype=np.float32):
        if capacity <= 0 or capacity & (capacity - 1):
            raise ValueError("capacity must be a positive power of two")
        self._buf = np.empty(capacity, dtype=dtype)
        self._size = capacity
        self._mask = capacity - 1
        self._write = 0  # producer-owned; published after data writes
        self._read = 0  # consumer-owned
        self.dropped = 0  # producer-owned; samples discarded when full

    @property
    def capacity(self) -> int:
        return self._size

    @property
    def readable(self) -> int:
        """Samples available to the consumer."""
        return self._write - self._read

    @property
    def writable(self) -> int:
        return self._size - self.readable

    def write(self, samples) -> int:
        """Copy as many samples as fit; returns the count written.

        Allocation-free: slice assignment into the preallocated buffer.
        """
        n = min(len(samples), self.writable)
        self.dropped += len(samples) - n
        if n <= 0:
            return 0
        buf = self._buf
        start = self._write & self._mask
        end = start + n
        if end <= self._size:
            buf[start:end] = samples[:n]
        else:
            first = self._size - start
            buf[start:] = samples[:first]
            buf[: n - first] = samples[first:n]
        self._write += n  # publish — GIL-atomic, ordered after the copies
        return n

    def read_into(self, out: np.ndarray) -> int:
        """Copy up to len(out) samples into preallocated `out`; returns count."""
        n = min(len(out), self.readable)
        if n <= 0:
            return 0
        buf = self._buf
        start = self._read & self._mask
        end = start + n
        if end <= self._size:
            out[:n] = buf[start:end]
        else:
            first = self._size - start
            out[:first] = buf[start:]
            out[first:n] = buf[: n - first]
        self._read += n
        return n

    def read(self, n: int | None = None) -> np.ndarray:
        """Allocate and return up to n samples (tests / one-off use)."""
        n = self.readable if n is None else min(n, self.readable)
        out = np.empty(n, dtype=self._buf.dtype)
        self.read_into(out)
        return out

    def clear(self) -> None:
        self._read = self._write

import numpy as np

from jackosc.audio.ring import RingBuffer


def test_basic_roundtrip():
    r = RingBuffer(8)
    assert r.write(np.arange(5, dtype=np.float32)) == 5
    assert r.readable == 5
    np.testing.assert_array_equal(r.read(5), np.arange(5, dtype=np.float32))
    assert r.readable == 0


def test_wraparound():
    r = RingBuffer(4)
    assert r.write(np.array([1, 2, 3], dtype=np.float32)) == 3
    r.read(3)
    assert r.write(np.array([4, 5, 6, 7], dtype=np.float32)) == 4
    np.testing.assert_array_equal(r.read(4), np.array([4, 5, 6, 7], dtype=np.float32))


def test_full_drops_newest_and_counts():
    r = RingBuffer(4)
    r.write(np.arange(4, dtype=np.float32))
    assert r.write(np.array([9.0], dtype=np.float32)) == 0
    assert r.dropped == 1
    np.testing.assert_array_equal(r.read(4), np.arange(4, dtype=np.float32))


def test_read_into_preallocated():
    r = RingBuffer(8)
    r.write(np.arange(6, dtype=np.float32))
    out = np.empty(4, dtype=np.float32)
    assert r.read_into(out) == 4
    np.testing.assert_array_equal(out, np.arange(4, dtype=np.float32))
    assert r.read_into(out) == 2
    np.testing.assert_array_equal(out[:2], np.array([4.0, 5.0]))


def test_capacity_must_be_power_of_two():
    try:
        RingBuffer(1000)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_producer_consumer_interleaved():
    """Small interleaved writes/reads must never corrupt or lose data."""
    r = RingBuffer(16)
    written = []
    read_back = []
    seq = 0
    for i in range(500):
        chunk = np.arange(seq, seq + (i % 5) + 1, dtype=np.float32)
        written.append(chunk.copy())
        seq += len(chunk)
        r.write(chunk)
        n = (i * 3) % 7
        read_back.append(r.read(n))
    read_back.append(r.read())
    flat = np.concatenate(read_back)
    expect = np.concatenate(written)
    np.testing.assert_array_equal(flat, expect)

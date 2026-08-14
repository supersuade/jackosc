import numpy as np

from jackosc.config import Channel
from jackosc.dsp.extract import ChannelExtractor
from jackosc.dsp.smooth import AttackRelease
from jackosc.dsp.window import WindowAccumulator
from jackosc.rules import (
    AmplitudeRule,
    Band,
    CentroidRule,
    DominantRule,
    FrequencyMapRule,
    MultibandRule,
    OnsetRule,
    PitchRule,
)

SR = 48000.0
WINDOW = 1024
HOP = 512


def sine(freq, seconds=1.0, sr=SR, amp=1.0):
    n = int(sr * seconds)
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def feed_all(channel, signal):
    """Feed a signal through window accumulator + extractor; returns (ex, acc, finals)."""
    acc = WindowAccumulator(channel.window, channel.hop)
    ex = ChannelExtractor(channel, SR)
    finals = []
    for i in range(0, len(signal), HOP):
        for w in acc.feed(signal[i : i + HOP]):
            for _idx, final, _raw in ex.process_window(w):
                finals.append(final)
    return ex, acc, finals


def test_dominant_frequency_finds_440hz():
    ch = Channel(name="a", rules=[DominantRule(fmin=50, fmax=2000, smoothing=[0, 0])])
    _ex, _acc, finals = feed_all(ch, sine(440.0))
    assert finals
    assert abs(finals[-1] - 440.0) < 5.0


def test_dominant_normalized_in_unit_range():
    ch = Channel(
        name="a",
        rules=[DominantRule(fmin=100, fmax=1000, normalize=True, smoothing=[0, 0])],
    )
    _ex, _acc, finals = feed_all(ch, sine(550.0))
    assert 0.4 < finals[-1] < 0.6


def test_goertzel_responds_to_freq_and_rejects_others():
    ch = Channel(name="a", rules=[AmplitudeRule(freq=440.0, smoothing=[0, 0])])
    _ex, _acc, on_freq = feed_all(ch, sine(440.0))
    ch2 = Channel(name="b", rules=[AmplitudeRule(freq=880.0, smoothing=[0, 0])])
    _ex2, _acc2, off_freq = feed_all(ch2, sine(440.0))
    assert on_freq[-1] > 0.1
    assert off_freq[-1] < on_freq[-1] / 5.0


def test_frequency_map_silent_until_calibrated():
    ch = Channel(
        name="a",
        rules=[FrequencyMapRule(f0=400, f1=500, cal_max=None, smoothing=[0, 0])],
    )
    ex, acc, finals = feed_all(ch, sine(440.0))
    assert np.isnan(finals[-1])
    # live calibration: mutate bounds in place; same extractor picks them up
    rule = ch.rules[0]
    raws = []
    for i in range(0, len(sine(440.0, seconds=0.2)), HOP):
        for w in acc.feed(sine(440.0, seconds=0.2)[i : i + HOP]):
            for _idx, _final, raw in ex.process_window(w):
                raws.append(raw)
    rule.cal_min = 0.0
    rule.cal_max = float(max(raws)) * 2.0
    more = []
    for i in range(0, len(sine(440.0, seconds=0.5)), HOP):
        for w in acc.feed(sine(440.0, seconds=0.5)[i : i + HOP]):
            for _idx, final, _raw in ex.process_window(w):
                more.append(final)
    assert 0.35 < more[-1] < 0.65


def test_attack_release_converges_monotonically():
    sm = AttackRelease(attack_ms=5.0, release_ms=150.0, sample_rate=SR)
    vals = []
    y = 0.0
    for _ in range(200):
        y = sm.process(1.0, step=HOP)
        vals.append(y)
    assert vals[0] < 1.0
    assert all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))
    assert vals[-1] > 0.99


def test_smoother_passes_nan_without_corruption():
    sm = AttackRelease(attack_ms=5.0, release_ms=150.0, sample_rate=SR)
    sm.process(1.0, step=HOP)
    assert np.isnan(sm.process(float("nan"), step=HOP))
    y = sm.process(1.0, step=HOP)
    assert 0.9 < y <= 1.0


def test_smoother_silence_decays_to_exact_zero():
    sm = AttackRelease(attack_ms=5.0, release_ms=150.0, sample_rate=SR)
    sm.process(1.0, step=HOP)
    y = 1.0
    for _ in range(3000):  # minutes of silence at window cadence
        y = sm.process(0.0, step=HOP)
    assert y == 0.0  # no denormal drip


def test_window_accumulator_emits_complete_windows():
    acc = WindowAccumulator(WINDOW, HOP)
    out = acc.feed(np.arange(1500, dtype=np.float32))
    assert len(out) == 1
    assert len(out[0]) == WINDOW
    out2 = acc.feed(np.arange(500, dtype=np.float32))
    assert len(out2) == 1
    np.testing.assert_array_equal(out2[0][: HOP], out[0][HOP:])


# ---- onset rule ------------------------------------------------------


def burst_signal(seconds=2.0, tone_freq=60.0, burst=0.5, amp=0.5, ramp=0.0):
    """Alternating silence/tone segments; each tone start is an onset.

    ``ramp`` > 0 fades the tone in/out (raised cosine) to remove the
    broadband click of a hard step — needed to test band selectivity.
    """
    seg = int(seconds / burst)
    parts = []
    for i in range(seg):
        n = int(burst * SR)
        if i % 2 == 0:
            parts.append(np.zeros(n, dtype=np.float32))
        else:
            t = np.arange(n) / SR
            tone = amp * np.sin(2 * np.pi * tone_freq * t)
            if ramp > 0:
                fade = max(1, int(ramp * SR))
                env = np.ones(n, dtype=np.float32)
                env[:fade] = np.linspace(0.0, 1.0, fade)
                env[-fade:] = np.linspace(1.0, 0.0, fade)
                tone = tone * env
            parts.append(tone.astype(np.float32))
    return np.concatenate(parts)


def test_onset_triggers_on_energy_rise_and_decays():
    ch = Channel(
        name="a",
        rules=[OnsetRule(f0=40, f1=80, threshold=1.0, ratio=1.0, hold_ms=50, decay_ms=40, min_change=0)],
    )
    _ex, _acc, finals = feed_all(ch, burst_signal())
    assert any(f == 1.0 for f in finals)  # pulse fires at tone starts
    i0 = finals.index(1.0)
    tail = finals[i0 + 1 : i0 + 8]
    assert all(tail[k] >= tail[k + 1] for k in range(len(tail) - 1))  # decays monotonically
    assert finals[-1] < 1e-3  # fully decayed between bursts


def test_onset_band_selectivity():
    # 1500 Hz bursts with smooth (ramped) onsets: in-band rule triggers,
    # out-of-band rule stays silent (a hard step would click into every band)
    sig = burst_signal(tone_freq=1500.0, ramp=0.02)
    ch_in = Channel(name="a", rules=[OnsetRule(f0=1000, f1=2000, threshold=1.0, ratio=1.0, hold_ms=50, decay_ms=40)])
    assert any(f == 1.0 for f in feed_all(ch_in, sig)[2])
    ch_out = Channel(name="b", rules=[OnsetRule(f0=40, f1=80, threshold=1.0, ratio=1.0, hold_ms=50, decay_ms=40)])
    assert all(f == 0.0 for f in feed_all(ch_out, sig)[2])


def test_onset_silent_until_calibrated():
    ch = Channel(name="a", rules=[OnsetRule(f0=40, f1=80, threshold=None, decay_ms=40)])
    _ex, _acc, finals = feed_all(ch, burst_signal())
    assert all(np.isnan(f) for f in finals)


def test_onset_hold_suppresses_repeat_triggers():
    # onsets at 0.25 s and 0.75 s (~47 windows apart)
    sig = burst_signal(seconds=1.0, burst=0.25)
    ch_hold = Channel(name="a", rules=[OnsetRule(f0=40, f1=80, threshold=1.0, ratio=1.0, hold_ms=800, decay_ms=40)])
    assert feed_all(ch_hold, sig)[2].count(1.0) == 1
    ch_short = Channel(name="b", rules=[OnsetRule(f0=40, f1=80, threshold=1.0, ratio=1.0, hold_ms=40, decay_ms=40)])
    assert feed_all(ch_short, sig)[2].count(1.0) == 2


# ---- output stage: invert + gate ------------------------------------


def _tone(sec, amp):
    n = int(sec * SR)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


def _map_rule(**kw):
    return FrequencyMapRule(f0=400, f1=500, smoothing=[0, 0], **kw)


def test_gate_hysteresis_and_reopen():
    """Gate: opens above gate_on, stays open between off and on, closes
    below gate_off, reopens. Closed output is exactly 0."""
    loud, medium, quiet = _tone(0.4, 0.5), _tone(0.4, 0.316), _tone(0.4, 0.03)
    probe = Channel(name="p", window=1024, hop=512, rules=[_map_rule()])
    _ex, _acc, _f = feed_all(probe, loud)
    raw_loud = _ex.raw_value(0)  # x = raw / (2*raw_loud) ≈ 0.5 for loud

    ch = Channel(
        name="a",
        window=1024,
        hop=512,
        rules=[_map_rule(cal_min=0.0, cal_max=2.0 * raw_loud, gate_on=0.3, gate_off=0.1)],
    )
    _ex, _acc, finals = feed_all(ch, np.concatenate([loud, medium, quiet, loud]))
    W = int(0.4 * SR / HOP)  # windows per segment (≈37)
    seg_loud1 = finals[2 : W - 2]
    seg_med = finals[W + 2 : 2 * W - 2]
    seg_quiet = finals[2 * W + 2 : 3 * W - 2]
    seg_loud2 = finals[3 * W + 2 : 4 * W - 2]
    assert all(abs(v - 0.5) < 0.05 for v in seg_loud1)  # open, ≈0.5
    assert all(abs(v - 0.2) < 0.05 for v in seg_med)  # hysteresis: stays open in the gap
    assert all(v == 0.0 for v in seg_quiet)  # closed → exactly 0
    assert all(abs(v - 0.5) < 0.05 for v in seg_loud2)  # reopens


def test_invert_flips_open_value_but_not_gated_zero():
    loud, quiet = _tone(0.4, 0.5), _tone(0.4, 0.03)
    probe = Channel(name="p", window=1024, hop=512, rules=[_map_rule()])
    _ex, _acc, _f = feed_all(probe, loud)
    raw_loud = _ex.raw_value(0)

    ch = Channel(
        name="a",
        window=1024,
        hop=512,
        rules=[_map_rule(cal_min=0.0, cal_max=4.0 * raw_loud, invert=True, gate_on=0.2, gate_off=0.05)],
    )
    _ex, _acc, finals = feed_all(ch, np.concatenate([loud, quiet]))
    W = int(0.4 * SR / HOP)
    seg_loud = finals[2 : W - 2]
    seg_quiet = finals[W + 2 : 2 * W - 2]
    assert all(abs(v - 0.75) < 0.05 for v in seg_loud)  # 1 - 0.25
    assert all(v == 0.0 for v in seg_quiet)  # closed stays 0, not 1 - 0


# ---- spectral centroid / rolloff -------------------------------------


def test_centroid_tracks_brightness():
    # low tone → low centroid, high tone → high centroid; invert skipped on Hz
    ch = Channel(
        name="a",
        rules=[CentroidRule(fmin=20, fmax=20000, normalize=False, invert=True, smoothing=[0, 0])],
    )
    _ex, _acc, low = feed_all(ch, sine(100.0))
    _ex, _acc, high = feed_all(ch, sine(5000.0))
    assert abs(low[-1] - 100.0) < 15
    assert abs(high[-1] - 5000.0) < 150
    assert high[-1] > low[-1] * 10


def test_centroid_normalized_in_range():
    ch = Channel(
        name="a",
        rules=[CentroidRule(fmin=100, fmax=1000, normalize=True, smoothing=[0, 0])],
    )
    _ex, _acc, finals = feed_all(ch, sine(550.0))
    assert 0.4 < finals[-1] < 0.6  # (550-100)/900 = 0.5


def test_rolloff_at_percent():
    ch = Channel(
        name="a",
        rules=[CentroidRule(method="rolloff", percent=85.0, fmin=20, fmax=20000, normalize=False, smoothing=[0, 0])],
    )
    _ex, _acc, finals = feed_all(ch, sine(440.0))
    assert abs(finals[-1] - 440.0) < 100  # 85% of a single tone's energy sits at its bin


# ---- pitch (YIN) -----------------------------------------------------


def test_pitch_tracks_low_and_high_frequencies():
    ch = Channel(
        name="a",
        rules=[PitchRule(fmin=30, fmax=2000, normalize=False, smoothing=[0, 0])],
    )
    _ex, _acc, low = feed_all(ch, sine(60.0, seconds=0.5))
    _ex, _acc, high = feed_all(ch, sine(440.0, seconds=0.5))
    assert any(np.isfinite(v) for v in low)  # buffer fills, then pitches
    low_pitched = [v for v in low if np.isfinite(v)]
    high_pitched = [v for v in high if np.isfinite(v)]
    assert abs(low_pitched[-1] - 60.0) < 1.0
    assert abs(high_pitched[-1] - 440.0) < 1.0


def test_pitch_normalized_in_range():
    ch = Channel(
        name="a",
        rules=[PitchRule(fmin=100, fmax=1000, normalize=True, smoothing=[0, 0])],
    )
    _ex, _acc, finals = feed_all(ch, sine(550.0, seconds=0.5))
    pitched = [v for v in finals if np.isfinite(v)]
    assert 0.4 < pitched[-1] < 0.6  # (550-100)/900 = 0.5


def test_pitch_silent_on_noise():
    rng = np.random.default_rng(42)
    noise = (rng.standard_normal(24000) * 0.1).astype(np.float32)
    ch = Channel(
        name="a",
        rules=[PitchRule(fmin=40, fmax=2000, normalize=False, smoothing=[0, 0])],
    )
    _ex, _acc, finals = feed_all(ch, noise)
    assert all(not np.isfinite(v) for v in finals)  # aperiodic → NaN (silent)


def test_pitch_nan_while_buffer_fills():
    ch = Channel(
        name="a",
        rules=[PitchRule(fmin=40, fmax=2000, normalize=False, smoothing=[0, 0])],
    )
    _ex, _acc, finals = feed_all(ch, sine(60.0, seconds=0.5))
    assert not np.isfinite(finals[0])  # buffer not full yet
    assert any(np.isfinite(v) for v in finals)


# ---- multiband -------------------------------------------------------


def _mb_rule(**kw):
    p0 = Channel(name="p", rules=[FrequencyMapRule(f0=40, f1=80, smoothing=[0, 0])])
    _ex, _acc, _ = feed_all(p0, sine(60.0))
    raw0 = _ex.raw_value(0)
    p1 = Channel(name="q", rules=[FrequencyMapRule(f0=1000, f1=2000, smoothing=[0, 0])])
    _ex, _acc, _ = feed_all(p1, sine(1500.0))
    raw1 = _ex.raw_value(0)
    return MultibandRule(
        bands=[
            Band(f0=40, f1=80, cal_min=0.0, cal_max=2.0 * raw0),
            Band(f0=1000, f1=2000, cal_min=0.0, cal_max=2.0 * raw1),
        ],
        smoothing=[0, 0],
        **kw,
    )


def test_multiband_bands_independent():
    ch = Channel(name="a", rules=[_mb_rule()])
    _ex, _acc, _ = feed_all(ch, sine(60.0))
    vals60 = _ex.multi_value(0)
    _ex2, _acc2, _ = feed_all(ch, sine(1500.0))
    vals1500 = _ex2.multi_value(0)
    assert 0.4 < vals60[0] < 0.6 and vals60[1] < 0.05  # bass in band 0 only
    assert vals1500[0] < 0.05 and 0.4 < vals1500[1] < 0.6  # high tone in band 1


def test_multiband_invert_flips_each_band():
    ch = Channel(name="a", rules=[_mb_rule(invert=True)])
    _ex, _acc, _ = feed_all(ch, sine(60.0))
    vals = _ex.multi_value(0)
    assert 0.4 < vals[0] < 0.6  # 1 - 0.5
    assert vals[1] > 0.95  # quiet band → ~1.0


def test_multiband_calibrate_band(monkeypatch):
    """Engine-level: capture loop → per-band calibrate → config apply."""
    import threading
    import time

    from jackosc.audio.ring import RingBuffer
    from jackosc.config import AppConfig
    from jackosc.engine import AnalysisEngine
    from jackosc.state import ValueStore

    ring = RingBuffer(1 << 14)
    wake = threading.Event()

    class FakeJack:
        samplerate = 48000.0
        running = True
        binds = []

        def __init__(self, name):
            pass

        def open(self, specs):
            pass

        def close(self):
            pass

        def ring(self, i):
            return ring

        def wake_event(self, i):
            return wake

    monkeypatch.setattr("jackosc.engine.JackClient", FakeJack)
    cfg = AppConfig(
        channels=[
            Channel(
                name="a",
                window=1024,
                hop=512,
                rules=[MultibandRule(bands=[Band(f0=40, f1=80), Band(f0=1000, f1=2000)])],
            )
        ],
        autosave=False,
    )
    store = ValueStore()
    engine = AnalysisEngine(store)
    engine.apply_config(cfg)
    assert engine.audio_available

    signal = sine(60.0, seconds=2.0)
    idx = 0

    def feeder():
        nonlocal idx
        while idx < len(signal):
            chunk = signal[idx : idx + 4096]
            ring.write(chunk)
            wake.set()
            idx += len(chunk)
            time.sleep(0.005)

    t = threading.Thread(target=feeder, daemon=True)
    t.start()
    try:
        result = engine.calibrate("a", 0, seconds=0.3, band=0)
        assert result["band"] == 0
        assert result["cal_max"] > 0
        b0 = engine.config.channels[0].rules[0].bands[0]
        assert b0.cal_max == result["cal_max"]
        assert b0.cal_min == result["cal_min"]
    finally:
        engine.stop()


# ---- auto-reconnect --------------------------------------------------


def test_auto_reconnect(monkeypatch):
    """Monitor retries _setup_audio when JACK is down, then reconnects
    after the server comes back (and after a mid-run death)."""
    import threading
    import time

    from jackosc.audio.client import AudioUnavailable
    from jackosc.audio.ring import RingBuffer
    from jackosc.config import AppConfig
    from jackosc.engine import AnalysisEngine
    from jackosc.state import ValueStore

    ring = RingBuffer(1 << 14)
    wake = threading.Event()
    opens = {"n": 0}
    clients = []

    class FakeJack:
        samplerate = 48000.0
        blocksize = 1024
        running = True
        xruns = 0
        binds = []

        def __init__(self, name):
            self.name = name

        def open(self, specs):
            opens["n"] += 1
            if opens["n"] == 1:
                raise AudioUnavailable("jack down")
            clients.append(self)

        def close(self):
            self.running = False

        def ring(self, i):
            return ring

        def wake_event(self, i):
            return wake

        def cb_stats(self):
            return {"count": 0, "p50_us": 0.0, "p99_us": 0.0, "max_us": 0.0}

    monkeypatch.setattr("jackosc.engine.JackClient", FakeJack)
    cfg = AppConfig(channels=[Channel(name="a", window=1024, hop=512, rules=[])], autosave=False)
    store = ValueStore()
    engine = AnalysisEngine(store)
    engine._reconnect_interval = 0.05
    engine.apply_config(cfg)

    assert not engine.audio_available  # first open failed
    assert opens["n"] == 1

    time.sleep(0.3)  # monitor retries
    assert engine.audio_available
    assert opens["n"] == 2
    first = clients[0]

    # jack dies mid-run: status is honest immediately, monitor reconnects
    first.running = False
    assert not engine.status()["audio"]
    assert "jack down" not in (engine.status()["audio_error"] or "")
    time.sleep(0.3)
    assert engine.audio_available
    assert opens["n"] == 3
    assert engine._client is not first
    engine.stop()


def test_onset_calibrate_sets_threshold(monkeypatch):
    """Engine-level: capture loop → calibrate → config apply, no JACK needed."""
    import threading
    import time

    from jackosc.audio.ring import RingBuffer
    from jackosc.config import AppConfig
    from jackosc.engine import AnalysisEngine
    from jackosc.state import ValueStore

    ring = RingBuffer(1 << 14)
    wake = threading.Event()

    class FakeJack:
        samplerate = 48000.0
        running = True
        binds = []

        def __init__(self, name):
            pass

        def open(self, specs):
            pass

        def close(self):
            pass

        def ring(self, i):
            return ring

        def wake_event(self, i):
            return wake

    monkeypatch.setattr("jackosc.engine.JackClient", FakeJack)

    cfg = AppConfig(
        channels=[Channel(name="a", window=1024, hop=512, rules=[OnsetRule(f0=40, f1=80)])],
        autosave=False,
    )
    store = ValueStore()
    engine = AnalysisEngine(store)
    engine.apply_config(cfg)
    assert engine.audio_available

    signal = burst_signal(seconds=2.0)
    idx = 0

    def feeder():
        nonlocal idx
        while idx < len(signal):
            chunk = signal[idx : idx + 4096]
            ring.write(chunk)
            wake.set()
            idx += len(chunk)
            time.sleep(0.005)

    t = threading.Thread(target=feeder, daemon=True)
    t.start()
    try:
        result = engine.calibrate("a", 0, seconds=0.3)
        assert "threshold" in result
        assert result["threshold"] > 0.0
        assert engine.config.channels[0].rules[0].threshold == result["threshold"]
    finally:
        engine.stop()

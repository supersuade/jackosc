"""ChannelExtractor: runs one channel's rule set over analysis windows.

Thread-affine: owned by a single analysis worker. Computes one
magnitude spectrum per window (shared by all spectrum rules and cached
for the web UI) plus per-rule state: Goertzel filters, bin caches, and
attack/release smoothers. ``set_rules`` hot-swaps the rule set; state
is rebuilt when the rule list object changes (i.e. on any config apply).
"""

from __future__ import annotations

import math

import numpy as np

from jackosc.dsp.goertzel import Goertzel
from jackosc.dsp.pitch import yin_pitch
from jackosc.dsp.smooth import AttackRelease
from jackosc.dsp.spectrum import (
    band_power,
    band_sum,
    bin_to_hz,
    energy_flux,
    hz_to_bin,
    magnitude_spectrum,
    parabolic_peak,
    spectral_centroid,
    spectral_rolloff,
)
from jackosc.rules import (
    AmplitudeRule,
    CentroidRule,
    DominantRule,
    FrequencyMapRule,
    MultibandRule,
    OnsetRule,
    PitchRule,
)

__all__ = ["ChannelExtractor"]

_NAN = float("nan")


class ChannelExtractor:
    def __init__(self, channel, sample_rate: float):
        self.window = channel.window
        self.hop = channel.hop
        self.sample_rate = sample_rate
        self._win = np.hanning(channel.window).astype(np.float32)
        self._rules: list = []
        self._last_mag: np.ndarray | None = None
        self._prev_mag: np.ndarray | None = None
        self._raw: dict[int, float] = {}
        self._onset: dict[int, dict] = {}
        self.set_rules(channel.rules)

    # -- rules -------------------------------------------------------

    def set_rules(self, rules: list) -> None:
        if rules is self._rules:
            return
        self._rules = rules
        self._goertzel: dict[int, Goertzel] = {}
        self._cache: dict[int, tuple[int, int]] = {}
        self._smooth: dict[int, AttackRelease] = {}
        self._onset: dict[int, dict] = {}
        self._gate: dict[int, dict] = {}
        self._pitch: dict[int, dict] = {}
        self._multi: dict[int, dict] = {}
        self._raw_multi: dict[int, list] = {}
        self._multi_values: dict[int, np.ndarray] = {}
        self._prev_mag = None
        sr = self.sample_rate
        for i, rule in enumerate(rules):
            if isinstance(rule, AmplitudeRule):
                self._goertzel[i] = Goertzel(rule.freq, sr)
                self._smooth[i] = AttackRelease(*rule.smoothing, sr)
            elif isinstance(rule, OnsetRule):
                lo = hz_to_bin(rule.f0, self.window, sr) if rule.f0 is not None else 0.0
                hi = hz_to_bin(rule.f1, self.window, sr) if rule.f1 is not None else self.window / 2
                i0 = max(0, int(math.floor(lo)))
                i1 = min(self.window // 2, int(math.ceil(hi)))
                self._cache[i] = (i0, max(i0, i1))
                self._onset[i] = {"pulse": 0.0, "cool": 0}
            elif isinstance(rule, MultibandRule):
                caches = []
                smooths = []
                for band in rule.bands:
                    lo = hz_to_bin(band.f0, self.window, sr)
                    hi = hz_to_bin(band.f1, self.window, sr)
                    i0 = max(0, int(math.floor(lo)))
                    i1 = min(self.window // 2, int(math.ceil(hi)))
                    caches.append((i0, max(i0, i1)))
                    smooths.append(AttackRelease(*rule.smoothing, sr))
                self._multi[i] = {"caches": caches, "smooths": smooths}
            elif isinstance(rule, PitchRule):
                # YIN needs ~2 periods of fmin: per-rule rolling buffer
                buf_len = min(8192, max(self.window, int(2.0 * sr / rule.fmin) + 8))
                self._pitch[i] = {"buf": np.zeros(buf_len, dtype=np.float32), "n": 0, "win_idx": 0}
                self._smooth[i] = AttackRelease(*rule.smoothing, sr)
            elif isinstance(rule, (DominantRule, FrequencyMapRule, CentroidRule)):
                lo = hz_to_bin(
                    rule.fmin if isinstance(rule, (DominantRule, CentroidRule)) else rule.f0,
                    self.window,
                    sr,
                )
                hi = hz_to_bin(
                    rule.fmax if isinstance(rule, (DominantRule, CentroidRule)) else rule.f1,
                    self.window,
                    sr,
                )
                i0 = max(0, int(math.floor(lo)))
                i1 = min(self.window // 2, int(math.ceil(hi)))
                self._cache[i] = (i0, max(i0, i1))
                self._smooth[i] = AttackRelease(*rule.smoothing, sr)

    # -- processing ---------------------------------------------------

    def process_window(self, window: np.ndarray) -> list[tuple[int, float, float]]:
        """Evaluate all enabled rules; returns [(rule_idx, final, raw)]."""
        out: list[tuple[int, float, float]] = []
        mag = magnitude_spectrum(window, self._win)
        self._last_mag = mag
        for i, rule in enumerate(self._rules):
            if not rule.enabled:
                continue
            if isinstance(rule, AmplitudeRule):
                g = self._goertzel[i]
                g.reset()
                g.feed(window)
                raw = g.amplitude() * rule.gain + rule.offset
                final = self._smooth[i].process(self._curve(raw, rule), step=self.hop)
            elif isinstance(rule, OnsetRule):
                lo, hi = self._cache[i]
                raw = energy_flux(mag, self._prev_mag, lo, hi)
                final = self._onset_value(i, raw, rule)
            elif isinstance(rule, MultibandRule):
                final, raw = self._multiband_values(i, mag, rule)
            elif isinstance(rule, PitchRule):
                final, raw = self._pitch_value(i, window, rule)
            elif isinstance(rule, CentroidRule):
                lo, hi = self._cache[i]
                if rule.method == "rolloff":
                    raw = bin_to_hz(spectral_rolloff(mag, lo, hi, rule.percent), self.window, self.sample_rate)
                else:
                    raw = bin_to_hz(spectral_centroid(mag, lo, hi), self.window, self.sample_rate)
                val = (raw - rule.fmin) / (rule.fmax - rule.fmin) if rule.normalize else raw
                final = self._smooth[i].process(val, step=self.hop)
            elif isinstance(rule, DominantRule):
                lo, hi = self._cache[i]
                peak_bin, _ = parabolic_peak(mag, lo, hi)
                raw = bin_to_hz(peak_bin, self.window, self.sample_rate)
                val = (raw - rule.fmin) / (rule.fmax - rule.fmin) if rule.normalize else raw
                final = self._smooth[i].process(val, step=self.hop)
            else:  # FrequencyMapRule
                lo, hi = self._cache[i]
                raw = band_sum(mag, lo, hi) if rule.method == "sum" else band_power(mag, lo, hi)
                final = self._smooth[i].process(self._map(raw, rule), step=self.hop)
            final = self._output_stage(i, final, rule)
            self._raw[i] = raw
            out.append((i, final, raw))
        self._prev_mag = mag  # flux for the next window
        return out

    def _pitch_value(self, i: int, window: np.ndarray, rule: PitchRule) -> tuple[float, float]:
        """Append the window to the rule's rolling buffer, run YIN when full.

        Returns (final, raw): NaN while the buffer fills or when the
        frame is unvoiced (raw = f0 in Hz).
        """
        st = self._pitch[i]
        buf = st["buf"]
        n = st["n"]
        # windows overlap by (window - hop); only the last `hop` samples are
        # new, so append those to reconstruct the contiguous stream
        if st["win_idx"] == 0:
            chunk = window
            st["win_idx"] = 1
        else:
            chunk = window[-self.hop :]
        wlen = len(chunk)
        total = n + wlen
        if total <= len(buf):
            buf[n:total] = chunk
            n = total
        else:
            drop = total - len(buf)
            buf[: n - drop] = buf[drop:n]
            buf[n - drop :] = chunk
            n = len(buf)
        st["n"] = n
        if n < len(buf):
            return _NAN, _NAN
        f0 = yin_pitch(buf, self.sample_rate, rule.fmin, rule.fmax, rule.threshold)
        if f0 is None:
            return _NAN, _NAN
        val = (f0 - rule.fmin) / (rule.fmax - rule.fmin) if rule.normalize else f0
        return self._smooth[i].process(val, step=self.hop), f0

    def _multiband_values(self, i: int, mag: np.ndarray, rule: MultibandRule) -> tuple[float, float]:
        """Per-band calibrated values; publishes the array for the store."""
        st = self._multi[i]
        vals = np.empty(len(rule.bands), dtype=np.float64)
        raws = []
        for b, band in enumerate(rule.bands):
            lo, hi = st["caches"][b]
            raw = band_power(mag, lo, hi) if band.method == "power" else band_sum(mag, lo, hi)
            raws.append(raw)
            vals[b] = st["smooths"][b].process(self._map(raw, band), step=self.hop)
        if rule.invert:
            vals = 1.0 - vals
        self._multi_values[i] = vals
        self._raw_multi[i] = raws
        return float(vals[0]), float(raws[0])

    def _output_stage(self, i: int, final: float, rule) -> float:
        """Shared post-processing: gate (with hysteresis), then invert.

        The gate decides on the natural value; closed outputs exactly 0
        even when inverted. Not applied to onset pulses or raw-Hz
        dominant-frequency output.
        """
        if final != final:  # NaN (e.g. uncalibrated): stay silent
            return final
        if isinstance(rule, (OnsetRule, MultibandRule)):
            return final
        if isinstance(rule, (DominantRule, CentroidRule, PitchRule)) and not rule.normalize:
            return final
        if rule.gate_on is not None:
            st = self._gate.setdefault(i, {"open": False})
            off = rule.gate_off if rule.gate_off is not None else rule.gate_on / 2.0
            if st["open"]:
                if final < off:
                    st["open"] = False
            elif final >= rule.gate_on:
                st["open"] = True
            if not st["open"]:
                return 0.0
        if rule.invert:
            return 1.0 - final
        return final

    def _onset_value(self, i: int, flux: float, rule: OnsetRule) -> float:
        """Trigger logic + pulse envelope for an onset rule.

        NaN while uncalibrated (threshold None), like frequency_map.
        """
        if rule.threshold is None:
            return _NAN
        st = self._onset[i]
        thr = rule.threshold * rule.ratio
        if st["cool"] > 0:
            st["cool"] -= 1
        if flux > thr and st["cool"] == 0:
            st["pulse"] = 1.0
            st["cool"] = max(1, int(round(rule.hold_ms * self.sample_rate / 1000.0 / self.hop)))
        elif st["pulse"] > 0.0:
            if rule.decay_ms > 0:
                st["pulse"] *= math.exp(-self.hop / (rule.decay_ms / 1000.0 * self.sample_rate))
            else:
                st["pulse"] = 0.0
            if st["pulse"] < 1e-3:
                st["pulse"] = 0.0
        return st["pulse"]

    @staticmethod
    def _curve(x: float, rule) -> float:
        if rule.curve == "log":
            return math.log1p(max(x, 0.0))
        if rule.curve == "pow":
            return x ** rule.curve_pow
        return x

    @staticmethod
    def _map(raw: float, rule: FrequencyMapRule) -> float:
        """Raw band energy → 0..1 via calibration; NaN until calibrated."""
        if rule.cal_max is None:
            return _NAN
        denom = rule.cal_max - rule.cal_min
        if denom <= 0.0:
            return _NAN
        x = (raw - rule.cal_min) / denom
        if rule.curve == "log":
            x = math.log1p(max(x, 0.0) * 9.0) / math.log(10.0)
        elif rule.curve == "pow":
            x = x ** rule.curve_pow
        if rule.clamp:
            x = min(max(x, 0.0), 1.0)
        return float(x)

    # -- accessors ----------------------------------------------------

    def last_spectrum(self) -> np.ndarray | None:
        return self._last_mag

    def raw_value(self, rule_idx: int, band: int | None = None) -> float | None:
        if band is not None:
            raws = self._raw_multi.get(rule_idx)
            return raws[band] if raws and 0 <= band < len(raws) else None
        return self._raw.get(rule_idx)

    def multi_value(self, rule_idx: int) -> np.ndarray | None:
        return self._multi_values.get(rule_idx)

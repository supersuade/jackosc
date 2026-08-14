"""Extraction rule models (pydantic).

A discriminated union on ``type`` validates that each rule carries its
own parameters. The shared ``RuleOutput`` fields drive the OSC output
stage: address, curve, smoothing, and change gating.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "Rule",
    "RuleOutput",
    "AmplitudeRule",
    "DominantRule",
    "FrequencyMapRule",
    "OnsetRule",
    "CentroidRule",
    "PitchRule",
    "MultibandRule",
    "Band",
]


class RuleOutput(BaseModel):
    """Shared output stage: OSC addressing, curve, smoothing, gating."""

    osc_pattern: str = Field(
        default="/osc",
        description="OSC address pattern, e.g. /ch/1/band/0",
    )
    targets: list[str] = Field(
        default_factory=list,
        description="target names; empty = all enabled targets",
    )
    curve: Literal["linear", "log", "pow"] = "linear"
    curve_pow: float = Field(default=2.0, ge=0.1, le=10.0)
    smoothing: list[float] = Field(
        default_factory=lambda: [5.0, 150.0],
        description="[attack_ms, release_ms]; 0 = instant",
    )
    min_change: float = Field(
        default=0.0,
        ge=0.0,
        description="skip sending when |delta| is below this",
    )
    invert: bool = Field(
        default=False,
        description="output 1 - x (flips polarity; gated-off stays 0)",
    )
    gate_on: float | None = Field(
        default=None,
        description="open the gate when value >= this; None = no gate",
    )
    gate_off: float | None = Field(
        default=None,
        description="close the gate when value < this (defaults to gate_on/2)",
    )
    enabled: bool = True

    @field_validator("smoothing")
    @classmethod
    def _smoothing_pair(cls, v: list[float]) -> list[float]:
        if len(v) != 2 or v[0] < 0 or v[1] < 0:
            raise ValueError("smoothing must be [attack_ms, release_ms]")
        return v

    @field_validator("gate_off")
    @classmethod
    def _gate_pair(cls, v: float | None, info) -> float | None:
        on = info.data.get("gate_on")
        if v is not None and on is None:
            raise ValueError("gate_off requires gate_on")
        if v is not None and on is not None and v >= on:
            raise ValueError("gate_off must be < gate_on")
        return v


class AmplitudeRule(RuleOutput):
    """Amplitude at a single frequency via a streaming Goertzel filter."""

    type: Literal["amplitude"] = "amplitude"
    freq: float = Field(gt=0, description="frequency to track, Hz")
    gain: float = Field(default=1.0, ge=0.0)
    offset: float = 0.0


class DominantRule(RuleOutput):
    """Peak-picked dominant frequency over [fmin, fmax], Hz (sub-bin)."""

    type: Literal["dominant_frequency"] = "dominant_frequency"
    fmin: float = Field(default=20.0, ge=0.0)
    fmax: float = Field(default=20000.0, gt=0.0)
    normalize: bool = Field(
        default=False,
        description="emit 0..1 within [fmin, fmax] instead of Hz",
    )
    # Hz values are already jittery; do not add release lag by default.
    smoothing: list[float] = Field(default_factory=lambda: [0.0, 20.0])


class FrequencyMapRule(RuleOutput):
    """Energy in [f0, f1] mapped to 0..1 via calibrated min/max."""

    type: Literal["frequency_map"] = "frequency_map"
    f0: float = Field(gt=0)
    f1: float = Field(gt=0)
    method: Literal["power", "sum"] = Field(
        default="power",
        description="'power' = sum of squared magnitudes, 'sum' = magnitudes",
    )
    cal_min: float = 0.0
    cal_max: float | None = Field(
        default=None,
        description="None until auto-calibrated; output stays NaN (silent) until set",
    )
    clamp: bool = True

    @field_validator("f1")
    @classmethod
    def _ordered(cls, v: float, info) -> float:
        if v <= info.data.get("f0", 0.0):
            raise ValueError("f1 must be > f0")
        return v


class OnsetRule(RuleOutput):
    """Edge-triggered pulse on sudden energy rise (band-energy flux).

    Triggers when the positive log-domain change of band energy exceeds
    ``threshold * ratio`` (threshold auto-calibrated, default p95 of
    flux), then outputs 1.0 with an exponential decay. ``hold_ms``
    suppresses re-triggering during the same transient.

    Note: a hard onset is a broadband click, so band selection reduces
    but cannot eliminate cross-band triggering — calibrate on typical
    material.
    """

    type: Literal["onset"] = "onset"
    f0: float | None = Field(default=None, description="band low Hz; None = full spectrum")
    f1: float | None = Field(default=None, description="band high Hz; None = Nyquist")
    threshold: float | None = Field(
        default=None,
        description="flux trigger level; None until calibrated (rule stays silent)",
    )
    ratio: float = Field(default=1.0, ge=0.1, le=100.0, description="trigger when flux > threshold * ratio")
    hold_ms: float = Field(default=50.0, ge=0.0, description="ignore new onsets for this long")
    decay_ms: float = Field(default=150.0, ge=0.0, description="pulse decay time constant")
    min_change: float = Field(default=0.001, ge=0.0)
    # curve/smoothing do not apply: the pulse decay is the envelope.

    @field_validator("f1")
    @classmethod
    def _ordered(cls, v: float | None, info) -> float | None:
        f0 = info.data.get("f0")
        if v is not None and f0 is not None and v <= f0:
            raise ValueError("f1 must be > f0")
        return v


class CentroidRule(RuleOutput):
    """Spectral centroid (brightness) or rolloff over [fmin, fmax].

    centroid: frequency-weighted center of mass of the spectrum — a
    "brightness" float. rolloff: the frequency below which `percent` of
    the band energy sits (more robust to loud single bins). With
    `normalize`, emits 0..1 within [fmin, fmax] — the natural light
    control.
    """

    type: Literal["centroid"] = "centroid"
    fmin: float = Field(default=20.0, ge=0.0)
    fmax: float = Field(default=20000.0, gt=0.0)
    method: Literal["centroid", "rolloff"] = "centroid"
    percent: float = Field(default=85.0, ge=1.0, le=100.0, description="rolloff energy percentage")
    normalize: bool = Field(
        default=True,
        description="emit 0..1 within [fmin, fmax] instead of Hz",
    )
    # The value is already an average; smoothing mainly tames transients.
    smoothing: list[float] = Field(default_factory=lambda: [0.0, 20.0])


class PitchRule(RuleOutput):
    """Fundamental frequency via YIN autocorrelation.

    Accurate for low frequencies where FFT-bin peaks fail. Keeps its own
    rolling sample buffer (~2 periods of fmin, capped at 8192 samples)
    so a short FFT window can still track bass. Unvoiced frames output
    NaN (silent). Adds buffer latency ≈ 2/fmin seconds.
    """

    type: Literal["pitch"] = "pitch"
    fmin: float = Field(default=40.0, gt=0.0, description="lowest expected pitch, Hz")
    fmax: float = Field(default=2000.0, gt=0.0, description="highest expected pitch, Hz")
    threshold: float = Field(
        default=0.1,
        gt=0.0,
        le=1.0,
        description="YIN aperiodicity threshold; lower = stricter",
    )
    normalize: bool = Field(
        default=False,
        description="emit 0..1 within [fmin, fmax] instead of Hz",
    )
    smoothing: list[float] = Field(default_factory=lambda: [0.0, 20.0])

    @field_validator("fmax")
    @classmethod
    def _ordered(cls, v: float, info) -> float:
        if v <= info.data.get("fmin", 0.0):
            raise ValueError("fmax must be > fmin")
        return v


class Band(BaseModel):
    """One calibrated band of a MultibandRule (mirrors FrequencyMapRule fields)."""

    f0: float = Field(gt=0)
    f1: float = Field(gt=0)
    method: Literal["power", "sum"] = "power"
    cal_min: float = 0.0
    cal_max: float | None = None  # None until calibrated; band silent until set
    clamp: bool = True
    curve: Literal["linear", "log", "pow"] = "linear"
    curve_pow: float = Field(default=2.0, ge=0.1, le=10.0)

    @field_validator("f1")
    @classmethod
    def _ordered(cls, v: float, info) -> float:
        if v <= info.data.get("f0", 0.0):
            raise ValueError("f1 must be > f0")
        return v


class MultibandRule(RuleOutput):
    """Several calibrated bands, sent as ONE OSC message with N floats.

    Each band is an independent band-energy → 0..1 mapping (per-band
    calibration). Smoothing and invert apply per band; the gate is not
    used (each band's calibration already floors it).
    """

    type: Literal["multiband"] = "multiband"
    bands: list[Band] = Field(min_length=1, max_length=8)


Rule = Annotated[
    Union[
        AmplitudeRule,
        DominantRule,
        FrequencyMapRule,
        OnsetRule,
        CentroidRule,
        PitchRule,
        MultibandRule,
    ],
    Field(discriminator="type"),
]

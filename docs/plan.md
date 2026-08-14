# jackosc — roadmap

## Shipped

- **Bridge core**: thin-callback JACK client → lock-free SPSC rings → per-channel
  analysis workers (Goertzel amplitude, dominant frequency, band mapping) →
  ValueStore (GIL-atomic ref swaps) → OSC sender (multi-target fan-out,
  per-target prefix, min-change gating). Hot config swap at window boundaries;
  channel changes rebind JACK ports without touching audio mid-buffer.
- **Web app**: live spectrum + rule meters over WS (~30 fps), REST config API
  with a bearer-token auth seam (open reads, gated writes, `JACKOSC_AUTH_TOKEN`),
  atomic JSON profiles (save/load/delete).
- **Rule editor UI**: form-driven channel/rule/target CRUD, spectrum band-drag
  rule creation, frequency-map auto-calibration, draft/apply model with dirty
  indicator, advanced JSON panel.

## In progress

### 1. Rule-param live preview — shipped

Selecting a rule in the editor highlights its parameters on the live spectrum:

- `frequency_map` — translucent band over [f0, f1], stroked edges.
- `dominant_frequency` — band over [fmin, fmax] plus a marker line at the
  currently detected peak (when not normalized).
- `amplitude` — marker line at the tracked frequency.

Behavior: click a rule box to select (click again / click the canvas to
deselect); the overlay renders at the next WS frame, independent of audio
state (works even with an empty spectrum). Selected rule gets a highlight
border.

Verified in-browser: fill/line geometry for all three rule types, deselect
clears, no server changes required.

## Backlog

### 2. OSC packet inspector — shipped

Live view of what the sender emits, tapped inside `OscSender` (the single
writer, so it shows the exact post-prefix, post-gating output):

- Bounded history (512) + queue; `GET /api/packets`, streaming
  `/ws/packets`, auth-gated `POST /api/packets/test` (immediate send to all
  enabled targets through the normal path).
- UI table: time, target, address, value; substring filter; pause (resume
  catches up from history).

Verified end to end: test packet appears in the UI and on the wire as
correct OSC bytes (`/test` `,f` 0.5 → `2f74657374…3f000000`); filter,
pause, and resume behavior all confirmed in-browser; 33 tests green.

### 3. Real-audio soak on PipeWire — shipped

Tools: `tools/tonegen.py` (JACK sine client), `tools/soak.py` (tone → jackosc
→ UDP sink; adds/removes a temporary `soaksink` target; polls xruns, ring
drops, callback timing; exits 1 on violation).

Callback timing is measured inside the realtime callback (fixed ring,
`perf_counter_ns`, p50/p99/max) and surfaced in the UI status line.

Results on this workstation (PipeWire-JACK, 48 kHz, 1024 frames/period =
21.3 ms budget):

- 90 s soak: **0 xruns, 0 ring drops**, callback p50 57 µs / p99 99 µs /
  max 490 µs (≈0.5 % of budget), OSC at ~56 pkts/s.
- 5 min soak (before harness fix): 0 xruns, 0 drops, p99 148 µs.
- 60 s soak against the user's live instance: 0 xruns, 0 drops, packets
  flowing to both targets.

rt state found: PipeWire's graph loop (`data-loop.0`) runs SCHED_RR prio 20
via rtkit; the jackosc client callback thread is SCHED_OTHER. Zero xruns
anyway at this load. If xruns appear under heavier load: raise
`rt.prio`/`rt.time.soft` in a `pipewire.conf.d` override, or reduce the
buffer period. Acceptance run: `tools/soak.py --seconds 1800`.

## Ideas (not yet scheduled)

Rule types, in rough priority:

1. ~~onset/trigger~~ — **shipped**: edge-triggered pulse on band-energy
   flux (`max(0, log1p(E_t) − log1p(E_{t−1}))`), calibrated threshold (p95),
   hold + exponential decay. Verified live: 60 Hz burst tone → pulses at
   exactly 1.0 s spacing, clean decay. `tools/tonegen.py --burst 0.5` tests it.
2. ~~invert + gate output stages~~ — **shipped**: per-rule `invert` (1−x)
   and a hysteresis gate (`gate_on` opens, `gate_off` closes, default
   half of on; closed output is exactly 0 even when inverted). Hidden for
   onset and non-normalized dominant.
3. ~~spectral centroid / rolloff~~ — **shipped**: `centroid` rule,
   frequency-weighted spectrum center of mass (brightness) or `rolloff`
   (frequency below which `percent`% of energy sits), over [fmin, fmax],
   `normalize` → 0..1 (the lights mode). Verified live: 100 Hz tone →
   0.008, 5 kHz tone → 0.499 against (f−fmin)/(fmax−fmin).
4. ~~pitch (YIN/autocorrelation)~~ — **shipped**: `pitch` rule via YIN with
   FFT-accelerated difference function and per-rule contiguous rolling
   buffer (~2 periods of fmin, ≤ 8192 samples — ~50 ms latency at
   fmin=40). Verified live: 60 Hz tone → 60.000 Hz, 150 Hz → 150.001 Hz
   (sub-100 Hz tracking the FFT peak can't do). Unvoiced frames → NaN.
5. ~~multiband~~ — **shipped**: one rule, N calibrated bands, sent as
   ONE OSC message with N float args (`/bands` `,fff`). Per-band
   calibration (`band` param on the calibrate endpoint), per-band
   smoothing + invert. Verified live: 60 Hz → `[0.87, 0, 0]`,
   1500 Hz → `[0, 0.20, 0]`; wire datagram carries `,fff` + 3 floats.
6. **flux** — superseded by onset's band-energy strength (kept as the onset mechanism).
7. **level** — full-band RMS → calibrated 0–1.

Non-extractor:

- ~~auto-reconnect on JACK restart~~ — **shipped**: `jackosc-reconnect`
  monitor thread retries `_setup_audio` every 2 s when the client is
  missing or dead (tears down straggler workers first); status flips to
  audio-off immediately when the client dies; retry failures log only on
  message change. Engine-tested: fail-then-reconnect and mid-run death.
- ~~one-step config undo~~ — **shipped**: session-scoped undo/redo over
  full-config snapshots (50 deep). Per-field edit sessions = one step
  (blur commits), checkboxes/selects/structural ops/apply/calibrate/
  profile loads all push. Ctrl+Z / Ctrl+Shift+Z or Ctrl+Y + toolbar
  buttons. When the draft is clean (matches the server), undo/redo
  auto-applies — reverting the last applied change server-side in one
  keypress. Limits: session-only; can't see config edits made outside
  the UI (file edits, other tabs).
- ~~OSC bundle mode~~ — **shipped**: per-target `bundle` flag; bundling
  targets receive one atomic `#bundle` datagram per sender cycle with
  every enabled rule's current value (multiband = one multi-arg message
  inside), sent only when a value moved ≥ its `min_change`. Individual
  mode unchanged. Verified on the wire: `#bundle` + IMMEDIATELY timetag
  + both messages in one 56-byte datagram.
- multi-arg OSC per rule (multiband already sends N floats in one message)

### Help doc feature — shipped

In-app Help modal ("?" in the header): rule-type table, the output
pipeline (raw → curve → smoothing → gate → invert → min-change → OSC),
calibration workflow (percentiles + "silent until calibrated"),
window/latency tradeoffs, and settings/JSON-only keys (`version`,
`auth_token`). Closes via button, Esc, or backdrop click.

The topics below are now covered there; keep this section as the source
of truth when editing the modal content.

- Calibration workflow: what Auto-calibrate captures (~3 s), the percentiles
  it sets (p2/p98 for map bounds, p95 for onset threshold), why "calibrate
  on typical material", and the "silent until calibrated" behavior.
- One-pole smoothing: attack/release meaning, per-window application, and
  how it interacts with `min_change` and gating.
- The full output pipeline order: raw → curve → smoothing → gate → invert →
  min-change → OSC (a diagram).
- Rule-type semantics for lighting (amplitude vs dominant vs map vs onset)
  and window/hop latency tradeoffs (46.9 Hz bin width, ~21 ms window @ 48 kHz).
- **JSON-only settings**: `version` and `auth_token` (set via the config
  file or `JACKOSC_AUTH_TOKEN`; never shown in the UI), plus when the raw
  JSON panel is the right tool (bulk edits, pasting profiles).

## Cross-cutting

- Auth: writes gated, reads open by design; the `ConfigAuth` seam is the
  swap point for sessions/OAuth.
- Xruns: callback stays allocation-free; ring drops are counted and shown in
  the UI status line.
- Config: schema-versioned JSON, atomic writes, profiles are full snapshots
  minus `auth_token`.

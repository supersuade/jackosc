"use strict";

// ---------- state ---------------------------------------------------

const $ = (id) => document.getElementById(id);
const cards = new Map(); // channel name -> { card, canvas, values }
let cfg = null; // current config (auto-applied on change)
let applyTimer = null; // debounce timer for text edits
let token = localStorage.getItem("jackosc_token") || "";
let drag = null; // spectrum band drag: { ci, x0, x1 }
let selectedRule = null; // "ci:ri" of the rule highlighted on the spectrum
let live = { status: null, channels: [], spectra: {}, values: [], ruleIds: {}, multi: {} };
const collapsedChannels = new Set(); // channel names hidden in the editor

// undo/redo: snapshots of the full config (JSON strings), session-scoped
const MAX_HISTORY = 50;
let history = [];
let hIndex = -1;

const NUM_FIELDS = new Set([
  "freq", "gain", "offset", "fmin", "fmax", "f0", "f1",
  "window", "hop", "port", "curve_pow", "min_change", "cal_min", "osc_rate",
  "ratio", "hold_ms", "decay_ms", "sample_rate",
]);
const RULE_SHARED = ["type", "osc_pattern", "targets", "curve", "curve_pow", "smoothing", "min_change", "enabled", "invert", "gate_on", "gate_off"];

// ---------- utils ---------------------------------------------------

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function headers(extra = {}) {
  const h = { "Content-Type": "application/json", ...extra };
  if (token) h.Authorization = "Bearer " + token;
  return h;
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: headers(), ...opts });
  let body = null;
  try { body = await r.json(); } catch { /* non-JSON */ }
  if (!r.ok) {
    const detail = body && (body.detail || body.error) ? JSON.stringify(body.detail || body.error) : r.statusText;
    throw new Error(`${r.status}: ${detail}`);
  }
  return body;
}

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let o = obj;
  for (let i = 0; i < parts.length - 1; i++) o = o[parts[i]];
  o[parts[parts.length - 1]] = value;
}

// ---------- undo / redo ----------------------------------------------

function pushHistory() {
  const json = JSON.stringify(cfg);
  if (history.length && history[hIndex] === json) return;
  history = history.slice(0, hIndex + 1); // discard redo tail
  history.push(json);
  if (history.length > MAX_HISTORY) history.shift();
  hIndex = history.length - 1;
  updateUndo();
}

function canUndo() { return hIndex > 0; }
function canRedo() { return hIndex >= 0 && hIndex < history.length - 1; }

function restoreSnapshot(json) {
  clearTimeout(applyTimer); // drop any pending debounced edit
  applyTimer = null;
  cfg = JSON.parse(json);
  renderConfig();
  updateUndo();
}

function undo() {
  if (!canUndo()) return;
  hIndex--;
  restoreSnapshot(history[hIndex]);
  applyConfig({ silent: true }); // everything is applied: sync the server back
}

function redo() {
  if (!canRedo()) return;
  hIndex++;
  restoreSnapshot(history[hIndex]);
  applyConfig({ silent: true });
}

function updateUndo() {
  $("undoBtn").disabled = !canUndo();
  $("redoBtn").disabled = !canRedo();
}

$("undoBtn").addEventListener("click", undo);
$("redoBtn").addEventListener("click", redo);

// ---------- help modal ----------------------------------------------

function closeHelp() { $("helpModal").hidden = true; }

$("helpBtn").addEventListener("click", () => { $("helpModal").hidden = false; });
$("helpClose").addEventListener("click", closeHelp);
$("helpModal").addEventListener("click", (e) => { if (e.target === $("helpModal")) closeHelp(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("helpModal").hidden) closeHelp();
});

document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const k = e.key.toLowerCase();
  if (k === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (k === "y") { e.preventDefault(); redo(); }
});

function defaultRule(type) {
  const base = {
    type, osc_pattern: "/osc", targets: [], curve: "linear", curve_pow: 2,
    smoothing: [5, 150], min_change: 0, enabled: true,
    invert: false, gate_on: null, gate_off: null,
  };
  if (type === "amplitude") return { ...base, freq: 100, gain: 1, offset: 0 };
  if (type === "dominant_frequency") return { ...base, fmin: 20, fmax: 2000, normalize: false, smoothing: [0, 20] };
  if (type === "onset") {
    return { ...base, f0: 40, f1: 80, threshold: null, ratio: 1, hold_ms: 50, decay_ms: 150, min_change: 0.001 };
  }
  if (type === "centroid") {
    return { ...base, fmin: 20, fmax: 20000, method: "centroid", percent: 85, normalize: true, smoothing: [0, 20] };
  }
  if (type === "pitch") {
    return { ...base, fmin: 40, fmax: 2000, threshold: 0.1, normalize: false, smoothing: [0, 20] };
  }
  if (type === "multiband") {
    const band = () => ({ f0: 40, f1: 80, method: "power", cal_min: 0, cal_max: null, clamp: true, curve: "linear", curve_pow: 2 });
    return { ...base, bands: [band(), band(), band()] };
  }
  return { ...base, f0: 40, f1: 80, method: "power", cal_min: 0, cal_max: null, clamp: true };
}

// ---------- config load / apply -------------------------------------

let toastTimer = null;

function showToast(msg, kind = "error") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (kind === "ok" ? " ok" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

// immediate apply (checkboxes, selects, structural ops)
function applyNow() {
  clearTimeout(applyTimer);
  applyTimer = null;
  return applyConfig();
}

// debounced apply for text/number edits (avoids invalid mid-typing states)
function scheduleApply() {
  clearTimeout(applyTimer);
  applyTimer = setTimeout(applyNow, 400);
}

async function loadConfig() {
  try {
    const data = await api("/api/config");
    cfg = data.config;
    clearTimeout(applyTimer);
    applyTimer = null;
    history = [JSON.stringify(cfg)]; // fresh session history seeded with server state
    hIndex = 0;
    renderStatus(data.status, data.auth_enabled);
    renderConfig();
    updateUndo();
    loadProfiles();
  } catch (e) {
    const icon = $("status");
    icon.classList.add("warn");
    icon.classList.remove("ok");
    $("statusTip").textContent = "config load failed: " + e.message;
    icon.title = "config load failed";
  }
}

async function applyConfig(opts = {}) {
  if (!cfg) return false;
  try {
    const data = await api("/api/config", { method: "PUT", body: JSON.stringify(cfg) });
    cfg = data.config;
    if (!opts.silent) pushHistory();
    renderConfig();
    return true;
  } catch (e) {
    showToast("apply failed: " + e.message);
    return false;
  }
}

// raw JSON textarea keeps a manual apply (invalid JSON mid-edit)
$("apply").addEventListener("click", async () => {
  try { cfg = JSON.parse($("cfgJson").value); }
  catch (e) { showToast("invalid JSON: " + e.message); return; }
  await applyNow();
});
$("reload").addEventListener("click", loadConfig);

$("downloadConfig").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `jackosc-config-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
});

$("token").value = token;
$("token").addEventListener("input", (e) => {
  token = e.target.value;
  localStorage.setItem("jackosc_token", token);
});

// ---------- renderers: channels & rules ------------------------------

const RULE_TYPE_DESC = {
  amplitude: "Level of one exact frequency (Goertzel filter)",
  dominant_frequency: "Peak frequency in a range, in Hz (or 0..1)",
  frequency_map: "Band energy mapped to 0..1 by calibration",
  onset: "Pulse on sudden energy rise (beats/transients)",
  centroid: "Brightness (spectral centroid) or rolloff in a range",
  pitch: "Fundamental frequency (YIN autocorrelation)",
  multiband: "Several calibrated bands in one OSC message (N floats)",
};

function renderConfig() {
  renderChannelsEditor();
  renderTargets();
  renderGeneral();
  $("cfgJson").value = JSON.stringify(cfg, null, 2);
  document.querySelectorAll(".rule").forEach((el) =>
    el.classList.toggle("selected", el.dataset.rule === selectedRule),
  );
  markDuplicatePatterns();
  syncLabelTitles();
}

// Red-flag rules whose OSC address pattern is used by more than one rule.
function markDuplicatePatterns() {
  const counts = new Map();
  for (const ch of cfg.channels) for (const r of ch.rules)
    counts.set(r.osc_pattern, (counts.get(r.osc_pattern) || 0) + 1);
  document.querySelectorAll("[data-rule] .pat").forEach((input) => {
    const el = input.closest("[data-rule]");
    const [ci, ri] = el.dataset.rule.split(":").map(Number);
    const rule = cfg.channels[ci] && cfg.channels[ci].rules[ri];
    const n = rule ? counts.get(rule.osc_pattern) || 0 : 0;
    input.classList.toggle("dup", n > 1);
    input.title = n > 1
      ? `osc pattern is NOT unique (${n} rules use it)`
      : "OSC address pattern, e.g. /kick/amp";
  });
}

// Hovering a field's label shows the same tooltip as the control it wraps.
function syncLabelTitles() {
  document.querySelectorAll(".fld, .tg").forEach((label) => {
    if (label.title) return; // explicit label titles win (e.g. target toggles)
    const ctl = label.querySelector("input, select");
    if (ctl && ctl.title) label.title = ctl.title;
  });
}

function renderChannelsEditor() {
  const wrap = $("channelsEditor");
  wrap.innerHTML = "";
  if (!cfg) return;
  cfg.channels.forEach((ch, ci) => wrap.appendChild(channelEditor(ch, ci)));
}

function field(ci, ri, name, label, opts = "") {
  const path = ri == null ? `channels.${ci}.${name}` : `channels.${ci}.rules.${ri}.${name}`;
  const v = ri == null ? cfg.channels[ci][name] : cfg.channels[ci].rules[ri][name];
  return `<label class="fld">${label}<input data-path="${path}" ${opts} value="${esc(v ?? "")}"></label>`;
}

function channelEditor(ch, ci) {
  const rules = ch.rules
    .map((r, ri) => ruleEditor(ch, r, ci, ri))
    .join("");
  const collapsed = collapsedChannels.has(ch.name);
  const d = document.createElement("div");
  d.className = "ch-editor" + (collapsed ? " collapsed" : "");
  d.innerHTML = `
    <div class="ch-head">
      <button class="ch-toggle" data-act="toggle-channel" data-ci="${ci}" title="${collapsed ? "Expand channel" : "Collapse channel"}">${collapsed ? "▸" : "▾"}</button>
      ${field(ci, null, "name", "name", `title="Channel name (JACK port)"`)}
      <button data-act="dup-channel" data-ci="${ci}" title="Duplicate channel">⧉</button>
      <button data-act="del-channel" data-ci="${ci}" class="danger" title="Delete channel">✕</button>
    </div>
    <div class="ch-body">
      ${field(ci, null, "connect_to", "connect", `list="connectOptions" placeholder="manual" title="JACK source to auto-connect: 'auto' = system:capture_N; empty = leave unpatched (use a patchbay)"`)}
      ${field(ci, null, "window", "window", `type="number" step="64" title="FFT window size (samples)"`)}
      ${field(ci, null, "hop", "hop", `type="number" step="1" title="Window advance (samples)"`)}
      <div class="rules">${rules}
        <button data-act="add-rule" data-ci="${ci}" class="mini" title="Add a rule to this channel">+ Rule</button>
      </div>
    </div>`;
  return d;
}

function ruleEditor(ch, r, ci, ri) {
  const p = `channels.${ci}.rules.${ri}`;
  const isNormHz =
    (r.type === "dominant_frequency" || r.type === "centroid" || r.type === "pitch") && r.normalize;
  const showInvert = r.type === "amplitude" || r.type === "frequency_map" || r.type === "multiband" || isNormHz;
  const showGate = r.type === "amplitude" || r.type === "frequency_map" || isNormHz;
  const targets = (cfg.targets || [])
    .map((t) => {
      const on = (r.targets || []).includes(t.name);
      return `<label class="tg" title="Send this rule to this target"><input type="checkbox" data-path="${p}.targets.${t.name}" ${on ? "checked" : ""} title="Send this rule to this target"> ${esc(t.name)}</label>`;
    })
    .join("");
  const calib = (r.type === "frequency_map" || r.type === "onset") ? `
    <div class="calib">
      ${r.type === "frequency_map"
        ? `<label class="fld">cal_min<input data-path="${p}.cal_min" type="number" step="any" value="${esc(r.cal_min)}" title="Low energy bound (raw units)"></label>
           <label class="fld">cal_max<input data-path="${p}.cal_max" type="number" step="any" placeholder="uncalibrated" value="${esc(r.cal_max ?? "")}" title="High energy bound; output silent until set"></label>`
        : `<label class="fld">threshold<input data-path="${p}.threshold" type="number" step="any" placeholder="uncalibrated" value="${esc(r.threshold ?? "")}" title="Flux trigger level; output silent until set"></label>`}
      <button data-act="calibrate" data-ci="${ci}" data-ri="${ri}" class="mini" title="${r.type === "onset" ? "Capture ~3 s of audio and set the trigger threshold" : "Capture ~3 s of audio and set cal_min/cal_max"}">${r.type === "onset" ? "Auto-calibrate threshold" : "Auto-calibrate (3 s)"}</button>
    </div>` : "";
  const output = `
    ${r.type === "onset" ? "" : `
    <label class="fld">curve
      <select data-path="${p}.curve" title="Shaping: linear, log (compresses), pow (accentuates)">
        ${["linear", "log", "pow"].map((c) => `<option value="${c}" ${r.curve === c ? "selected" : ""}>${c}</option>`).join("")}
      </select>
    </label>
    ${r.curve === "pow" ? `<label class="fld">pow<input data-path="${p}.curve_pow" type="number" step="0.1" value="${esc(r.curve_pow)}" title="Exponent for the pow curve"></label>` : ""}
    <label class="fld">attack/rel ms<input data-path="${p}.smoothing.0" type="number" step="1" value="${esc(r.smoothing[0])}" title="Attack (rise) time in ms; 0 = instant"></label>
    <label class="fld">rel<input data-path="${p}.smoothing.1" type="number" step="1" value="${esc(r.smoothing[1])}" title="Release (fall) time in ms; 0 = instant"></label>`}
    <label class="fld">min Δ<input data-path="${p}.min_change" type="number" step="0.001" value="${esc(r.min_change)}" title="Skip sending when the change is smaller than this"></label>`;
  const gate = `
    ${showInvert ? `
    <label class="tg"><input type="checkbox" data-path="${p}.invert" ${r.invert ? "checked" : ""} title="Output 1 - x (flips polarity)"> invert</label>` : ""}
    ${showGate ? `
    <label class="fld">gate on<input data-path="${p}.gate_on" type="number" step="any" placeholder="off" value="${esc(r.gate_on ?? "")}" title="Open the gate when the value >= this"></label>
    <label class="fld">gate off<input data-path="${p}.gate_off" type="number" step="any" placeholder="${r.gate_on != null ? (r.gate_on / 2).toFixed(2) : "half"}" value="${esc(r.gate_off ?? "")}" title="Close the gate when the value < this (default: half of gate on)"></label>` : ""}`;
  const d = document.createElement("div");
  d.className = "rule";
  d.dataset.rule = `${ci}:${ri}`;
  d.innerHTML = `
    <div class="rule-head">
      <select data-path="${p}.type" title="${RULE_TYPE_DESC[r.type] || "Rule type"}">
        ${["amplitude", "dominant_frequency", "frequency_map", "onset", "centroid", "pitch", "multiband"]
          .map((t) => `<option value="${t}" ${r.type === t ? "selected" : ""} title="${RULE_TYPE_DESC[t]}">${t}</option>`)
          .join("")}
      </select>
      <input data-path="${p}.osc_pattern" class="pat" value="${esc(r.osc_pattern)}" title="OSC address pattern, e.g. /kick/amp">
      <label class="tg"><input type="checkbox" data-path="${p}.enabled" ${r.enabled ? "checked" : ""} title="Enable or disable this rule"> on</label>
      <button data-act="dup-rule" data-ci="${ci}" data-ri="${ri}" title="Duplicate rule">⧉</button>
      <button data-act="del-rule" data-ci="${ci}" data-ri="${ri}" class="danger" title="Delete rule">✕</button>
    </div>
    <div class="rule-params">
      <div class="pg"><span class="pg-label">detector</span><div class="pg-body">${ruleParams(r, p)}</div></div>
      <div class="pg"><span class="pg-label">output</span><div class="pg-body">${output}</div></div>
      ${gate.trim() ? `<div class="pg"><span class="pg-label">gate</span><div class="pg-body">${gate}</div></div>` : ""}
    </div>
    ${r.type === "multiband" ? bandsEditor(r, p, ci, ri) : ""}
    <div class="rule-targets">${targets ? `send to: ${targets}` : ""}</div>
    ${calib}
    <div class="rule-out">
      <canvas class="rule-spark" width="48" height="14"></canvas>
      <span class="rule-value">—</span>
    </div>`;
  return d.outerHTML;
}

function bandsEditor(r, p, ci, ri) {
  const bands = r.bands || []; // guard against hand-edited configs
  const rows = bands
    .map((b, bi) => `
      <div class="band">
        <span class="band-idx">${bi}</span>
        <input data-path="${p}.bands.${bi}.f0" type="number" value="${esc(b.f0)}" title="Band low edge (Hz)">
        <input data-path="${p}.bands.${bi}.f1" type="number" value="${esc(b.f1)}" title="Band high edge (Hz)">
        <select data-path="${p}.bands.${bi}.method" title="power = energy (squared), sum = magnitudes">
          <option value="power" ${b.method === "power" ? "selected" : ""}>power</option>
          <option value="sum" ${b.method === "sum" ? "selected" : ""}>sum</option>
        </select>
        <input data-path="${p}.bands.${bi}.cal_min" type="number" step="any" value="${esc(b.cal_min)}" title="Low energy bound (raw units)">
        <input data-path="${p}.bands.${bi}.cal_max" type="number" step="any" placeholder="uncalibrated" value="${esc(b.cal_max ?? "")}" title="High energy bound; band silent until set">
        <button data-act="calibrate-band" data-ci="${ci}" data-ri="${ri}" data-bi="${bi}" class="mini" title="Capture ~3 s; sets this band's cal_min/cal_max">calibrate</button>
        <button data-act="del-band" data-ci="${ci}" data-ri="${ri}" data-bi="${bi}" class="danger" title="Remove band">✕</button>
      </div>`)
    .join("");
  return `<div class="bands">${rows}
    <button data-act="add-band" data-ci="${ci}" data-ri="${ri}" class="mini" title="Add a band">+ band</button></div>`;
}

function ruleParams(r, p) {
  const inl = (name, label, opts = "") =>
    `<label class="fld">${label}<input data-path="${p}.${name}" ${opts} value="${esc(r[name])}"></label>`;
  if (r.type === "amplitude") {
    return inl("freq", "freq Hz", `type="number" step="1" title="Frequency to track (Hz)"`) +
      inl("gain", "gain", `type="number" step="0.1" title="Scales the raw value"`) +
      inl("offset", "offset", `type="number" step="0.1" title="Added to the raw value"`);
  }
  if (r.type === "onset") {
    return inl("f0", "f0", `type="number" placeholder="full" title="Band low edge; empty = full spectrum"`) +
      inl("f1", "f1", `type="number" placeholder="full" title="Band high edge; empty = full spectrum"`) +
      inl("threshold", "thr", `type="number" step="any" placeholder="uncalibrated" title="Flux trigger level; set via Auto-calibrate"`) +
      inl("ratio", "ratio", `type="number" step="0.1" title="Trigger when flux > threshold x ratio"`) +
      inl("hold_ms", "hold ms", `type="number" title="Ignore new onsets for this long (ms)"`) +
      inl("decay_ms", "decay ms", `type="number" title="Pulse decay time constant (ms)"`);
  }
  if (r.type === "dominant_frequency") {
    return inl("fmin", "fmin", `type="number" title="Lowest searchable frequency (Hz)"`) +
      inl("fmax", "fmax", `type="number" title="Highest searchable frequency (Hz)"`) +
      `<label class="tg"><input type="checkbox" data-path="${p}.normalize" ${r.normalize ? "checked" : ""} title="Emit 0..1 within [fmin, fmax] instead of Hz"> normalize 0..1</label>`;
  }
  if (r.type === "centroid") {
    return inl("fmin", "fmin", `type="number" title="Lowest searchable frequency (Hz)"`) +
      inl("fmax", "fmax", `type="number" title="Highest searchable frequency (Hz)"`) +
      `<label class="fld">method<select data-path="${p}.method" title="centroid = brightness center of mass; rolloff = frequency below which N% of energy sits">
        <option value="centroid" ${r.method === "centroid" ? "selected" : ""}>centroid</option>
        <option value="rolloff" ${r.method === "rolloff" ? "selected" : ""}>rolloff</option>
      </select></label>` +
      (r.method === "rolloff" ? inl("percent", "percent", `type="number" step="1" title="Energy percentage for the rolloff"`) : "") +
      `<label class="tg"><input type="checkbox" data-path="${p}.normalize" ${r.normalize ? "checked" : ""} title="Emit 0..1 within [fmin, fmax] instead of Hz"> normalize</label>`;
  }
  if (r.type === "pitch") {
    return inl("fmin", "fmin", `type="number" title="Lowest expected pitch (Hz); lower = more latency"`) +
      inl("fmax", "fmax", `type="number" title="Highest expected pitch (Hz)"`) +
      inl("threshold", "thr", `type="number" step="0.05" title="YIN aperiodicity threshold; lower = stricter"`) +
      `<label class="tg"><input type="checkbox" data-path="${p}.normalize" ${r.normalize ? "checked" : ""} title="Emit 0..1 within [fmin, fmax] instead of Hz"> normalize</label>`;
  }
  return inl("f0", "f0", `type="number" title="Band low edge (Hz)"`) +
    inl("f1", "f1", `type="number" title="Band high edge (Hz)"`) +
    `<label class="fld">method<select data-path="${p}.method" title="power = energy (squared magnitudes), sum = magnitudes">
      <option value="power" ${r.method === "power" ? "selected" : ""}>power</option>
      <option value="sum" ${r.method === "sum" ? "selected" : ""}>sum</option>
    </select></label>` +
    `<label class="tg"><input type="checkbox" data-path="${p}.clamp" ${r.clamp ? "checked" : ""} title="Clip output to 0..1"> clamp</label>`;
}

// ---------- renderers: targets / profiles ----------------------------

function renderGeneral() {
  $("general").innerHTML = `
    <label class="fld">OSC rate (Hz)<input data-path="osc_rate" type="number" step="1" value="${esc(cfg.osc_rate)}" title="Send cadence: max OSC messages or bundles per second"></label>
    <label class="tg"><input type="checkbox" data-path="auto_connect" ${cfg.auto_connect ? "checked" : ""} title="Treat empty channel connect as 'auto' (system:capture_N)"> auto-connect</label>
    <label class="fld">jack name<input data-path="jack_name" value="${esc(cfg.jack_name)}" title="JACK client name; applies on next reconnect/restart"></label>
    <label class="fld">sample rate<input data-path="sample_rate" type="number" placeholder="JACK's rate" value="${esc(cfg.sample_rate ?? "")}" title="Analysis rate override (Hz); empty = JACK's; applies on next rebind"></label>
    <label class="fld">cb warn (µs)<input data-path="cb_warn_us" type="number" placeholder="25% of period" value="${esc(cfg.cb_warn_us ?? "")}" title="Status icon turns red when callback p99 >= this (µs); empty = auto"></label>
    <label class="tg"><input type="checkbox" data-path="autosave" ${cfg.autosave ? "checked" : ""} title="Write the config to disk after every change"> autosave</label>`;
}

function renderTargets() {
  const wrap = $("targetsEditor");
  wrap.innerHTML = "";
  if (!cfg) return;
  cfg.targets.forEach((t, i) => {
    const d = document.createElement("div");
    d.className = "target";
    d.innerHTML = `
      <input data-path="targets.${i}.name" value="${esc(t.name)}" title="name">
      <input data-path="targets.${i}.host" value="${esc(t.host)}" title="host">
      <input data-path="targets.${i}.port" type="number" value="${esc(t.port)}" title="port">
      <input data-path="targets.${i}.prefix" placeholder="prefix" value="${esc(t.prefix)}" title="osc prefix">
      <label class="tg"><input type="checkbox" data-path="targets.${i}.enabled" ${t.enabled ? "checked" : ""} title="Enable or disable this target"> on</label>
      <label class="tg"><input type="checkbox" data-path="targets.${i}.bundle" ${t.bundle ? "checked" : ""} title="Send all rule values as one OSC #bundle per cycle"> bundle</label>
      <button data-act="del-target" data-name="${esc(t.name)}" class="danger" title="delete target">✕</button>`;
    wrap.appendChild(d);
  });
}

async function loadProfiles() {
  try {
    const { profiles } = await api("/api/profiles");
    const sel = $("profiles");
    sel.innerHTML = "";
    for (const p of profiles) {
      const o = document.createElement("option");
      o.value = p;
      o.textContent = p;
      sel.appendChild(o);
    }
  } catch { /* server offline */ }
}

// ---------- edit events (delegated) ---------------------------------

function onInput(e) {
  const el = e.target;
  const path = el.dataset.path;
  if (!path || !cfg) return;
  if (el.tagName === "SELECT") return; // selects fire input+change; onChange owns them
  const last = path.split(".").pop();
  let v;
  if (NUM_FIELDS.has(last) || /^\d+$/.test(last)) v = parseFloat(el.value) || 0;
  else if (last === "cal_max" || last === "threshold" || last === "gate_on" || last === "gate_off" || last === "sample_rate" || last === "cb_warn_us") v = el.value === "" ? null : parseFloat(el.value);
  else v = el.value;
  setPath(cfg, path, v);
  scheduleApply(); // debounced: commit once typing pauses
}

function onChange(e) {
  const el = e.target;
  const path = el.dataset.path;
  if (!path || !cfg) return;
  if (el.type === "text" || el.type === "number") return; // onInput handles these; blur 'change' is a duplicate
  if (el.type === "checkbox") {
    const m = path.match(/^(.+\.targets)\.([^.]+)$/);
    if (m) {
      const arr = getPath(cfg, m[1]) || [];
      const i = arr.indexOf(m[2]);
      if (el.checked && i < 0) arr.push(m[2]);
      if (!el.checked && i >= 0) arr.splice(i, 1);
    } else {
      setPath(cfg, path, el.checked);
    }
    applyNow(); // immediate
    if (path.endsWith(".normalize")) renderConfig(); // invert/gate visibility depends on it
    return;
  }
  if (el.tagName === "SELECT") {
    if (path.endsWith(".type")) {
      const m = path.match(/^channels\.(\d+)\.rules\.(\d+)\.type$/);
      if (m) {
        // switchRuleType reads the OLD type for the pattern check, then rebuilds
        switchRuleType(Number(m[1]), Number(m[2]), el.value);
        renderConfig();
        applyNow();
        return;
      }
    }
    setPath(cfg, path, el.value);
    renderConfig();
    applyNow();
  }
}

function isDefaultPattern(pattern, channel, type) {
  return new RegExp(`^/${channel}/${type}(/\\d+)?$`).test(pattern);
}

// Rebuild a rule for a new type: start from the type's defaults (which
// carry required fields like multiband's `bands`), preserve shared and
// overlapping values, and regenerate auto-default osc_patterns.
function switchRuleType(ci, ri, newType) {
  const old = cfg.channels[ci].rules[ri];
  const fresh = defaultRule(newType);
  for (const k of Object.keys(fresh)) {
    if (k !== "type" && k in old) fresh[k] = old[k]; // keep the new type
  }
  for (const k of RULE_SHARED) {
    if (k in old && !(k in fresh)) fresh[k] = old[k];
  }
  const ch = cfg.channels[ci];
  if (isDefaultPattern(old.osc_pattern, ch.name, old.type)) {
    fresh.osc_pattern = uniquifyPattern(`/${ch.name}/${newType}`);
  }
  cfg.channels[ci].rules[ri] = fresh;
}

function onClick(e) {
  const btn = e.target.closest("[data-act]");
  if (btn) {
    const act = btn.dataset.act;
    const ci = Number(btn.dataset.ci);
    const ri = Number(btn.dataset.ri);
    if (act === "add-channel") addChannel();
    else if (act === "toggle-channel") {
      const name = cfg.channels[ci].name;
      if (collapsedChannels.has(name)) collapsedChannels.delete(name);
      else collapsedChannels.add(name);
      renderConfig();
    }
    else if (act === "dup-channel") {
      const copy = JSON.parse(JSON.stringify(cfg.channels[ci]));
      copy.name = dupName(copy.name);
      cfg.channels.splice(ci + 1, 0, copy);
      renderConfig();
      applyNow();
    }
    else if (act === "del-channel") { cfg.channels.splice(ci, 1); renderConfig(); applyNow(); }
    else if (act === "add-rule") {
      const rule = defaultRule("amplitude");
      rule.osc_pattern = uniquifyPattern(`/${cfg.channels[ci].name}/amplitude`);
      cfg.channels[ci].rules.push(rule);
      renderConfig();
      applyNow();
    }
    else if (act === "dup-rule") {
      const copy = JSON.parse(JSON.stringify(cfg.channels[ci].rules[ri]));
      copy.osc_pattern = uniquifyPattern(copy.osc_pattern); // avoid address collision
      cfg.channels[ci].rules.splice(ri + 1, 0, copy);
      renderConfig();
      applyNow();
    }
    else if (act === "del-rule") { cfg.channels[ci].rules.splice(ri, 1); renderConfig(); applyNow(); }
    else if (act === "calibrate") doCalibrate(ci, ri);
    else if (act === "add-band") {
      cfg.channels[ci].rules[ri].bands.push({ f0: 40, f1: 80, method: "power", cal_min: 0, cal_max: null, clamp: true, curve: "linear", curve_pow: 2 });
      renderConfig();
      applyNow();
    }
    else if (act === "del-band") {
      cfg.channels[ci].rules[ri].bands.splice(Number(btn.dataset.bi), 1);
      renderConfig();
      applyNow();
    }
    else if (act === "calibrate-band") doCalibrate(ci, ri, Number(btn.dataset.bi));
    else if (act === "add-target") addTarget();
    else if (act === "del-target") {
      const i = cfg.targets.findIndex((t) => t.name === btn.dataset.name);
      if (i >= 0) cfg.targets.splice(i, 1);
      renderConfig();
      applyNow();
    }
    return;
  }
  // click on a rule box (not its inputs/buttons) selects it for the preview
  const ruleBox = e.target.closest(".rule");
  if (ruleBox && !e.target.closest("input, select")) {
    setSelectedRule(ruleBox.dataset.rule);
  }
}

function setSelectedRule(id) {
  selectedRule = selectedRule === id ? null : id;
  document.querySelectorAll(".rule").forEach((el) =>
    el.classList.toggle("selected", el.dataset.rule === selectedRule),
  );
  if (selectedRule) {
    const ci = Number(selectedRule.split(":")[0]);
    const name = live.channels[ci] && live.channels[ci].name;
    const card = cards.get(name);
    if (card) drawSpectrum(card.canvas, live.spectra[ci], ci);
  }
}

function dupName(name) {
  const m = name.match(/^(.*?)(\d+)$/);
  const base = m ? m[1] : name;
  let n = m ? parseInt(m[2], 10) : 1;
  let candidate;
  do {
    n++;
    candidate = (base + n).slice(0, 64);
  } while (cfg.channels.some((c) => c.name === candidate));
  return candidate;
}

// OSC address pattern that no rule in the config currently uses.
function uniquifyPattern(base) {
  const used = new Set();
  for (const ch of cfg.channels) for (const r of ch.rules) used.add(r.osc_pattern);
  let cand = base, n = 2;
  while (used.has(cand)) cand = `${base}/${n++}`;
  return cand;
}

function addChannel() {
  const name = prompt("channel name [A-Za-z0-9_-]");
  if (!name) return;
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) { showToast("invalid name: use letters, digits, '_', '-'"); return; }
  cfg.channels.push({ name, connect_to: null, window: 1024, hop: 512, rules: [] });
  renderConfig();
  applyNow();
}

function addTarget() {
  const name = prompt("target name [A-Za-z0-9_-]");
  if (!name || !/^[A-Za-z0-9_-]{1,64}$/.test(name)) { showToast("invalid name: use letters, digits, '_', '-'"); return; }
  const host = prompt("host (IP or hostname)");
  const port = parseInt(prompt("UDP port"), 10);
  if (!host || !(port >= 1 && port <= 65535)) { showToast("invalid host or port"); return; }
  cfg.targets.push({ name, host, port, enabled: true, prefix: "", bundle: false });
  renderConfig();
  applyNow();
}

async function doCalibrate(ci, ri, band) {
  const ch = cfg.channels[ci];
  const rule = ch.rules[ri];
  if (rule.type !== "frequency_map" && rule.type !== "multiband" && rule.type !== "onset") return;
  clearTimeout(applyTimer); // config is already applied; don't let a stale debounce overwrite
  applyTimer = null;
  try {
    const data = await api(
      `/api/channels/${encodeURIComponent(ch.name)}/rules/${ri}/calibrate`,
      { method: "POST", body: JSON.stringify({ seconds: 3, band: band ?? null }) },
    );
    cfg = data.config.config;
    pushHistory();
    renderConfig();
    if (data.threshold != null) {
      showToast(`calibrated ${data.samples} windows: threshold ${data.threshold.toFixed(4)}`, "ok");
    } else {
      const label = data.band != null ? `band ${data.band}: ` : "";
      showToast(`calibrated ${data.samples} windows: ${label}${data.cal_min.toFixed(3)} .. ${data.cal_max.toFixed(3)}`, "ok");
    }
  } catch (e) {
    showToast("calibrate failed: " + e.message);
  }
}

// ---------- profiles -------------------------------------------------

$("saveProfile").addEventListener("click", async () => {
  const name = prompt("profile name");
  if (!name) return;
  try { await api(`/api/profiles/${encodeURIComponent(name)}`, { method: "POST" }); showToast("profile saved", "ok"); loadProfiles(); }
  catch (e) { showToast("save failed: " + e.message); }
});

$("loadProfile").addEventListener("click", async () => {
  const name = $("profiles").value;
  if (!name) return;
  try {
    const data = await api(`/api/profiles/${encodeURIComponent(name)}/load`, { method: "POST" });
    cfg = data.config;
    clearTimeout(applyTimer);
    applyTimer = null;
    pushHistory();
    renderConfig();
  } catch (e) { showToast("load failed: " + e.message); }
});

$("deleteProfile").addEventListener("click", async () => {
  const name = $("profiles").value;
  if (!name || !confirm(`delete profile ${name}?`)) return;
  try { await api(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" }); showToast("profile deleted", "ok"); loadProfiles(); }
  catch (e) { showToast("delete failed: " + e.message); }
});

$("channelsEditor").addEventListener("input", onInput);
$("channelsEditor").addEventListener("change", onChange);
$("channelsEditor").addEventListener("click", onClick);
$("targetsEditor").addEventListener("input", onInput);
$("targetsEditor").addEventListener("change", onChange);
$("targetsEditor").addEventListener("click", onClick);
$("general").addEventListener("input", onInput);
$("general").addEventListener("change", onChange);
$("addChannel").addEventListener("click", addChannel);
$("addTarget").addEventListener("click", addTarget);

// ---------- live view -------------------------------------------------

const PAD_L = 30; // left strip for y labels (backing px)
const PAD_B = 16; // bottom strip for x labels (backing px)

// Compact display of raw spectrum magnitudes (arbitrary units).
function fmtMag(v) {
  if (!Number.isFinite(v)) return "0";
  if (v >= 100) return v.toFixed(0);
  if (v >= 10) return v.toFixed(1);
  if (v >= 1) return v.toFixed(2);
  if (v >= 0.01) return v.toFixed(4);
  return v.toExponential(1);
}

function renderStatus(st, authEnabled) {
  if (!st) return;
  const periodUs = st.samplerate && st.blocksize ? (st.blocksize / st.samplerate) * 1e6 : null;
  const warnUs = (cfg && cfg.cb_warn_us) || (periodUs ? Math.max(1000, periodUs * 0.25) : 2000);
  const warn =
    !st.audio ||
    st.xruns > 0 ||
    st.dropped > 0 ||
    (st.audio && st.cb_count && st.cb_p99_us >= warnUs);
  const icon = $("status");
  icon.classList.toggle("warn", !!warn);
  icon.classList.toggle("ok", !warn);
  const lines = [
    `audio ${st.audio ? "on" : "off"}${st.samplerate ? ` @ ${Math.round(st.samplerate)} Hz` : ""}` +
      (st.blocksize ? ` · ${st.blocksize} frames/period${periodUs ? ` (${(periodUs / 1000).toFixed(1)} ms budget)` : ""}` : ""),
    `xruns ${st.xruns} · dropped ${st.dropped}`,
    st.audio && st.cb_count
      ? `callback: p50 ${st.cb_p50_us}µs · p99 ${st.cb_p99_us}µs · max ${st.cb_max_us}µs`
      : "callback: no data yet",
    `warn threshold: p99 ≥ ${Math.round(warnUs)}µs${cfg && cfg.cb_warn_us ? " (set)" : " (25% of period budget)"}`,
  ];
  if (!st.audio && st.audio_error) lines.push(`error: ${st.audio_error}`);
  if (authEnabled) lines.push("auth: on");
  const tip = lines.join("\n");
  $("statusTip").textContent = tip;
  icon.title = tip;
}

function renderChannels(names) {
  for (const name of names) {
    if (!cards.has(name)) {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = `<h3>${esc(name)}</h3>
        <div class="graph-wrap">
          <canvas width="640" height="160"></canvas>
          <div class="cursor-label" hidden></div>
        </div>
        <div class="values"></div>`;
      $("channels").appendChild(card);
      const canvas = card.querySelector("canvas");
      attachDrag(canvas, names.indexOf(name));
      attachHover(canvas, names.indexOf(name));
      cards.set(name, { card, canvas, values: card.querySelector(".values") });
    }
  }
  for (const name of [...cards.keys()]) {
    if (!names.includes(name)) {
      cards.get(name).card.remove();
      cards.delete(name);
    }
  }
}

function drawSpectrum(canvas, spec, ci) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const ch = live.channels[ci];
  const sr = (live.status && live.status.samplerate) || (cfg && cfg.sample_rate);
  const win = ch && ch.window;
  const plotX = PAD_L, plotW = w - PAD_L, plotH = h - PAD_B;
  const n = win ? win / 2 + 1 : 0;

  // selected-rule band + drag overlays, in plot coordinates
  if (sr && win) {
    for (const ov of selectedRuleOverlays(ci, plotW)) {
      if (ov.fill && ov.x1 - ov.x0 > 2) {
        ctx.fillStyle = ov.fill;
        ctx.fillRect(plotX + ov.x0, 0, ov.x1 - ov.x0, plotH);
      }
      ctx.strokeStyle = ov.stroke;
      ctx.strokeRect(plotX + ov.x0 + 0.5, 0.5, Math.max(0, ov.x1 - ov.x0 - 1), plotH - 1);
      if (ov.markerX != null) {
        ctx.strokeStyle = "#ffffff";
        ctx.beginPath();
        ctx.moveTo(plotX + ov.markerX, 0);
        ctx.lineTo(plotX + ov.markerX, plotH);
        ctx.stroke();
      }
    }
  }
  if (drag && drag.ci === ci && drag.x1 != null) {
    const scale = w / canvas.clientWidth;
    const x0 = Math.min(drag.x0, drag.x1) * scale - PAD_L;
    const x1 = Math.max(drag.x0, drag.x1) * scale - PAD_L;
    ctx.fillStyle = "rgba(59, 130, 246, 0.25)";
    ctx.fillRect(x0 + plotX, 0, x1 - x0, plotH);
  }

  if (!sr || !win || !spec || spec.length < 2) return;

  // rolling-average peak -> stable y scale (fast attack, slow release)
  let peak = 0;
  for (const m of spec) if (m > peak) peak = m;
  const card = cards.get(ch.name);
  if (card) {
    const prev = card.peakAvg == null ? peak : card.peakAvg;
    const a = peak > prev ? 0.15 : 0.05;
    card.peakAvg = prev + a * (peak - prev);
    if (card.peakAvg < 1e-9) card.peakAvg = 1e-9;
  }
  const yMax = (card && card.peakAvg) || Math.max(peak, 1e-9);

  // bars
  const bars = 256;
  const bw = plotW / bars;
  ctx.fillStyle = "#3b82f6";
  for (let b = 0; b < bars; b++) {
    const k = Math.min(n - 1, Math.floor(Math.pow(b / bars, 1.5) * (n - 1)));
    const bh = Math.min(plotH, (spec[k] / yMax) * plotH);
    ctx.fillRect(plotX + b * bw, plotH - bh, bw + 1, bh);
  }

  // gridlines + subtle labels (y labels track the rolling scale in real units)
  ctx.font = "9px ui-monospace, monospace";
  ctx.strokeStyle = "rgba(139, 147, 167, 0.2)";
  ctx.lineWidth = 1;
  [0.25, 0.5, 0.75].forEach((frac) => {
    const y = plotH - frac * plotH;
    ctx.beginPath();
    ctx.moveTo(plotX, y);
    ctx.lineTo(plotX + plotW, y);
    ctx.stroke();
    ctx.fillStyle = "rgba(139, 147, 167, 0.6)";
    ctx.textAlign = "right";
    ctx.fillText(fmtMag(frac * yMax), plotX - 4, y + 3);
  });
  const xOf = (hz) => Math.pow((hz * win) / sr / (n - 1), 2 / 3) * plotW;
  const fMin = sr / win, fMax = sr / 2 * 0.95;
  ctx.textAlign = "left";
  [50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].forEach((f) => {
    if (f < fMin || f > fMax) return;
    const x = plotX + xOf(f);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, plotH);
    ctx.stroke();
    ctx.fillStyle = "rgba(139, 147, 167, 0.6)";
    ctx.fillText(f >= 1000 ? `${f / 1000}k` : String(f), x + 2, h - 4);
  });
}

// Map a rule's parameters to canvas x-positions for the live preview overlay.
// Returns a list (multiband rules draw one band each).
function selectedRuleOverlays(ci, width) {
  if (!selectedRule || !cfg) return [];
  const [sci, sri] = selectedRule.split(":").map(Number);
  if (sci !== ci) return [];
  const ch = cfg.channels[sci];
  const rule = ch && ch.rules[sri];
  if (!rule) return [];
  const sr = (live.status && live.status.samplerate) || cfg.sample_rate;
  if (!sr) return [];
  const win = ch.window;
  const n = win / 2 + 1;
  const xOf = (hz) => Math.pow((hz * win) / sr / (n - 1), 2 / 3) * width;
  const mk = (x0, x1, markerX = null) => ({ fill: "rgba(34, 211, 238, 0.18)", stroke: "#22d3ee", x0, x1, markerX });
  if (rule.type === "multiband") {
    return rule.bands.map((b) => mk(xOf(b.f0), xOf(b.f1)));
  }
  if (rule.type === "amplitude") {
    const x = xOf(rule.freq);
    return [Object.assign(mk(x - 1.5, x + 1.5), { fill: null, markerX: x })];
  }
  if (rule.type === "onset") {
    if (!rule.f0 || !rule.f1) return [];
    return [mk(xOf(rule.f0), xOf(rule.f1))];
  }
  const band = mk(
    xOf(rule.type === "frequency_map" ? rule.f0 : rule.fmin),
    xOf(rule.type === "frequency_map" ? rule.f1 : rule.fmax),
  );
  if ((rule.type === "dominant_frequency" || rule.type === "centroid" || rule.type === "pitch") && !rule.normalize) {
    const v = live.values[sci] && live.values[sci][sri];
    if (Number.isFinite(v)) band.markerX = xOf(v);
  }
  return [band];
}

function renderValues(el, ci, row, ruleIds) {
  el.textContent = "";
  if (!row) return;
  row.forEach((v, j) => {
    const id = ruleIds[`${ci}:${j}`];
    if (!id) return;
    const span = document.createElement("span");
    span.className = "value";
    const rule = cfg && cfg.channels[ci] && cfg.channels[ci].rules[j];
    if (rule && rule.type === "multiband") {
      const m = live.multi && live.multi[`${ci}:${j}`];
      span.textContent = `${id}: ${m ? m.map((x) => (x == null ? "—" : Number(x).toFixed(2))).join(" ") : "—"}`;
    } else {
      span.textContent = `${id}: ${v == null ? "—" : Number(v).toFixed(3)}`;
    }
    el.appendChild(span);
  });
}

function onState(s) {
  live.status = s.status;
  live.channels = s.channels || [];
  live.spectra = s.spectra || {};
  live.values = s.values || [];
  live.ruleIds = s.rule_ids || {};
  live.multi = s.multi || {};
  if (s.status) renderStatus(s.status);
  const names = live.channels.map((c) => c.name);
  renderChannels(names);
  names.forEach((name, i) => {
    const c = cards.get(name);
    if (!c) return;
    drawSpectrum(c.canvas, live.spectra[i], i);
    renderValues(c.values, i, live.values[i], live.ruleIds);
  });
  updateRuleReadouts();
}

// ---------- rule readouts + sparklines -------------------------------

const sparkHistory = new Map(); // "ci:ri" -> { meta, vals: number[] }

function drawSpark(canvas, vals) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  let lo = Infinity, hi = -Infinity;
  let any = false;
  for (const v of vals) {
    if (!Number.isFinite(v)) continue;
    any = true;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!any) return;
  // floor the y-range at 5% of the value magnitude: a 0→0.001 change is a
  // subtle blip, not a full-height spike, while Hz values keep a little zoom
  let range = hi - lo;
  const minRange = Math.max(1e-9, 0.05 * Math.max(Math.abs(lo), Math.abs(hi), 1));
  if (range < minRange) {
    const mid = (lo + hi) / 2;
    lo = mid - minRange / 2;
    hi = mid + minRange / 2;
    range = minRange;
  }
  ctx.strokeStyle = "#22d3ee";
  ctx.lineWidth = 1;
  ctx.beginPath();
  let started = false;
  const n = vals.length;
  for (let i = 0; i < n; i++) {
    const v = vals[i];
    if (!Number.isFinite(v)) { started = false; continue; } // gap
    const x = (i / (n - 1)) * (w - 1);
    const y = 1 + (1 - (v - lo) / range) * (h - 2);
    if (started) ctx.lineTo(x, y);
    else { ctx.moveTo(x, y); started = true; }
  }
  ctx.stroke();
}

// Live value chip in the bottom-right of each editor rule box.
function updateRuleReadouts() {
  document.querySelectorAll("[data-rule]").forEach((el) => {
    const key = el.dataset.rule;
    const [ci, ri] = key.split(":").map(Number);
    const v = live.values[ci] && live.values[ci][ri];
    const span = el.querySelector(".rule-value");
    const canvas = el.querySelector(".rule-spark");
    if (!span) return;
    const rule = cfg && cfg.channels[ci] && cfg.channels[ci].rules[ri];
    // sparkline history (resets when the rule's type/pattern changes)
    if (canvas) {
      const st = sparkHistory.get(key) || { meta: "", vals: [] };
      const meta = rule ? rule.type + "/" + rule.osc_pattern : "";
      if (st.meta !== meta) { st.vals = []; st.meta = meta; }
      st.vals.push(Number.isFinite(v) ? v : NaN);
      if (st.vals.length > 60) st.vals.shift();
      sparkHistory.set(key, st);
      drawSpark(canvas, st.vals);
    }
    if (v == null) {
      span.textContent = "—";
      return;
    }
    if (rule && rule.type === "multiband") {
      const m = live.multi && live.multi[key];
      span.textContent = m ? m.map((x) => (x == null ? "—" : Number(x).toFixed(2))).join(" ") : "—";
      return;
    }
    const isHz = rule && rule.type === "dominant_frequency" && !rule.normalize;
    span.textContent = isHz ? `${Number(v).toFixed(1)} Hz` : Number(v).toFixed(3);
  });
}

// ---------- spectrum band drag ---------------------------------------

function attachHover(canvas, ci) {
  const label = canvas.parentElement.querySelector(".cursor-label");
  canvas.addEventListener("pointermove", (e) => updateCursorLabel(canvas, label, ci, e));
  canvas.addEventListener("pointerleave", () => { label.hidden = true; });
}

// Frequency + amplitude of the point under the cursor, in a small label.
// Amplitude is read from the cursor's y position against the rolling
// scale (matches the y gridline labels), not from the bin value.
function updateCursorLabel(canvas, label, ci, e) {
  const ch = live.channels[ci];
  const sr = (live.status && live.status.samplerate) || (cfg && cfg.sample_rate);
  const win = ch && ch.window;
  const spec = live.spectra[ci];
  if (!sr || !win || !spec || spec.length < 2) {
    label.hidden = true;
    return;
  }
  const plotW = canvas.width - PAD_L;
  const plotH = canvas.height - PAD_B;
  const xPlot = e.offsetX * (canvas.width / canvas.clientWidth) - PAD_L;
  if (xPlot < 0 || xPlot > plotW) {
    label.hidden = true;
    return;
  }
  const n = spec.length;
  const k = Math.pow(xPlot / plotW, 1.5) * (n - 1);
  const hz = (k * sr) / win;
  const yPlot = e.offsetY * (canvas.height / canvas.clientHeight);
  const frac = Math.min(1, Math.max(0, 1 - yPlot / plotH));
  const card = cards.get(ch.name);
  const yMax = (card && card.peakAvg) || Math.max(...spec) || 1;
  const mag = frac * yMax;
  label.textContent =
    `${hz >= 1000 ? (hz / 1000).toFixed(2) + " kHz" : hz.toFixed(1) + " Hz"} · ${fmtMag(mag)}`;
  const w = canvas.clientWidth;
  const lx = e.offsetX + 12 > w - 130 ? e.offsetX - 132 : e.offsetX + 12;
  label.style.left = `${Math.max(0, lx)}px`;
  label.style.top = `${Math.max(0, e.offsetY - 30)}px`;
  label.hidden = false;
}

function attachDrag(canvas, ci) {
  canvas.style.cursor = "crosshair";
  canvas.addEventListener("pointerdown", (e) => {
    if (selectedRule && Number(selectedRule.split(":")[0]) === ci) setSelectedRule(null);
    drag = { ci, x0: e.offsetX, x1: null };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (drag && drag.ci === ci) {
      drag.x1 = e.offsetX;
      drawSpectrum(canvas, live.spectra[ci], ci);
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    if (drag && drag.ci === ci) {
      drag.x1 = e.offsetX;
      finishBand();
      drag = null;
      drawSpectrum(canvas, live.spectra[ci], ci);
    }
  });
}

function finishBand() {
  if (!cfg) return;
  const ch = live.channels[drag.ci];
  if (!ch) return;
  const sr = (live.status && live.status.samplerate) || cfg.sample_rate;
  if (!sr) { showToast("no samplerate — audio offline"); return; }
  const canvas = cards.get(ch.name).canvas;
  const scale = canvas.width / canvas.clientWidth;
  const plotW = canvas.width - PAD_L;
  const n = ch.window / 2 + 1;
  const px = (x) => Math.max(0, x * scale - PAD_L);
  const x0 = px(Math.min(drag.x0, drag.x1));
  const x1 = px(Math.max(drag.x0, drag.x1));
  const k0 = Math.pow(x0 / plotW, 1.5) * (n - 1);
  const k1 = Math.pow(x1 / plotW, 1.5) * (n - 1);
  if (k1 - k0 < 1) return; // click, not a drag
  const f0 = Math.round((k0 * sr) / ch.window);
  const f1 = Math.round((k1 * sr) / ch.window);
  if (f1 <= f0) return;
  const rule = defaultRule("frequency_map");
  rule.f0 = f0;
  rule.f1 = f1;
  rule.osc_pattern = uniquifyPattern(`/${ch.name}/frequency_map`);
  cfg.channels[drag.ci].rules.push(rule);
  renderConfig();
  applyNow();
  const el = document.querySelector(`[data-rule="${drag.ci}:${cfg.channels[drag.ci].rules.length - 1}"]`);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------- OSC packet inspector --------------------------------------

let pktPaused = false;
let pktRows = []; // newest-first

function connectPackets() {
  const ws = new WebSocket(`ws://${location.host}/ws/packets`);
  ws.onmessage = (ev) => {
    if (pktPaused) return;
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type !== "packets") return;
      pktRows = [...msg.packets.reverse(), ...pktRows].slice(0, 200);
      renderPackets();
    } catch { /* ignore */ }
  };
  ws.onclose = () => setTimeout(connectPackets, 2000);
}

async function loadPackets() {
  try {
    const { packets } = await api("/api/packets?limit=200");
    pktRows = packets.reverse();
    renderPackets();
  } catch { /* server offline */ }
}

function renderPackets() {
  const q = $("pktFilter").value.toLowerCase();
  const rows = pktRows.filter(
    (p) => !q || p.address.toLowerCase().includes(q) || p.target.toLowerCase().includes(q),
  );
  $("pktCount").textContent = `${rows.length} shown · ${pktRows.length} buffered`;
  const list = $("pktList");
  list.innerHTML = "";
  for (const p of rows) {
    const d = document.createElement("div");
    d.className = "pkt";
    d.innerHTML = `<span class="t">${(p.t / 1e6).toFixed(1)} ms</span>
      <span class="target">${esc(p.target)}</span>
      <span class="addr mono">${esc(p.address)}</span>
      <span class="val">${Number(p.value).toFixed(3)}</span>`;
    list.appendChild(d);
  }
}

$("pktFilter").addEventListener("input", renderPackets);
$("pktPause").addEventListener("click", (e) => {
  pktPaused = !pktPaused;
  e.target.textContent = pktPaused ? "Resume" : "Pause";
  if (!pktPaused) loadPackets(); // catch up on packets delivered while paused
});

$("pktSend").addEventListener("click", async () => {
  const address = $("pktAddr").value.trim() || "/test";
  const value = parseFloat($("pktVal").value) || 0;
  try {
    await api("/api/packets/test", { method: "POST", body: JSON.stringify({ address, value }) });
  } catch (e) {
    showToast("test send failed: " + e.message);
  }
});

// ---------- boot -------------------------------------------------------

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => { try { onState(JSON.parse(ev.data)); } catch { /* ignore */ } };
  ws.onclose = () => setTimeout(connect, 2000);
}

loadConfig();
connect();
loadPackets();
connectPackets();

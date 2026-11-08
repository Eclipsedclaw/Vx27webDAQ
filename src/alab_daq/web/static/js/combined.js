// The combined view of the Run page: channels of several boards in one plot (up to 16 traces), from one moment.
// Live it shows the DAQ's matched set: one event of every board with the same timestamp cell, so with the clock
// chain and a common trigger every trace is the same trigger; from files it pairs the run's boards by trigger ID.
// The time axis is in µs from the trigger and covers every chosen board's window (a VX2730 samples every 2 ns, a
// VX2740 every 8 ns); "line up by timestamps" shifts each board by its timestamp against the first board, so a
// pulse fed into two boards lines up. The y axis: mV from each channel's baseline (each model's own ADC scale),
// ADC counts from the baseline, or raw ADC counts.
import { get } from "./api.js";
import { T, t } from "./i18n.js";
import { MAX_SHOWN, channelPicker, defaultChannels } from "./chanpick.js";
import { fmtTime } from "./plots.js";
import { clear, fail, fmtInt, fmtNum, h, icon, segmented, setText, store } from "./ui.js";

const PALETTE = ["#2f6fdf", "#d9452b", "#1f9d55", "#c27c0e", "#8a4fd8", "#0f8fa8", "#d23f8f", "#6b8e23",
                 "#1d4ed8", "#b91c1c", "#047857", "#a16207", "#6d28d9", "#0e7490", "#be185d", "#4d7c0f"];
const YUNITS = [["mv", T("mV from baseline")], ["adc", T("ADC from baseline")], ["raw", T("raw ADC")]];
const ALIGNS = [["trigger", T("each board's trigger")], ["timestamp", T("timestamps")]];
const Y_LABEL = { mv: "mV", adc: T("ADC counts"), raw: T("ADC counts") };

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// the decimals a tick step needs: 0.5 -> 1, 0.025 -> 3, 2 -> 0
function decimals(step) {
  let d = 0;
  while (d < 6 && Math.abs(Math.round(step * 10 ** d) - step * 10 ** d) > 1e-6) d++;
  return d;
}

function loadTraces() {
  try {
    const v = JSON.parse(store("comb.traces") || "null");
    if (Array.isArray(v)) return v.filter((x) => Array.isArray(x) && typeof x[0] === "string" && Number.isInteger(x[1])).slice(0, MAX_SHOWN);
  } catch (e) { /* none kept */ }
  return null;
}

// getSource() -> {source: "live"|"file", file}
export function combinedView(app, { getSource }) {
  let traces = loadTraces();                       // [[board, ch], ...]; null: not chosen yet
  let yunit = YUNITS.some(([k]) => k === store("comb.y")) ? store("comb.y") : "mv";
  let align = store("comb.align") === "timestamp" ? "timestamp" : "trigger";
  let zoom = null, eventNo = 0, events = 0, available = [], view = null, active = false;
  let adderBoard = store("comb.adder") || "";

  const el = h("div.comb.hidden");
  const chips = h("div.row.wrap.comb-chips");
  const addBtn = h("button.btn.btn-sm", { type: "button", onclick: () => toggleAdder(), "aria-expanded": "false",
                                          title: t("Choose the channels of each board to put in this plot (at most 16 in all)") },
                   icon("plus"), t("Channels…"));
  const ySeg = segmented(YUNITS.map(([k, v]) => [k, t(v)]), yunit, (v) => { yunit = v; store("comb.y", v); load(true); }, { label: t("Y axis") });
  ySeg.title = t("mV: each channel minus its baseline (the mean of its first 16 samples), in mV of its board's input range (2 Vpp: a VX2730 count is 0.122 mV, a VX2740 count 0.031 mV). ADC: the same in counts. Raw: the counts as recorded");
  const aSeg = segmented(ALIGNS.map(([k, v]) => [k, t(v)]), align, (v) => { align = v; store("comb.align", v); zoom = null; load(true); }, { label: t("Time 0") });
  aSeg.title = t("Each board's trigger at time 0, or the boards placed by their timestamps against the first board: the same moment lines up (with the clock chain)");
  const evInput = h("input.input.num", { type: "number", min: "0", value: "0", "aria-label": t("Event"), onchange: () => setEvent(Number(evInput.value) || 0) });
  const evCount = h("span.text2.small.num");
  const evGroup = h("div.group", h("span.label", t("Event")),
                    h("div.stepper", h("button.btn.btn-sm.btn-icon", { type: "button", "aria-label": t("Previous event"), onclick: () => setEvent(eventNo - 1) }, icon("left")),
                      evInput, h("button.btn.btn-sm.btn-icon", { type: "button", "aria-label": t("Next event"), onclick: () => setEvent(eventNo + 1) }, icon("right"))),
                    evCount);
  const resetBtn = h("button.btn.btn-sm", { type: "button", onclick: () => { zoom = null; load(true); }, title: t("The whole time window of every board again") },
                     icon("reset"), t("Reset zoom"));
  const adderSel = h("select.input", { "aria-label": t("Board"), onchange: () => { adderBoard = adderSel.value; store("comb.adder", adderBoard); fillAdder(); } });
  const picker = channelPicker({ label: t("Channels of this board"), onChange: (list) => setBoardTraces(adderSel.value, list) });
  const adder = h("div.chan-box.hidden", h("div.row.wrap", h("span.label", t("Board")), adderSel,
                                           h("span.text2.small", t("the channels chosen here join the plot; every board together: at most 16"))), picker.el);
  const info = h("div.wave-sub");
  const plotEl = h("div.plot.comb-plot");
  const empty = h("div.wave-empty.hidden");
  const readout = h("div.row.wrap.comb-read");
  el.append(h("div.toolbar.two-rows",
              h("div.toolbar-row", h("span.label", t("In the plot")), chips, addBtn),
              h("div.toolbar-row", h("div.group", h("span.label", t("Y axis")), ySeg), h("div.group", h("span.label", t("Time 0")), aSeg),
                evGroup, h("span.spacer"), resetBtn)),
            adder, info, h("div.comb-body", h("div.wave-plot", plotEl, empty)), readout);

  // ---- the traces ----
  function boardsList() {
    if (available.length) return available;
    const cfg = app.state.config.config;
    return cfg.boards.filter((b) => b.enabled).map((b) => ({ name: b.name, model: b.model, channels: b.channels.length,
                                                             enabled: b.channels.map((c) => c.enabled) }));
  }

  function ensureTraces() {
    if (traces) return;
    traces = [];
    for (const b of boardsList().slice(0, 4)) for (const c of defaultChannels(b.channels, b.enabled, 2)) traces.push([b.name, c]);
    saveTraces();
  }

  function saveTraces() { store("comb.traces", JSON.stringify(traces)); }

  function colorOf(i) { return PALETTE[i % PALETTE.length]; }

  function showChips() {
    const byKey = new Map(((view && view.traces) || []).map((x) => [`${x.board}:${x.ch}`, x]));
    clear(chips, traces.length ? traces.map(([b, c], i) => {
      const v = byKey.get(`${b}:${c}`);
      const why = v && !v.present ? v.why : "";
      return h("span.comb-chip", { title: why ? t("{b} ch{c}: {why}", { b, c, why: t(why) }) : `${b} ch${c}`, class: why ? "muted" : "" },
               h("span.swatch", { style: `background:${colorOf(i)}` }), `${b} · ch${c}`,
               h("button.btn-ghost.btn-icon.mini", { type: "button", "aria-label": t("Remove {b} ch{c}", { b, c }),
                                                      onclick: () => { traces.splice(i, 1); saveTraces(); fillAdder(); load(true); } }, "×"));
    }) : h("span.text2.small", t("no channel yet: press Channels…")));
  }

  function toggleAdder(open) {
    const show = open ?? adder.classList.contains("hidden");
    adder.classList.toggle("hidden", !show);
    addBtn.setAttribute("aria-expanded", show ? "true" : "false");
    addBtn.classList.toggle("on", show);
    if (show) fillAdder();
  }

  function fillAdder() {
    const list = boardsList();
    const keep = adderSel.value || adderBoard;
    clear(adderSel, list.map((b) => h("option", { value: b.name }, `${b.name} · ${b.model || ""}`)));
    if (list.some((b) => b.name === keep)) adderSel.value = keep;
    const b = list.find((x) => x.name === adderSel.value);
    if (!b) return;
    const mine = traces.filter(([n]) => n === b.name).map(([, c]) => c);
    picker.set({ channels: b.channels, enabledList: b.enabled || null, selected: mine, max: MAX_SHOWN - (traces.length - mine.length) });
  }

  function setBoardTraces(name, list) {
    const others = traces.filter(([n]) => n !== name);
    const kept = traces.filter(([n, c]) => n === name && list.includes(c));
    const added = list.filter((c) => !kept.some(([, k]) => k === c)).map((c) => [name, c]);
    traces = [...others, ...kept, ...added].slice(0, MAX_SHOWN);
    saveTraces();
    load(true);
  }

  // ---- the plot ----
  let u = null, data = [[]], series = [], lines = [], builtSig = "";
  function build() {
    if (u) u.destroy();
    const grid = css("--grid"), axis = css("--axis"), text2 = css("--text-2");
    const axisStyle = { stroke: text2, grid: { stroke: grid, width: 1 }, ticks: { stroke: axis, width: 1, size: 4 },
                        font: "11px system-ui, sans-serif", labelFont: "11px system-ui, sans-serif" };
    u = new uPlot({
      width: Math.max(200, plotEl.clientWidth), height: Math.max(160, plotEl.clientHeight),
      padding: [18, 12, 0, 0], legend: { show: false },
      scales: { x: { time: false, range: (_, min, max) => xRange(min, max) }, y: { range: (_, min, max) => yRange(min, max) } },
      axes: [{ ...axisStyle, size: 34, label: "µs",
               values: (_, vals) => { const d = vals.length > 1 ? decimals(Math.abs(vals[1] - vals[0])) : 0; return vals.map((v) => v.toFixed(d).replace(/^-/, "−")); } },
             { ...axisStyle, size: 60, label: t(Y_LABEL[yunit]) }],
      series: [{}, ...series.map((s) => ({ stroke: s.color, width: 1.25, spanGaps: true, points: { show: false } }))],
      cursor: { drag: { x: true, y: false, setScale: false }, points: { show: false } },
      hooks: { draw: [drawLines], setSelect: [selected], setCursor: [moved] },
    }, data, plotEl);
    u.over.addEventListener("dblclick", () => { zoom = null; load(true); });
    u.over.addEventListener("mouseleave", () => showReadout(null));
  }

  function xRange(min, max) {
    if (zoom) return [zoom[0] / 1000, zoom[1] / 1000];
    const r = view && view.range_ns;
    return r && r[1] > r[0] ? [r[0] / 1000, r[1] / 1000] : [Number.isFinite(min) ? min : 0, Number.isFinite(max) ? max : 1];
  }

  function yRange(min, max) {
    if (!Number.isFinite(min) || !Number.isFinite(max)) return [-1, 1];
    const pad = Math.max((max - min) * 0.08, yunit === "mv" ? 0.05 : 2);
    return [min - pad, max + pad];
  }

  // time 0 (the first board's trigger) and, lined up by timestamps, each other board's trigger
  function drawLines(uu) {
    if (!view) return;
    const { ctx, bbox } = uu, pr = uPlot.pxRatio || 1;
    ctx.save();
    ctx.setLineDash([5 * pr, 4 * pr]);
    ctx.lineWidth = pr;
    ctx.strokeStyle = css("--text-2");
    const marks = new Map();
    for (const b of view.boards) if (b.present) marks.set(b.shift_ns, [...(marks.get(b.shift_ns) || []), b.name]);
    for (const [ns, names] of marks) {
      const x = uu.valToPos(ns / 1000, "x", true);
      if (x < bbox.left || x > bbox.left + bbox.width) continue;
      ctx.beginPath(); ctx.moveTo(x, bbox.top); ctx.lineTo(x, bbox.top + bbox.height); ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = `${Math.round(10.5 * pr)}px system-ui, sans-serif`;
      ctx.fillStyle = css("--text-2");
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(marks.size > 1 ? t("trigger {b}", { b: names.join(", ") }) : t("trigger"), x + 3 * pr, bbox.top - 2 * pr);
      ctx.setLineDash([5 * pr, 4 * pr]);
    }
    ctx.restore();
  }

  function selected(uu) {
    const { left, width } = uu.select;          // read before clearing: setSelect changes the same object
    uu.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
    if (width < 4) return;
    const a = uu.posToVal(left, "x") * 1000, b = uu.posToVal(left + width, "x") * 1000;
    if (b - a < 4) return;
    zoom = [a, b];
    load(true);
  }

  function nearest(xs, x) {
    let lo = 0, hi = xs.length - 1;
    if (hi < 0) return -1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (xs[mid] < x) lo = mid; else hi = mid; }
    return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
  }

  function moved(uu) {
    const left = uu.cursor.left;
    if (left === null || left === undefined || left < 0) { showReadout(null); return; }
    showReadout(uu.posToVal(left, "x"));
  }

  function showReadout(x) {
    if (x === null || !lines.length) {
      clear(readout, lines.length ? h("span.text2.small", t("point at the plot for the values; drag across it to zoom, double-click for all")) : null);
      return;
    }
    const unit = yunit === "mv" ? " mV" : "";
    clear(readout, h("span.num.small.comb-time", fmtTime(x * 1000)),
          ...lines.map((l) => {
            const k = nearest(l.xs, x);
            const v = k < 0 || Math.abs(l.xs[k] - x) > l.step * 1.5 ? null : l.ys[k];
            return h("span.comb-val", h("span.swatch", { style: `background:${l.color}` }), `${l.name} `,
                     h("b.num", v === null ? "—" : `${fmtNum(v, yunit === "mv" ? 2 : yunit === "adc" ? 1 : 0)}${unit}`));
          }));
  }

  function show(v) {
    view = v;
    if (v.available) available = v.available;
    events = v.events || 0;
    showChips();
    const by = new Map(v.traces.map((x) => [`${x.board}:${x.ch}`, x]));
    lines = [];
    series = [];
    traces.forEach(([b, c], i) => {
      const x = by.get(`${b}:${c}`);
      if (!x || !x.present) return;
      const n = x.y.length, xs = new Float64Array(n);
      if (x.mode === "raw") for (let k = 0; k < n; k++) xs[k] = (x.x0_ns + k * x.step_ns) / 1000;
      else for (let k = 0; k < n; k++) xs[k] = (x.x0_ns + (Math.floor(k / 2) + (k % 2 ? 0.75 : 0.25)) * x.step_ns) / 1000;
      lines.push({ name: `${b}·ch${c}`, color: colorOf(i), xs, ys: x.y, step: x.step_ns / 1000 });
      series.push({ color: colorOf(i) });
    });
    data = lines.length ? uPlot.join(lines.map((l) => [l.xs, l.ys])) : [[]];
    const sig = JSON.stringify([series.map((s) => s.color), yunit]);    // a new plot only for other traces or units:
    if (!u || sig !== builtSig) { builtSig = sig; build(); }             // a live refresh must not end a drag
    else u.setData(data, true);
    const missing = traces.length && !lines.length;
    empty.classList.toggle("hidden", !missing && traces.length > 0);
    setText(empty, !traces.length ? t("no channel chosen: press Channels…") : missing ? t("no waveform of these channels yet") : "");
    showInfo();
    showReadout(null);
    showControls();
  }

  function showInfo() {
    const v = view;
    if (!v) { setText(info, ""); return; }
    const parts = [];
    const present = v.boards.filter((b) => b.present);
    if (v.source === "file") {
      parts.push(t("Run {run}: event {k} of {n} of {b}, and the same trigger ID on the other boards", { run: v.run || "?", k: fmtInt(v.event), n: fmtInt(v.events), b: v.board }));
      if (v.missing && v.missing.length) parts.push(t("not found on {b}", { b: v.missing.join(", ") }));
    } else if (v.matched) {
      parts.push(v.running ? t("Live: one moment on every board, from the run going") : t("Live: one moment on every board, from the last run"));
    } else {
      parts.push(t("Live: each board's newest event (no matched moment yet): they can be different triggers"));
    }
    if (present.length > 1) {
      if (v.same_ids) parts.push(t("trigger ID {id} on every board", { id: fmtInt(present[0].trigger_id) }));
      else parts.push(t("trigger IDs {ids}", { ids: present.map((b) => `${b.name} ${fmtInt(b.trigger_id)}`).join(", ") }));
      parts.push(t("timestamps differ by {ns}", { ns: fmtTime(v.spread_ns || 0) }));
      if (v.source === "live" && !v.chained) parts.push(t("the boards do not share a clock (no clock chain): their timestamps are not one clock"));
    } else if (present.length === 1) {
      parts.push(t("trigger ID {id}", { id: fmtInt(present[0].trigger_id) }));
    }
    parts.push(present.map((b) => `${b.name} ${fmtNum(b.sample_ns, 0)} ns · ${fmtTime(b.window_ns[0])} … ${fmtTime(b.window_ns[1])}`).join(" | "));
    setText(info, parts.join(" · "));
  }

  function showControls() {
    const src = getSource();
    evGroup.classList.toggle("hidden", src.source !== "file");
    evInput.max = String(Math.max(0, events - 1));
    setText(evCount, t("of {n}", { n: fmtInt(events) }));
    resetBtn.disabled = !zoom;
  }

  function setEvent(k) {
    eventNo = Math.max(0, Math.min(Math.max(0, events - 1), k));
    evInput.value = String(eventNo);
    load(true);
  }

  // ---- data ----
  let seq = null, busy = false, again = false, gone = false;
  async function load(force) {
    if (gone || !active) return;
    if (busy) { again = again || force; return; }
    ensureTraces();
    showChips();
    if (!traces.length) { show({ boards: [], traces: [], range_ns: [0, 1], source: getSource().source }); return; }
    busy = true;
    try {
      const src = getSource();
      const bins = Math.min(1500, Math.max(200, Math.round(plotEl.clientWidth * (window.devicePixelRatio || 1) / 2)));
      const q = { traces: traces.map(([b, c]) => `${b}:${c}`).join(","), bins, y: yunit, align,
                  t0: zoom ? zoom[0] : undefined, t1: zoom ? zoom[1] : undefined };
      let v;
      if (src.source === "file" && src.file) v = await get("/api/files/combined", { ...q, path: src.file, event: eventNo });
      else {
        v = await get("/api/live/combined", { ...q, since: force ? undefined : seq });
        if (v.same) return;
        seq = v.seq;
      }
      if (!gone) show(v);
    } catch (err) {
      if (getSource().source === "file" || force) fail(err);
    } finally {
      busy = false;
      if (again) { again = false; load(true); }
    }
  }

  const ro = new ResizeObserver(() => { if (u && plotEl.clientWidth > 20 && plotEl.clientHeight > 20) u.setSize({ width: plotEl.clientWidth, height: plotEl.clientHeight }); });
  ro.observe(plotEl);

  return {
    el,
    setActive(on) {
      active = on;
      el.classList.toggle("hidden", !on);
      if (on) { requestAnimationFrame(() => load(true)); }
    },
    sourceChanged() { eventNo = 0; evInput.value = "0"; zoom = null; available = []; seq = null; load(true); },
    load,
    update(type) {
      if (!active) return;
      if (type === "config" && getSource().source === "live") { available = []; if (!adder.classList.contains("hidden")) fillAdder(); }
    },
    restyle() { builtSig = ""; if (active) build(); },
    unmount() { gone = true; ro.disconnect(); if (u) u.destroy(); },
  };
}

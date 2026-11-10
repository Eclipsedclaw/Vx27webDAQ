// Plots, drawn with uPlot (static/vendor/uplot). One series per plot, so no legend: the card title names it.
// Waveforms come thinned by the server (a min/max pair per bin, in the order they occur: spikes stay).
import { clear, h, setText } from "./ui.js";
import { t } from "./i18n.js";

// the default ADC range and sample time, until a board says its own (setBoard)
export const ADC_MAX = 65535;

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function alpha(color, a) {
  const m = /^#([0-9a-f]{6})$/i.exec(color);
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function tick(v) {
  return Math.abs(v) >= 10000 ? `${Math.round(v / 1000)}K` : String(Math.round(v));
}

// whole-number ticks only: samples, seconds and ADC counts are counts
const INCRS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000,
               100000, 200000, 500000, 1e6];
// waveform x ticks, in samples
const WAVE_INCRS = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1e6];
export const SAMPLE_NS = 8;              // 125 MS/s (V2740); a V2730 has 2 ns

const MINUS = "−";
// a time on the x axis: µs once the step or the times are large, with just the decimals the step needs
function tickTime(ns, stepNs, micro) {
  if (!micro) return `${ns < 0 ? MINUS : ""}${Math.abs(Math.round(ns))} ns`;
  let d = 0;
  while (d < 3 && Math.abs(Math.round((stepNs / 1000) * 10 ** d) - (stepNs / 1000) * 10 ** d) > 1e-9) d++;
  return `${ns < 0 ? MINUS : ""}${Math.abs(ns / 1000).toFixed(d)} µs`;
}

// a time in full detail (whole ns: samples are 2 or 8 ns apart): "−12 ns", "3.988 µs"
export function fmtTime(ns) {
  const sign = ns < 0 ? MINUS : "", a = Math.abs(Math.round(ns));
  return a < 1000 ? `${sign}${a} ns` : `${sign}${(a / 1000).toFixed(3)} µs`;
}

// x of every value the server sent: raw samples, or a min/max pair per bin at the bin's centre
export function toXY(data) {
  const n = data.y.length, xs = new Float64Array(n);
  if (data.mode === "raw") for (let i = 0; i < n; i++) xs[i] = data.x0 + i;
  else for (let i = 0; i < n; i++) xs[i] = data.x0 + (Math.floor(i / 2) + 0.5) * data.step;
  return [xs, Float64Array.from(data.y)];
}

function baseOptions(el, extra) {
  const grid = css("--grid"), axis = css("--axis"), text2 = css("--text-2");
  const axisStyle = { stroke: text2, grid: { stroke: grid, width: 1 }, ticks: { stroke: axis, width: 1, size: 4 },
                      font: "11px system-ui, sans-serif", labelFont: "11px system-ui, sans-serif" };
  return {
    width: Math.max(120, el.clientWidth), height: Math.max(80, el.clientHeight),
    padding: [10, 10, 0, 0], legend: { show: false },
    axes: [{ ...axisStyle, size: 30, incrs: INCRS, values: (u, vals) => vals.map(tick) },
           { ...axisStyle, size: 50, incrs: INCRS }],
    ...extra,
  };
}

export class WavePlot {
  // onZoom(x0, x1): the user dragged over a range (mouse mode "zoom"); onMeasure(x0, x1): the same in mouse
  // mode "measure"; onWheel(x0, x1): the mouse wheel zoomed the time axis to that window; onReset(): back to
  // the whole waveform; readout: an element for the value under the pointer.
  // The x axis has two lines: the sample, and its time from the trigger (the pre-trigger sample of the settings
  // the data were taken with), or from the first sample when those settings are not known. setBoard() gives the
  // board's sample time and ADC range.
  constructor(el, { onZoom, onMeasure, onWheel, onReset, readout, sync } = {}) {
    this.sync = sync || null;       // plots with the same key share the cursor: one dashed line on all of them
    this.el = el;
    this.onZoom = onZoom;
    this.onMeasure = onMeasure;
    this.onWheel = onWheel;
    this.onReset = onReset;
    this.readout = readout;
    this.t0 = null;         // the trigger's sample: time 0 (null: unknown, time counts from the first sample)
    this.sampleNs = SAMPLE_NS;
    this.adcMax = ADC_MAX;
    this.range = { mode: "auto", lo: 8000, hi: 9000, half: 200 };
    this.baseline = null;
    this.full = 0;          // the full waveform length, so a short waveform shows what is missing
    this.nsamps = 0;
    this.zoomed = null;     // [x0, x1] after a drag, until reset
    this.mouse = "zoom";    // what a drag does: "zoom" or "measure"
    this.span = null;       // [x0, x1): the range measured, shaded
    this.show = { trigger: false, threshold: false };     // the dashed reference lines to draw
    this.ref = { trigger_at: null, threshold: null, threshold_used: false };
    this.data = [[], []];
    this.unit = "adc";       // the y axis: ADC counts, or mV from the baseline (setUnit)
    this.k = 1;              // mV per count
    this.view = null;
    this.build();
    this.ro = new ResizeObserver(() => this.fit());
    this.ro.observe(el);
  }

  build() {
    if (this.u) this.u.destroy();
    const self = this, accent = css("--accent"), shade = css("--shade");
    const ink = { line: css("--text-2"), faint: css("--muted"), surface: css("--surface"), accent };
    const opts = baseOptions(this.el, {
      scales: {
        x: { time: false, range: (u, min, max) => self.xRange(min, max) },
        y: { range: (u, min, max) => self.yRange(min, max) },
      },
      padding: [18, 12, 0, 0],            // room above the plot for the trigger line's label
      series: [{}, { stroke: accent, width: 1.25, points: { show: false } }],
      cursor: { drag: { x: true, y: false, setScale: false }, points: { show: false }, bind: { dblclick: () => null },
                ...(this.sync ? { sync: { key: this.sync, setSeries: false } } : {}) },
      hooks: {
        drawClear: [(u) => { self.drawMissing(u, shade); self.drawSpan(u, ink); }],
        draw: [(u) => self.drawLines(u, ink)],
        setSelect: [(u) => self.selected(u)],
        setCursor: [(u) => self.moved(u)],
      },
    });
    // x: the sample on the first line, its time on the second; ticks where the time is round
    Object.assign(opts.axes[0], {
      size: 40, space: 52, incrs: WAVE_INCRS,
      splits: (u, i, min, max, incr) => {
        const t0 = this.t0 ?? 0, out = [];
        for (let v = t0 + Math.ceil((min - t0) / incr - 1e-9) * incr; v <= max + 1e-9; v += incr) out.push(v);
        return out;
      },
      values: (u, splits, i, space, incr) => {
        const t0 = this.t0 ?? 0, ns = this.sampleNs, stepNs = incr * ns;
        const micro = stepNs >= 1000 || splits.some((v) => Math.abs(v - t0) * ns >= 1000);
        return splits.map((v) => `${Math.round(v)}\n${tickTime((v - t0) * ns, stepNs, micro)}`);
      },
    });
    this.u = new uPlot(opts, this.data, this.el);
    this.u.over.addEventListener("dblclick", () => this.resetZoom(true));
    this.u.over.addEventListener("mouseleave", () => this.readout && setText(this.readout, ""));
    this.u.over.addEventListener("wheel", (e) => this.wheel(e), { passive: false });
    this.curLabel = h("div.cur-label.hidden");          // the dashed cursor line's label: sample and time
    this.u.over.appendChild(this.curLabel);
  }

  // the mouse wheel zooms the time axis around the pointer (all plots: the panel sets the same window on each)
  wheel(e) {
    if (!this.onWheel || !this.data[0].length) return;
    e.preventDefault();
    const u = this.u, end = Math.max(this.full || 0, this.nsamps || 0, 1);
    const px = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1);
    const f = Math.min(2, Math.max(0.5, Math.pow(1.0022, px)));          // > 1: out, < 1: in (1.25 a notch)
    const min = u.scales.x.min, max = u.scales.x.max;
    const at = Math.min(max, Math.max(min, u.posToVal(e.clientX - u.over.getBoundingClientRect().left, "x")));
    let x0 = at - (at - min) * f, x1 = at + (max - at) * f;
    if (x1 - x0 < 10) { x0 = at - 5; x1 = at + 5; }                     // 10 samples at the most
    x0 = Math.max(0, Math.floor(x0));
    x1 = Math.min(end, Math.ceil(x1));
    if (x0 <= 0 && x1 >= end) {                                          // all of it again: not zoomed
      if (this.zoomed) { this.resetZoom(false); this.onWheel(null, null); }
      return;
    }
    if (this.zoomed && this.zoomed[0] === x0 && this.zoomed[1] === x1) return;
    this.onWheel(x0, x1);
  }

  xRange(min, max) {
    if (this.zoomed) return this.zoomed;
    const end = Math.max(this.full || 0, this.nsamps || 0, Number.isFinite(max) ? max : 1);
    return [0, Math.max(end, 1)];
  }

  mv() { return this.unit === "mv" && this.baseline !== null; }
  yv(adc) { return this.mv() ? (adc - this.baseline) * this.k : adc; }    // an ADC value on the y axis

  thresholdLevel() {
    const r = this.ref;
    return this.show.threshold && r.threshold !== null && r.threshold !== undefined && this.baseline !== null
      ? this.yv(this.baseline + r.threshold) : null;
  }

  yRange(min, max) {
    const r = this.range;
    if (r.mode === "full") return [this.yv(0), this.yv(this.adcMax)];
    if (r.mode === "custom") return [this.yv(r.lo), this.yv(r.hi)];
    if (r.mode === "baseline" && this.baseline !== null) return [this.yv(this.baseline - r.half), this.yv(this.baseline + r.half)];
    if (!Number.isFinite(min) || !Number.isFinite(max)) return [this.yv(0), this.yv(this.adcMax)];
    const level = this.thresholdLevel();            // Auto keeps the threshold in sight when it is shown
    if (level !== null) { min = Math.min(min, level); max = Math.max(max, level); }
    if (this.mv()) {
      const pad = Math.max((max - min) * 0.08, 0.05);
      return [min - pad, max + pad];
    }
    const pad = Math.max((max - min) * 0.08, 2);
    return [Math.floor(min - pad), Math.ceil(max + pad)];
  }

  drawMissing(u, color) {                 // a short waveform: the samples it lacks, shaded
    if (!this.full || this.nsamps >= this.full) return;
    const { ctx, bbox } = u;
    const a = Math.max(bbox.left, u.valToPos(this.nsamps, "x", true));
    const b = Math.min(bbox.left + bbox.width, u.valToPos(this.full, "x", true));
    if (b > a) {
      ctx.save();
      ctx.fillStyle = color;
      ctx.fillRect(a, bbox.top, b - a, bbox.height);
      ctx.restore();
    }
  }

  drawSpan(u, ink) {                      // the measured range: an accent wash with hairline edges, under the data
    if (!this.span) return;
    const { ctx, bbox } = u, pr = uPlot.pxRatio || 1;
    const left = bbox.left, right = bbox.left + bbox.width;
    const a = u.valToPos(this.span[0], "x", true), b = u.valToPos(this.span[1], "x", true);
    if (b < left || a > right) return;
    ctx.save();
    ctx.fillStyle = alpha(ink.accent, 0.12);
    ctx.fillRect(Math.max(a, left), bbox.top, Math.min(b, right) - Math.max(a, left), bbox.height);
    ctx.strokeStyle = ink.accent;
    ctx.lineWidth = pr;
    for (const x of [a, b]) {
      if (x >= left && x <= right) { ctx.beginPath(); ctx.moveTo(x, bbox.top); ctx.lineTo(x, bbox.top + bbox.height); ctx.stroke(); }
    }
    ctx.restore();
  }

  // the dashed trigger-time (vertical) and threshold (horizontal) lines, over the data with a surface halo, and
  // a small label in text ink; a threshold outside the y range gets an arrow label at that edge
  drawLines(u, ink) {
    const r = this.ref;
    const { ctx, bbox } = u, pr = uPlot.pxRatio || 1;
    const left = bbox.left, right = bbox.left + bbox.width, top = bbox.top, bottom = bbox.top + bbox.height;
    const dashed = (x0, y0, x1, y1, color) => {
      ctx.setLineDash([]);
      ctx.strokeStyle = ink.surface;
      ctx.lineWidth = 3.5 * pr;
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      ctx.setLineDash([5 * pr, 4 * pr]);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5 * pr;
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
    };
    const label = (text, x, y, alignRight, color) => {
      ctx.setLineDash([]);
      ctx.font = `${Math.round(10.5 * pr)}px system-ui, sans-serif`;
      const w = ctx.measureText(text).width, hgt = 14 * pr, pad = 3 * pr;
      const x0 = alignRight ? x - w - 2 * pad : x;
      ctx.fillStyle = alpha(ink.surface, 0.9);
      ctx.fillRect(x0, y, w + 2 * pad, hgt);
      ctx.fillStyle = color;
      ctx.textAlign = "left";             // uPlot leaves its axes' alignment on the canvas
      ctx.textBaseline = "middle";
      ctx.fillText(text, x0 + pad, y + hgt / 2);
    };
    ctx.save();
    if (this.show.trigger && r.trigger_at !== null && r.trigger_at !== undefined) {
      const x = u.valToPos(r.trigger_at, "x", true);
      if (x >= left && x <= right) {
        dashed(x, top, x, bottom, ink.line);
        const room = right - x > 90 * pr;     // the label above the plot, beside the line's top
        label(t("trigger"), room ? x + 3 * pr : x - 3 * pr, top - 16 * pr, !room, ink.line);
      }
    }
    if (this.show.threshold && r.threshold !== null && r.threshold !== undefined && this.baseline !== null) {
      const level = this.yv(this.baseline + r.threshold);
      const y = u.valToPos(level, "y", true);
      const color = r.threshold_used ? ink.line : ink.faint;
      const text = r.threshold_used ? t("threshold {n}", { n: r.threshold > 0 ? `+${r.threshold}` : r.threshold })
                                    : t("threshold {n} · not used", { n: r.threshold > 0 ? `+${r.threshold}` : r.threshold });
      if (y >= top && y <= bottom) {
        dashed(left, y, right, y, color);
        label(text, right - 4 * pr, y < top + 22 * pr ? y + 4 * pr : y - 18 * pr, true, color);
      } else {
        label(`${text} ${y > bottom ? "↓" : "↑"}`, right - 4 * pr, y > bottom ? bottom - 18 * pr : top + 4 * pr, true, ink.faint);
      }
    }
    ctx.restore();
  }

  selected(u) {
    const s = u.select;
    if (s.width < 4) return;
    const x0 = Math.max(0, Math.floor(u.posToVal(s.left, "x")));
    const x1 = Math.ceil(u.posToVal(s.left + s.width, "x"));
    u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
    if (this.mouse === "measure") {
      if (x1 - x0 >= 1 && this.onMeasure) this.onMeasure(x0, x1);
      return;
    }
    if (x1 - x0 < 4) return;
    this.zoomed = [x0, x1];
    u.setScale("x", { min: x0, max: x1 });
    if (this.onZoom) this.onZoom(x0, x1);
  }

  moved(u) {
    this.label(u);
    if (!this.readout) return;
    const i = u.cursor.idx;
    if (i === null || i === undefined || !this.data[0].length) { setText(this.readout, ""); return; }
    const x = Math.round(this.data[0][i]), time = fmtTime((x - (this.t0 ?? 0)) * this.sampleNs);
    const y = this.mv() ? `${this.data[1][i].toFixed(2)} mV` : String(Math.round(this.data[1][i]));
    clear(this.readout, h("span.rd-long", this.mv() ? t("sample {x} · {time} · {y}", { x, time, y }) : t("sample {x} · {time} · ADC {y}", { x, time, y })),     // a narrow plot: the short one
          h("span.rd-short", `${x} · ${time} · ${y}`));
  }

  // the label at the cursor line (on every plot of the sync group): the sample and its time from the trigger
  label(u) {
    const left = u.cursor.left;
    if (left === null || left === undefined || left < 0 || !this.data[0].length) { this.curLabel.classList.add("hidden"); return; }
    const x = Math.round(u.posToVal(left, "x"));
    setText(this.curLabel, t("sample {n} · {time}", { n: x, time: fmtTime((x - (this.t0 ?? 0)) * this.sampleNs) }));
    this.curLabel.classList.remove("hidden");
    const w = this.curLabel.offsetWidth, room = u.over.clientWidth;
    this.curLabel.style.left = `${Math.max(0, Math.min(room - w, left - w / 2))}px`;
  }

  resetZoom(tell) {
    if (!this.zoomed) return;
    this.zoomed = null;
    this.u.setData(this.data, true);
    if (tell && this.onReset) this.onReset();
  }

  setRange(range) {
    this.range = { ...range };
    this.refresh();
  }

  setShow(show) { this.show = { ...show }; this.refresh(); }
  setBoard({ sample_ns, adc_max } = {}) {
    const ns = Number(sample_ns) || SAMPLE_NS, max = Number(adc_max) || ADC_MAX;
    if (ns === this.sampleNs && max === this.adcMax) return;
    this.sampleNs = ns;
    this.adcMax = max;
    this.refresh();
  }
  setMouse(mode) { this.mouse = mode; this.el.classList.toggle("measuring", mode === "measure"); }
  setUnit(unit, k) {
    if (unit === this.unit && k === this.k) return;
    this.unit = unit;
    this.k = Number(k) || 1;
    this.setView(this.view);
  }
  setSpan(span) { this.span = span ? [...span] : null; this.u.redraw(false); }

  refresh() {                     // the scales again (the y range may change), then a redraw
    this.u.setData(this.data, true);
    if (this.zoomed) this.u.setScale("x", { min: this.zoomed[0], max: this.zoomed[1] });
  }

  // view: one channel from the server (data, nsamps, full_length, baseline, trigger_at, threshold), or null
  setView(view) {
    this.view = view;
    if (!view || !view.data) {
      this.data = [[], []];
      this.baseline = null;
      this.nsamps = this.full = 0;
    } else {
      this.data = toXY(view.data);
      this.baseline = view.baseline ?? null;
      if (this.mv()) this.data[1] = this.data[1].map((v) => (v - this.baseline) * this.k);
      this.nsamps = view.nsamps || 0;
      this.full = view.full_length || 0;
    }
    this.ref = { trigger_at: view ? view.trigger_at ?? null : null, threshold: view ? view.threshold ?? null : null,
                 threshold_used: Boolean(view && view.threshold_used) };
    this.t0 = this.ref.trigger_at;
    this.refresh();
  }

  fit() {
    const w = this.el.clientWidth, hgt = this.el.clientHeight;
    if (w > 20 && hgt > 20 && (w !== this.u.width || hgt !== this.u.height)) this.u.setSize({ width: w, height: hgt });
  }

  restyle() { this.build(); }

  destroy() {
    this.ro.disconnect();
    this.u.destroy();
  }
}

// A plain x-y plot of one series: the FFT (MHz, dBFS) or the samples histogram (ADC code, entries; drawn as steps).
// A drag zooms the x axis, and the zoom stays while the data are refreshed, until a double-click.
export class XYPlot {
  constructor(el, { readout } = {}) {
    this.el = el;
    this.readout = readout;
    this.opts = { xLabel: "", yLabel: "", stepped: false, xFmt: (v) => String(v), yFmt: (v) => String(v) };
    this.data = [[], []];
    this.sig = "";
    this.zoomed = null;      // [x0, x1] after a drag
    this.ro = new ResizeObserver(() => this.fit());
    this.ro.observe(el);
  }

  build() {
    if (this.u) this.u.destroy();
    const accent = css("--accent"), o = this.opts, self = this;
    const opts = baseOptions(this.el, {
      scales: { x: { time: false } },
      series: [{}, { stroke: accent, width: 1.25, fill: o.stepped ? alpha(accent, 0.15) : undefined, points: { show: false },
                     paths: o.stepped && uPlot.paths && uPlot.paths.stepped ? uPlot.paths.stepped({ align: 1 }) : undefined }],
      cursor: { drag: { x: true, y: false, setScale: false }, points: { show: false }, bind: { dblclick: () => null } },
      hooks: { setCursor: [(u) => self.moved(u)], setSelect: [(u) => self.selected(u)] },
    });
    opts.axes[0].label = o.xLabel;
    opts.axes[1].label = o.yLabel;
    opts.axes[0].incrs = undefined;
    opts.axes[1].incrs = undefined;
    opts.axes[0].values = undefined;
    opts.axes[0].size = 40;
    opts.axes[1].size = 56;
    this.u = new uPlot(opts, this.data, this.el);
    this.keepZoom();
    this.u.over.addEventListener("mouseleave", () => this.readout && setText(this.readout, ""));
    this.u.over.addEventListener("dblclick", () => this.resetZoom());
  }

  selected(u) {
    const s = u.select;
    if (s.width < 4) return;
    const x0 = u.posToVal(s.left, "x"), x1 = u.posToVal(s.left + s.width, "x");
    u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
    this.zoomed = [x0, x1];
    this.keepZoom();
  }

  keepZoom() { if (this.zoomed && this.u) this.u.setScale("x", { min: this.zoomed[0], max: this.zoomed[1] }); }

  resetZoom() {
    if (!this.zoomed) return;
    this.zoomed = null;
    if (this.u) this.u.setData(this.data, true);
  }

  set(xs, ys, opts = {}) {
    this.opts = { ...this.opts, ...opts };
    this.data = [Array.from(xs), Array.from(ys)];
    const sig = JSON.stringify([this.opts.xLabel, this.opts.yLabel, this.opts.stepped]);
    if (!this.u || sig !== this.sig) {
      if (sig !== this.sig) this.zoomed = null;          // another kind of plot: another x axis
      this.sig = sig;
      this.build();
    } else {
      this.u.setData(this.data, true);
      this.keepZoom();
    }
  }

  moved(u) {
    if (!this.readout) return;
    const i = u.cursor.idx;
    if (i === null || i === undefined || !this.data[0].length) { setText(this.readout, ""); return; }
    setText(this.readout, `${this.opts.xFmt(this.data[0][i])} · ${this.opts.yFmt(this.data[1][i])}`);
  }

  fit() {
    if (!this.u) return;
    const w = this.el.clientWidth, hgt = this.el.clientHeight;
    if (w > 20 && hgt > 20 && (w !== this.u.width || hgt !== this.u.height)) this.u.setSize({ width: w, height: hgt });
  }

  restyle() { if (this.u) this.build(); }

  destroy() { this.ro.disconnect(); if (this.u) this.u.destroy(); }
}

export class LinePlot {
  constructor(el, { yLabel = "", xLabel = "" } = {}) {
    this.el = el;
    this.labels = { yLabel, xLabel };
    this.data = [[], []];
    this.build();
    this.ro = new ResizeObserver(() => this.fit());
    this.ro.observe(el);
  }

  build() {
    if (this.u) this.u.destroy();
    const accent = css("--accent");
    const opts = baseOptions(this.el, {
      scales: { x: { time: false }, y: { range: (u, min, max) => [0, Math.max(1, Math.ceil((max || 1) * 1.15))] } },
      series: [{}, { stroke: accent, width: 2, fill: alpha(accent, 0.1), points: { show: false } }],
      cursor: { drag: { x: false, y: false }, points: { size: 8, fill: accent, stroke: css("--surface"), width: 2 } },
    });
    opts.axes[1].incrs = undefined;           // MB/s may be fractional
    opts.axes[0].label = this.labels.xLabel;
    opts.axes[1].label = this.labels.yLabel;
    opts.axes[0].size = 40;
    opts.axes[1].size = 56;
    this.u = new uPlot(opts, this.data, this.el);
  }

  setPoints(points) {
    this.data = [points.map((p) => p[0]), points.map((p) => p[1])];
    this.u.setData(this.data, true);
  }

  fit() {
    const w = this.el.clientWidth, hgt = this.el.clientHeight;
    if (w > 20 && hgt > 20 && (w !== this.u.width || hgt !== this.u.height)) this.u.setSize({ width: w, height: hgt });
  }

  restyle(labels) {
    if (labels) this.labels = labels;
    this.build();
  }

  destroy() {
    this.ro.disconnect();
    this.u.destroy();
  }
}

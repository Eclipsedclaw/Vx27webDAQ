// The waveform panel of the Run page: the channels of one board, live from the run or from a .raw or .root file.
// A board has 32 (V2730) or 64 (V2740) channels: the panel shows a page of 8 or 16 of them (0–7, 8–15, …). Two rows
// of controls above the plots: what to show (source, board, channels, event), then how (ADC range, the dashed
// trigger and threshold lines, what a drag does). The plots share the x window: a drag or the mouse wheel zooms all
// of them to the same samples, which the server sends in full detail; in Measure mode a drag marks the same range
// on every plot, and the table under the plots gives its numbers per channel (from all the samples). The x axes
// give each sample's time from the trigger too (the board's own sample time: 2 ns on a V2730, 8 ns on a V2740).
import { get } from "./api.js";
import { T, t } from "./i18n.js";
import { ADC_MAX, SAMPLE_NS, WavePlot, fmtTime } from "./plots.js";
import { basename, chip, chooseFile, clear, fail, fmtInt, fmtNum, h, icon, segmented, setText, splitter, store } from "./ui.js";

const RANGES = [["auto", T("Auto")], ["full", T("Full scale")], ["custom", T("Custom")], ["baseline", T("Baseline ± N")]];
const MAX_CARDS = 16;

// a timestamp (8 ns ticks since the start of the run): "1.234 µs" when small, "12.461359872 s" when large
function fmtStamp(ticks) {
  const ns = Number(ticks) * 8;
  return ns < 1e6 ? fmtTime(ns) : `${(ns / 1e9).toFixed(9).replace(/0{1,3}$/, "")} s`;
}

function loadJSON(key, fallback) {
  try { return { ...fallback, ...(JSON.parse(store(key) || "null") || {}) }; } catch (e) { return { ...fallback }; }
}

// onBoard(name): called whenever the board shown changes (the Run page marks its row)
export function wavePanel(app, { onBoard } = {}) {
  const panel = h("section.wave-panel", { "aria-label": t("Waveforms") });
  const sub = h("div.wave-sub");

  // ---- what to show ----
  let source = store("wave.source") === "file" && store("wave.file") ? "file" : "live";
  let file = store("wave.file") || "", fileInfo = null, eventNo = 0, paused = false, zoom = null, enlarged = null;
  let boardInfo = { sample_ns: SAMPLE_NS, adc_max: ADC_MAX, pre_trigger: null, record_length: 0, model: "" };
  let range = loadJSON("wave.range", { mode: "auto", lo: 8000, hi: 9000, half: 200 });
  let lines = loadJSON("wave.lines", { trigger: false, threshold: false });
  let mouse = store("wave.mouse") === "measure" ? "measure" : "zoom";
  let perPage = store("wave.per") === "16" ? 16 : 8;
  let group = Number(store("wave.group") || 0);
  let span = null;
  let nChannels = 64, t0 = null, eventFacts = null;
  const sourceSeg = segmented([["live", t("Live")], ["file", t("File")]], source, (v) => setSource(v), { label: t("Source") });
  const fileName = h("span.chip", { title: "" });
  const openBtn = h("button.btn.btn-sm", { type: "button", onclick: pickFile }, icon("folder"), t("Open file…"));
  const boardSel = h("select.input", { "aria-label": t("Board"), onchange: () => boardChanged() });
  const groupSel = h("select.input", { "aria-label": t("Channels"), onchange: () => { group = Number(groupSel.value); store("wave.group", group); resetZoom(); load(true); showCards(); } });
  const perSeg = segmented([["8", "8"], ["16", "16"]], String(perPage), (v) => { perPage = Number(v); store("wave.per", v); group = 0; fillGroups(); showCards(); resetZoom(); load(true); },
                           { label: t("Channels per page") });
  perSeg.title = t("How many channels are shown at once");
  const evInput = h("input.input.num", { type: "number", min: "0", value: "0", "aria-label": t("Event"),
                                         onchange: () => setEvent(Number(evInput.value) || 0) });
  const evCount = h("span.text2.small.num");
  const evGroup = h("div.group", h("span.label", t("Event")),
                    h("div.stepper", h("button.btn.btn-sm.btn-icon", { type: "button", "aria-label": t("Previous event"), onclick: () => setEvent(eventNo - 1) }, icon("left")),
                      evInput, h("button.btn.btn-sm.btn-icon", { type: "button", "aria-label": t("Next event"), onclick: () => setEvent(eventNo + 1) }, icon("right"))),
                    evCount);
  const pauseBtn = h("button.btn.btn-sm", { type: "button", onclick: () => { paused = !paused; pauseBtn.classList.toggle("on", paused); if (!paused) load(true); } },
                     icon("pause"), t("Pause"));
  const allBtn = h("button.btn.btn-sm", { type: "button", onclick: () => enlarge(null) }, icon("shrink"), t("Show all channels"));

  // ---- how to show it ----
  const rangeSeg = segmented(RANGES.map(([k, v]) => [k, t(v)]), range.mode, (v) => setRange({ mode: v }), { label: t("ADC range") });
  rangeSeg.title = t("The y axis of all plots: fitted to each waveform, the board's whole ADC scale (0–16383 on a V2730, 0–65535 on a V2740), a range you type, or each channel's baseline (the mean of its first 16 samples) ± N counts");
  const lo = h("input.input.num", { type: "number", min: "0", max: "65535", value: String(range.lo), "aria-label": t("from"),
                                    onchange: () => setRange({ lo: Number(lo.value) }) });
  const hi = h("input.input.num", { type: "number", min: "0", max: "65535", value: String(range.hi), "aria-label": t("to"),
                                    onchange: () => setRange({ hi: Number(hi.value) }) });
  const half = h("input.input.num", { type: "number", min: "1", max: "65535", value: String(range.half), "aria-label": "± N",
                                      title: t("Half the height of the y axis, in ADC counts, around each channel's baseline"),
                                      onchange: () => setRange({ half: Number(half.value) }) });
  const customBox = h("div.group", lo, h("span.text2", "–"), hi);
  const halfBox = h("div.group", h("span.text2", "±"), half, h("span.text2.small", t("counts")));
  const trigLineBtn = h("button.btn.btn-sm", { type: "button", "aria-pressed": "false", onclick: () => setLines({ trigger: !lines.trigger }),
                                               title: t("A dashed line at the trigger: the pre-trigger sample of the settings the data were taken with") },
                        icon("vdash"), t("Trigger time"));
  const thrLineBtn = h("button.btn.btn-sm", { type: "button", "aria-pressed": "false", onclick: () => setLines({ threshold: !lines.threshold }),
                                              title: t("A dashed line at each channel's threshold: its baseline − the threshold for negative pulses, + for positive ones; used when the boards trigger on their channels") },
                       icon("hdash"), t("Threshold"));
  const mouseSeg = segmented([["zoom", t("Zoom")], ["measure", t("Measure")]], mouse, (v) => setMouse(v), { label: t("Mouse") });
  mouseSeg.title = t("What a drag across a plot does: zoom all plots to that range, or measure that range on all plots. The mouse wheel over a plot zooms the time axis in either mode.");
  const resetBtn = h("button.btn.btn-sm", { type: "button", onclick: () => { resetZoom(); load(true); },
                                            title: t("Every plot back to the whole waveform and the ADC range chosen here") }, icon("reset"), t("Reset zoom"));
  panel.appendChild(h("div.toolbar.two-rows",
    h("div.toolbar-row",
      h("h2.card-title", t("Waveforms")),
      h("div.group", sourceSeg, fileName, openBtn),
      h("div.group", h("span.label", t("Board")), boardSel),
      h("div.group", h("span.label", t("Channels")), groupSel, perSeg),
      evGroup, h("span.spacer"), pauseBtn, allBtn),
    h("div.toolbar-row",
      h("div.group", h("span.label", t("ADC range")), rangeSeg, customBox, halfBox),
      h("div.group", h("span.label", t("Lines")), trigLineBtn, thrLineBtn),
      h("div.group", h("span.label", t("Mouse")), mouseSeg),
      h("span.spacer"), resetBtn)));
  panel.appendChild(sub);

  // ---- the plots, and the measurement under them ----
  const wrap = h("div.wave-wrap");
  const grid = h("div.wave-grid");
  wrap.appendChild(grid);
  const spanText = h("span.text2.small.num");
  const measureRows = h("tbody");
  const measureBox = h("div.card.fill.measure-box",
    h("div.card-head", h("h2.card-title", t("Measure")), spanText, h("span.spacer"),
      h("button.btn.btn-sm", { type: "button", onclick: () => setSpan(null) }, icon("reset"), t("Clear"))),
    h("div.card-body.flush.table-wrap",
      h("table.table.measure-table",
        h("thead", h("tr", h("th", t("Channel")),
                     h("th.n", { title: t("Lowest sample in the range (ADC)") }, t("min")),
                     h("th.n", { title: t("Highest sample in the range (ADC)") }, t("max")),
                     h("th.n", { title: t("Mean of the samples in the range (ADC)") }, t("mean")),
                     h("th.n", { title: t("Standard deviation of the samples in the range (ADC)") }, "σ"),
                     h("th.n", { title: t("The sample farthest from the baseline (the mean of the first 16 samples), minus the baseline (ADC)") }, t("peak")),
                     h("th.n", { title: t("Where the peak is: the sample, and its time from the trigger") }, t("at")),
                     h("th.n", { title: t("Sum of (sample − baseline) over the range, times the sample time (ADC·ns)") }, t("area (ADC·ns)")))),
        measureRows)));
  const body = splitter(wrap, measureBox, { key: "wave.measure", vertical: true, initial: 0.64, min: 110 });
  body.classList.add("wave-body");
  body.showSecond(false);
  panel.appendChild(body);
  const cards = [];
  for (let k = 0; k < MAX_CARDS; k++) {
    const title = h("span.wave-title"), meta = h("span.wave-meta"), warn = h("span"), read = h("span.wave-read");
    const expand = h("button.btn.btn-ghost.btn-sm.btn-icon", { type: "button", "aria-label": t("This channel alone"),
                                                               title: t("This channel alone, or all channels again"),
                                                               onclick: () => enlarge(enlarged === k ? null : k) }, icon("expand"));
    const plotEl = h("div.plot"), empty = h("div.wave-empty.hidden");
    const card = h("div.wave", h("div.wave-head", title, meta, warn, read, expand), h("div.wave-plot", plotEl, empty));
    grid.appendChild(card);
    const plot = new WavePlot(plotEl, { readout: read, onZoom: (x0, x1) => zoomAll(x0, x1), onMeasure: (x0, x1) => setSpan([x0, x1]),
                                        onWheel: (x0, x1) => wheelZoom(x0, x1), onReset: () => { resetZoom(); load(true); } });
    plot.setRange(range);
    plot.setShow(lines);
    plot.setMouse(mouse);
    const cells = Array.from({ length: 8 }, (_, i) => h(i ? "td.n" : "td"));
    const row = h("tr", cells);
    measureRows.appendChild(row);
    cards.push({ card, title, meta, warn, read, plot, empty, expand, row, cells, ch: null });
  }
  let lastBins = 0;
  const ro = new ResizeObserver(() => layout());
  ro.observe(wrap);

  function channelsShown() {
    const first = group * perPage, out = [];
    for (let c = first; c < Math.min(nChannels, first + perPage); c++) out.push(c);
    return out;
  }

  function showCards() {
    const chans = channelsShown();
    cards.forEach((x, k) => {
      x.ch = k < chans.length ? chans[k] : null;
      const shown = x.ch !== null && (enlarged === null || enlarged === k);
      x.card.classList.toggle("hidden", !shown);
      x.row.classList.toggle("hidden", x.ch === null);
    });
    layout();
  }

  // 4 plots a row when there is room, else 2 or 1; the rows share the height of the panel
  function layout() {
    const w = wrap.clientWidth, hh = wrap.clientHeight;
    if (!w) return;
    const shown = enlarged === null ? channelsShown().length : 1;
    const cols = shown === 1 ? 1 : w >= 760 ? 4 : w >= 380 ? 2 : 1;
    const rows = Math.max(1, Math.ceil(shown / cols));
    grid.style.setProperty("--cols", cols);
    grid.style.gridAutoRows = `${Math.max(150, Math.floor((hh - 10 * (rows - 1)) / rows))}px`;
    if (lastBins && Math.abs(wantBins() - lastBins) / lastBins > 0.25) load(true);
  }

  function wantBins() {
    const card = cards.find((x) => !x.card.classList.contains("hidden")) || cards[0];
    return Math.min(2000, Math.max(200, Math.round(card.card.clientWidth * (window.devicePixelRatio || 1))));
  }

  function enlarge(k) {
    enlarged = k;
    allBtn.classList.toggle("hidden", k === null);
    cards.forEach((x) => x.expand.replaceChildren(icon(k === null ? "expand" : "shrink")));
    showCards();
  }

  // ---- state of the controls ----
  function boards() {
    if (source === "file" && fileInfo) {
      const s = fileInfo.settings || {};
      return [{ name: s.board || "?", text: `${s.board || "?"} · ${s.model || fileInfo.model || ""}`, channels: fileInfo.channels }];
    }
    const cfg = app.state.config.config;
    return cfg.boards.filter((b) => b.enabled).map((b) => ({ name: b.name, text: `${b.name} · ${b.model}`, channels: b.channels.length }));
  }

  function fillBoards() {
    const keep = boardSel.value || store("wave.board") || "";
    const list = boards();
    clear(boardSel, list.map((b) => h("option", { value: b.name }, b.text)));
    if (list.some((b) => b.name === keep)) boardSel.value = keep;
    const b = list.find((x) => x.name === boardSel.value) || list[0];
    nChannels = b ? b.channels : 0;
    fillGroups();
    if (onBoard) onBoard(boardSel.value);
  }

  function fillGroups() {
    const n = Math.max(1, Math.ceil(nChannels / perPage));
    if (group >= n) group = 0;
    clear(groupSel, Array.from({ length: n }, (_, g) => h("option", { value: String(g) },
      `${g * perPage}–${Math.min(nChannels, (g + 1) * perPage) - 1}`)));
    groupSel.value = String(group);
  }

  function boardChanged() {
    store("wave.board", boardSel.value);
    const b = boards().find((x) => x.name === boardSel.value);
    nChannels = b ? b.channels : nChannels;
    fillGroups();
    showCards();
    resetZoom();
    load(true);
    if (onBoard) onBoard(boardSel.value);
  }

  // show the waveforms of a board (by name); false if the source shown (a file) has no data of it
  function showBoard(name) {
    if (![...boardSel.options].some((o) => o.value === name)) return false;
    if (boardSel.value !== name) { boardSel.value = name; boardChanged(); }
    return true;
  }

  function showControls() {
    sourceSeg.set(source);
    const isFile = source === "file";
    evGroup.classList.toggle("hidden", !isFile);
    pauseBtn.classList.toggle("hidden", isFile);
    fileName.classList.toggle("hidden", !isFile || !file);
    setText(fileName, basename(file));
    fileName.title = file;
    customBox.classList.toggle("hidden", range.mode !== "custom");
    halfBox.classList.toggle("hidden", range.mode !== "baseline");
    allBtn.classList.toggle("hidden", enlarged === null);
    for (const [btn, on] of [[trigLineBtn, lines.trigger], [thrLineBtn, lines.threshold]]) {
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    }
    mouseSeg.set(mouse);
    const n = fileInfo ? fileInfo.events : 0;
    evInput.max = String(Math.max(0, n - 1));
    setText(evCount, isFile ? t("of {n}", { n: fmtInt(n) }) : "");
    let text;
    if (isFile && fileInfo) {
      const s = fileInfo.settings || {};
      text = t("File {path}: {n} events of board {b} ({m}) · run {run}", { path: basename(file), n: fmtInt(fileInfo.events), b: s.board || "?", m: s.model || "?", run: s.run || "?" });
      if (fileInfo.truncated) text += " · " + t("the file ends inside an event (cut): its last complete event is the last one shown");
      if (s.note) text += " · " + t("note: {note}", { note: s.note.replace(/\s*\n\s*/g, " · ") });
      sub.title = s.note || "";
    } else {
      const running = app.state.run && app.state.run.active;
      text = running ? t("Live: the newest event of the board, from the run going") : t("Live: the newest event of the board in the last run (no run is going)");
      sub.title = "";
    }
    if (eventFacts && eventFacts.trigger_id !== null && eventFacts.trigger_id !== undefined) {
      text += " · " + t("trigger ID {id} · timestamp {ts} ({time})", { id: fmtInt(eventFacts.trigger_id), ts: fmtInt(eventFacts.timestamp),
                                                                       time: fmtStamp(eventFacts.timestamp) });
    }
    text += " · " + t("{ns} ns per sample", { ns: fmtNum(boardInfo.sample_ns, boardInfo.sample_ns % 1 ? 1 : 0) });
    text += " · " + (t0 !== null ? t("time 0 = the trigger, sample {n}", { n: fmtInt(t0) })
                                 : t("time counted from the first sample (the trigger's place is not known)"));
    if (mouse === "measure" && !span) text += " · " + t("drag across a plot to measure a range");
    setText(sub, text);
  }

  async function setSource(v) {
    if (v === "file" && !file) { await pickFile(); return; }
    source = v;
    store("wave.source", v);
    resetZoom();
    eventFacts = null;
    if (v === "file") await openFile(file); else { fileInfo = null; fillBoards(); showCards(); showControls(); load(true); }
  }

  async function pickFile() {
    const path = await chooseFile({ title: t("Open a .raw or ROOT file") });
    if (path) await openFile(path);
    else showControls();
  }

  async function openFile(path) {
    try {
      fileInfo = await get("/api/files/info", { path });
    } catch (err) {
      fail(err);
      source = "live";
      store("wave.source", "live");
      fileInfo = null;
      fillBoards();
      showCards();
      showControls();
      load(true);
      return;
    }
    file = path;
    source = "file";
    store("wave.file", path);
    store("wave.source", "file");
    eventNo = 0;
    evInput.value = "0";
    fillBoards();
    showCards();
    showControls();
    load(true);
  }

  function setEvent(k) {
    const n = fileInfo ? fileInfo.events : 0;
    eventNo = Math.max(0, Math.min(n - 1, k));
    evInput.value = String(eventNo);
    load(true);
  }

  function setRange(change) {
    range = { ...range, ...change };
    if (range.lo >= range.hi) {
      if ("hi" in change) range.lo = Math.max(0, range.hi - 1); else range.hi = Math.min(65535, range.lo + 1);
      if (range.lo >= range.hi) range.lo = range.hi - 1;
    }
    range.half = Math.max(1, Math.min(65535, range.half || 1));
    lo.value = String(range.lo);
    hi.value = String(range.hi);
    half.value = String(range.half);
    rangeSeg.set(range.mode);
    store("wave.range", JSON.stringify(range));
    cards.forEach((c) => c.plot.setRange(range));
    showControls();
  }

  function setLines(change) {
    lines = { ...lines, ...change };
    store("wave.lines", JSON.stringify(lines));
    cards.forEach((c) => c.plot.setShow(lines));
    showControls();
  }

  function setMouse(v) {
    mouse = v;
    store("wave.mouse", v);
    cards.forEach((c) => c.plot.setMouse(v));
    showControls();
  }

  function setSpan(s) {
    span = s;
    cards.forEach((c) => c.plot.setSpan(s));
    body.showSecond(Boolean(s));
    showSpanText();
    showControls();
    load(true);
  }

  function showSpanText() {
    const s = span;
    if (!s) return;
    const ns = boardInfo.sample_ns;
    const at = (x) => fmtTime((x - (t0 ?? 0)) * ns);
    setText(spanText, t("samples {a}–{b} · {ta} to {tb} · {n} samples · {ns} ns",
                        { a: fmtInt(s[0]), b: fmtInt(s[1] - 1), ta: at(s[0]), tb: at(s[1] - 1), n: fmtInt(s[1] - s[0]),
                          ns: fmtInt((s[1] - s[0]) * ns) }));
  }

  function zoomAll(x0, x1) {
    zoom = [x0, x1];
    cards.forEach((c) => { c.plot.zoomed = [x0, x1]; c.plot.u.setScale("x", { min: x0, max: x1 }); });
    load(true);
  }

  let wheelTimer = null;
  function wheelZoom(x0, x1) {
    if (x0 === null) resetZoom();
    else {
      zoom = [x0, x1];
      cards.forEach((c) => { c.plot.zoomed = [x0, x1]; c.plot.u.setScale("x", { min: x0, max: x1 }); });
    }
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => load(true), 150);
  }

  function resetZoom() {
    zoom = null;
    cards.forEach((c) => c.plot.resetZoom(false));
  }

  // ---- data ----
  let seq = null, busy = false, again = false, gone = false;
  async function load(force) {
    if (gone) return;
    if (busy) { again = again || force; return; }
    const chans = channelsShown();
    if (!boardSel.value || !chans.length) { clearPlots(); return; }
    busy = true;
    try {
      const bins = wantBins();
      lastBins = bins;
      const q = { channels: chans.join(","), bins, x0: zoom ? zoom[0] : undefined, x1: zoom ? zoom[1] : undefined,
                  m0: span ? span[0] : undefined, m1: span ? span[1] : undefined };
      let view;
      if (source === "file") view = await get("/api/files/waveforms", { ...q, path: file, event: eventNo });
      else {
        view = await get("/api/live/waveforms", { ...q, board: boardSel.value, since: force ? undefined : seq });
        if (view.same) return;
        seq = view.seq;
      }
      if (!gone) show(view);
    } catch (err) {
      if (source === "file") fail(err);
    } finally {
      busy = false;
      if (again) { again = false; load(true); }
    }
  }

  function clearPlots() {
    cards.forEach((x) => { x.plot.setView(null); setText(x.title, ""); });
  }

  function show(view) {
    const s = view.settings || {};
    boardInfo = { sample_ns: s.sample_ns || SAMPLE_NS, adc_max: s.adc_max || ADC_MAX, pre_trigger: s.pre_trigger,
                  record_length: s.record_length, model: s.model };
    eventFacts = { trigger_id: view.trigger_id, timestamp: view.timestamp };
    if (view.n_channels && view.n_channels !== nChannels) { nChannels = view.n_channels; fillGroups(); showCards(); }
    const ref = view.channels.find((ch) => ch.trigger_at !== null && ch.trigger_at !== undefined);
    const newT0 = ref ? ref.trigger_at : null;
    if (newT0 !== t0) { t0 = newT0; showSpanText(); }
    const byCh = new Map(view.channels.map((ch) => [ch.ch, ch]));
    const isFile = view.source === "file";
    cards.forEach((x) => {
      if (x.ch === null) return;
      const ch = byCh.get(x.ch) || { ch: x.ch, enabled: false, present: false };
      x.plot.setBoard(boardInfo);
      setText(x.title, `ch${ch.ch}`);
      x.card.classList.toggle("short", Boolean(ch.short));
      clear(x.warn, ch.short ? chip(t("short: {n} of {full}", { n: fmtInt(ch.nsamps), full: fmtInt(ch.full_length) }), "warning", "alert") : null);
      let emptyText = "";
      if (!ch.enabled) emptyText = t("not read out");
      else if (!ch.present) emptyText = isFile ? t("not in this event") : t("waiting for data");
      x.empty.classList.toggle("hidden", !emptyText);
      setText(x.empty, emptyText);
      clear(x.meta, ch.present && ch.data ? [h("span.wave-mu", `μ ${ch.mean.toFixed(0)} · `), `σ ${ch.std.toFixed(2)}`] : null);
      x.meta.title = ch.present && ch.data ? `${fmtInt(ch.nsamps)} ${t("samples")} · μ ${fmtNum(ch.mean, 1)} · σ ${fmtNum(ch.std, 2)} · baseline ${fmtNum(ch.baseline, 1)} · min ${fmtInt(ch.minimum)} · max ${fmtInt(ch.maximum)}` : "";
      x.plot.setView(ch.present ? ch : null);
      showMeasure(x, ch);
    });
    showControls();
  }

  function showMeasure(x, ch) {
    const m = ch.measure;
    const ns = boardInfo.sample_ns;
    const values = !m || !m.n ? [] : [fmtInt(m.min), fmtInt(m.max), fmtNum(m.mean, 1), fmtNum(m.std, 2),
                                      (m.peak > 0 ? "+" : "") + fmtNum(m.peak, 1),
                                      `${fmtInt(m.peak_at)} · ${fmtTime((m.peak_at - (t0 ?? 0)) * ns)}`, fmtInt(m.area)];
    setText(x.cells[0], `ch${ch.ch}`);
    for (let i = 1; i < 8; i++) setText(x.cells[i], values[i - 1] ?? "—");
    x.row.classList.toggle("muted", !values.length);
    x.row.title = !ch.present ? t("no waveform") : m && !m.n ? t("the waveform ends before the range")
                : m ? t("{n} samples · baseline {b}", { n: fmtInt(m.n), b: fmtNum(ch.baseline, 1) }) : "";
  }

  fillBoards();
  showCards();
  showControls();
  enlarge(null);
  const timer = setInterval(() => { if (source === "live" && !paused) load(false); },
                            Math.max(200, app.state.settings.values.refresh_ms || 500));
  requestAnimationFrame(() => { layout(); if (source === "file") openFile(file); else load(true); });

  return {
    el: panel,
    showBoard,
    board: () => boardSel.value,
    update(type) {
      if (type === "config" && source === "live") { fillBoards(); showCards(); showControls(); }
      if (type === "run") showControls();
    },
    restyle() { cards.forEach((c) => { c.plot.restyle(); c.plot.setRange(range); c.plot.setShow(lines); c.plot.setMouse(mouse); c.plot.setSpan(span); }); load(true); },
    unmount() { gone = true; clearInterval(timer); ro.disconnect(); cards.forEach((c) => c.plot.destroy()); },
  };
}

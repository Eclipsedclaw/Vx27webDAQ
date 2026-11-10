// The waveform panel of the Run page: the channels of one board, live from the run or from a .raw or .root file.
// A board has 16 or 32 (VX2730) or 64 (VX2740) channels: the panel shows the ones chosen with Channels… (at most
// 16, each board its own choice, kept in this browser). "Combined" puts channels of several boards in one plot
// instead (combined.js). Two rows of controls above the plots: what to show (source, board, channels, event), then how (ADC range, the dashed
// trigger and threshold lines, what a drag does). The plots share the x window: a drag or the mouse wheel zooms all
// of them to the same samples, which the server sends in full detail; in Measure mode a drag marks the same range
// on every plot, and the table under the plots gives its numbers per channel (from all the samples). The x axes
// give each sample's time from the trigger too (the board's own sample time: 2 ns on a V2730, 8 ns on a V2740).
// Live, the boards are those the server can show (of the run going, else of the last run, else of the configuration),
// not the configuration being edited. Pause keeps the event shown, with all its samples: zooming and measuring
// then work on that copy here until Resume.
import { SLOW_MS, get, post } from "./api.js";
import { T, t } from "./i18n.js";
import { ADC_MAX, SAMPLE_NS, WavePlot, XYPlot, fmtTime } from "./plots.js";
import { Ring, SampleHistogram, envelope, measure, powerSpectrum, spectrumDb } from "./dsp.js";
import { basename, chip, chooseFile, clear, fail, fmtInt, fmtNum, h, icon, segmented, setText, splitter, store, toast } from "./ui.js";
import { MAX_SHOWN, channelPicker, defaultChannels, rangesText } from "./chanpick.js";
import { combinedView } from "./combined.js";

const RANGES = [["auto", T("Auto")], ["full", T("Full scale")], ["custom", T("Custom")], ["baseline", T("Baseline ± N")]];
const MAX_CARDS = 16;
const PLOTS = [["wave", T("Waveforms")], ["fft", T("FFT")], ["hist", T("Histogram")]];
const AVERAGES = [1, 4, 16, 64];
const BASELINE_N = 16;          // a waveform's baseline: the mean of its first 16 samples (as the server's)

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
  const panel = h("section.wave-panel", { "aria-label": t("Waveforms") });     // the plots (the main part of the page)
  const controls = h("div.plot-controls", { "aria-label": t("Plot controls") });  // their controls (the Run page's Plot tab)
  const sub = h("div.wave-sub");

  // ---- what to show ----
  let source = store("wave.source") === "file" && store("wave.file") ? "file" : "live";
  let file = store("wave.file") || "", fileInfo = null, eventNo = 0, paused = false, zoom = null, enlarged = null;
  let boardInfo = { sample_ns: SAMPLE_NS, adc_max: ADC_MAX, pre_trigger: null, record_length: 0, model: "" };
  let range = loadJSON("wave.range", { mode: "auto", lo: 8000, hi: 9000, half: 200 });
  let lines = loadJSON("wave.lines", { trigger: false, threshold: false });
  let mouse = store("wave.mouse") === "measure" ? "measure" : "zoom";
  let viewMode = store("wave.view") === "combined" ? "combined" : "channels";
  let sel = [];                   // the channels shown, of the board shown
  // the scope tools (WaveDump2's plot types): waveforms (each event, or the mean of the newest N), FFT (the mean
  // power of the newest N), the samples histogram (every event so far); the y axis in ADC or mV from the baseline
  let plotMode = PLOTS.some(([k]) => k === store("wave.plot")) ? store("wave.plot") : "wave";
  let avgN = AVERAGES.includes(Number(store("wave.avg"))) ? Number(store("wave.avg")) : 1;
  let yUnit = store("wave.unit") === "mv" ? "mv" : "adc";
  let mvPerAdc = 2000 / 65536;
  let span = null;
  let nChannels = 64, t0 = null, eventFacts = null;
  // live: the boards the server can show (/api/live/boards), and where they come from ("run", "last" or
  // "configuration"); null until it has said
  let liveBoards = null, liveFrom = "";
  let liveError = null, stopped = false;     // the newest live request failed: its message; refused (4xx): not asked again
  let frozen = null;                          // Pause: the event kept, every sample of the channels shown
  let opening = 0, freezing = 0, boardsAsk = 0;   // counters of requests: the answer to an earlier one is dropped
  const runThr = new Map();                   // "board|ch": the threshold the run uses (from its live view), for Tune…
  let tuneKey = "";                           // the board and channel the Tune inputs were filled for
  const sourceSeg = segmented([["live", t("Live")], ["file", t("File")]], source, (v) => setSource(v), { label: t("Source") });
  const fileName = h("span.chip", { title: "" });
  const openBtn = h("button.btn.btn-sm", { type: "button", onclick: pickFile }, icon("folder"), t("Open file…"));
  const boardSel = h("select.input", { "aria-label": t("Board"), onchange: () => boardChanged() });
  const pickText = h("span.num");
  const pickBtn = h("button.btn.btn-sm", { type: "button", "aria-expanded": "false", onclick: () => togglePicker(),
                                          title: t("Choose the channels to show: at most 16 at a time") }, icon("grid"), pickText);
  const picker = channelPicker({ label: t("Channels shown"), onChange: (list) => setSel(list) });
  const pickBox = h("div.chan-box.hidden", picker.el);
  const viewSeg = segmented([["channels", t("Channels")], ["combined", t("Combined")]], viewMode, (v) => setView(v), { label: t("View") });
  viewSeg.title = t("Channels: the channels of one board, each in its own plot. Combined: channels of several boards in one plot, from the same moment");
  const evInput = h("input.input.num", { type: "number", min: "0", value: "0", "aria-label": t("Event"),
                                         onchange: () => setEvent(Number(evInput.value) || 0) });
  const evCount = h("span.text2.small.num");
  const evGroup = h("div.group", h("span.label", t("Event")),
                    h("div.stepper", h("button.btn.btn-sm.btn-icon", { type: "button", "aria-label": t("Previous event"), onclick: () => setEvent(eventNo - 1) }, icon("left")),
                      evInput, h("button.btn.btn-sm.btn-icon", { type: "button", "aria-label": t("Next event"), onclick: () => setEvent(eventNo + 1) }, icon("right"))),
                    evCount);
  const pauseBtn = h("button.btn.btn-sm", { type: "button", "aria-pressed": "false", onclick: () => setPaused(!paused) },
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
                                              title: t("A dashed line at each channel's threshold: its baseline plus the threshold (below the baseline for a negative one); used when the boards trigger on their channels") },
                       icon("hdash"), t("Threshold"));
  const mouseSeg = segmented([["zoom", t("Zoom")], ["measure", t("Measure")]], mouse, (v) => setMouse(v), { label: t("Mouse") });
  mouseSeg.title = t("What a drag across a plot does: zoom all plots to that range, or measure that range on all plots. The mouse wheel over a plot zooms the time axis in either mode.");
  const resetBtn = h("button.btn.btn-sm", { type: "button", onclick: () => { resetZoom(); load(true); },
                                            title: t("Every plot back to the whole waveform and the ADC range chosen here") }, icon("reset"), t("Reset zoom"));
  // tuning during the run (operators): a shown channel's DC offset or threshold, like WaveDump2's oscilloscope tools
  const tuneBtn = h("button.btn.btn-sm", { type: "button", "aria-expanded": "false", onclick: () => toggleTune(),
                                          title: t("Change a channel's DC offset or threshold while the run goes") }, icon("sliders"), t("Tune…"));
  const tuneCh = h("select.input", { "aria-label": t("Channel"), onchange: () => fillTune() });
  const tuneDc = h("input.input.num", { type: "number", min: "0", max: "100", step: "0.1", style: "width:90px", "aria-label": t("DC offset") });
  const tuneThr = h("input.input.num", { type: "number", min: "-65535", max: "65535", step: "1", style: "width:90px", "aria-label": t("threshold"),
                                        title: t("ADC counts from each channel's baseline, as the board gets them: negative for negative pulses (falling edge), positive for positive ones (rising edge)") });
  const tuneKeep = h("input", { type: "checkbox", checked: true });
  const tuneMsg = h("span.text2.small");
  const tuneDcBtn = h("button.btn.btn-sm", { type: "button", onclick: () => tuneSet("dc_offset", tuneDc.value) }, t("Set"));
  const tuneThrBtn = h("button.btn.btn-sm", { type: "button", onclick: () => tuneSet("threshold", tuneThr.value) }, t("Set"));
  const tuneBox = h("div.chan-box.hidden", h("div.row.wrap", h("b.small", t("While the run goes:")), h("span.label", t("Channel")), tuneCh,
    h("span.label", t("DC offset")), tuneDc, h("span.text2", "%"), tuneDcBtn,
    h("span.label", t("threshold")), tuneThr, h("span.text2.small", t("counts")), tuneThrBtn,
    h("label.row.nowrap", { title: t("Also into the configuration being edited, so the next runs use it") }, tuneKeep, t("keep for the next runs")), tuneMsg));
  const boardGroup = h("div.group", h("span.label", t("Board")), boardSel);
  const chanGroup = h("div.group", h("span.label", t("Channels")), pickBtn);
  const plotSeg = segmented(PLOTS.map(([k, v]) => [k, t(v)]), plotMode, (v) => setPlot(v), { label: t("Plot") });
  plotSeg.title = t("Waveforms; FFT: the spectrum of each waveform (its mean taken off, a Hann window) in dB of full scale; Histogram: how often each ADC value came, over the events so far");
  const avgSel = h("select.input", { "aria-label": t("Average"), title: t("Waveforms and FFT: each event, or the mean of the newest N events"),
                                    onchange: () => { avgN = Number(avgSel.value); store("wave.avg", avgN); restart(); } },
                   AVERAGES.map((n) => h("option", { value: String(n) }, n === 1 ? t("each event") : t("mean of {n}", { n }))));
  avgSel.value = String(avgN);
  const unitSeg = segmented([["adc", "ADC"], ["mv", "mV"]], yUnit, (v) => setUnit(v), { label: t("Y axis") });
  unitSeg.title = t("ADC counts, or mV from each channel's baseline (its board's 2 Vpp input range: 0.122 mV a count on a VX2730, 0.031 mV on a VX2740)");
  const restartBtn = h("button.btn.btn-sm", { type: "button", onclick: () => restart(), title: t("Start the means and the histograms again") },
                      icon("refresh"), t("Restart"));
  const avgGroup = h("div.group", avgSel);
  const unitGroup = h("div.group", h("span.label", t("Y axis")), unitSeg);
  const rangeGroup = h("div.group", h("span.label", t("ADC range")), rangeSeg, customBox, halfBox);
  const linesGroup = h("div.group", h("span.label", t("Lines")), trigLineBtn, thrLineBtn);
  const mouseGroup = h("div.group", h("span.label", t("Mouse")), mouseSeg);
  const howRow = h("div.ctl-stack",
      h("div.group", h("span.label", t("Plot")), plotSeg), avgGroup, unitGroup, rangeGroup, linesGroup, mouseGroup,
      h("div.row.wrap.ctl-buttons", app.can("control") ? tuneBtn : null, restartBtn, resetBtn));
  const combo = combinedView(app, { getSource: () => ({ source, file }) });
  controls.append(
    h("div.ctl-stack",
      h("div.group", h("span.label", t("View")), viewSeg),
      h("div.group", h("span.label", t("Source")), sourceSeg, openBtn), fileName,
      boardGroup, chanGroup),
    pickBox,
    h("div.ctl-stack", evGroup, h("div.row.wrap.ctl-buttons", pauseBtn, allBtn)),
    howRow, tuneBox, combo.controls);
  panel.appendChild(sub);

  // ---- the plots, and the measurement under them ----
  const wrap = h("div.wave-wrap");
  const grid = h("div.wave-grid");
  const errText = h("span.grow.break");
  const errBox = h("div.notice.error.hidden", { role: "alert" }, icon("alert"), errText);   // a live request that failed
  wrap.append(errBox, grid);
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
                     h("th.n", { title: t("The samples at the two edges of the range, and their difference (like two cursors)") }, t("edges (Δ)")),
                     h("th.n", { title: t("Sum of (sample − baseline) over the range, times the sample time (ADC·ns)") }, t("area (ADC·ns)")))),
        measureRows)));
  const body = splitter(wrap, measureBox, { key: "wave.measure", vertical: true, initial: 0.64, min: 110 });
  body.classList.add("wave-body");
  body.showSecond(false);
  panel.appendChild(body);
  panel.appendChild(combo.el);
  const cards = [];
  const noneBox = h("div.notice.info.hidden", icon("info"), h("span.grow", t("No plot is open: choose the channels to show with Channels (Plot tab)")));
  wrap.insertBefore(noneBox, grid);
  for (let k = 0; k < MAX_CARDS; k++) {
    const x = {};                  // this card: its place in `cards` changes when a plot before it is closed
    const title = h("span.wave-title"), meta = h("span.wave-meta"), warn = h("span"), read = h("span.wave-read");
    const expand = h("button.btn.btn-ghost.btn-sm.btn-icon", { type: "button", "aria-label": t("This channel alone"),
                                                               title: t("This channel alone, or all channels again"),
                                                               onclick: () => { const i = cards.indexOf(x); enlarge(enlarged === i ? null : i); } }, icon("expand"));
    const ybtn = h("button.btn.btn-ghost.btn-sm.btn-icon", { type: "button", "aria-label": t("This channel's y range"),
                                                             title: t("This channel's own y range (else the panel's)"), onclick: () => toggleCardRange(cards.indexOf(x)) }, icon("sliders"));
    const shut = h("button.btn.btn-ghost.btn-sm.btn-icon", { type: "button", "aria-label": t("Close this plot"),
                                                             title: t("Close this plot: its channel is not asked for any more, and what the plot held here (its waveform, means, FFT and histogram) is let go. Channels shows it again"),
                                                             onclick: () => closeCard(x) }, icon("close"));
    const rangeRow = h("div.ch-range.hidden");
    const plotEl = h("div.plot"), xyEl = h("div.plot.hidden"), empty = h("div.wave-empty.hidden");
    const card = h("div.wave", h("div.wave-head", title, meta, warn, read, ybtn, expand, shut), rangeRow, h("div.wave-plot", plotEl, xyEl, empty));
    grid.appendChild(card);
    const plot = new WavePlot(plotEl, { readout: read, onZoom: (x0, x1) => zoomAll(x0, x1), onMeasure: (x0, x1) => setSpan([x0, x1]),
                                        onWheel: (x0, x1) => wheelZoom(x0, x1), onReset: () => { resetZoom(); load(true); },
                                        sync: "vx27-wave" });      // one dashed cursor line on every channel plot
    plot.setRange(range);
    plot.setShow(lines);
    plot.setMouse(mouse);
    const cells = Array.from({ length: 9 }, (_, i) => h(i ? "td.n" : "td"));
    const row = h("tr", cells);
    measureRows.appendChild(row);
    plot.setUnit(yUnit, mvPerAdc);
    cards.push(Object.assign(x, { card, title, meta, warn, read, plot, empty, expand, row, cells, ch: null, plotEl, xyEl, xy: null, ybtn, rangeRow,
                                  ring: new Ring(avgN), hist: new SampleHistogram(), accKey: null, taken: new Set(), nEvents: 0 }));
  }
  let lastBins = 0;
  const ro = new ResizeObserver(() => layout());
  ro.observe(wrap);

  function channelsShown() {
    return sel.filter((c) => c < nChannels).slice(0, MAX_SHOWN);
  }

  function liveBoard(name = boardSel.value) {
    return (liveBoards || []).find((b) => b.name === name) || null;
  }

  // the channels a board shows: as chosen in this browser, else its first 16 that are read out (live: in the run)
  function boardEnabled() {
    if (source === "file") return fileInfo ? fileInfo.enabled || null : null;
    const lb = liveBoard();
    if (lb && Array.isArray(lb.read_out)) return lb.read_out;
    const b = app.state.config.config.boards.find((x) => x.name === boardSel.value);
    return b ? b.channels.map((c) => c.enabled) : null;
  }

  function boardTrigger() {
    if (source === "file") return null;
    const lb = liveBoard();
    if (lb && Array.isArray(lb.in_trigger)) return lb.in_trigger.some(Boolean) ? lb.in_trigger : null;
    const c = app.state.config, i = c.config.boards.findIndex((x) => x.name === boardSel.value);
    if (i < 0 || !(c.boards[i] || {}).self_trigger) return null;
    return c.config.boards[i].channels.map((x) => x.enabled && x.self_trigger);
  }

  function loadSel() {
    let kept = null;
    try { kept = JSON.parse(store(`wave.sel.${boardSel.value}`) || "null"); } catch (e) { kept = null; }
    const list = Array.isArray(kept) ? kept.filter((c) => Number.isInteger(c) && c >= 0 && c < nChannels).slice(0, MAX_SHOWN) : null;
    // a choice kept for channels this board does not have (none left of it): its first ones instead; "None" stays
    sel = list && (list.length || !kept.length) ? list : defaultChannels(nChannels, boardEnabled());
    showPick();
    if (!tuneBox.classList.contains("hidden")) fillTune();
  }

  function setSel(list) {
    sel = list.slice(0, MAX_SHOWN);
    store(`wave.sel.${boardSel.value}`, JSON.stringify(sel));
    if (!tuneBox.classList.contains("hidden")) fillTune();
    if (enlarged !== null) enlarged = null;
    setText(pickText, sel.length ? `${rangesText(sel)} (${sel.length})` : t("none"));
    showCards();
    load(true);
  }

  function showPick() {
    setText(pickText, sel.length ? `${rangesText(sel)} (${sel.length})` : t("none"));
    picker.set({ channels: nChannels, enabledList: boardEnabled(), triggerList: boardTrigger(), selected: sel });
  }

  function togglePicker(open) {
    const showIt = open ?? pickBox.classList.contains("hidden");
    pickBox.classList.toggle("hidden", !showIt);
    pickBtn.classList.toggle("on", showIt);
    pickBtn.setAttribute("aria-expanded", showIt ? "true" : "false");
    layout();
  }

  function toggleTune(open) {
    const showIt = open ?? tuneBox.classList.contains("hidden");
    tuneBox.classList.toggle("hidden", !showIt);
    tuneBtn.classList.toggle("on", showIt);
    tuneBtn.setAttribute("aria-expanded", showIt ? "true" : "false");
    if (showIt) { tuneKey = ""; fillTune(); }
    layout();
  }

  // the channels shown, and the values of the one chosen: filled again only for another board or channel, so that
  // what is being typed stays
  function fillTune() {
    const keep = tuneCh.value;
    clear(tuneCh, channelsShown().map((c) => h("option", { value: String(c) }, `ch${c}`)));
    if ([...tuneCh.options].some((o) => o.value === keep)) tuneCh.value = keep;
    const key = `${boardSel.value}|${tuneCh.value}`;
    if (key !== tuneKey && tuneCh.value !== "") {
      tuneKey = key;
      const v = tuneValues(boardSel.value, Number(tuneCh.value));
      tuneDc.value = v.dc === null ? "" : String(v.dc);
      tuneThr.value = v.thr === null ? "" : String(v.thr);
    }
    showTune();
  }

  // a channel's DC offset and threshold as the run has them: the live board's lists when the server gives them, the
  // threshold of its newest live event; else the configuration being edited
  function tuneValues(board, c) {
    const lb = liveBoard(board), b = app.state.config.config.boards.find((x) => x.name === board), cc = b && b.channels[c];
    const of = (key) => (lb && Array.isArray(lb[key]) && lb[key][c] !== undefined && lb[key][c] !== null ? lb[key][c] : null);
    let dc = of("dc_offset"), thr = of("threshold");
    if (thr === null && runThr.has(`${board}|${c}`)) thr = runThr.get(`${board}|${c}`);
    if (dc === null && cc) dc = cc.dc_offset;
    if (thr === null && cc) thr = cc.positive ? cc.threshold : -cc.threshold;      // as the board gets it
    return { dc, thr };
  }

  function showTune() {
    const r = app.state.run, running = Boolean(r && r.active && r.phase === "running");
    const ok = running && source === "live" && app.can("control") && tuneCh.options.length > 0;
    for (const x of [tuneDcBtn, tuneThrBtn]) x.disabled = !ok;
    if (!running) setText(tuneMsg, t("during a run: the change goes to the board at once"));
  }

  async function tuneSet(what, value) {
    if (value === "" || !Number.isFinite(Number(value))) return;
    try {
      const r = await post("/api/run/control", { cmd: "set", board: boardSel.value, channel: Number(tuneCh.value), what,
                                                 value: Number(value), keep: tuneKeep.checked });
      setText(tuneMsg, r.ok ? t("done: {m}", { m: r.message }) + (r.kept ? ` · ${t("kept for the next runs")}` : "") : r.message);
      toast(r.ok ? t("Changed during the run: {m}", { m: r.message }) : r.message, r.ok ? "ok" : "error");
      load(true);
    } catch (err) { fail(err); }
  }

  function setView(v) {
    viewMode = v;
    store("wave.view", v);
    viewSeg.set(v);
    const comb = v === "combined";
    for (const x of [boardGroup, chanGroup, howRow, sub, body]) x.classList.toggle("hidden", comb);
    if (comb) { togglePicker(false); toggleTune(false); }
    combo.setActive(comb);
    showControls();
    if (!comb) { layout(); load(true); }
  }

  function showCards() {
    const chans = channelsShown();
    // a channel shown before keeps its card (its means and histogram) when others are closed or added
    const mine = new Map(cards.filter((x) => x.ch !== null && chans.includes(x.ch)).map((x) => [x.ch, x]));
    const free = cards.filter((x) => mine.get(x.ch) !== x);
    const order = [...chans.map((c) => mine.get(c) || free.shift()), ...free];
    if (order.some((x, k) => cards[k] !== x)) {
      cards.splice(0, cards.length, ...order);
      cards.forEach((x) => { grid.appendChild(x.card); measureRows.appendChild(x.row); });
    }
    cards.forEach((x, k) => {
      if (x.ch !== (k < chans.length ? chans[k] : null)) x.rangeRow.classList.add("hidden");
      x.ch = k < chans.length ? chans[k] : null;
      if (x.ch === null && (x.accKey !== null || x.xy)) release(x);
      const shown = x.ch !== null && (enlarged === null || enlarged === k);
      x.card.classList.toggle("hidden", !shown);
      x.row.classList.toggle("hidden", x.ch === null);
    });
    noneBox.classList.toggle("hidden", chans.length > 0 || Boolean(liveError));
    applyRanges();
    layout();
  }

  // a plot closed: its channel leaves the channels shown (as in Channels), so the server is not asked for it any
  // more, and its card lets go of what it held (showCards); at Pause its samples leave the copy kept too
  function closeCard(x) {
    const c = x.ch;
    if (c === null) return;
    if (enlarged !== null) enlarge(null);
    if (frozen) frozen = { ...frozen, channels: frozen.channels.filter((ch) => ch.ch !== c) };
    setSel(sel.filter((k) => k !== c));
    showPick();
  }

  // a card that shows no channel keeps nothing: its waveform, the means or FFT sums, the histogram and the events
  // taken go, and its FFT or histogram plot is taken down (made again when it is needed)
  function release(x) {
    clearCard(x);
    x.accKey = null;
    x.plot.setView(null);
    if (x.xy) { x.xy.destroy(); x.xy = null; }
    setText(x.title, "");
    clear(x.meta);
    clear(x.warn);
    x.cells.forEach((c, i) => setText(c, i ? "—" : ""));
  }

  // ---- a channel's own y range (per board and channel, in this browser), else the panel's ----
  function chanRange(c) {
    if (c === null || c === undefined) return null;
    try { return JSON.parse(store(`wave.crange.${boardSel.value}.${c}`) || "null"); } catch (e) { return null; }
  }

  function rangeFor(c) {
    const r = chanRange(c);
    return r && r.mode && r.mode !== "panel" ? { ...range, ...r } : range;
  }

  function applyRanges() {
    cards.forEach((x) => {
      x.plot.setRange(rangeFor(x.ch));
      const own = chanRange(x.ch);
      x.ybtn.classList.toggle("on", Boolean(own && own.mode !== "panel"));
    });
  }

  function toggleCardRange(k) {
    const x = cards[k], open = x.rangeRow.classList.contains("hidden");
    x.rangeRow.classList.toggle("hidden", !open);
    if (open) fillCardRange(x);
    layout();
  }

  function fillCardRange(x) {
    const r = { mode: "panel", lo: range.lo, hi: range.hi, half: range.half, ...(chanRange(x.ch) || {}) };
    const save = (change) => {
      Object.assign(r, change);
      store(`wave.crange.${boardSel.value}.${x.ch}`, r.mode === "panel" ? null : JSON.stringify(r));
      applyRanges();
      fillCardRange(x);
    };
    const num = (key, width) => h("input.input.num", { type: "number", value: String(r[key]), style: `width:${width}`, "aria-label": key,
                                                       onchange: (e) => save({ [key]: Number(e.target.value) }) });
    const seg = h("select.input.input-sm", { "aria-label": t("y range of ch{c}", { c: x.ch }), onchange: (e) => save({ mode: e.target.value }) },
                  [["panel", t("as all plots")], ["auto", t("Auto")], ["full", t("Full scale")], ["custom", t("Custom")], ["baseline", t("Baseline ± N")]]
                    .map(([v, l]) => h("option", { value: v }, l)));
    seg.value = r.mode;
    clear(x.rangeRow, seg, r.mode === "custom" ? [num("lo", "80px"), h("span.text2", "–"), num("hi", "80px")] : null,
          r.mode === "baseline" ? [h("span.text2", "±"), num("half", "70px")] : null);
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
  // a file: every board of its run (their files beside it, each opened only when chosen), else its own board alone
  function boards() {
    if (source === "file" && fileInfo) {
      const s = fileInfo.settings || {}, own = s.board || "?";
      const list = Array.isArray(fileInfo.run_files) && fileInfo.run_files.some((r) => r.board === own) ? fileInfo.run_files
                                                                                                      : [{ board: own, path: file }];
      return list.map((r) => ({ name: r.board, path: r.path, channels: fileInfo.channels,
                                text: r.board === own ? `${own} · ${s.model || fileInfo.model || ""}` : r.board }));
    }
    if (liveBoards) return liveBoards.map((b) => ({ name: b.name, text: `${b.name} · ${b.model}`, channels: b.channels }));
    const cfg = app.state.config.config;              // until the server has said which boards the live view has
    return cfg.boards.filter((b) => b.enabled).map((b) => ({ name: b.name, text: `${b.name} · ${b.model}`, channels: b.channels.length }));
  }

  // the boards the live view can show, from the server: asked at the start, when a run starts or ends, and while
  // they are the configuration's (no run yet) when it changes
  async function refreshLiveBoards() {
    const my = ++boardsAsk;
    let r = null;
    try { r = await get("/api/live/boards"); } catch (err) { r = null; }
    if (my !== boardsAsk || gone) return;
    if (r && Array.isArray(r.boards)) { liveBoards = r.boards; liveFrom = r.source || ""; }
    if (source !== "live") return;
    const before = boardSel.value, n = nChannels;
    fillBoards();
    if (boardSel.value !== before || nChannels !== n) { boardChanged(); return; }
    showCards();
    showControls();
    stopped = false;                                  // a board refused before may be in the new run
    if (!paused) load(true);
  }

  function fillBoards() {
    const own = source === "file" && fileInfo ? (fileInfo.settings || {}).board || "?" : "";     // a file: its board
    const keep = own || boardSel.value || store("wave.board") || "";
    const list = boards();
    clear(boardSel, list.map((b) => h("option", { value: b.name }, b.text)));
    if (list.some((b) => b.name === keep)) boardSel.value = keep;
    const b = list.find((x) => x.name === boardSel.value) || list[0];
    nChannels = b ? b.channels : 0;
    loadSel();
    if (onBoard) onBoard(boardSel.value);
  }

  function boardChanged() {
    if (source === "file" && fileInfo) {               // another board of the file's run: its file, at the same event
      const b = boards().find((x) => x.name === boardSel.value);
      if (b && b.path && b.path !== file) { openFile(b.path, { sameRun: true }); return; }
    }
    store("wave.board", boardSel.value);
    const b = boards().find((x) => x.name === boardSel.value);
    nChannels = b ? b.channels : nChannels;
    loadSel();
    showCards();
    resetZoom();
    if (onBoard) onBoard(boardSel.value);
    retake();
  }

  // the data begin again (another board, file or source): the means and histograms start empty, the live view is
  // asked again even after it refused, and at Pause the new board's newest event is kept
  function retake() {
    liveFailed(null);
    if (source === "live" && paused) { clearAcc(); freeze(); } else restart();
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
    evGroup.classList.toggle("hidden", !isFile || viewMode === "combined");
    pauseBtn.classList.toggle("hidden", isFile);
    fileName.classList.toggle("hidden", !isFile || !file);
    setText(fileName, basename(file));
    fileName.title = file;
    customBox.classList.toggle("hidden", range.mode !== "custom");
    halfBox.classList.toggle("hidden", range.mode !== "baseline");
    allBtn.classList.toggle("hidden", enlarged === null || viewMode === "combined");
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
    if (mouse === "measure" && !span && plotMode === "wave") text += " · " + t("drag across a plot to measure a range");
    if (plotMode === "fft") text += " · " + t("FFT: the mean power of the newest {n} events, each with its mean taken off and a Hann window; 0 dBFS = a sine over the whole ADC range", { n: avgN });
    if (plotMode === "hist") text += " · " + t("Histogram: the sample values of the {n} events taken so far", { n: eventsTaken() });
    if (plotMode === "wave" && avgN > 1) text += " · " + t("the mean of the newest {n} events", { n: avgN });
    const wave = plotMode === "wave";
    for (const g of [rangeGroup, linesGroup, mouseGroup, unitGroup]) g.classList.toggle("hidden", !wave);
    avgGroup.classList.toggle("hidden", plotMode === "hist");
    restartBtn.classList.toggle("hidden", wave && avgN === 1);
    resetBtn.classList.toggle("hidden", !wave);
    setText(sub, text);
  }

  async function setSource(v) {
    if (v === "file" && !file) { await pickFile(); return; }
    opening += 1;                                     // a file still being read is not shown after all
    source = v;
    store("wave.source", v);
    resetZoom();
    eventFacts = null;
    if (v === "file") await openFile(file); else { fileInfo = null; fillBoards(); showCards(); showControls(); retake(); combo.sourceChanged(); }
  }

  async function pickFile() {
    const path = await chooseFile({ title: t("Open a .raw or ROOT file") });
    if (path) await openFile(path);
    else showControls();
  }

  // sameRun: another board's file of the run shown (the Board menu): the event number stays, and when it cannot be
  // opened the file shown stays
  async function openFile(path, { sameRun = false } = {}) {
    const my = ++opening;
    let info;
    try {
      info = await get("/api/files/info", { path }, { timeout: SLOW_MS });
    } catch (err) {
      if (my !== opening || gone) return;             // something else was chosen meanwhile
      fail(err);
      if (sameRun && fileInfo && source === "file") { fillBoards(); return; }
      source = "live";
      store("wave.source", "live");
      fileInfo = null;
      fillBoards();
      showCards();
      showControls();
      retake();
      return;
    }
    if (my !== opening || gone) return;               // Live, or another file, was chosen while this one was read
    fileInfo = info;
    file = path;
    source = "file";
    store("wave.file", path);
    store("wave.source", "file");
    eventNo = sameRun ? Math.max(0, Math.min((info.events || 1) - 1, eventNo)) : 0;
    evInput.value = String(eventNo);
    fillBoards();
    showCards();
    showControls();
    retake();
    combo.sourceChanged();
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
    applyRanges();
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
    if (gone || viewMode === "combined") return;
    if (busy) { again = again || force; return; }
    const chans = channelsShown();
    if (!boardSel.value || !chans.length) { clearPlots(); return; }
    if (source === "live" && paused) { if (frozen) show(frozenView(chans)); return; }    // the event kept at Pause
    if (source === "live" && stopped && !force) return;          // refused: asked again only after a change
    busy = true;
    const full = plotMode !== "wave" || avgN > 1;              // every sample: the browser works them out
    const ask = { source, board: boardSel.value, file, event: eventNo, chans: chans.join(","), full, paused };
    try {
      const bins = wantBins();
      lastBins = bins;
      const q = full ? { channels: ask.chans, full: 1, m0: span ? span[0] : undefined, m1: span ? span[1] : undefined }
                     : { channels: ask.chans, bins, x0: zoom ? zoom[0] : undefined, x1: zoom ? zoom[1] : undefined,
                         m0: span ? span[0] : undefined, m1: span ? span[1] : undefined };
      let view;
      if (source === "file") view = await get("/api/files/waveforms", { ...q, path: file, event: eventNo }, { timeout: SLOW_MS });
      else {
        view = await get("/api/live/waveforms", { ...q, board: ask.board, since: force ? undefined : seq });
        if (view.same) return;
      }
      if (gone) return;
      if (!asked(ask)) { again = true; return; }       // the board, file, event or channels changed meanwhile: not theirs
      if (source === "live") { seq = view.seq; liveFailed(null); }
      show(view);
    } catch (err) {
      if (gone || !asked(ask)) return;
      if (source === "file") fail(err); else liveFailed(err);
    } finally {
      busy = false;
      if (again) { again = false; load(true); }
    }
  }

  function asked(a) {                                  // is this still what the panel shows?
    return a.source === source && a.board === boardSel.value && a.chans === channelsShown().join(",") && a.paused === paused &&
           a.full === (plotMode !== "wave" || avgN > 1) && (a.source !== "file" || (a.file === file && a.event === eventNo));
  }

  // a live request failed (a board that is not in the run, the server away): no plots but the message. A refusal
  // (4xx) is not asked again at every refresh, only after a change (another board or source, a run starting or
  // ending, Restart, a zoom); err null: the plots again
  function liveFailed(err) {
    const msg = err ? err.message || String(err) : null;
    stopped = Boolean(err && err.status >= 400 && err.status < 500);
    if (msg === liveError) return;
    liveError = msg;
    setText(errText, msg || "");
    errBox.classList.toggle("hidden", !msg);
    grid.classList.toggle("hidden", Boolean(msg));
    if (msg) { clearPlots(); eventFacts = null; showControls(); } else layout();
  }

  // ---- Pause: the newest event, every sample of the channels shown (at most 65536 a channel), kept here; zooming,
  // the wheel, measuring and the plot types work on it until Resume (the server's live view moves on) ----
  function setPaused(on) {
    paused = on;
    pauseBtn.classList.toggle("on", on);
    pauseBtn.setAttribute("aria-pressed", on ? "true" : "false");
    if (on) { if (source === "live") freeze(); return; }
    freezing += 1;                                     // a copy still coming is not wanted any more
    frozen = null;
    stopped = false;
    load(true);
  }

  async function freeze() {
    const my = ++freezing;
    frozen = null;
    const chans = channelsShown();
    if (!boardSel.value || !chans.length) { clearPlots(); return; }
    let view;
    try {
      view = await get("/api/live/waveforms", { channels: chans.join(","), full: 1, board: boardSel.value });
    } catch (err) {
      if (my === freezing && !gone) liveFailed(err);
      return;
    }
    if (my !== freezing || gone || !paused || source !== "live") return;
    frozen = view;
    liveFailed(null);
    show(frozenView(channelsShown()));
  }

  // the event kept, as the server would send it: each channel thinned to the zoom window (or all of it for the
  // means, FFT and histograms) and the range measured; a channel chosen after Pause is not in it
  function frozenView(chans) {
    const v = frozen, ns = (v.settings || {}).sample_ns || SAMPLE_NS;
    const full = plotMode !== "wave" || avgN > 1, bins = wantBins();
    lastBins = bins;
    const byCh = new Map(v.channels.map((c) => [c.ch, c]));
    const enabled = boardEnabled() || [];
    const channels = chans.map((c) => {
      const ch = byCh.get(c);
      if (!ch) return { ch: c, enabled: enabled[c] ?? true, present: false, notKept: true };
      if (!ch.present || !ch.data) return ch;
      const y = ch.data.y;
      return { ...ch, data: full ? ch.data : envelope(y, zoom ? zoom[0] : 0, zoom ? zoom[1] : null, bins),
               measure: span ? measure(y, span[0], span[1], ch.baseline, ns) : undefined };
    });
    return { ...v, channels };
  }

  // ---- the scope tools ----
  // A card's means and histograms hold one board of one run (or one file) and one channel (`from`), and each event
  // once (evId), however often it comes back (stepping back in a file, a zoom, Pause). Returns the measurement of the
  // trace shown when the browser makes the trace (the mean of N), else undefined (the server's)
  function showPlot(x, ch, from, evId) {
    const wave = plotMode === "wave";
    x.plotEl.classList.toggle("hidden", !wave);
    x.xyEl.classList.toggle("hidden", wave);
    const key = `${from}|${x.ch}`;
    if (x.accKey !== key) { clearCard(x); if (x.xy) x.xy.resetZoom(); x.accKey = key; }
    const y = ch.present && ch.data ? ch.data.y : null;
    let fresh = false;
    if (y && !x.taken.has(evId)) {                       // an event this card has not taken yet
      if (x.taken.size > 4096) x.taken.clear();          // live: only the newest ones come back
      x.taken.add(evId);
      x.nEvents += 1;
      fresh = true;
    }
    if (wave && avgN === 1) { x.plot.setView(ch.present ? ch : null); return undefined; }
    if (wave) {                                          // the mean of the newest N waveforms
      if (fresh) x.ring.add(Int32Array.from(y));
      if (!x.ring.count) { x.plot.setView(null); return null; }
      const mean = x.ring.mean(), nb = Math.min(BASELINE_N, mean.length);
      let base = 0;
      for (let i = 0; i < nb; i++) base += mean[i] / nb;
      x.plot.setView({ ...ch, present: true, data: { mode: "raw", x0: 0, step: 1, y: Array.from(mean) }, baseline: base, nsamps: mean.length });
      clear(x.meta, t("mean of {k}", { k: x.ring.count }));
      return span ? { ...measure(mean, span[0], span[1], base, boardInfo.sample_ns), float: true } : null;   // of the mean shown
    }
    if (!x.xy) x.xy = new XYPlot(x.xyEl, { readout: x.read });
    if (plotMode === "fft") {
      if (fresh && y.length > 8) { const ps = powerSpectrum(y); x.ring.add(Float32Array.from(ps.power)); x.fftN = ps.n; x.fftM = ps.m; }
      if (!x.ring.count) { x.xy.set([], [], {}); return undefined; }
      const sp = spectrumDb(x.ring.sum, x.ring.count, x.fftN, x.fftM, boardInfo.sample_ns, boardInfo.adc_max);
      const f = sp.freq.slice(1), db = sp.db.slice(1);           // without the mean (0 Hz)
      let k = 0;
      for (let i = 1; i < db.length; i++) if (db[i] > db[k]) k = i;
      x.xy.set(f, db, { xLabel: "MHz", yLabel: "dBFS", stepped: false, xFmt: (v) => `${fmtNum(v, 2)} MHz`, yFmt: (v) => `${fmtNum(v, 1)} dBFS` });
      clear(x.meta, t("peak {f} MHz, {db} dBFS · mean of {k}", { f: fmtNum(f[k] || 0, 2), db: fmtNum(db[k] || 0, 1), k: x.ring.count }));
      return undefined;
    }
    if (fresh) x.hist.add(y);                            // the samples histogram
    const b = x.hist.bins(300), st = x.hist.stats();
    x.xy.set(b.xs, b.ys, { xLabel: b.width > 1 ? t("ADC ({w} codes a bin)", { w: b.width }) : "ADC", yLabel: t("entries"), stepped: true,
                         xFmt: (v) => `ADC ${fmtInt(v)}${b.width > 1 ? `–${fmtInt(v + b.width - 1)}` : ""}`, yFmt: (v) => fmtInt(v) });
    clear(x.meta, st.n ? `${fmtInt(st.n)} · μ ${fmtNum(st.mean, 1)} · σ ${fmtNum(st.std, 2)}` : null);
    return undefined;
  }

  function setPlot(v) {
    plotMode = v;
    store("wave.plot", v);
    plotSeg.set(v);
    if (v !== "wave" && span) setSpan(null);
    restart();
  }

  function setUnit(v) {
    yUnit = v;
    store("wave.unit", v);
    unitSeg.set(v);
    cards.forEach((c) => c.plot.setUnit(v, mvPerAdc));
  }

  // the means and histograms begin again (Restart, another plot type or average, board, file or source)
  function restart() {
    clearAcc();
    stopped = false;
    showControls();
    load(true);
  }

  function clearCard(x) {
    x.ring = new Ring(avgN);
    x.hist.clear();
    x.taken = new Set();
    x.nEvents = 0;
  }

  function clearAcc() { cards.forEach((x) => clearCard(x)); }

  function eventsTaken() {                             // the events in the histograms (of the first channel shown)
    const x = cards.find((c) => c.ch !== null);
    return x ? x.nEvents : 0;
  }

  function clearPlots() {
    cards.forEach((x) => {
      x.plot.setView(null);
      if (x.xy) x.xy.set([], [], {});
      setText(x.title, "");
      clear(x.meta);
      clear(x.warn);
      x.cells.forEach((c, i) => setText(c, i ? "—" : ""));
    });
  }

  function show(view) {
    const s = view.settings || {};
    boardInfo = { sample_ns: s.sample_ns || SAMPLE_NS, adc_max: s.adc_max || ADC_MAX, pre_trigger: s.pre_trigger,
                  record_length: s.record_length, model: s.model };
    if (s.mv_per_adc && s.mv_per_adc !== mvPerAdc) { mvPerAdc = s.mv_per_adc; cards.forEach((c) => c.plot.setUnit(yUnit, mvPerAdc)); }
    // where the means and histograms come from (one board of one run, or one file), and which event this is
    const from = view.source === "file" ? `file|${view.path || file}` : `live|${view.board}|${s.run_dir || ""}`;
    const evId = view.source === "file" ? `event ${view.event}` : `${view.trigger_id}|${view.timestamp}`;
    eventFacts = { trigger_id: view.trigger_id, timestamp: view.timestamp };
    if (view.n_channels && view.n_channels !== nChannels) { nChannels = view.n_channels; loadSel(); showCards(); }
    const ref = view.channels.find((ch) => ch.trigger_at !== null && ch.trigger_at !== undefined);
    t0 = ref ? ref.trigger_at : null;
    showSpanText();                                    // the times of the range measured, at this board's sample time
    if (view.source === "live") {                      // the thresholds the run uses, for Tune…
      for (const ch of view.channels) if (ch.threshold !== null && ch.threshold !== undefined) runThr.set(`${view.board}|${ch.ch}`, ch.threshold);
    }
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
      else if (ch.notKept) emptyText = t("not in the paused event");
      else if (!ch.present) emptyText = isFile ? t("not in this event") : t("waiting for data");
      x.empty.classList.toggle("hidden", !emptyText);
      setText(x.empty, emptyText);
      clear(x.meta, ch.present && ch.data ? [h("span.wave-mu", `μ ${ch.mean.toFixed(0)} · `), `σ ${ch.std.toFixed(2)}`] : null);
      x.meta.title = ch.present && ch.data ? `${fmtInt(ch.nsamps)} ${t("samples")} · μ ${fmtNum(ch.mean, 1)} · σ ${fmtNum(ch.std, 2)} · baseline ${fmtNum(ch.baseline, 1)} · min ${fmtInt(ch.minimum)} · max ${fmtInt(ch.maximum)}` : "";
      const m = showPlot(x, ch, from, evId);
      showMeasure(x, ch, m === undefined ? ch.measure : m);
    });
    showControls();
  }

  // m: the measurement of the trace shown (the server's, or the browser's for the mean of N: not whole counts)
  function showMeasure(x, ch, m) {
    const ns = boardInfo.sample_ns;
    const adc = m && m.float ? (v) => fmtNum(v, 1) : fmtInt;
    const values = !m || !m.n ? [] : [adc(m.min), adc(m.max), fmtNum(m.mean, 1), fmtNum(m.std, 2),
                                      (m.peak > 0 ? "+" : "") + fmtNum(m.peak, 1),
                                      `${fmtInt(m.peak_at)} · ${fmtTime((m.peak_at - (t0 ?? 0)) * ns)}`,
                                      m.first !== undefined ? `${adc(m.first)} → ${adc(m.last)} (${m.last - m.first >= 0 ? "+" : ""}${adc(m.last - m.first)})` : "—",
                                      fmtInt(m.area)];
    setText(x.cells[0], `ch${ch.ch}`);
    for (let i = 1; i < 9; i++) setText(x.cells[i], values[i - 1] ?? "—");
    x.row.classList.toggle("muted", !values.length);
    x.row.title = !ch.present ? t("no waveform") : m && !m.n ? t("the waveform ends before the range")
                : m ? t("{n} samples · baseline {b}", { n: fmtInt(m.n), b: fmtNum(m.baseline ?? ch.baseline, 1) }) : "";
  }

  // a run starting or ending: the boards of the live view may change
  function runState() {
    const r = app.state.run;
    return r && r.active ? `run ${r.run_dir}` : `last ${(r && r.last && r.last.run_dir) || ""}`;
  }
  let runKey = runState();

  fillBoards();                    // from the configuration until the server says which boards the live view has
  showCards();
  showControls();
  enlarge(null);
  setView(viewMode);
  const timer = setInterval(() => { if (source === "live" && !paused) { if (viewMode === "combined") combo.load(false); else load(false); } },
                            Math.max(200, app.state.settings.values.refresh_ms || 500));
  refreshLiveBoards();             // then the live view's boards, and the first live load
  requestAnimationFrame(() => { layout(); if (source === "file") openFile(file); else if (liveBoards) load(true); });

  return {
    el: panel,
    controls,
    showBoard,
    board: () => boardSel.value,
    update(type) {
      if (type === "run") {
        const k = runState();
        if (k !== runKey) { runKey = k; refreshLiveBoards(); }
        showControls();
        showTune();
      }
      if (type === "config" && (!liveBoards || liveFrom === "configuration")) refreshLiveBoards();
      combo.update(type);
    },
    restyle() { cards.forEach((c) => { c.plot.restyle(); c.plot.setRange(rangeFor(c.ch)); c.plot.setShow(lines); c.plot.setMouse(mouse); c.plot.setSpan(span); if (c.xy) c.xy.restyle(); }); load(true); combo.restyle(); },
    unmount() { gone = true; clearInterval(timer); ro.disconnect(); cards.forEach((c) => { c.plot.destroy(); if (c.xy) c.xy.destroy(); }); combo.unmount(); },
  };
}

// Tool: the boards' housekeeping, as the DAQ program reads it every few seconds during a run (Configuration:
// housekeeping every N s) into RUN_BOARD_hk.csv next to the data: temperatures, fans and DC-DC, the trigger
// counters, the dead time, each channel's self-trigger rate, and the error flags. Live (the run going, or the
// last run) or from a data file (the housekeeping file of its board); one quantity at a time over the run time,
// and every value of the newest reading.
import { SLOW_MS, get } from "../api.js";
import { T, t } from "../i18n.js";
import { basename, chooseFile, clear, fail, fmtInt, fmtNum, h, icon, segmented, setText, store } from "../ui.js";

const LABELS = {
  temp_air_in: T("air in"), temp_air_out: T("air out"), temp_core: T("FPGA core"), temp_first_adc: T("first ADC"),
  temp_last_adc: T("last ADC"), temp_hottest_adc: T("hottest ADC"), temp_dcdc: T("DC-DC"),
  fan1_speed: T("fan 1"), fan2_speed: T("fan 2"), dcdc_vin: T("DC-DC in (V)"), dcdc_vout: T("DC-DC out (V)"),
  dcdc_iout: T("DC-DC current (A)"), dcdc_duty: T("DC-DC duty cycle"), core_freq: T("core clock"),
  error_flags: T("error flags"), acq_status: T("acquisition status"), trigger_cnt: T("triggers counted"),
  lost_trigger_cnt: T("triggers lost"), realtime: T("real time"), deadtime: T("dead time"), livetime: T("live time"),
  icr_hz: T("incoming triggers (Hz)"), rate_hz: T("events taken (Hz)"), dead_frac: T("dead time (%)"), events: T("events"),
};
const GROUPS = [
  ["temps", T("Temperatures"), (k) => k.startsWith("temp_"), "°C"],
  ["rates", T("Trigger rates"), (k) => k === "icr_hz" || k === "rate_hz", "Hz"],
  ["dead", T("Dead time"), (k) => k === "dead_frac", "%"],
  ["counts", T("Counters"), (k) => k === "trigger_cnt" || k === "lost_trigger_cnt", ""],
  ["power", T("Fans and DC-DC"), (k) => /^(fan|dcdc_|core_freq)/.test(k), ""],
  ["channels", T("Channel trigger rates"), (k) => /^ch\d+_trg_rate$/.test(k), "Hz"],
];
const PALETTE = ["#2f6fdf", "#d9452b", "#1f9d55", "#c27c0e", "#8a4fd8", "#0f8fa8", "#d23f8f", "#6b8e23",
                 "#1d4ed8", "#b91c1c", "#047857", "#a16207", "#6d28d9", "#0e7490", "#be185d", "#4d7c0f"];
const MAX_LINES = 16;

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function label(k) {
  const m = /^ch(\d+)_trg_rate$/.exec(k);
  return m ? `ch${m[1]}` : t(LABELS[k] || k);
}

function fmtValue(k, v) {
  if (v === null || v === undefined) return "—";
  if (k === "error_flags" || k === "acq_status") return `0x${Math.round(v).toString(16)}`;
  if (k === "dead_frac") return `${fmtNum(100 * v, 2)} %`;
  if (k.startsWith("temp_")) return `${fmtNum(v, 1)} °C`;
  if (Number.isInteger(v)) return fmtInt(v);
  return fmtNum(v, Math.abs(v) >= 100 ? 0 : 2);
}

export default {
  id: "hk",
  title: T("Board housekeeping"),
  icon: "chart",
  summary: T("temperatures, fans, DC-DC, error flags, trigger counters and dead time of every board, over the run"),
  open: false,
  badge(app) {
    const r = app.state.run, boards = (r && (r.active ? r.boards : r.last && r.last.boards)) || [];
    const bad = boards.filter((b) => b.error_flags || b.board_fail).map((b) => b.name);
    return bad.length ? t("errors on {list}", { list: bad.join(", ") }) : "";
  },
  make(body, ctx) {
    const app = ctx.app;
    let source = store("hk.source") === "file" && store("hk.file") ? "file" : "live";
    let file = store("hk.file") || "", group = store("hk.group") || "temps", board = store("hk.board") || "";
    let data = null, u = null, sig = "", gone = false, wasActive = Boolean(app.state.run && app.state.run.active);
    const srcSeg = segmented([["live", t("Live")], ["file", t("File")]], source, (v) => setSource(v), { label: t("Source") });
    const fileChip = h("span.chip");
    const openBtn = h("button.btn.btn-sm", { type: "button", onclick: pick }, icon("folder"), t("Open file…"));
    const boardSel = h("select.input", { "aria-label": t("Board"), onchange: () => { board = boardSel.value; store("hk.board", board); show(); } });
    const groupSeg = segmented(GROUPS.map(([k, v]) => [k, t(v)]), group, (v) => { group = v; store("hk.group", v); show(); }, { label: t("Show") });
    const info = h("div.text2.small");
    const plotEl = h("div.plot");
    const empty = h("div.wave-empty.hidden");
    const table = h("div.hk-values");
    body.append(h("div.stack", { style: "gap:10px" },
      h("div.row.wrap", h("span.label", t("Source")), srcSeg, fileChip, openBtn, h("span.label", t("Board")), boardSel),
      h("div.row.wrap", h("span.label", t("Show")), groupSeg),
      info,
      h("div.hk-plot", h("div.wave-plot", plotEl, empty)),
      h("div", h("div.label", { style: "margin-bottom:6px" }, t("The newest reading")), table)));

    async function pick() {
      const path = await chooseFile({ title: t("The housekeeping of which data file?") });
      if (!path) return;
      file = path;
      store("hk.file", path);
      setSource("file");
    }

    // choice counts the choices (Live, a file), ask the requests: only the answer to the newest request of the
    // choice shown is used, so live numbers never land on a file's plot
    let choice = 0, ask = 0;
    function setSource(v) {
      if (v === "file" && !file) { pick(); srcSeg.set(source); return; }
      choice += 1;
      source = v;
      store("hk.source", v);
      srcSeg.set(v);
      load();
    }

    async function load() {
      const my = choice, n = ++ask, from = source, path = file;
      let d = null;
      try {
        if (from === "file") {
          const one = await get("/api/files/hk", { path }, { timeout: SLOW_MS });
          d = { source: "file", run: one.run, boards: [{ ...one, name: one.board }] };
        } else d = await get("/api/live/hk");
      } catch (err) {
        if (from === "file" && my === choice && n === ask) fail(err);
        d = null;
      }
      if (gone || my !== choice || n !== ask || from !== source) return;
      data = d;
      show();
    }

    function current() {
      const list = (data && data.boards) || [];
      return list.find((b) => b.name === board) || list[0] || null;
    }

    function show() {
      fileChip.classList.toggle("hidden", source !== "file");
      setText(fileChip, basename(file));
      fileChip.title = file;
      const list = (data && data.boards) || [];
      const keep = boardSel.value || board;
      clear(boardSel, list.map((b) => h("option", { value: b.name }, `${b.name} · ${b.model || ""}`)));
      if (list.some((b) => b.name === keep)) boardSel.value = keep;
      const b = current();
      const cols = (b && b.columns) || {};
      const rows = cols.run_s ? cols.run_s.length : 0;
      if (!b) setText(info, source === "live" ? t("No run yet: the housekeeping is read during runs.") : "");
      else if (b.missing) setText(info, t("No housekeeping file beside this data file ({p})", { p: basename(b.path) }));
      else {
        const run = data.run ? `${data.run} · ` : "";
        setText(info, `${run}${b.name}: ` + t("{n} readings ({k} shown) · {p}", { n: fmtInt(b.rows || rows), k: fmtInt(b.shown || rows), p: basename(b.path || "") }) +
                (source === "live" && data.running ? ` · ${t("the run is going")}` : ""));
      }
      const [, , match, unit] = GROUPS.find((g) => g[0] === group) || GROUPS[0];
      let keys = Object.keys(cols).filter(match);
      if (group === "channels") keys = keys.filter((k) => cols[k].some((v) => v)).slice(0, MAX_LINES);
      const scale = group === "dead" ? 100 : 1;
      const x = cols.run_s || [];
      const lines = keys.map((k) => cols[k].map((v) => (v === null ? null : v * scale)));
      const newSig = JSON.stringify([keys, unit, css("--grid")]);
      if (!u || newSig !== sig) { sig = newSig; build(keys, unit, [x, ...lines]); } else u.setData([x, ...lines]);
      const missing = !keys.length || !x.length;
      empty.classList.toggle("hidden", !missing);
      setText(empty, !b ? t("no housekeeping yet") : t("the board did not report these values"));
      // the newest reading
      const last = (k) => { const v = cols[k]; return v && v.length ? v[v.length - 1] : null; };
      const names = Object.keys(cols).filter((k) => k !== "time" && k !== "run_s" && !/^ch\d+_trg_rate$/.test(k));
      clear(table, names.length ? names.map((k) => h("div.hk-cell", { class: k === "error_flags" && last(k) ? "bad" : "" },
                                                    h("span.label", label(k)), h("b.num", fmtValue(k, last(k)))))
                                : h("span.text2.small", "—"));
    }

    function build(keys, unit, aligned) {
      if (u) u.destroy();
      const grid = css("--grid"), axis = css("--axis"), text2 = css("--text-2");
      const axisStyle = { stroke: text2, grid: { stroke: grid, width: 1 }, ticks: { stroke: axis, width: 1, size: 4 },
                          font: "11px system-ui, sans-serif", labelFont: "11px system-ui, sans-serif" };
      u = new uPlot({
        width: Math.max(200, plotEl.clientWidth), height: 260,           // the legend goes under it
        padding: [10, 12, 0, 0], legend: { show: true, live: true },
        scales: { x: { time: false } },
        axes: [{ ...axisStyle, size: 36, label: t("run time (s)") }, { ...axisStyle, size: 56, label: unit }],
        series: [{ label: t("s") }, ...keys.map((k, i) => ({ label: label(k), stroke: PALETTE[i % PALETTE.length], width: 1.5,
                                                              spanGaps: true, points: { show: aligned[0].length < 40 } }))],
        cursor: { drag: { x: false, y: false } },
      }, aligned, plotEl);
    }

    const ro = new ResizeObserver(() => { if (u && plotEl.clientWidth > 20 && plotEl.clientWidth !== u.width) u.setSize({ width: plotEl.clientWidth, height: 260 }); });
    ro.observe(plotEl);
    const timer = setInterval(() => { if (source === "live" && app.state.run && app.state.run.active) load(); }, 3000);
    load();
    return {
      update(type) {
        if (type !== "run") return;
        const now = Boolean(app.state.run && app.state.run.active);
        if (source === "live" && now !== wasActive) load();          // a run started or ended: its boards
        wasActive = now;
      },
      unmount() { gone = true; clearInterval(timer); ro.disconnect(); if (u) u.destroy(); },
    };
  },
};

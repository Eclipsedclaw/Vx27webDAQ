// Configuration page: everything a run does, for the run, the trigger setup and each board with its channels.
// Operators edit (the server keeps the configuration of the next run, and every page sees the edits); viewers see
// the same form, read-only. Each board shows the exact CAEN FELib parameters it will get ("What this board gets").
// The check list stays in view below the divider. Edits go to the server with the version they started from: when
// another operator (or Tune… during a run) changed it meanwhile, this page's changes are put onto theirs.
import { post, put } from "../api.js";
import { T, t } from "../i18n.js";
import { CONF_FILES, basename, chip, choosePath, clear, confirmDialog, dialog, fail, fmtBytes, fmtInt, fmtNum, h, icon, segmented,
         setText, splitter, store, toast, toggle } from "../ui.js";
import { ROLE_NAMES, SETUP_NAMES, sendToBoards, sequenceGoing, viewerNotice } from "./run.js";

const SETUP_TEXT = {
  master: T("Cables: CLK-OUT → CLK-IN and TRG-OUT → TRG-IN from the first board (the master, e.g. the VX2730) to the next, and so on along the chain. The master triggers on its own channels (the logic below) and sends its trigger out on TRG-OUT; every other board records when it arrives at its TRG-IN and passes it on. Every board records the same moments; the boards further down get the trigger a few ns later (a fixed delay: give them a little more pre-trigger)."),
  external: T("Cables: a trigger pulse into TRG-IN of the first board; TRG-OUT of each board into TRG-IN of the next one. Every board records every trigger, so the trigger IDs of the boards agree; the boards further down the chain get the trigger a few ns later (a fixed delay)."),
  self: T("No trigger cables. Each board triggers when its own channels fire (the logic below). With the clock chain the boards share one clock and start together, so their timestamps agree and events of different boards can be matched by time."),
  logic_or: T("Cables: TRG-OUT of every board into a logic unit (for example a CAEN V976 making the OR), and its outputs back into TRG-IN of every board, with cables of the same length. Each board sends out its channel triggers; every board records the common trigger."),
  periodic: T("For pedestals and tests: software triggers sent by the DAQ program, or each board's internal test pulse. No trigger cables, unless the software trigger goes along the TRG-OUT → TRG-IN chain."),
};
const SYNC_TEXT = {
  chain: T("Cables: CLK-OUT of each board into CLK-IN of the next one (CAEN A319A cable). The first board is the master: its clock and its start drive the others, and the run delays line up their starts, so all timestamps count the same clock."),
  independent: T("No clock cables: each board runs on its own clock and is started by the DAQ program one after the other; their timestamps do not line up."),
};
const PERIODIC = [["software", T("software trigger")], ["test_pulse", T("test pulse")]];
const TARGETS = [["each", T("each board")], ["chain", T("master, passed on TRG-OUT → TRG-IN")]];
const LOGIC_TEXT = { OR: T("any channel"), AND: T("all channels"), Majority: T("at least N channels") };
const MASTER_OUT_TEXT = { ITLA: T("its channel trigger (ITLA)"), AcceptTrg: T("the triggers it took (AcceptTrg)") };
const COMBINE_TEXT = { A: T("logic A"), B: T("logic B"), A_AND_B: T("A AND B"), A_OR_B: T("A OR B") };
const START_TEXT = { SINlevel: T("while S-IN is high"), SINedge: T("on the rising edge of S-IN"),
                     FirstTrigger: T("on the first pulse on TRG-IN"), LVDS: T("on an LVDS signal"), P0: T("on the VME P0 signal") };
const DAC_TEXT = { Static: T("a fixed level"), Ramp: T("a ramp over the ADC range"), Sin5MHz: T("a 5 MHz sine"),
                   Square: T("a square wave"), IPE: T("pulses (the pulse emulator)"), ChInput: T("a copy of a channel's input"),
                   MemOccupancy: T("the memory occupancy"), ChSum: T("the sum of the channels"), OverThrSum: T("the channels over threshold") };

// ---- this page's edits onto someone else's (a 409 conflict): what changed here since `base`, field by field ----
function clone(x) { return JSON.parse(JSON.stringify(x)); }

function same(a, b) {                       // deep equality of JSON values, whatever the order of the keys
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null || Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  return ka.length === Object.keys(b).length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k]));
}

const plain = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

function mergeInto(target, base, mine) {    // the fields of `mine` that differ from `base`, onto `target`
  for (const k of Object.keys(mine)) {
    const a = mine[k], b = base ? base[k] : undefined;
    if (same(a, b)) continue;
    if (plain(a) && plain(b) && plain(target[k])) mergeInto(target[k], b, a);
    else if (k === "channels" && Array.isArray(a) && Array.isArray(b) && Array.isArray(target[k])) {
      for (let c = 0; c < a.length && c < b.length && c < target[k].length; c++) {     // channels by index
        if (!same(a[c], b[c])) mergeInto(target[k][c], b[c], a[c]);
      }
    } else target[k] = clone(a);
  }
}

// boards by index and name; when boards were added or removed here: this page's list, with every board it did not
// change as the server has it now
function mergeBoards(target, base, mine) {
  if (mine.length === base.length) {
    mine.forEach((mb, i) => {
      const bb = base[i];
      if (same(mb, bb)) return;
      const tb = target[i] && target[i].name === bb.name ? target[i] : target.find((x) => x.name === bb.name);
      if (tb) mergeInto(tb, bb, mb);
    });
    return target;
  }
  return mine.map((mb) => {
    const bb = base.find((x) => x.name === mb.name), tb = target.find((x) => x.name === mb.name);
    return bb && tb && same(mb, bb) ? tb : clone(mb);
  });
}

function fmtNs(ns) {
  if (ns === null || ns === undefined || !Number.isFinite(ns)) return "—";
  if (ns < 1000) return `${fmtNum(ns, ns % 1 ? 1 : 0)} ns`;
  const [v, u] = ns < 1e6 ? [ns / 1e3, "µs"] : ns < 1e9 ? [ns / 1e6, "ms"] : [ns / 1e9, "s"];
  return `${Number(v.toPrecision(4))} ${u}`;
}

export function configPage(root, app) {
  const can = app.can("control");
  const page = h("div.page.fit");
  root.appendChild(page);
  const path = h("span.sub.mono.small.break"), dirty = h("span.row");
  page.appendChild(h("div.page-head", h("h1", t("Configuration")), path, dirty));
  const notice = viewerNotice(app, t("You are watching: the configuration is read-only. Operators edit it."));
  if (notice) page.appendChild(notice);
  const sendBtn = h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: sendBoards,
                                          title: t("Reset every board switched on and send it the configuration now, as at the start of a run, without taking data") },
                    icon("send"), t("Send to boards"));
  page.appendChild(h("div.toolbar",
    h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: newConfig }, icon("plus"), t("New")),
    h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: openFile }, icon("folder"), t("Open…")),
    h("button.btn.btn-sm.btn-primary", { type: "button", disabled: !can, onclick: () => save(null) }, icon("download"), t("Save")),
    h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: saveAs }, t("Save as…")),
    h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: reload }, icon("reset"), t("Reload from file")),
    h("span.sep"),
    sendBtn,
    h("span.spacer"), h("span.hint", t("Changes are kept on the server for the next run; Save writes them into a .json file."))));

  const form = h("div.stack", { style: "padding-right:4px" });
  const top = h("div", { style: "overflow:auto;flex:1 1 auto;min-height:0" }, form);
  const problemsBox = h("ul.problems");
  const checkCard = h("div.card.fill", h("div.card-head", h("h2.card-title", t("Check"))), h("div.card-body.scroll", problemsBox));
  page.appendChild(splitter(top, checkCard, { key: "config.check", vertical: true, initial: 0.78 }));

  let model = clone(app.state.config.config);    // what the form shows and edits
  let base = clone(model);                        // the server's copy the edits started from
  let version = app.state.config.version;         // its version
  let timer = null;                               // an edit waiting to go (400 ms after the last)
  let pendingRebuild = false;                     // an edit that changes the form: built again from the server's answer
  let inputs = {};

  // ---- the note ----
  let noteTimer = null;
  const noteInput = h("textarea.input.note-input", { rows: "3", maxlength: "4000", disabled: !can, "aria-label": t("Note"),
                      placeholder: can ? t("What these runs are about: the source and where it is, high voltage, cables, what is being tested…") : "",
                      oninput: () => { clearTimeout(noteTimer); noteTimer = setTimeout(() => sendNote().catch(notify), 600); } });
  noteInput.value = app.state.config.note || "";
  top.insertBefore(h("div.card", { style: "margin-bottom:12px" },
    h("div.card-head", h("h2.card-title", t("Note")),
      h("span.card-sub", t("Kept with the configuration (Save writes it into the file) and with every run: in its run folder, in the header of each data file, and next to it as NAME.json"))),
    h("div.card-body", noteInput)), form);

  // ---- sending edits ----
  // One write at a time (the configuration, then the note), each with the version it started from. A 409 conflict
  // (someone else changed it meanwhile): this page's changes since `base` onto the server's copy, sent once more; a
  // second conflict: the server's copy, and a toast. A refused or lost write keeps the edits and says so (a red
  // chip); they go with the next edit, or again after 5 s when the server did not answer.
  let pumping = null, wanted = false, noteWanted = false;   // the writes going; another one to follow
  let unsent = null, notified = null, retryTimer = null;    // why the edits are not on the server; the last toast
  let stale = false;                                        // a newer configuration came while edits were waiting
  let leaving = false;                                      // the page is being closed: the writes must outlive it

  function edited(rebuild = false) {
    pendingRebuild = pendingRebuild || rebuild;
    showInfo();
    clearTimeout(timer);
    timer = setTimeout(() => send().catch(notify), 400);
  }

  function notify(err) {                       // a write that failed by itself (not asked for by Save): told once
    const msg = err && err.message ? err.message : String(err);
    if (msg !== notified) { notified = msg; fail(err); }
  }

  function send() {                            // resolves when the edits made so far are on the server, else rejects
    clearTimeout(timer);
    timer = null;
    wanted = true;
    return pumpNow();
  }

  function sendNote() {
    clearTimeout(noteTimer);
    noteTimer = null;
    noteWanted = true;
    return pumpNow();
  }

  function pumpNow() {
    clearTimeout(retryTimer);
    if (!pumping) pumping = pump().finally(() => { pumping = null; });
    return pumping;
  }

  async function pump() {
    let failure = null;
    while (!failure && (wanted || noteWanted)) {
      if (wanted) { wanted = false; try { await putConfig(false); } catch (err) { failure = err; } }
      if (noteWanted) { noteWanted = false; try { await putNote(false); } catch (err) { failure = failure || err; } }
    }
    if (failure) throw failure;
  }

  function conflict(err) { return err && err.status === 409 && err.data && err.data.conflict && err.data.current; }

  async function putConfig(second) {
    const sent = clone(model), rebuild = pendingRebuild;
    pendingRebuild = false;
    const k = sent.boards.findIndex((b) => { const nc = b.n_channels ?? 0; return !Number.isInteger(nc) || nc < 0 || nc > 64; });
    if (k >= 0) {                              // the server would refuse the whole configuration (400)
      pendingRebuild = pendingRebuild || rebuild;
      const err = new Error(t("The number of channels must be a whole number from {lo} to {hi}", { lo: 0, hi: 64 }));
      setUnsent(err.message);
      throw err;
    }
    let P;
    try {
      P = await put("/api/config", { config: sent, base: version }, { keepalive: leaving });
    } catch (err) {
      if (conflict(err) && !second) {
        rebase(err.data.current);
        pendingRebuild = pendingRebuild || rebuild;
        return putConfig(true);
      }
      if (conflict(err)) {
        adopt(err.data.current);
        setUnsent(null);
        toast(t("Someone else changed the configuration at the same time: your edit could not be applied"), "error");
        throw err;
      }
      pendingRebuild = pendingRebuild || rebuild;
      failed(err);
      throw err;
    }
    setUnsent(null);
    took(P, sent, rebuild);
    return undefined;
  }

  async function putNote(second) {
    let c;
    try {
      c = await put("/api/note", { note: noteInput.value, base: version }, { keepalive: leaving });
    } catch (err) {
      if (conflict(err) && !second) {          // the configuration changed meanwhile: onto it, and the note again
        rebase(err.data.current);
        if (!same(model, base)) wanted = true;
        return putNote(true);
      }
      if (conflict(err)) {
        adopt(err.data.current);
        toast(t("Someone else changed the configuration at the same time: your edit could not be applied"), "error");
      } else failed(err);
      throw err;
    }
    newest(c);
    base = clone(c.config);                    // a note changes only the note (no one else wrote, or it was a 409)
    version = c.version;
    if (app.state.config.version <= version) stale = false;
    showHeader();
    return undefined;
  }

  function failed(err) {                       // not on the server: kept here, and sent again when it may work
    setUnsent(err.message || String(err));
    if (!err.status || err.status >= 500) retryTimer = setTimeout(() => { if (unsent) flush().catch(notify); }, 5000);
  }

  function setUnsent(why) {
    unsent = why;
    if (!why) notified = null;
    showHeader();
  }

  function newest(c) {                         // the server's copy c, unless a newer one came meanwhile
    if (!app.state.config || !(c.version < app.state.config.version)) app.state.config = c;
    return app.state.config;
  }

  // the server took `sent` and answered with its configuration P
  function took(P, sent, rebuild) {
    newest(P);
    base = clone(P.config);
    version = P.version;
    if (app.state.config.version <= version) stale = false;
    if (timer !== null || wanted || !same(model, sent)) {   // edits made while it went: they go next, the rebuild with them
      pendingRebuild = pendingRebuild || rebuild;
      showProblems(); showPlans(); showInfo(); showHeader();
      return;
    }
    if (stale && !typing()) { adopt(); return; }          // someone else's newer change came meanwhile
    if (rebuild || !same(P.config, sent)) adopt(P); else { showProblems(); showPlans(); showInfo(); showHeader(); }
  }

  // someone else changed the configuration since `base`: this page's changes since then onto the server's copy now
  function rebase(current) {
    const theirs = newest(current);
    const merged = clone(theirs.config);
    mergeInto(merged.run, base.run, model.run);
    mergeInto(merged.trigger, base.trigger, model.trigger);
    merged.boards = mergeBoards(merged.boards, base.boards, model.boards);
    model = merged;
    base = clone(theirs.config);
    version = theirs.version;
    stale = false;
    redraw();
  }

  // the edits and the note still waiting go now (Save, Send to boards, a reload, the page closing); rejects when the
  // server did not take them
  function flush(keepalive = false) {
    if (keepalive) leaving = true;
    if (timer) { clearTimeout(timer); timer = null; wanted = true; }
    if (noteTimer) { clearTimeout(noteTimer); noteTimer = null; noteWanted = true; }
    if (unsent) { wanted = wanted || !same(model, base); noteWanted = noteWanted || noteInput.value !== (app.state.config.note || ""); }
    if (!wanted && !noteWanted && !pumping) {
      if (unsent) setUnsent(null);             // the edits that did not go were undone meanwhile: nothing waits
      return Promise.resolve();
    }
    return pumpNow();
  }

  function idle() { return !timer && !pumping && !noteTimer && !unsent && same(model, base); }

  function typing() {
    const el = document.activeElement;
    return form.contains(el) && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
  }

  // ---- form helpers ----
  function field(label, control, hint) {
    return [h("span.label", label), h("div.row.nowrap", control), hint ? h("span.hint.hint-cell", hint) : null].filter(Boolean);
  }

  function number(get, set, where, { lo = 0, hi = 1e12, step = 1, width, float = false, rebuild = false } = {}) {
    const el = h("input.input.num", { type: "number", min: String(lo), max: String(hi), step: String(step), value: String(get() ?? ""),
                                      disabled: !can,
                                      oninput: () => { const v = Number(el.value); if (el.value !== "" && Number.isFinite(v)) { set(float ? v : Math.round(v)); edited(rebuild); } } });
    if (width) el.style.width = width;
    if (where) inputs[where] = el;
    return el;
  }

  function optional(get, set, where, placeholder, opts = {}) {     // a number that may be empty (CAEN's default)
    const el = h("input.input.num", { type: "number", value: get() === null || get() === undefined ? "" : String(get()), disabled: !can,
                                      placeholder, style: `width:${opts.width || "110px"}`,
                                      oninput: () => { const v = el.value === "" ? null : Number(el.value); if (v === null || Number.isFinite(v)) { set(v === null ? null : opts.float ? v : Math.round(v)); edited(); } } });
    if (where) inputs[where] = el;
    return el;
  }

  function text(get, set, where, width) {
    const el = h("input.input", { value: get(), disabled: !can, oninput: () => { set(el.value.trim()); edited(); } });
    if (width) el.style.width = width;
    if (where) inputs[where] = el;
    return el;
  }

  function select(options, get, set, where, rebuild = true) {
    const el = h("select.input", { disabled: !can, onchange: () => { set(el.value); edited(rebuild); if (rebuild) build(); } },
                 options.map(([v, label]) => h("option", { value: String(v), selected: String(v) === String(get()) }, label)));
    if (where) inputs[where] = el;
    return el;
  }

  // ---- the form ----
  function build() {
    inputs = {};
    const c = app.state.config, r = model.run, g = model.trigger;
    // the run
    const runGrid = h("div.form-grid");
    const sync = segmented([["chain", t("clock chain")], ["independent", t("independent boards")]], r.sync,
                           (v) => { r.sync = v; edited(true); build(); }, { disabled: !can, label: t("synchronization") });
    inputs["run.sync"] = sync;
    const dataDir = text(() => r.data_dir, (v) => { r.data_dir = v; }, "run.data_dir", "min(520px, 100%)");
    const dataPick = h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: async () => {
      const p = await choosePath({ title: t("Data folder"), want: "folder", start: model.run.data_dir });
      if (!p) return;
      model.run.data_dir = p;                    // the model and the input of now: the form may have been built again meanwhile
      if (inputs["run.data_dir"]) inputs["run.data_dir"].value = p;
      edited();
    } }, icon("folder"), t("Choose…"));
    runGrid.append(
      ...field(t("synchronization"), sync, t(SYNC_TEXT[r.sync] || "")),
      ...field(t("run time"), [number(() => r.duration_s, (v) => { r.duration_s = v; }, "run.duration_s", { lo: 0, hi: 1e7, width: "110px" }), h("span.text2", t("s")), h("span.live", { id: "live-duration" })],
               t("0: until someone presses Stop")),
      ...field(t("events"), [number(() => r.max_events, (v) => { r.max_events = v; }, "run.max_events", { lo: 0, hi: 1e12, width: "130px" })],
               t("stop when the first board has this many events; 0: no limit")),
      ...field(t("keep the data"), toggle(t("write each board's stream to its .raw file"), r.write_raw, (on) => { r.write_raw = on; edited(); }, { disabled: !can }),
               t("Off: only the live view; nothing is kept")),
      ...field(t("data folder"), [dataDir, dataPick], t("RUN_BOARD.raw for each board, e.g. run00012_b0.raw (the run number is one more than the highest run in this folder); a NAME.json next to each, and NAME.root after the conversion")),
      ...field(t("file size"), [number(() => r.file_mb, (v) => { r.file_mb = v; }, "run.file_mb", { lo: 16, hi: 1 << 20, width: "110px" }), h("span.text2", "MB")],
               t("a new file (_001.raw, _002.raw, …) when a file reaches this size")),
      ...field(t("housekeeping"), [h("span.text2", t("every")), number(() => r.hk_s, (v) => { r.hk_s = v; }, "run.hk_s", { lo: 1, hi: 3600, step: 1, float: true, width: "90px" }), h("span.text2", t("s"))],
               t("each board's temperatures, fans, DC-DC, error flags, trigger and lost-trigger counters and dead time, into RUN_BOARD_hk.csv beside its data (and into its ROOT file); Tools: Board housekeeping")),
      ...field(t("temperature warning"), [number(() => r.temp_warn_c, (v) => { r.temp_warn_c = v; }, "run.temp_warn_c", { lo: 0, hi: 150, step: 1, float: true, width: "90px" }), h("span.text2", "°C")],
               t("a warning in the DAQ output when a board's hottest sensor reaches this; 0: none (the board protects itself from overheating: it stops and needs a reset)")),
      ...field(t("sync window"), [number(() => r.sync_window_ns, (v) => { r.sync_window_ns = v; }, "run.sync_window_ns", { lo: 8, hi: 1e9, step: 8, width: "110px" }), h("span.text2", "ns")],
               t("the sync check (one trigger for all boards): the same trigger ID on two boards counts as one trigger when the timestamps are this close")));
    const runCard = h("div.card", h("div.card-head", h("h2.card-title", t("Run"))), h("div.card-body", runGrid));

    // the trigger
    const tg = h("div.form-grid");
    const setupSeg = segmented(c.setups.map((s) => [s, t(SETUP_NAMES[s] || s)]), g.setup, (v) => { g.setup = v; edited(true); build(); },
                               { disabled: !can, label: t("trigger setup") });
    inputs["trigger.setup"] = setupSeg;
    tg.append(...field(t("trigger setup"), setupSeg), h("span"), h("span.hint.hint-cell.explain-line", t(SETUP_TEXT[g.setup] || "")));
    if (g.setup === "master") {
      const out = segmented(c.master_outs.map((x) => [x, t(MASTER_OUT_TEXT[x])]), g.master_out, (v) => { g.master_out = v; edited(true); build(); },
                            { disabled: !can, label: t("the master sends") });
      inputs["trigger.master_out"] = out;
      tg.append(...field(t("the master sends"), out, t("on TRG-OUT. ITLA: its channel trigger, at once. AcceptTrg: only the triggers it took (none while it is busy), so every board keeps the master's trigger IDs. Try both on the boards and compare the trigger IDs on the Run page")));
    }
    if (g.setup === "self" || g.setup === "logic_or" || g.setup === "master") {
      const logic = segmented(c.logics.map((l) => [l, `${l} · ${t(LOGIC_TEXT[l])}`]), g.logic, (v) => { g.logic = v; edited(true); build(); },
                              { disabled: !can, label: t("channel logic") });
      inputs["trigger.logic"] = logic;
      tg.append(...field(t("channel logic"), logic, t("which of a board's triggering channels (Channels below: \"in trigger\") make its trigger (ITLA)")));
      if (g.logic === "Majority") tg.append(...field(t("majority"), number(() => g.majority, (v) => { g.majority = v; }, "trigger.majority", { lo: 1, hi: 64, width: "90px" }), t("channels needed at the same time")));
      tg.append(...field(t("gate"), [number(() => g.gate_ns, (v) => { g.gate_ns = v; }, "trigger.gate_ns", { lo: 8, hi: 1e6, width: "100px" }), h("span.text2", "ns")],
                         t("how long a channel's trigger stays open, for AND and majority (ITLAGateWidth)")));
      const comb = segmented((c.combines || ["A"]).map((x) => [x, t(COMBINE_TEXT[x] || x)]), g.combine, (v) => { g.combine = v; edited(true); build(); },
                             { disabled: !can, label: t("board trigger") });
      inputs["trigger.combine"] = comb;
      tg.append(...field(t("board trigger"), comb, t("from the two channel logics: logic A (ITLA, the one above) alone, logic B (ITLB) alone, both at once, or either; each channel joins group A or B (Channels: group)")));
      if (g.combine !== "A") {
        const lb = segmented(c.logics.map((l) => [l, `${l} · ${t(LOGIC_TEXT[l])}`]), g.logic_b, (v) => { g.logic_b = v; edited(true); build(); },
                             { disabled: !can, label: t("channel logic B") });
        inputs["trigger.logic_b"] = lb;
        tg.append(...field(t("channel logic B"), lb, t("which of the channels of group B make logic B (ITLB)")));
        if (g.logic_b === "Majority") tg.append(...field(t("majority B"), number(() => g.majority_b, (v) => { g.majority_b = v; }, "trigger.majority_b", { lo: 1, hi: 64, width: "90px" }), t("channels needed at the same time")));
        tg.append(...field(t("gate B"), [number(() => g.gate_b_ns, (v) => { g.gate_b_ns = v; }, "trigger.gate_b_ns", { lo: 8, hi: 1e6, width: "100px" }), h("span.text2", "ns")],
                           t("how long a channel's trigger stays open in logic B (ITLBGateWidth)")));
      }
    }
    if (g.setup === "periodic") {
      const src = segmented(PERIODIC.map(([k, v]) => [k, t(v)]), g.periodic_source, (v) => { g.periodic_source = v; edited(true); build(); },
                            { disabled: !can, label: t("source") });
      tg.append(...field(t("source"), src));
      if (g.periodic_source === "software") {
        tg.append(...field(t("rate"), [number(() => g.software_rate_hz, (v) => { g.software_rate_hz = v; }, "trigger.software_rate_hz", { lo: 0.01, hi: 1000, step: 0.1, float: true, width: "100px" }), h("span.text2", "Hz")],
                           t("software triggers per second, sent by the DAQ program (at most 1000 Hz)")),
                  ...field(t("sent to"), segmented(TARGETS.map(([k, v]) => [k, t(v)]), g.software_target, (v) => { g.software_target = v; edited(true); build(); },
                                                   { disabled: !can, label: t("sent to") }),
                           t("each board: no cables, every board records other moments; the master: it passes the trigger on TRG-OUT → TRG-IN, so every board records the same moment")));
      } else {
        tg.append(...field(t("period"), [number(() => g.test_pulse_period_ns, (v) => { g.test_pulse_period_ns = v; }, "trigger.test_pulse_period_ns", { lo: 16, hi: 1e10, width: "140px" }), h("span.text2", "ns"), h("span.live", { id: "live-period" })]),
                  ...field(t("width"), [number(() => g.test_pulse_width_ns, (v) => { g.test_pulse_width_ns = v; }, "trigger.test_pulse_width_ns", { lo: 8, hi: 1e9, width: "110px" }), h("span.text2", "ns")],
                           t("each board triggers on its own internal test pulse; in a clock chain they start together")));
      }
    }
    tg.append(...field(t("signal level"), segmented(c.io_levels.map((x) => [x, x]), g.io_level, (v) => { g.io_level = v; edited(); }, { disabled: !can, label: t("signal level") }),
                       t("NIM or TTL on the front-panel connectors (TRG-IN, TRG-OUT, GPIO, S-IN)")),
              ...field(t("busy veto"), toggle(t("GPIO of all boards wired together"), g.busy_veto, (on) => { g.busy_veto = on; edited(); }, { disabled: !can }),
                       t("One cable along the GPIO connectors of all boards, 50 Ω at both ends (NIM only): a busy board vetoes all boards, so at high rates they keep the same events (WaveDump2 manual, Appendix A)")));
    const trigCard = h("div.card", h("div.card-head", h("h2.card-title", t("Trigger"))), h("div.card-body", tg));

    // the boards
    const boardsBox = h("div.cfg-boards");
    model.boards.forEach((b, i) => boardsBox.appendChild(boardEditor(b, i)));
    const boardsCard = h("div.card",
      h("div.card-head", h("h2.card-title", t("Boards")),
        h("span.card-sub", t("In the order of the chain: the first board switched on is the master.")),
        h("span.spacer"),
        h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: adoptChecked, title: t("Each board's model and number of channels as the last Check boards read them") },
          icon("check"), t("Use what the boards said")),
        h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: sameWindow, title: t("The same record length and pre-trigger in time on every board (a V2730 samples every 2 ns, a V2740 every 8 ns)") },
          icon("sliders"), t("Same window on all…")),
        h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: setAllThresholds, title: t("One threshold and pulse direction for every channel of every board") },
          icon("sliders"), t("Set all thresholds…")),
        h("button.btn.btn-sm", { type: "button", disabled: !can || model.boards.length >= c.max_boards, onclick: addBoard }, icon("plus"), t("Add board"))),
      h("div.card-body", boardsBox));
    clear(form, runCard, trigCard, boardsCard);
    showInfo();
    showProblems();
    showPlans();
    showHeader();
  }

  function boardEditor(b, i) {
    const c = app.state.config;
    const info = c.boards[i] || {};
    const n = model.boards.length;
    const plan = planFor(i);
    const move = (d) => { const j = i + d; [model.boards[i], model.boards[j]] = [model.boards[j], model.boards[i]]; reordered(); };
    const role = b.enabled && plan ? plan.role : null;
    const head = h("div.row.cfg-board-head",          // wraps on a narrow screen: the buttons stay in the card
      h("span.board-name", b.name || "?"),
      role ? chip(t(ROLE_NAMES[role] || role), role === "master" ? "accent" : "") : chip(t("switched off")),
      h("span.hint", b.model),
      h("span.spacer"),
      toggle(t("in the run"), b.enabled, (on) => { b.enabled = on; reordered(); }, { disabled: !can }),
      h("button.btn.btn-sm.btn-icon", { type: "button", disabled: !can || i === 0, title: t("Move up (the first board is the master)"),
                                        "aria-label": t("Move up (the first board is the master)"), onclick: () => move(-1) }, icon("up")),
      h("button.btn.btn-sm.btn-icon", { type: "button", disabled: !can || i === n - 1, title: t("Move down"), "aria-label": t("Move down"), onclick: () => move(1) }, icon("down")),
      h("button.btn.btn-sm.btn-icon", { type: "button", disabled: !can || n >= c.max_boards, title: t("Copy this board"), "aria-label": t("Copy this board"),
                                        onclick: () => copyBoard(i) }, icon("copy")),
      h("button.btn.btn-sm.btn-icon", { type: "button", disabled: !can || n < 2, title: t("Remove this board"), "aria-label": t("Remove this board"),
                                        onclick: () => { model.boards.splice(i, 1); reordered(); } }, icon("trash")));
    const grid = h("div.form-grid.board-grid");
    const choices = c.model_choices.includes(b.model) ? c.model_choices : [b.model, ...c.model_choices];
    grid.append(
      ...field(t("name"), text(() => b.name, (v) => { b.name = v; }, `boards[${i}].name`, "120px"), t("in file names and on the page: letters, digits, _ . -")),
      ...field(t("address"), text(() => b.uri, (v) => { b.uri = v; }, `boards[${i}].uri`, "min(320px, 100%)"),
               t("dig2://IP-ADDRESS (Ethernet) or dig2://usb:PID (USB 3); the factory address is 192.168.0.254")),
      ...field(t("model"), select(choices.map((m) => [m, m]), () => b.model, (v) => { setModel(b, v); b.n_channels = 0; }, `boards[${i}].model`),
               ((m) => (m ? t(m.text) : ""))(c.models.find((m) => b.model.includes(m.family)))),
      ...field(t("channels"), channelCount(b, i, info), t("its inputs, as Check boards reads them: 64 on a VX2740, 32 or 16 on a VX2730")),
      ...field(t("record length"), [number(() => b.record_length, (v) => { b.record_length = v; }, `boards[${i}].record_length`, { lo: 4, hi: 6250000, width: "120px" }),
                                    h("span.text2", t("samples")), h("span.live", { id: `live-rec-${i}` })]),
      ...field(t("pre-trigger"), [number(() => b.pre_trigger, (v) => { b.pre_trigger = v; }, `boards[${i}].pre_trigger`, { lo: 0, hi: 6250000, width: "120px" }),
                                  h("span.text2", t("samples")), h("span.live", { id: `live-pre-${i}` })],
               t("samples kept before the trigger")));
    if (info.decimation) {
      grid.append(...field(t("decimation"), select(c.decimations.map((d) => [d, d === 1 ? t("1 (none)") : `${d}`]), () => b.decimation, (v) => { b.decimation = Number(v); }, `boards[${i}].decimation`, false),
                           t("keep one sample in N: the board still samples at 125 MS/s (2740, 2745)")));
    }
    const delays = h("details.cfg-delays", { ontoggle: () => store(`config.delays.${i}`, delays.open ? "1" : null) },
      h("summary", icon("sliders"), t("Chain delays")),
      h("div.form-grid",
        ...field(t("run delay"), [optional(() => b.run_delay_ns, (v) => { b.run_delay_ns = v; }, `boards[${i}].run_delay_ns`, plan ? String(plan.run_delay_ns) : "0"), h("span.text2", "ns")],
                 t("empty: CAEN's value for this place in the chain (8 ns × (2 × boards after it, + 4 on the master)); it lines up the starts of the boards")),
        ...field(t("clock-out delay"), [optional(() => b.clock_out_delay_ps, (v) => { b.clock_out_delay_ps = v; }, `boards[${i}].clock_out_delay_ps`, plan ? String(plan.clock_out_delay_ps) : "0", { float: true }), h("span.text2", "ps")],
                 t("empty: CAEN's value (−2148 ps on the master, −3111 ps in the middle, 0 at the end); steps of 74 ps; depends on the cables"))));
    delays.open = store(`config.delays.${i}`) === "1";
    return h("div.cfg-board", { dataset: { board: String(i) } }, head, grid, delays, triggerEditor(b, i, info), ioEditor(b, i, info),
             channelEditor(b, i, info), extraEditor(b, i), planBox(b, i));
  }

  // a board's number of channels: sent when it is complete (Enter, or leaving the field), from 0 (the model's usual
  // number) to 64; fewer channels than now asks first, since the settings of the others go with them
  function channelCount(b, i, info) {
    const el = h("input.input.num", { type: "number", min: "0", max: "64", step: "1", disabled: !can, style: "width:90px",
                                      value: String(b.n_channels || info.channels || 0), onchange: () => changeCount(b, i, info, el) });
    inputs[`boards[${i}].n_channels`] = el;
    return el;
  }

  async function changeCount(b, i, info, el) {
    const v = Number(el.value), now = b.channels.length;
    const back = () => { el.value = String(b.n_channels || info.channels || now); };
    if (el.value.trim() === "" || !Number.isInteger(v) || v < 0 || v > 64) {
      toast(t("The number of channels must be a whole number from {lo} to {hi}", { lo: 0, hi: 64 }), "error");
      back();
      return;
    }
    const n = v || info.usual_channels || now;
    if (n < now && !(await confirmDialog(t("Fewer channels"), [t("Channels {a}–{b} of {name} will be removed, with their settings.", { a: n, b: now - 1, name: b.name })],
                                         t("Remove"), "danger"))) { back(); return; }
    b.n_channels = v;
    edited(true);
  }

  // a board's model: what the new model does not have goes back to its neutral value (decimation 1, gains 0),
  // else the check reports an error in a field the form no longer shows
  function setModel(b, name) {
    b.model = name;
    const m = (app.state.config.models || []).find((x) => name.includes(x.family));
    if (!m || !m.decimation) b.decimation = 1;
    if (!m || !m.gain) b.channels.forEach((ch) => { ch.gain = 0; });
  }

  // boards moved, switched on or off, added or removed: only the first board switched on (the master) may start
  // the run on a hardware signal, so a board no longer first loses that (its field is not shown any more)
  function reordered() {
    const first = model.boards.findIndex((b) => b.enabled);
    model.boards.forEach((b, k) => { if (b.enabled && k !== first && b.io_start) b.io_start = ""; });
    edited(true);
    build();
  }

  // what board i gets: its plan by name, else by its place in the run (a board renamed and not yet sent)
  function planFor(i) {
    const c = app.state.config, plans = c.plans || [], b = model.boards[i];
    const named = b && plans.find((p) => p.name === b.name);
    if (named) return named;
    const place = (c.boards[i] || {}).place;
    return b && b.enabled && place !== null && place !== undefined ? plans[place] || null : null;
  }

  // the board's own trigger, as WaveDump2 shows it: the mask (the channels that make the board trigger: click them
  // in or out) and every channel's threshold. Only on a board that triggers on its own channels; the Channels
  // table below edits the same values, and after an edit in either the other shows it: the mask buttons and "in
  // trigger" boxes (data-mask), the thresholds (data-thr) and both summaries. skip: the input being typed in
  function syncChannel(i, k, skip = null) {
    const x = model.boards[i] && model.boards[i].channels[k];
    if (!x) return;
    form.querySelectorAll(`[data-mask="${i}:${k}"]`).forEach((el) => {
      if (el.type === "checkbox") { el.checked = Boolean(x.self_trigger); return; }
      el.classList.toggle("on", Boolean(x.self_trigger));
      el.setAttribute("aria-pressed", x.self_trigger ? "true" : "false");
      el.classList.toggle("dim", !x.enabled);
      el.title = !x.enabled ? t("ch{c}: not read out, so it does not trigger", { c: k }) : t("ch{c}: click to put it in the board trigger or take it out", { c: k });
    });
    form.querySelectorAll(`[data-thr="${i}:${k}"]`).forEach((el) => { if (el !== skip) el.value = String(x.threshold); });
    showSummaries(i);
  }

  function syncBoard(i) { model.boards[i].channels.forEach((_, k) => syncChannel(i, k)); }

  function showSummaries(i) {
    const b = model.boards[i], [read, trig] = chanSummary(i);
    const sum = form.querySelector(`[data-trig-summary="${i}"]`);
    if (sum) setText(sum, trigSummary(b));
    const on = form.querySelector(`[data-ch-summary="${i}"]`), trg = form.querySelector(`[data-ch-trigger="${i}"]`);
    if (on) setText(on, read);
    if (trg) setText(trg, trig);
  }

  function chanSummary(i) {                  // the Channels section's title: how many are read out, and in the trigger
    const b = model.boards[i], self = Boolean((app.state.config.boards[i] || {}).self_trigger);
    return [t("Channels: {on} of {n} read out", { on: b.channels.filter((x) => x.enabled).length, n: b.channels.length }),
            self ? ` · ${t("{k} in the board trigger", { k: b.channels.filter((x) => x.enabled && x.self_trigger).length })}` : ""];
  }

  function trigSummary(b) {
    const mask = b.channels.filter((x) => x.self_trigger && x.enabled);
    const thr = mask.map((x) => x.threshold);
    const range = !thr.length ? t("no channel in the mask") : Math.min(...thr) === Math.max(...thr)
      ? t("threshold {t} counts", { t: thr[0] }) : t("thresholds {a} to {b} counts", { a: Math.min(...thr), b: Math.max(...thr) });
    return t("{k} of {n} channels in the mask", { k: mask.length, n: b.channels.length }) + ` · ${range}`;
  }

  function triggerEditor(b, i, info) {
    if (!info.self_trigger) {
      return h("div.cfg-trig-note.hint", icon("info"),
               t("Trigger: this board takes its trigger on TRG-IN, so its channels get no mask and no thresholds"));
    }
    const ch = b.channels;
    const box = h("details.cfg-trig", { ontoggle: () => store(`config.trig.${i}`, box.open ? null : "0") },
      h("summary", icon("zoom"), t("Trigger"), h("span.hint", { dataset: { trigSummary: String(i) } }, trigSummary(b))));
    box.open = store(`config.trig.${i}`) !== "0";
    const all = (fn) => { ch.forEach((x, c) => fn(x, c)); edited(true); syncBoard(i); };
    const btn = (text, title, fn) => h("button.btn.btn-sm", { type: "button", disabled: !can, title, onclick: () => all(fn) }, text);
    const maskGrid = h("div.chan-grid", { role: "group", "aria-label": t("Trigger mask") }, ch.map((x, c) => {
      const b2 = h("button.chan", { type: "button", disabled: !can, "aria-pressed": x.self_trigger ? "true" : "false",
                                    dataset: { mask: `${i}:${c}` },
                                    title: !x.enabled ? t("ch{c}: not read out, so it does not trigger", { c })
                                                      : t("ch{c}: click to put it in the board trigger or take it out", { c }),
                                    onclick: () => { x.self_trigger = !x.self_trigger; edited(); syncChannel(i, c); } }, String(c));
      if (x.self_trigger) b2.classList.add("on");
      if (!x.enabled) b2.classList.add("dim");
      return b2;
    }));
    maskGrid.style.setProperty("--cols", Math.min(16, ch.length));
    const same = h("input.input.num", { type: "number", min: "0", max: "65535", disabled: !can, style: "width:90px",
                                        value: String((ch.find((x) => x.self_trigger) || ch[0] || {}).threshold ?? 100), "aria-label": t("same threshold for all") });
    const thrGrid = h("div.thr-grid", ch.map((x, c) => {
      const el = h("input.input.num", { type: "number", min: "0", max: "65535", value: String(x.threshold), disabled: !can,
                                        "aria-label": t("threshold of ch{c}", { c }), dataset: { thr: `${i}:${c}` },
                                        oninput: () => { const v = Math.round(Number(el.value)); if (el.value !== "" && Number.isFinite(v) && v >= 0) {
                                          x.threshold = v; edited(); syncChannel(i, c, el); } } });
      return h("label.thr-cell", h("span.small", `ch${c}`), el);
    }));
    box.append(h("div.stack.cfg-trig-body",
      h("div.row.wrap", h("b.small", t("Mask")),
        h("span.hint", t("the channels whose pulses make the board trigger, with the channel logic of the Trigger section")), h("span.spacer"),
        btn(t("Select all"), t("Every channel in the board trigger"), (x) => { x.self_trigger = true; }),
        btn(t("None"), t("No channel in the board trigger"), (x) => { x.self_trigger = false; }),
        btn(t("Read out only"), t("The channels that are read out"), (x) => { x.self_trigger = x.enabled; })),
      maskGrid,
      h("div.row.wrap", h("b.small", t("Thresholds")),
        h("span.hint", t("ADC counts from each channel's baseline, in the direction of its pulses (Channels: positive)")), h("span.spacer"),
        h("span.label", t("same for all")), same,
        h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: () => {
          const v = Math.round(Number(same.value));          // an emptied field is not 0
          if (same.value.trim() === "" || !Number.isFinite(v) || v < 0 || v > 65535) {
            toast(t("The threshold must be a whole number from {lo} to {hi}", { lo: 0, hi: 65535 }), "error");
            return;
          }
          all((x) => { x.threshold = v; });
        } }, t("Set"))),
      thrGrid));
    return box;
  }

  // the front panel of a board (WaveDump2's Board settings, Advanced): each signal as the trigger setup needs it,
  // or as set here; and the test signals (the test pulse on TRG-OUT or GPIO, DAC OUT)
  function ioEditor(b, i, info) {
    const ch = app.state.config.io_choices || {};
    const set = [["io_trg_out", "TRG-OUT"], ["io_gpio_out", "GPIO"], ["io_veto", t("veto")], ["io_start", t("start")],
                 ["io_tstamp_reset", t("timestamp reset")], ["io_dac_out", "DAC OUT"]].filter(([k]) => b[k]).map(([k, n]) => `${n} ${b[k]}`);
    const box = h("details.cfg-io", { ontoggle: () => store(`config.io.${i}`, box.open ? "1" : null) },
      h("summary", icon("link"), t("Front panel and test signals"), set.length ? h("span.hint", ` · ${set.join(" · ")}`) : null));
    box.open = store(`config.io.${i}`) === "1";
    const opts = (list, empty, text = {}) => [["", empty], ...(list || []).map((v) => [v, text[v] ? `${v} · ${t(text[v])}` : v])];
    const sel = (key, list, empty, text) => select(opts(list, empty, text), () => b[key] || "", (v) => { b[key] = v; }, `boards[${i}].${key}`, true);
    const isMaster = info.place === 0;
    const grid = h("div.form-grid",
      ...field("TRG-OUT", sel("io_trg_out", ch.trg_out, t("as the trigger setup needs")),
               t("TestPulse: the internal test pulse (period and width of the Trigger section), to see on a scope or to feed back into channels; Busy, Run, RefClk, …. In a trigger chain this board's TRG-OUT carries the trigger: leave it")),
      ...field("GPIO", sel("io_gpio_out", ch.gpio, t("as the trigger setup needs")), t("the GPIO connector as an output (the busy veto uses it)")),
      ...field(t("veto"), [sel("io_veto", ch.veto, t("none")),
                           b.io_veto ? number(() => b.io_veto_width_ns, (v) => { b.io_veto_width_ns = v; }, `boards[${i}].io_veto_width_ns`, { lo: 0, hi: 1e9, width: "100px" }) : null,
                           b.io_veto ? h("span.text2", t("ns (0: the board's)")) : null,
                           b.io_veto ? sel("io_veto_polarity", ch.polarity, t("the board's polarity")) : null],
               t("no data while this signal is on (S-IN, GPIO, LVDS, …)")),
      isMaster ? field(t("start"), sel("io_start", ch.start, t("the DAQ starts the run (Start run)"), START_TEXT),
                       t("a hardware start: the run (and the clock chain's run signal) begins on this signal; Stop still ends it")) : [],
      ...field(t("timestamp reset"), sel("io_tstamp_reset", ch.tsreset, t("at the run start")), t("the timestamps count from this signal instead")),
      ...field("DAC OUT", [sel("io_dac_out", ch.dac, t("not set"), DAC_TEXT),
                         b.io_dac_out === "Static" ? number(() => b.io_dac_level, (v) => { b.io_dac_level = v; }, `boards[${i}].io_dac_level`, { lo: 0, hi: 65535, width: "100px" }) : null,
                         b.io_dac_out === "ChInput" ? h("span.text2", t("channel")) : null,
                         b.io_dac_out === "ChInput" ? number(() => b.io_dac_channel, (v) => { b.io_dac_channel = v; }, `boards[${i}].io_dac_channel`, { lo: 0, hi: 63, width: "80px" }) : null],
               t("the analog test output (where the board has one): e.g. pulses into a channel of every board to see that they line up (the combined view, lined up by timestamps). If a board does not know the value it warns and runs without it")));
    box.appendChild(grid);
    return box;
  }

  function syncedThreshold(el, i, k) {             // a threshold of the Channels table: the Trigger panel follows it
    el.dataset.thr = `${i}:${k}`;
    el.addEventListener("input", () => syncChannel(i, k, el));
    return el;
  }

  function channelEditor(b, i, info) {
    const self = Boolean(info.self_trigger);         // this board triggers on its own channels
    const groups = Boolean(model.trigger.combine) && model.trigger.combine !== "A";
    const chs = b.channels;
    const box = h("details.cfg-channels", { ontoggle: () => store(`config.channels.${i}`, box.open ? "1" : null) },
      h("summary", icon("grid"), h("span", { dataset: { chSummary: String(i) } }, chanSummary(i)[0]),
        h("span.hint", { dataset: { chTrigger: String(i) } }, chanSummary(i)[1])));
    box.open = store(`config.channels.${i}`) === "1";
    // set all: each field starts as the channels are (a box half ticked, a number empty, when they differ); only the
    // fields changed here go to every channel, never an empty one, and "in trigger" only where it is shown
    const touched = new Set();
    const every = (key) => chs.length > 0 && chs.every((x) => Boolean(x[key]));
    const mixed = (key) => chs.some((x) => Boolean(x[key])) && !every(key);
    const one = (key) => (chs.length && chs.every((x) => x[key] === chs[0][key]) ? String(chs[0][key]) : "");
    const touch = (key) => () => { touched.add(key); apply.disabled = !can; };
    const tick = (key, label) => {
      const el = h("input", { type: "checkbox", checked: every(key), disabled: !can, "aria-label": label, onchange: touch(key) });
      el.indeterminate = mixed(key);
      return el;
    };
    const allOn = tick("enabled", t("read out"));
    const allDc = h("input.input.num", { type: "number", min: "0", max: "100", step: "0.1", value: one("dc_offset"), disabled: !can, style: "width:80px",
                                         "aria-label": t("DC offset"), oninput: touch("dc_offset") });
    const allThr = h("input.input.num", { type: "number", min: "0", max: "65535", value: one("threshold"), disabled: !can, style: "width:90px",
                                          "aria-label": t("threshold"), oninput: touch("threshold") });
    const allPos = tick("positive", t("positive pulses"));
    const allTrig = tick("self_trigger", t("in trigger"));
    const apply = h("button.btn.btn-sm", { type: "button", disabled: true, onclick: () => {
      const dc = Number(allDc.value), thr = Math.round(Number(allThr.value));
      if (touched.has("dc_offset") && (allDc.value.trim() === "" || !(dc >= 0 && dc <= 100))) {
        toast(t("The DC offset must be from {lo} to {hi} %", { lo: 0, hi: 100 }), "error");
        return;
      }
      if (touched.has("threshold") && (allThr.value.trim() === "" || !(thr >= 0 && thr <= 65535))) {
        toast(t("The threshold must be a whole number from {lo} to {hi}", { lo: 0, hi: 65535 }), "error");
        return;
      }
      for (const ch of chs) {
        if (touched.has("enabled")) ch.enabled = allOn.checked;
        if (touched.has("dc_offset")) ch.dc_offset = dc;
        if (touched.has("threshold")) ch.threshold = thr;
        if (touched.has("positive")) ch.positive = allPos.checked;
        if (self && touched.has("self_trigger")) ch.self_trigger = allTrig.checked;
      }
      edited(true);
      build();
    } }, t("Apply to all channels"));
    box.appendChild(h("div.row.all-row", h("span.label", t("all channels:")), h("label.row.nowrap", allOn, t("read out")),
                      h("span.label", t("DC offset")), allDc, h("span.text2", "%"),
                      h("span.label", t("threshold")), allThr, h("label.row.nowrap", allPos, t("positive pulses")),
                      self ? h("label.row.nowrap", allTrig, t("in trigger")) : null, apply));
    const head = h("tr", h("th", t("ch")), h("th", t("read out")),
                   h("th", { title: t("Where the baseline sits, in % of the input range: 50 % in the middle; for negative pulses a high value leaves room below") }, t("DC offset %")),
                   h("th", { title: t("ADC counts from the channel's baseline, in the direction of its pulses (not an ADC level)") }, t("threshold")),
                   h("th", { title: t("Ticked: positive pulses (rising edge, above the baseline); else negative pulses") }, t("positive")),
                   self ? h("th", { title: t("Takes part in the board's trigger (the channel logic of the Trigger section)") }, t("in trigger")) : null,
                   self && groups ? h("th", { title: t("The channel logic it joins: A (ITLA) or B (ITLB)") }, t("group")) : null,
                   info.gain ? h("th", t("gain dB")) : null);
    const rows = b.channels.map((ch, k) => {
      const w = `boards[${i}].channels[${k}]`;
      const cb = (key, label) => {
        const el = h("input", { type: "checkbox", checked: Boolean(ch[key]), disabled: !can, "aria-label": `${label} ch${k}`,
                                dataset: key === "self_trigger" ? { mask: `${i}:${k}` } : undefined,
                                onchange: () => { ch[key] = el.checked; edited(); syncChannel(i, k); } });
        return h("td", el);
      };
      return h("tr", h("th.rowh", String(k)), cb("enabled", t("read out")),
               h("td", number(() => ch.dc_offset, (v) => { ch.dc_offset = v; }, `${w}.dc_offset`, { lo: 0, hi: 100, step: 0.1, float: true, width: "80px" })),
               h("td", syncedThreshold(number(() => ch.threshold, (v) => { ch.threshold = v; }, `${w}.threshold`, { lo: 0, hi: 65535, width: "90px" }), i, k)),
               cb("positive", t("positive")), self ? cb("self_trigger", t("in trigger")) : null,
               self && groups ? h("td", select([["A", "A"], ["B", "B"]], () => ch.itl || "A", (v) => { ch.itl = v; }, `${w}.itl`, false)) : null,
               info.gain ? h("td", number(() => ch.gain, (v) => { ch.gain = v; }, `${w}.gain`, { lo: 0, hi: 40, step: 0.5, float: true, width: "80px" })) : null);
    });
    box.appendChild(h("div.table-wrap.ch-wrap", h("table.ch-table.ch-list", h("thead", head), h("tbody", rows))));
    return box;
  }

  function extraEditor(b, i) {
    const box = h("details.cfg-extra", { ontoggle: () => store(`config.extra.${i}`, box.open ? "1" : null) },
      h("summary", icon("tool"), t("Expert parameters"), b.extra.length ? h("span.hint", ` · ${b.extra.length}`) : null));
    box.open = store(`config.extra.${i}`) === "1" || b.extra.length > 0;
    const area = h("textarea.input.mono", { rows: String(Math.max(3, b.extra.length + 1)), disabled: !can, spellcheck: "false",
                                            placeholder: "/par/SomeParameter = value\n/ch/0..63/par/SomeChannelParameter = value",
                                            oninput: () => {
                                              b.extra = area.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
                                                const k = l.indexOf("=");
                                                return k < 0 ? [l, ""] : [l.slice(0, k).trim(), l.slice(k + 1).trim()];
                                              });
                                              edited();
                                            } });
    area.value = b.extra.map(([p, v]) => `${p} = ${v}`).join("\n");
    inputs[`boards[${i}].extra`] = area;
    box.append(h("p.hint", { style: "margin:4px 0 6px" }, t("One CAEN FELib parameter per line, PATH = VALUE, set after everything else (as WaveDump2's Manual Controller). The board's own list of parameters is in Tools → Boards after a board check.")), area);
    return box;
  }

  function planBox(b, i) {
    const box = h("details.cfg-plan", { dataset: { board: String(i) }, ontoggle: () => store(`config.plan.${i}`, box.open ? "1" : null) },
      h("summary", icon("list"), t("What this board gets")));
    box.open = store(`config.plan.${i}`) === "1";
    box.appendChild(h("div.plan-body"));
    return box;
  }

  function showPlans() {
    form.querySelectorAll(".cfg-plan").forEach((box) => {
      const p = planFor(Number(box.dataset.board));
      const body = box.querySelector(".plan-body");
      if (!p) { clear(body, h("p.hint", t("This board is not in the run (switched off), or its model is not known."))); return; }
      clear(body, h("p.hint", { style: "margin:4px 0" }, t("In this order, after /cmd/Reset; then the raw endpoint is activated and the board is armed.")),
            h("table.table.plan-table", h("tbody", p.settings.map(([path, value, why]) => h("tr", h("td.mono", path), h("td.mono", value), h("td.hint", t(why)))))));
    });
  }

  // ---- whole-configuration helpers ----
  function adoptChecked() {
    const res = (app.state.boards && app.state.boards.results) || [];
    let n = 0;
    for (const b of model.boards) {
      const r = res.find((x) => x.name === b.name && x.uri === b.uri && x.ok && x.facts && !x.stale);   // checked at this address
      if (!r) continue;
      const f = r.facts, fam = (f.model || "").replace(/\D/g, "").slice(0, 4);
      if (f.model && app.state.config.models.some((m) => m.family === fam)) setModel(b, f.model);
      if (f.channels) b.n_channels = f.channels;
      n += 1;
    }
    if (!n) { toast(t("No board answered the last check: press Check boards on the Run page first"), "error"); return; }
    edited(true);
    build();
    toast(t("Model and channels of {n} boards taken from the last check", { n }), "ok");
  }

  function addBoard() {
    if (model.boards.length >= app.state.config.max_boards) return;       // the server keeps no more
    const used = new Set(model.boards.map((b) => b.name));
    let k = model.boards.length;
    while (used.has(`b${k}`)) k++;
    const last = model.boards[model.boards.length - 1];
    const nb = last ? JSON.parse(JSON.stringify(last)) : { model: "V2740", record_length: 1024, pre_trigger: 128, decimation: 1, channels: [], extra: [], enabled: true };
    nb.name = `b${k}`;
    nb.uri = "dig2://192.168.0.254";
    nb.run_delay_ns = null;
    nb.clock_out_delay_ps = null;
    model.boards.push(nb);
    reordered();
  }

  function copyBoard(i) {
    if (model.boards.length >= app.state.config.max_boards) return;       // the server keeps no more
    const nb = JSON.parse(JSON.stringify(model.boards[i]));
    const used = new Set(model.boards.map((b) => b.name));
    let k = model.boards.length;
    while (used.has(`b${k}`)) k++;
    nb.name = `b${k}`;
    model.boards.splice(i + 1, 0, nb);
    reordered();
  }

  async function sameWindow() {
    const first = app.state.config.boards[0] || {};
    const len = h("input.input.num", { type: "number", min: "0.01", step: "0.01", value: String(first.window_ns ? first.window_ns / 1000 : 8.192), style: "width:120px" });
    const pre = h("input.input.num", { type: "number", min: "0", step: "0.01", value: String(first.pre_ns ? first.pre_ns / 1000 : 1.024), style: "width:120px" });
    const value = await dialog({
      title: t("The same window on every board"),
      body: h("div.stack", h("p", { style: "margin:0" }, t("Each board gets the record length and pre-trigger that last this long at its own sample time (rounded to whole samples, multiples of 4).")),
              h("div.form-grid", h("span.label", t("record length")), h("div.row", len, h("span.text2", "µs")),
                                 h("span.label", t("pre-trigger")), h("div.row", pre, h("span.text2", "µs")))),
      actions: [{ label: t("Cancel"), value: null },
                { label: t("Apply to all boards"), kind: "primary", run: () => {
                  const L = Number(len.value) * 1000, P = Number(pre.value) * 1000;
                  if (!(L > 0) || !(P >= 0) || P >= L) throw new Error(t("The pre-trigger must be shorter than the record"));
                  return [L, P];
                } }],
    });
    if (!value) return;
    model.boards.forEach((b, i) => {
      const ns = (app.state.config.boards[i] || {}).sample_ns || 8;
      b.record_length = Math.max(4, Math.round(value[0] / ns / 4) * 4);
      b.pre_trigger = Math.min(b.record_length - 4, Math.round(value[1] / ns / 4) * 4);
    });
    edited(true);
    build();
    toast(t("Window set on all boards"), "ok");
  }

  async function setAllThresholds() {
    const input = h("input.input.num", { type: "number", min: "0", max: "65535", value: "100" });
    const pos = h("input", { type: "checkbox" });
    let submit = null;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && submit) submit.click(); });
    const total = model.boards.reduce((a, b) => a + b.channels.length, 0);
    const value = await dialog({
      title: t("Threshold for every channel"),
      body: h("div.stack",
              h("p", { style: "margin:0" }, t("The same threshold and pulse direction for all {c} channels of all {n} boards; each channel can still be changed afterwards.", { c: total, n: model.boards.length })),
              h("div.field", h("label", t("Threshold in ADC counts from each channel's own baseline, in the direction of the pulse (not an ADC level)")), input),
              h("label.row.nowrap", pos, t("positive pulses (else negative)"))),
      actions: [{ label: t("Cancel"), value: null },
                { label: t("Apply to all boards"), kind: "primary", run: () => {
                  const v = Number(input.value);                 // an emptied field is not 0
                  if (input.value.trim() === "" || !Number.isInteger(v) || v < 0 || v > 65535) throw new Error(t("The threshold must be a whole number from {lo} to {hi}", { lo: 0, hi: 65535 }));
                  return v;
                } }],
      onOpen: (m) => { submit = m.querySelector(".btn-primary"); input.select(); },
    });
    if (value === null || value === undefined) return;
    for (const b of model.boards) for (const ch of b.channels) { ch.threshold = value; ch.positive = pos.checked; }
    edited(true);
    build();
    toast(t("Threshold {v} on all {c} channels", { v: value, c: total }), "ok");
  }

  // ---- what the form says ----
  function showInfo() {
    const c = app.state.config, r = model.run, g = model.trigger;
    const live = document.getElementById("live-duration");
    if (live) setText(live, r.duration_s ? `= ${fmtNs(r.duration_s * 1e9)}` : t("until Stop"));
    const period = document.getElementById("live-period");
    if (period) setText(period, `= ${fmtNs(g.test_pulse_period_ns)} · ${fmtNum(1e9 / Math.max(1, g.test_pulse_period_ns), 2)} Hz`);
    model.boards.forEach((b, i) => {
      const info = c.boards[i] || {};
      const ns = info.sample_ns || 8;
      const rec = document.getElementById(`live-rec-${i}`), pre = document.getElementById(`live-pre-${i}`);
      const bytes = 24 + 2 * b.record_length * b.channels.filter((x) => x.enabled).length;
      if (rec) setText(rec, t("= {time} · {ns} ns per sample · {b} per event", { time: fmtNs(b.record_length * ns), ns: fmtNum(ns, ns % 1 ? 1 : 0), b: fmtBytes(bytes) }));
      if (pre) setText(pre, `= ${fmtNs(b.pre_trigger * ns)}`);
    });
  }

  function showProblems() {
    const problems = app.state.config.problems;
    for (const el of Object.values(inputs)) if (el.classList) el.classList.remove("invalid");
    for (const p of problems) {
      const el = inputs[p.where];
      if (el && el.classList && p.level === "error") el.classList.add("invalid");
      const m = /^boards\[(\d+)\]\.channels\[(\d+)\]/.exec(p.where);
      if (m && p.level === "error") {             // a channel's error: open its table
        const box = form.querySelectorAll(".cfg-channels")[Number(m[1])];
        if (box) box.open = true;
      }
    }
    clear(problemsBox, problems.length ? problems.map((p) => h(`li.${p.level}`, icon(p.level === "error" ? "alert" : "info"),
                                                             h("span", `${p.level === "error" ? t("Error") : t("Warning")} · ${p.where}: ${p.message}`)))
                                       : h("li.ok", icon("check"), t("No problems: the DAQ program will accept this configuration.")));
  }

  function showHeader() {
    const c = app.state.config;
    setText(path, c.path ? t("File: {path}", { path: c.path }) : t("(not saved)"));
    clear(dirty, unsent ? chip(t("not sent to the server: {why}", { why: unsent }), "critical", "alert") : null,
          c.dirty ? chip(t("changed, not saved"), "warning") : null);
  }

  function showSendBtn() {                     // the boards are the DAQ's during a run or a sequence, the check's during one
    const busy = (app.state.run && app.state.run.active) || sequenceGoing(app) || (app.state.boards && app.state.boards.running);
    sendBtn.disabled = !can || Boolean(busy);
  }

  // ---- files ----
  // New, Open and Reload replace the configuration: edits not saved to a file, or not even on the server, are
  // discarded only when the user says so
  async function discardOk() {
    const waiting = Boolean(timer || pumping || unsent || noteTimer);
    if (!app.state.config.dirty && !waiting) return true;
    if (!(await confirmDialog(t("Unsaved changes"), [t("The configuration has unsaved changes. Discard them?")], t("Discard")))) return false;
    clearTimeout(timer);
    clearTimeout(noteTimer);
    clearTimeout(retryTimer);
    timer = noteTimer = null;
    wanted = noteWanted = false;
    setUnsent(null);
    if (pumping) await pumping.catch(() => {});      // the write going ends first: the new configuration comes after it
    return true;
  }

  async function newConfig() {
    if (!(await discardOk())) return;
    try { adopt(await post("/api/config/new")); } catch (err) { fail(err); }
  }

  async function openFile() {
    if (!(await discardOk())) return;
    const p = await choosePath({ title: t("Open a Vx27webDAQ configuration"), want: "file", filters: CONF_FILES,
                                start: app.state.settings.default_folder || app.state.config.path });   // the lab's data folder first
    if (!p) return;
    try { adopt(await post("/api/config/open", { path: p })); } catch (err) { fail(err); }
  }

  async function save(p) {
    try { await flush(); } catch (err) { fail(err); return; }     // the edits and the note first: else not saved
    if (!p && !app.state.config.path) { await saveAs(); return; }
    try {
      const r = await post("/api/config/save", p ? { path: p } : {});
      newest(r);
      if (same(r.config, base)) version = r.version;     // saving changed no setting: the edits still start from here
      toast(t("Saved {path}", { path: r.path }), "ok");
      showHeader();
    } catch (err) { fail(err); }
  }

  async function saveAs() {
    const c = app.state.config;
    const p = await choosePath({ title: t("Save the configuration"), want: "save", filters: CONF_FILES, start: c.path,
                                 name: c.path ? basename(c.path) : "vx27webdaq.json" });
    if (p) await save(p);
  }

  // Send to boards: the edits still waiting go to the server first, so the boards get what the page shows; when they
  // do not go, nothing is sent
  function sendBoards() {
    return sendToBoards(app, () => flush());
  }

  async function reload() {
    if (!(await discardOk())) return;
    try { adopt(await post("/api/config/reload")); } catch (err) { fail(err); }
  }

  // the server's configuration c (or a newer one that came meanwhile) into the form
  function adopt(c = app.state.config) {
    const use = newest(c);
    model = clone(use.config);
    base = clone(use.config);
    version = use.version;
    stale = false;
    if (!noteTimer && !noteWanted && document.activeElement !== noteInput) noteInput.value = use.note || "";
    redraw();
  }

  function redraw() {                          // the form built again where it was: its scroll, and the field being typed in
    const el = document.activeElement, key = Object.keys(inputs).find((k) => inputs[k] === el);
    let caret = null;
    try { caret = el && typeof el.selectionStart === "number" ? [el.selectionStart, el.selectionEnd] : null; } catch (e) { caret = null; }
    const scroll = top.scrollTop;
    build();
    top.scrollTop = scroll;
    const now = key && inputs[key];
    if (now && now.focus) {
      now.focus();
      try { if (caret) now.setSelectionRange(caret[0], caret[1]); } catch (e) { /* a number field has no caret to set */ }
    }
  }

  // a newer configuration waited while a field had the focus: it comes when the field is left
  form.addEventListener("focusout", () => setTimeout(() => { if (stale && idle() && !typing()) adopt(); }, 0));

  build();
  showSendBtn();
  return {
    update(type) {
      if (type === "run" || type === "sequence" || type === "boards") showSendBtn();
      if (type !== "config" && type !== "settings") return;
      if (type === "config" && !noteTimer && !noteWanted && document.activeElement !== noteInput) noteInput.value = app.state.config.note || "";
      if (type === "config" && app.state.config.version > version) {     // someone's change (or this page's, echoed)
        if (idle() && !typing()) adopt();
        else { stale = true; showProblems(); showHeader(); }
      } else { showProblems(); showPlans(); showHeader(); }
    },
    flush,
    unmount() { flush().catch(notify); },
  };
}

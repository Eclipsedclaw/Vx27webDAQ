// Run page, the front page: check the boards and start and stop runs (operators); the waveforms of one board,
// live or from a file, in the big part; beside them the progress of the run with one row per board, and the DAQ
// program's output. Everyone sees it live. The dividers can be dragged (kept for this browser).
import { get, post, put } from "../api.js";
import { T, t } from "../i18n.js";
import { LinePlot } from "../plots.js";
import { basename, chip, clear, confirmDialog, dialog, fail, fmtBytes, fmtCompact, fmtDuration, fmtInt, fmtNum, h, icon,
         setText, splitter, toast, toggle } from "../ui.js";
import { wavePanel } from "../wavepanel.js";

const BOARD_STATES = { waiting: ["", T("waiting")], connecting: ["busy pulse", T("connecting")], connected: ["busy", T("connected")],
                       configuring: ["busy pulse", T("configuring")], configured: ["busy", T("configured")],
                       armed: ["busy", T("armed")], running: ["ok", T("running")], stopping: ["busy pulse", T("stopping")],
                       stopped: ["", T("stopped")], error: ["error", T("error")] };
export const SETUP_NAMES = { external: T("common external trigger"), self: T("each board self-triggers"),
                             logic_or: T("OR in a logic unit"), periodic: T("periodic") };
export const ROLE_NAMES = { master: T("master"), middle: T("chain"), last: T("end of chain"), single: T("on its own") };
const QUIET_S = 5;

function fmtRate(v) {
  if (v === null || v === undefined) return "—";
  return v >= 1000 ? fmtCompact(v) : fmtNum(v, v >= 100 ? 0 : v >= 10 ? 1 : 2);
}

export function configSummary(app) {
  const c = app.state.config, cfg = c.config, sim = app.state.settings.values.use_simulator;
  const n = cfg.boards.filter((b) => b.enabled).length;
  const sync = n < 2 ? t("one board") : cfg.run.sync === "chain" ? t("clock chain") : t("independent boards");
  const data = sim ? t("in the run folder (simulator)") : cfg.run.write_raw ? cfg.run.data_dir : t("not written");
  return t("{n} boards · {sync} · trigger: {setup} · run time {d} · data: {data}",
           { n, sync, setup: t(SETUP_NAMES[cfg.trigger.setup] || cfg.trigger.setup),
             d: cfg.run.duration_s ? fmtDuration(cfg.run.duration_s) : t("until Stop"), data });
}

export function viewerNotice(app, text) {
  if (app.can("control")) return null;
  return h("div.notice", icon("eye"), h("span.grow", text),
           app.me.user ? null : h("button.btn.btn-sm", { type: "button", onclick: () => window.alabSignIn() }, t("Sign in")));
}

export function runPage(root, app) {
  const page = h("div.page.fit.run-page");
  root.appendChild(page);
  const summary = h("span.sub");
  page.appendChild(h("div.page-head", h("h1", t("Run")), summary));
  const notice = viewerNotice(app, t("You are watching. Operators check the boards and start and stop runs; sign in as one to do so."));
  if (notice) page.appendChild(notice);

  // ---- the run bar ----
  const checkBtn = h("button.btn", { type: "button", onclick: checkBoards,
                                     title: t("Connect to every board of the configuration and read what it is (model, serial, firmware, temperatures)") },
                     icon("link"), t("Check boards"));
  const startBtn = h("button.btn.btn-primary", { type: "button", onclick: startRun }, icon("run"), t("Start run"));
  const stopBtn = h("button.btn", { type: "button", onclick: stopRun, title: t("End the run cleanly: the master stops the chain, every board sends its last data and its stop record") },
                    icon("stop"), t("Stop"));
  const killBtn = h("button.btn.btn-danger", { type: "button", onclick: killRun,
                                               title: t("Only if Stop does not work: the end of the data can be lost and the boards stay armed") },
                    icon("kill"), t("Kill"));
  const convert = toggle(t("Convert to ROOT after the run"), app.state.settings.values.convert_to_root, async (on) => {
    try { await put("/api/settings", { changes: { convert_to_root: on } }); } catch (err) { fail(err); convert.input.checked = !on; }
  }, { disabled: !app.can("control") });
  convert.title = t("When the run ends, each of its .raw files becomes a .root file next to it, checked against the .raw file, which stays");
  const phase = h("span.run-phase");
  const who = h("span.text2.small");
  const timeText = h("span.num.text2");
  const bar = h("div.progress", h("span", { style: "width:0%" }));
  const result = h("div");
  const noteText = h("span.run-note-text"), noteLabel = h("b.small");
  const noteLine = h("div.row.nowrap.run-note", icon("file"), noteLabel, noteText,
                     app.can("control") ? h("a.small", { href: "#config", title: t("The note is on the Configuration page") }, t("edit")) : null);
  page.appendChild(h("div.card.run-bar",
    h("div.card-body.stack",
      h("div.row", checkBtn, h("span.sep"), startBtn, stopBtn, killBtn, h("span.sep"), phase, who, h("span.spacer"), timeText, convert),
      bar, noteLine, result)));

  // ---- waveforms | progress and output ----
  let boardEls = [];
  const waves = wavePanel(app, { onBoard: (name) => markBoard(name) });
  const tiles = h("div.stats.compact");
  const boardRows = h("tbody");
  const boardTable = h("table.table.cards-table",
    h("thead", h("tr",
      h("th", { title: t("Click a board to see its waveforms. The dot before the name is the last board check: grey not checked, green it answered, red it did not") }, t("Board")),
      h("th", t("State")),
      h("th.n", { title: t("Events the board sent in this run") }, t("Events")),
      h("th.n", { title: t("Events per second over the newest 10 s") }, "Hz"),
      h("th.n", { title: t("Data rate of the board over the newest 10 s") }, "MB/s"),
      h("th.n", { title: t("The trigger ID of the board's newest event (the boards of a common trigger count alike)") }, t("Trigger ID")),
      h("th.n", { title: t("The hottest of the board's temperature sensors (°C)") }, "°C"))),
    boardRows);
  const plotBox = h("div.bw-plot");
  const rootBox = h("div.stack", { style: "gap:6px" });
  const progressCard = h("div.card.fill", h("div.card-head", h("h2.card-title", t("Progress")), h("span.card-sub", { id: "check-when" })),
                         h("div.card-body.stack.side-body", tiles, h("div.table-wrap", boardTable), plotBox, rootBox));
  const log = h("div.log", { role: "log", "aria-live": "off" });
  const follow = toggle(t("Follow"), true, (on) => { if (on) log.scrollTop = log.scrollHeight; });
  const logCard = h("div.card.fill", h("div.card-head", h("h2.card-title", t("DAQ output")), h("span.spacer"), follow),
                    h("div.card-body.flush", { style: "flex:1 1 auto;display:flex;flex-direction:column" }, log));
  const side = splitter(progressCard, logCard, { key: "run.side", vertical: true, initial: 0.6 });
  page.appendChild(splitter(waves.el, side, { key: "run.main", vertical: false, initial: 0.64, min: 300 }));
  const plot = new LinePlot(plotBox, { xLabel: t("run time (s)"), yLabel: "MB/s" });

  // the boards shown: of the run going (or the last one), else of the configuration
  function boardList() {
    const r = app.state.run, shown = r && r.active ? r : r && r.last;
    if (shown && shown.boards && shown.boards.length) return shown.boards.map((b) => ({ name: b.name, uri: b.uri, role: b.role, model: b.model }));
    const plans = app.state.config.plans || [];
    return app.state.config.config.boards.filter((b) => b.enabled).map((b) => {
      const p = plans.find((x) => x.name === b.name);
      return { name: b.name, uri: b.uri, role: p ? p.role : "single", model: b.model };
    });
  }

  function buildBoards() {
    boardEls = boardList().map((b) => {
      const checkLed = h("span.led.small"), stateLed = h("span.led"), stateText = h("span"), quiet = h("span");
      const ev = h("td.n"), rate = h("td.n"), mbps = h("td.n"), trig = h("td.n"), temp = h("td.n");
      const msg = h("td.board-msg", { colspan: "7" });
      const where = `${b.name} · ${b.model} · ${b.uri} · ${t(ROLE_NAMES[b.role] || b.role)} · ${t("click: its waveforms")}`;
      const pick = () => showBoard(b.name);
      const row = h("tr.pick", { title: where, tabindex: "0", "aria-label": t("Board {n}: show its waveforms", { n: b.name }),
                                 onclick: pick, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } } },
                    h("td", h("div.row.nowrap", { style: "gap:6px" }, checkLed, h("b", b.name), h("span.hint", b.model),
                                b.role === "master" ? h("span.chip.accent", { title: t("chain master: its clock and start drive the other boards") }, t("M")) : null)),
                    h("td", h("span.row.nowrap", { style: "gap:6px" }, stateLed, stateText, quiet)), ev, rate, mbps, trig, temp);
      const msgRow = h("tr.msg-row.hidden", msg);
      return { row, msgRow, checkLed, stateLed, stateText, quiet, ev, rate, mbps, trig, temp, msg, where, name: b.name };
    });
    clear(boardRows, boardEls.map((b) => [b.row, b.msgRow]));
    markBoard(waves.board());
    showCheck();
    showRun();
  }

  function showBoard(name) {
    if (!waves.showBoard(name)) { toast(t("The file shown has no data of board {n}", { n: name }), "error"); return; }
    const r = waves.el.getBoundingClientRect();
    if (r.top < 0 || r.top > window.innerHeight - 120) waves.el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function markBoard(name) {
    for (const b of boardEls) {
      b.row.classList.toggle("on", b.name === name);
      b.row.setAttribute("aria-current", b.name === name ? "true" : "false");
    }
  }

  function showCheck() {
    const C = app.state.boards;
    const res = (C && C.results) || [];
    boardEls.forEach((b) => {
      const r = res.find((x) => x.name === b.name);
      const state = !r || r.ok === null || r.ok === undefined ? (r && r.checking ? "busy pulse" : "") : r.ok ? (r.stale ? "warn" : "ok") : "error";
      b.checkLed.className = `led small ${state}`;
      const f = r && r.facts ? r.facts : null;
      b.checkLed.title = !r || (r.ok === null && !r.checking) ? t("not checked") : r.checking ? t("checking…")
                       : r.ok ? `${f.model || "?"} · ${t("serial {s}", { s: f.serial || "?" })} · ${f.fw_type || "?"} ${f.fw_version || ""}${r.stale ? " · " + t("checked at another address") : ""}${C.simulator ? " · " + t("simulated") : ""}`
                       : r.error || t("no answer");
    });
    const when = document.getElementById("check-when");
    if (when) setText(when, C && C.checked_at ? t("boards checked {time} by {who}", { time: new Date(C.checked_at * 1000).toLocaleTimeString(), who: C.by }) + (C.simulator ? ` · ${t("simulated")}` : "") : "");
    const active = app.state.run && app.state.run.active;
    checkBtn.disabled = !app.can("control") || Boolean(C && C.running) || Boolean(active);
    checkBtn.title = active ? t("Not during a run: the table shows the boards") : t("Connect to every board of the configuration and read what it is (model, serial, firmware, temperatures)");
  }

  function showRun() {
    const r = app.state.run;
    const active = Boolean(r && r.active);
    const last = r && !r.active ? r.last : null;
    const shown = active ? r : last;
    const control = app.can("control");
    startBtn.disabled = !control || active;
    stopBtn.disabled = !control || !active || Boolean(r.stop_sent_at);
    const killOk = active && r.stop_sent_at && (Date.now() / 1000 - r.stop_sent_at) > r.kill_after_s;
    killBtn.disabled = !control || !killOk;
    setText(phase, active ? app.phaseText(r) : last ? (last.phase === "finished" ? t("Finished") : t("Failed")) : t("Idle"));
    phase.style.color = active ? "" : last && last.phase === "failed" ? "var(--critical-text)" : "";
    setText(who, shown ? [shown.run_name ? `${shown.run_name} · ` : "", t("started by {name} · {time}", { name: shown.started_by || "?", time: new Date(shown.started * 1000).toLocaleString() }),
                          shown.simulator ? " · " + t("SIMULATOR: no hardware is used") : ""].join("") : "");
    const tt = shown ? shown.t || 0 : 0;
    const dur = shown ? shown.duration : app.state.config.config.run.duration_s;
    bar.firstChild.style.width = `${dur ? Math.min(100, (100 * tt) / dur) : active ? 100 : 0}%`;
    bar.className = `progress ${last && last.phase === "failed" ? "critical" : last ? "good" : active && !dur ? "indeterminate" : ""}`;
    setText(timeText, dur ? `${fmtDuration(tt)} / ${fmtDuration(dur)}` : `${fmtDuration(tt)} · ${t("until Stop")}`);
    clear(result);
    if (last) {
      const why = last.stop_reason ? ` (${last.stop_reason})` : "";
      if (last.phase === "finished") {
        result.appendChild(h("div.notice", icon("check"), t("Run finished{why}: {n} events from {b} boards, {mb} in the data files; every board sent its stop record.",
                                                            { why, n: fmtInt(last.events), b: (last.boards || []).length, mb: fmtBytes(last.bytes) })));
      } else {
        result.appendChild(h("div.notice.error", icon("alert"),
                             t("Run failed (exit code {c}){why}: {err}", { c: last.exit_code ?? "?", why, err: (last.errors || []).slice(0, 3).join("; ") || t("see the DAQ output") })));
      }
    } else if (active && r.stop_sent_at) {
      result.appendChild(h("div.notice.info", icon("info"), t("Stop sent by {name}: the boards send their last data and stop records.", { name: r.stopped_by || "?" })));
    } else if (active && r.warnings && r.warnings.length) {
      result.appendChild(h("div.notice.warn", icon("alert"), r.warnings[r.warnings.length - 1]));
    }
    result.classList.toggle("hidden", !result.childElementCount);
    const note = active ? r.note || "" : app.state.config.note || "";
    setText(noteLabel, active ? t("Note:") : t("Note for the next run:"));
    setText(noteText, note.replace(/\s*\n\s*/g, " · "));
    noteText.title = note;
    noteLine.classList.toggle("hidden", !note);
    // the boards
    const states = shown && shown.boards ? shown.boards : [];
    const lag = active && shown.now ? Math.max(0, Date.now() / 1000 - shown.now) : 0;
    boardEls.forEach((b) => {
      const s = states.find((x) => x.name === b.name);
      const [led, text] = BOARD_STATES[s ? s.state : "waiting"] || ["", s ? s.state : t("waiting")];
      b.stateLed.className = `led ${led}`;
      setText(b.stateText, t(text) + (s && s.ended && !active ? ` · ${t(s.ended)}` : ""));
      setText(b.ev, s ? fmtInt(s.events) : "—");
      setText(b.rate, s ? fmtRate(s.rate) : "—");
      setText(b.mbps, s && s.mbps !== null && s.mbps !== undefined ? fmtNum(s.mbps, 2) : "—");
      setText(b.trig, s && s.last_trigger !== null && s.last_trigger !== undefined ? fmtInt(s.last_trigger) : "—");
      const temps = s && s.monitors ? Object.entries(s.monitors).filter(([k, v]) => k.startsWith("temp") && typeof v === "number").map(([, v]) => v) : [];
      setText(b.temp, temps.length ? fmtNum(Math.max(...temps), 0) : "—");
      b.temp.title = s && s.monitors ? Object.entries(s.monitors).map(([k, v]) => `${k}: ${v}`).join(" · ") : "";
      const age = s && s.age !== null && s.age !== undefined ? s.age + lag : null;
      const limit = Math.max(QUIET_S, s && s.rate ? 3 / s.rate : 0);
      const quiet = active && shown.phase === "running" && age !== null && age > limit;
      clear(b.quiet, quiet ? chip(t("quiet {s} s", { s: Math.round(age) }), "warning", "alert") : null);
      const msg = s && s.message ? s.message : "";
      setText(b.msg, msg);
      b.msgRow.classList.toggle("hidden", !msg);
      b.row.classList.toggle("error", Boolean(s && s.state === "error"));
      b.row.title = s ? `${b.where} · ${s.serial ? t("serial {s}", { s: s.serial }) + " · " : ""}${fmtBytes(s.bytes)} · ${(s.files || []).map((f) => basename(f.path)).join(", ")}` : b.where;
    });
    // tiles
    const first = states[0];
    const files = shown && shown.data_files ? shown.data_files : [];
    const totalMbps = states.reduce((a, s) => a + (s.mbps || 0), 0);
    clear(tiles,
          tile(t("Run time"), shown ? fmtDuration(tt) : "—", shown ? (dur ? t("of {r}", { r: fmtDuration(dur) }) : t("until Stop")) : ""),
          tile(t("Events"), first ? fmtInt(first.events) : "—", first ? t("of {b} (the first board)", { b: first.name }) : ""),
          tile(t("Event rate"), first ? fmtRate(first.rate) : "—", t("Hz · newest 10 s")),
          tile(t("Data rate"), shown && states.length ? fmtNum(totalMbps, 2) : "—", t("MB/s · all boards")),
          tile(t("Data"), shown ? fmtBytes(shown.bytes) : "—", shown ? t("{n} files", { n: files.length }) : ""),
          tile(t("Boards"), shown ? String(states.length) : String(boardEls.length),
               shown ? t(shown.sync === "chain" && states.length > 1 ? "clock chain" : states.length > 1 ? "independent boards" : "one board") : ""));
    plot.setPoints((shown && shown.mbps_series) || []);
  }

  function tile(label, value, note) {
    return h("div.stat", { title: note || "" }, h("span.label", label), h("div.value", value), note ? h("div.note", note) : null);
  }

  function showRoot() {
    const R = app.state.root;
    clear(rootBox);
    if (!R) return;
    const last = R.history && R.history.length ? R.history[R.history.length - 1] : null;
    if (R.busy) {
      const name = basename(R.current || "");
      rootBox.append(h("div.row.nowrap", h("span.led.busy.pulse"),
                       h("span.grow.break.small", t("ROOT: converting {name} ({n} of {total}) · {p} %", { name, n: R.number, total: R.total, p: Math.round(R.percent) }))),
                     h("div.row.nowrap", h("div.progress.grow", h("span", { style: `width:${R.percent}%` })),
                       app.can("control") ? h("button.btn.btn-sm", { type: "button", onclick: stopConverting }, t("Stop converting")) : null));
    } else if (last) {
      const ok = last.status === "ok";
      rootBox.append(h("div.row.nowrap", h(`span.led.${ok ? "ok" : "warn"}`),
                       h("span.grow.break.small", ok ? t("ROOT: {name} · {n} events, the same as in the .raw file", { name: basename(last.root), n: fmtInt(last.events_root) })
                                                     : t("ROOT: {name}: {status} ({message})", { name: basename(last.root || last.raw), status: t(last.status), message: last.message }))));
    }
  }

  // ---- the log ----
  function addLines(lines, reset) {
    const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    if (reset) log.replaceChildren();
    const frag = document.createDocumentFragment();
    for (const [n, text, err] of lines) {
      const warn = !err && text.includes("WARNING:");
      frag.appendChild(h(`div.line${err ? ".error" : warn ? ".warn" : ""}`, h("span.n", n), h("span", text)));
    }
    log.appendChild(frag);
    while (log.childElementCount > 5000) log.firstChild.remove();
    if (!log.childElementCount) log.appendChild(h("div.empty", t("The DAQ program has printed nothing yet.")));
    else if (log.firstChild.classList.contains("empty") && log.childElementCount > 1) log.firstChild.remove();
    if (follow.input.checked && (atEnd || reset)) log.scrollTop = log.scrollHeight;
  }

  // ---- actions ----
  async function checkBoards() {
    try { await post("/api/boards/check"); } catch (err) { fail(err); }
  }

  async function startRun() {
    let p;
    try { p = await get("/api/run/preview"); } catch (err) { fail(err); return; }
    if (p.errors.length || p.others.length || p.busy) {
      await dialog({ title: t("Cannot start"), body: h("ul.problems",
        p.busy ? h("li.error", icon("alert"), t("A run is going already.")) : null,
        p.others.map((o) => h("li.error", icon("alert"), t("A DAQ program is running already: pid {pid}", { pid: o.pid }))),
        p.errors.map((e) => h("li.error", icon("alert"), `${e.where}: ${e.message}`))) });
      return;
    }
    const facts = h("dl.facts",
      h("dt", t("Boards")), h("dd", `${p.boards} · ${t(p.boards > 1 ? (p.sync === "chain" ? "clock chain" : "independent boards") : "one board")}`),
      h("dt", t("Trigger")), h("dd", t(SETUP_NAMES[p.setup] || p.setup)),
      h("dt", t("Run time")), h("dd", p.duration ? `${p.duration} s (${fmtDuration(p.duration)})` : t("until Stop")),
      p.max_events ? [h("dt", t("Events")), h("dd", t("stop at {n} events of the first board", { n: fmtInt(p.max_events) }))] : null,
      h("dt", t("Run record")), h("dd.mono.small", p.runs_folder),
      h("dt", t("Data")), h("dd.mono.small", p.simulator ? t("in the run folder (simulator)") : p.data_folder ? `${p.data_folder}/${p.run_name}_BOARD.raw` : t("not written")),
      p.free_bytes !== null ? [h("dt", t("Free there")), h("dd", fmtBytes(p.free_bytes))] : null);
    const noteBox = h("textarea.input.note-input", { rows: "3", maxlength: "4000", "aria-label": t("Note for this run") });
    noteBox.value = app.state.config.note || "";
    const lines = [facts,
      h("div.field", h("label", t("Note for this run: kept with its configuration, in the run folder, in each data file and next to it")), noteBox),
      p.simulator ? h("div.notice.info", icon("info"), t("SIMULATOR: no hardware is used")) : null,
      p.need_bytes && p.free_bytes !== null && p.free_bytes < p.need_bytes
        ? h("div.notice.warn", icon("alert"), t("WARNING: about {gb} GB are needed", { gb: (p.need_bytes / 1e9).toFixed(1) })) : null,
      p.converting ? h("div.notice", icon("info"), t("The ROOT conversion of the last run goes on during this run ({n} file(s) left).", { n: p.converting })) : null,
      ...p.warnings.map((w) => h("div.notice.warn", icon("alert"), `${w.where}: ${w.message}`))];
    const what = p.duration ? t("Start a run of {t} on {n} boards?", { t: fmtDuration(p.duration), n: p.boards }) : t("Start a run on {n} boards (until Stop)?", { n: p.boards });
    if (!(await confirmDialog(what, lines, t("Start run")))) return;
    try {
      await post("/api/run/start", { note: noteBox.value });
      toast(t("Run started"), "ok");
    } catch (err) { fail(err); }
  }

  async function stopRun() {
    try { await post("/api/run/stop"); } catch (err) { fail(err); }
  }

  async function killRun() {
    if (!(await confirmDialog(t("Kill the DAQ program"), [t("End the DAQ program at once? The end of the data can be lost and the boards stay armed until the next run resets them.")],
                              t("Kill"), "danger"))) return;
    try { await post("/api/run/kill"); } catch (err) { fail(err); }
  }

  async function stopConverting() {
    try { await post("/api/root/cancel"); } catch (err) { fail(err); }
  }

  // ---- first draw, then updates ----
  setText(summary, configSummary(app));
  buildBoards();
  addLines(app.state.log.slice(-3000), true);
  showRoot();
  const timer = setInterval(showRun, 1000);
  let shape = boardList().map((b) => b.name).join(",");

  return {
    update(type, msg) {
      if (type === "run") {
        const now = boardList().map((b) => b.name).join(",");
        if (now !== shape) { shape = now; buildBoards(); } else showRun();
        showCheck();
      } else if (type === "log") addLines(msg.lines, false);
      else if (type === "log_reset") addLines([], true);
      else if (type === "boards") showCheck();
      else if (type === "root" || type === "root_done") showRoot();
      else if (type === "config" || type === "settings") {
        setText(summary, configSummary(app));
        convert.input.checked = app.state.settings.values.convert_to_root;
        const now = boardList().map((b) => b.name).join(",");
        if (now !== shape) { shape = now; buildBoards(); }
      }
      waves.update(type, msg);
    },
    restyle() { plot.restyle({ xLabel: t("run time (s)"), yLabel: "MB/s" }); waves.restyle(); showRun(); },
    unmount() { clearInterval(timer); plot.destroy(); waves.unmount(); },
  };
}

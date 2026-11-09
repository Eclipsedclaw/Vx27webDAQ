// Tool: a data file and what to do with it: show its waveforms on the Run page, its overview, convert a .raw
// file to ROOT (next to it, checked; never over an existing .root), build its run's events into one ROOT file
// (every board per trigger, WaveDump2's multi-board single file), or take some of its events as text.
import { get, post } from "../api.js";
import { T, t } from "../i18n.js";
import { basename, chooseFile, fail, fmtBytes, fmtInt, fmtNum, h, icon, setText, store, toast } from "../ui.js";

export default {
  id: "datafile",
  title: T("Data file"),
  icon: "file",
  summary: T("open a .raw or ROOT file: its waveforms, its overview, a ROOT file of it, its run's boards in one file, or some events as text"),
  open: true,
  make(body, ctx) {
    let file = store("tools.file") || "", info = null;
    const fileText = h("span.grow.mono.small.break");
    const infoLine = h("div.hint");
    const showBtn = h("button.btn", { type: "button", onclick: showOnRun }, icon("wave"), t("Show on the Run page"));
    const overviewBtn = h("button.btn", { type: "button", onclick: () => { ctx.open("overview"); ctx.emit("overview-file", file); } }, icon("grid"), t("Overview"));
    const convertBtn = h("button.btn", { type: "button", onclick: convert }, icon("refresh"), t("Convert to ROOT"));
    const buildBtn = h("button.btn", { type: "button", onclick: buildEvents }, icon("grid"), t("Every board per trigger"));
    const exFirst = h("input.input.num", { type: "number", min: "0", value: "0", style: "width:100px", "aria-label": t("first event") });
    const exCount = h("input.input.num", { type: "number", min: "1", max: "1000", value: "10", style: "width:90px", "aria-label": t("events") });
    const exChans = h("input.input", { placeholder: t("all channels (or e.g. 0-3,8)"), style: "width:200px", "aria-label": t("channels") });
    const exportBtn = h("button.btn", { type: "button", onclick: exportText }, icon("download"), t("Text file"));
    body.append(
      h("div.row.nowrap", icon("file"), fileText, h("button.btn.btn-sm", { type: "button", onclick: pick }, icon("folder"), t("Choose file…"))),
      infoLine,
      h("div.row", showBtn, overviewBtn),
      h("div.row", convertBtn, h("span.hint", t(".raw → .root next to it: one TTree \"events\", one entry per trigger; checked against the .raw file; never over an existing .root"))),
      h("div.row", buildBtn, h("span.hint", t("RUN_built.root beside the run's files: one entry per trigger of the first board, every board's event with the same trigger ID within the sync window (BOARD_found, BOARD_dt_ns, BOARD_chN); done after every run of several boards when the Settings say so"))),
      h("div.row", exportBtn, h("span.label", t("from event")), exFirst, h("span.label", t("events")), exCount, exChans,
        h("span.hint", t("a download, as WaveDump2's text files: per event its number, timestamp, samples, sample time and channels, then a column per channel"))));

    async function pick() {
      const p = await chooseFile({ title: t("Choose a data file") });
      if (p) { file = p; store("tools.file", p); await loadInfo(); }
    }

    async function loadInfo() {
      info = null;
      if (file) {
        try { info = await get("/api/files/info", { path: file }); } catch (err) { info = { error: err.message }; }
      }
      show();
    }

    function show() {
      setText(fileText, file || t("No file chosen yet."));
      const ok = info && !info.error;
      const s = ok ? info.settings || {} : {};
      setText(infoLine, info && info.error ? info.error : ok ? t("{kind} file · {n} events · board {b} ({m}, serial {sn}) · run {run} · {size} · {rl} samples of {ns} ns",
        { kind: info.kind === "root" ? "ROOT" : ".raw", n: fmtInt(info.events), b: s.board || "?", m: s.model || "?", sn: s.serial || "?", run: s.run || "?",
          size: fmtBytes(info.size), rl: fmtInt(info.record_length), ns: fmtNum(info.sample_ns, info.sample_ns % 1 ? 1 : 0) })
        + (info.truncated ? " · " + t("cut: it ends inside an event") : "") + (s.simulator ? " · " + t("simulated data") : "") : "");
      convertBtn.disabled = !ok || info.kind !== "raw";
      buildBtn.disabled = !ok || info.kind !== "raw";
      exportBtn.disabled = !ok;
      if (ok) exFirst.max = String(Math.max(0, info.events - 1));
      showBtn.disabled = overviewBtn.disabled = !ok;
    }

    function showOnRun() {
      store("wave.file", file);
      store("wave.source", "file");
      location.hash = "#run";
    }

    async function convert() {
      try {
        await post("/api/root/convert", { path: file });
        toast(t("Conversion queued: {name}", { name: basename(file) }), "ok");
        ctx.open("rootconv");
      } catch (err) { fail(err); }
    }

    async function buildEvents() {
      try {
        const r = await post("/api/root/build", { path: file });
        toast(t("Queued: {name}", { name: basename(r.out) }), "ok");
        ctx.open("rootconv");
      } catch (err) { fail(err); }
    }

    function exportText() {
      const q = new URLSearchParams({ path: file, first: String(Number(exFirst.value) || 0), count: String(Math.min(1000, Math.max(1, Number(exCount.value) || 1))) });
      if (exChans.value.trim()) q.set("channels", exChans.value.trim().replace(/\s+/g, ""));
      const a = h("a", { href: `/api/files/export?${q}`, download: "" });
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    loadInfo();
    return { update(type) { if (type === "root_done") loadInfo(); } };
  },
};

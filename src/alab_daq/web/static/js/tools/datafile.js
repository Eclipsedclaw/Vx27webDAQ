// Tool: a data file and what to do with it: show its waveforms on the Run page, its overview, or convert a .raw
// file to ROOT (next to it, checked; never over an existing .root).
import { get, post } from "../api.js";
import { T, t } from "../i18n.js";
import { basename, chooseFile, fail, fmtBytes, fmtInt, fmtNum, h, icon, setText, store, toast } from "../ui.js";

export default {
  id: "datafile",
  title: T("Data file"),
  icon: "file",
  summary: T("open a .raw or ROOT file: its waveforms, its overview, or a ROOT file of it"),
  open: true,
  make(body, ctx) {
    let file = store("tools.file") || "", info = null;
    const fileText = h("span.grow.mono.small.break");
    const infoLine = h("div.hint");
    const showBtn = h("button.btn", { type: "button", onclick: showOnRun }, icon("wave"), t("Show on the Run page"));
    const overviewBtn = h("button.btn", { type: "button", onclick: () => { ctx.open("overview"); ctx.emit("overview-file", file); } }, icon("grid"), t("Overview"));
    const convertBtn = h("button.btn", { type: "button", onclick: convert }, icon("refresh"), t("Convert to ROOT"));
    body.append(
      h("div.row.nowrap", icon("file"), fileText, h("button.btn.btn-sm", { type: "button", onclick: pick }, icon("folder"), t("Choose file…"))),
      infoLine,
      h("div.row", showBtn, overviewBtn),
      h("div.row", convertBtn, h("span.hint", t(".raw → .root next to it: one TTree \"events\", one entry per trigger; checked against the .raw file; never over an existing .root"))));

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

    loadInfo();
    return { update(type) { if (type === "root_done") loadInfo(); } };
  },
};

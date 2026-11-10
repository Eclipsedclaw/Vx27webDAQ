// Tool: the boards, as they said at the last board check: model, serial, firmware, channels, ADC, the largest raw
// buffer, temperatures; and the whole parameter tree of a board (as WaveDump2's Device Manager shows it), with a
// filter: the names and allowed values to use for the expert parameters of the Configuration page. The manual
// controller (WaveDump2's) gets or sets one parameter of an idle board, or sends it a command (operators; a click
// on a parameter fills it in). The next run resets every board and sets it from the configuration.
import { SLOW_MS, get, post } from "../api.js";
import { T, t } from "../i18n.js";
import { clear, fail, fmtBytes, fmtClock, fmtInt, fmtNum, h, icon, setText, store, toast } from "../ui.js";

export default {
  id: "boards",
  title: T("Boards"),
  icon: "board",
  summary: T("what each board said at the last check, and its whole parameter tree"),
  open: false,
  badge(app) {
    const C = app.state.boards;
    if (!C || !C.results) return "";
    const bad = C.results.filter((r) => r.ok === false).length;
    return C.running ? t("checking…") : bad ? t("{n} did not answer", { n: bad }) : "";
  },
  make(body, ctx) {
    const app = ctx.app;
    const checkBtn = h("button.btn", { type: "button", onclick: check }, icon("link"), t("Check boards"));
    const when = h("span.hint");
    const factsBox = h("div.table-wrap");
    const boardSel = h("select.input", { "aria-label": t("Board"), onchange: () => { store("tools.tree.board", boardSel.value); loadTree(); } });
    const filter = h("input.input", { placeholder: t("filter: name, value or description"), style: "width:min(320px,100%)",
                                      oninput: () => renderTree() });
    const allCh = h("input", { type: "checkbox", onchange: () => renderTree() });
    const treeInfo = h("span.hint");
    const treeBox = h("div.table-wrap", { style: "max-height:60vh" });
    let tree = null;
    // the manual controller
    const mcPath = h("input.input.mono", { placeholder: "/par/TrgOutMode   /ch/3/par/DCOffset   Reset", style: "width:min(300px,100%)",
                                          "aria-label": t("parameter path or command") });
    const mcValue = h("input.input.mono", { placeholder: t("value"), style: "width:150px", "aria-label": t("value") });
    const mcChoice = h("select.input.hidden", { "aria-label": t("allowed values"), onchange: () => { mcValue.value = mcChoice.value; } });
    const mcGet = h("button.btn.btn-sm", { type: "button", onclick: () => mc("get") }, t("Get"));
    const mcSet = h("button.btn.btn-sm", { type: "button", onclick: () => mc("set") }, t("Set"));
    const mcCmd = h("button.btn.btn-sm", { type: "button", onclick: () => mc("cmd"),
                                        title: t("Send the command named in the path field, e.g. Reset or ReloadCalibration (sent as /cmd/NAME)") }, t("Send command"));
    const mcOut = h("div.mono.small.mc-out");
    const mcBox = h("div.chan-box", { style: "margin-top:10px" },
      h("div.row.wrap", h("b.small", t("Manual controller")), h("span.hint", t("one parameter of the board chosen below, while no run is going; the next run resets the board and sets it from the configuration (lasting settings: Expert parameters)"))),
      h("div.row.wrap", mcPath, mcValue, mcChoice, mcGet, mcSet, mcCmd), mcOut);
    body.append(h("div.row", checkBtn, when), factsBox, mcBox,
                h("div.row", { style: "margin-top:10px" }, h("b.small", t("Parameters of")), boardSel, filter,
                  h("label.row.nowrap", allCh, t("every channel (else channel 0 only)")), h("span.spacer"), treeInfo),
                treeBox);

    // a run or a sequence of runs (also between two of its runs) has the boards: the server refuses a check then
    function busy() {
      return Boolean((app.state.run && app.state.run.active) || (app.state.sequence && app.state.sequence.state === "running"));
    }

    function showFacts() {
      const C = app.state.boards || {};
      const active = busy();
      checkBtn.disabled = !app.can("control") || Boolean(C.running) || active;
      checkBtn.title = !app.can("control") ? t("Operators check the boards") : active ? t("Not during a run: the Run page shows the boards") : "";
      setText(when, C.checked_at ? t("checked {time} by {who}", { time: fmtClock(C.checked_at), who: C.by }) + (C.simulator ? ` · ${t("simulated")}` : "") : t("not checked yet"));
      const rows = (C.results || []).map((r) => {
        const f = r.facts || {}, m = r.monitors || {};
        const temps = Object.entries(m).filter(([k, v]) => k.startsWith("temp") && typeof v === "number");
        return h("tr", h("td", h("span.row.nowrap", h(`span.led.small.${r.checking ? "busy.pulse" : r.ok ? (r.stale ? "warn" : "ok") : r.ok === false ? "error" : ""}`), h("b", r.name))),
                 h("td.mono.small", r.uri || ""),
                 r.ok ? [h("td", f.model || "—"), h("td", f.serial || "—"), h("td", `${f.fw_type || "?"} ${f.fw_version || ""}`), h("td.n", fmtInt(f.channels)),
                         h("td.n", f.adc_bits ? `${f.adc_bits} bit · ${fmtNum(f.adc_msps, 0)} MS/s` : "—"), h("td.n", f.max_raw ? fmtBytes(f.max_raw) : "—"),
                         h("td.n", { title: temps.map(([k, v]) => `${k}: ${v} °C`).join(" · ") }, temps.length ? `${fmtNum(Math.max(...temps.map(([, v]) => v)), 0)} °C` : "—")]
                      : h("td.board-msg", { colspan: "7" }, r.checking ? t("checking…") : r.error || t("not checked")));
      });
      clear(factsBox, h("table.table", h("thead", h("tr", h("th", t("Board")), h("th", t("address")), h("th", t("model")), h("th", t("serial")),
                                                        h("th", t("firmware")), h("th.n", t("channels")), h("th.n", t("ADC")),
                                                        h("th.n", { title: t("The largest raw buffer the board sends at once (MaxRawDataSize)") }, t("buffer")),
                                                        h("th.n", t("hottest")))),
                                     h("tbody", rows.length ? rows : h("tr", h("td.muted", { colspan: "9" }, t("No boards in the configuration."))))));
      const keep = boardSel.value || store("tools.tree.board") || "";
      clear(boardSel, (C.results || []).map((r) => h("option", { value: r.name }, r.name)));
      if ([...boardSel.options].some((o) => o.value === keep)) boardSel.value = keep;
    }

    async function loadTree() {
      tree = null;
      if (!boardSel.value) { renderTree(); return; }
      try { tree = await get("/api/boards/tree", { name: boardSel.value }); } catch (err) { tree = { error: err.message }; }
      renderTree();
    }

    function renderTree() {
      if (!tree || tree.error) {
        setText(treeInfo, "");
        clear(treeBox, h("div.empty-state", icon("board"), tree && tree.error ? tree.error : t("Check the boards to see their parameters.")));
        return;
      }
      const q = filter.value.trim().toLowerCase();
      const rows = tree.rows.filter((r) => (allCh.checked || !/^\/ch\/(?!0\/)\d+\//.test(r.path)) &&
                                           (!q || JSON.stringify(r).toLowerCase().includes(q)));
      setText(treeInfo, t("{n} of {m} parameters · saved {time}", { n: fmtInt(rows.length), m: fmtInt(tree.rows.length), time: fmtClock(tree.saved) })
                        + (tree.simulator ? ` · ${t("simulated")}` : ""));
      const extra = (r) => Object.entries(r).filter(([k]) => !["path", "value", "accessmode", "handle"].includes(k))
        .map(([k, v]) => `${k}: ${v}`).join(" · ");
      clear(treeBox, h("table.table.plan-table", h("thead", h("tr", h("th", t("parameter")), h("th", t("value")), h("th", t("access")), h("th", t("more")))),
        h("tbody", rows.slice(0, 3000).map((r) => h("tr.pick", { title: t("Click: into the manual controller"), onclick: () => pickParam(r) },
                                                     h("td.mono", r.path), h("td.mono", String(r.value ?? "")),
                                                     h("td.small", r.accessmode || ""), h("td.hint", extra(r)))))));
    }

    // a parameter of the tree into the manual controller: its path as FELib takes it, its value and allowed values
    function pickParam(r) {
      const m = /^\/(cmd)\/([^/]+)$/.exec(r.path);
      mcPath.value = m ? m[2] : r.path;
      mcValue.value = String(r.value ?? "");
      let allowed = r.allowedvalues ? String(r.allowedvalues).split(", ") : [];
      if (!allowed.length && tree) allowed = tree.rows.filter((x) => x.path.startsWith(`${r.path}/allowedvalues/`) && x.value !== undefined).map((x) => String(x.value));
      clear(mcChoice, h("option", { value: "" }, t("allowed values…")), allowed.map((v) => h("option", { value: v }, v)));
      mcChoice.classList.toggle("hidden", !allowed.length);
      mcPath.scrollIntoView({ block: "nearest" });
      mcPath.focus();
    }

    function showMc() {
      const C = app.state.boards || {}, active = busy();
      const ok = app.can("control") && !active && !C.running && boardSel.value;
      for (const b of [mcGet, mcSet, mcCmd]) b.disabled = !ok;
      mcBox.title = !app.can("control") ? t("Operators use the manual controller") : active ? t("Not during a run: the DAQ program has the boards") : "";
    }

    async function mc(action) {
      const path = mcPath.value.trim();
      if (!path) { mcPath.focus(); return; }
      if (action === "set" && !mcValue.value.trim()) { mcValue.focus(); return; }
      setText(mcOut, t("asking {b}…", { b: boardSel.value }));
      try {
        const r = await post("/api/boards/param", { board: boardSel.value, action, path, value: mcValue.value }, { timeout: SLOW_MS });
        const sim = r.simulator ? ` · ${t("simulated")}` : "";
        if (r.ok && action !== "cmd") { mcValue.value = String(r.value ?? ""); setText(mcOut, `${boardSel.value} ${r.path} = ${r.value}${sim}`); }
        else if (r.ok) setText(mcOut, t("{b}: command {c} sent", { b: boardSel.value, c: path }) + sim);
        else setText(mcOut, `${boardSel.value}: ${r.error}${sim}`);
        if (action !== "get") toast(r.ok ? t("Done: {m}", { m: mcOut.textContent }) : r.error, r.ok ? "ok" : "error");
      } catch (err) { setText(mcOut, err.message); fail(err); }
    }

    async function check() {
      try { await post("/api/boards/check"); } catch (err) { fail(err); }
    }

    showFacts();
    showMc();
    loadTree();
    return {
      update(type) {
        if (type === "boards") {
          const before = app.state.boards && app.state.boards.running;
          showFacts();
          if (!before) loadTree();
        }
        if (type === "run" || type === "config" || type === "boards" || type === "sequence") { showFacts(); showMc(); }
      },
    };
  },
};

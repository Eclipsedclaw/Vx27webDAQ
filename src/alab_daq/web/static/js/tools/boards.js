// Tool: the boards, as they said at the last board check: model, serial, firmware, channels, ADC, the largest raw
// buffer, temperatures; and the whole parameter tree of a board (as WaveDump2's Device Manager shows it), with a
// filter: the names and allowed values to use for the expert parameters of the Configuration page.
import { get, post } from "../api.js";
import { T, t } from "../i18n.js";
import { clear, fail, fmtBytes, fmtClock, fmtInt, fmtNum, h, icon, setText, store } from "../ui.js";

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
    body.append(h("div.row", checkBtn, when), factsBox,
                h("div.row", { style: "margin-top:10px" }, h("b.small", t("Parameters of")), boardSel, filter,
                  h("label.row.nowrap", allCh, t("every channel (else channel 0 only)")), h("span.spacer"), treeInfo),
                treeBox);

    function showFacts() {
      const C = app.state.boards || {};
      const active = app.state.run && app.state.run.active;
      checkBtn.disabled = !app.can("control") || Boolean(C.running) || Boolean(active);
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
        h("tbody", rows.slice(0, 3000).map((r) => h("tr", h("td.mono", r.path), h("td.mono", String(r.value ?? "")),
                                                     h("td.small", r.accessmode || ""), h("td.hint", extra(r)))))));
    }

    async function check() {
      try { await post("/api/boards/check"); } catch (err) { fail(err); }
    }

    showFacts();
    loadTree();
    return {
      update(type) {
        if (type === "boards") {
          const before = app.state.boards && app.state.boards.running;
          showFacts();
          if (!before) loadTree();
        }
        if (type === "run" || type === "config") showFacts();
      },
    };
  },
};

// Tool: the ROOT conversions, the one going (with its progress) and the ones before it, newest first.
import { post } from "../api.js";
import { T, t } from "../i18n.js";
import { basename, clear, fail, fmtClock, fmtInt, h, icon } from "../ui.js";

export default {
  id: "rootconv",
  title: T("ROOT conversions"),
  icon: "refresh",
  summary: T("every .raw file of a run becomes a checked .root file next to it"),
  open: false,
  badge(app) {
    const R = app.state.root;
    return R && R.busy ? t("converting · {p} %", { p: Math.round(R.percent) }) : "";
  },
  make(body, ctx) {
    const app = ctx.app;
    const box = h("div.stack", { style: "gap:8px" });
    body.appendChild(box);

    function show() {
      const R = app.state.root;
      const rows = [];
      if (R && R.busy) {
        rows.push(h("div.row.nowrap", h("span.led.busy.pulse"),
                    h("span.grow.break", t("ROOT: converting {name} ({n} of {total}) · {p} %", { name: basename(R.current), n: R.number, total: R.total, p: Math.round(R.percent) })),
                    app.can("control") ? h("button.btn.btn-sm", { type: "button", onclick: () => post("/api/root/cancel").catch(fail) }, t("Stop converting")) : null),
                  h("div.progress", h("span", { style: `width:${R.percent}%` })));
      }
      const hist = (R && R.history) || [];
      if (!hist.length && !(R && R.busy)) rows.push(h("div.empty-state", icon("refresh"), t("No conversions yet.")));
      for (const r of [...hist].reverse()) {
        rows.push(h("div.row.nowrap", h(`span.led.${r.status === "ok" ? "ok" : r.status === "cancelled" ? "" : "warn"}`),
                    h("span.grow.break.small", { title: r.message || "" },
                      `${basename(r.root || r.raw)} · ${t(r.status)}${r.status === "ok" ? ` · ${fmtInt(r.events_root)} ${t("events")}` : ""}` +
                      (r.status === "skipped" && r.message ? ` · ${r.message}` : "")),
                    h("span.hint.nowrap", `${r.by || "?"} · ${fmtClock(r.time).slice(11)}`)));
      }
      clear(box, rows);
    }

    show();
    return { update(type) { if (type === "root" || type === "root_done") show(); } };
  },
};

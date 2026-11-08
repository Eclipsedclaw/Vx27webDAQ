// The Setup panel of the Run page: the boards and how they are tied together, drawn from the configuration (what
// each board gets, core/config.py plan) with the live state over it: the clock chain (CLK-OUT -> CLK-IN, with the
// run signal and the run delays), the trigger (TRG-OUT -> TRG-IN along the chain, a pulser into the first board,
// or every TRG-OUT into a logic unit whose OR comes back), the GPIO busy line, what each board triggers on and
// what its TRG-OUT sends; and per board its state, events, rate, temperature, error flags and its timestamps
// against the first board (the sync check).
import { T, t } from "./i18n.js";
import { clear, fmtInt, fmtNum, h, setText } from "./ui.js";

const NS = "http://www.w3.org/2000/svg";
const W = 480, BOX_H = 92, GAP = 70, LEFT = 34, RIGHT = 12;
const ROLE = { master: T("master"), middle: T("chain"), last: T("end of chain"), single: T("on its own") };
const SOURCE_TEXT = { ITLA: T("its channels (ITLA)"), ITLB: T("its channels (ITLB)"), ITLA_AND_ITLB: T("its channels (ITLA AND ITLB)"),
                      ITLA_OR_ITLB: T("its channels (ITLA OR ITLB)"), TrgIn: T("TRG-IN"), SwTrg: T("software"), TestPulse: T("its test pulse") };

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, String(v));
  for (const c of children.flat()) if (c !== null && c !== undefined) el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

function fit(text, px) {                       // shorten a text to about px pixels of 12 px type
  const most = Math.max(4, Math.floor(px / 6.4));
  return text.length > most ? `${text.slice(0, most - 1)}…` : text;
}

// the value each parameter ends with in a board's plan (the last setting of a path wins)
function planValues(plan) {
  const v = {};
  for (const [path, value] of (plan && plan.settings) || []) v[path.replace(/^\/par\//, "")] = value;
  return v;
}

function sourceText(src) {
  return (src || "").split("|").map((x) => t(SOURCE_TEXT[x] || x)).join(" + ");
}

export function setupView(app) {
  const el = h("div.setup-view");
  const note = h("div.hint");
  const host = h("div.setup-svg");
  const foot = h("div.small.setup-foot");
  el.append(note, host, foot);

  function render() {
    const c = app.state.config, cfg = c.config;
    const boards = cfg.boards.map((b, i) => ({ b, info: c.boards[i] || {} })).filter((x) => x.b.enabled);
    const plans = new Map((c.plans || []).map((p) => [p.name, p]));
    const run = app.state.run, shown = run && (run.active ? run : run.last);
    const live = new Map(((shown && shown.boards) || []).map((b) => [b.name, b]));
    const match = shown && shown.matching;
    const setup = cfg.trigger.setup, chain = cfg.run.sync === "chain" && boards.length > 1;
    setText(note, run && run.active ? t("From the configuration being edited, with the live state of the run going (its boards by name).")
                                    : t("From the configuration being edited, with the state of the last run (its boards by name)."));
    const extTop = setup === "external" ? 54 : 0;
    const orBottom = setup === "logic_or" ? 66 : 0;
    const height = extTop + boards.length * BOX_H + Math.max(0, boards.length - 1) * GAP + orBottom + 30;
    const svg = s("svg", { viewBox: `0 0 ${W} ${height}`, width: "100%", role: "img", "aria-label": t("The boards and their cables") },
      s("defs", {},
        ...["clk", "trg", "gpio"].map((k) => s("marker", { id: `sv-arrow-${k}`, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
          s("path", { d: "M0,0 L10,5 L0,10 z", class: `sv-head-${k}` })))));
    const boxX = LEFT, boxW = W - LEFT - RIGHT;
    const y0 = (k) => extTop + k * (BOX_H + GAP);
    const values = boards.map(({ b }) => planValues(plans.get(b.name)));
    // the pulser of an external trigger
    if (setup === "external" && boards.length) {
      svg.append(s("rect", { x: boxX + boxW - 150, y: 6, width: 140, height: 26, rx: 6, class: "sv-ext" }),
                 s("text", { x: boxX + boxW - 80, y: 23, "text-anchor": "middle", class: "sv-t" }, t("pulser / external trigger")),
                 s("line", { x1: boxX + boxW - 80, y1: 32, x2: boxX + boxW - 80, y2: extTop - 2, class: "sv-trg", "marker-end": "url(#sv-arrow-trg)" }));
    }
    // the busy line: every GPIO wired together
    if (cfg.trigger.busy_veto && boards.length > 1) {
      const top = y0(0) + BOX_H / 2, bottom = y0(boards.length - 1) + BOX_H / 2;
      svg.append(s("line", { x1: 14, y1: top, x2: 14, y2: bottom, class: "sv-gpio" }),
                 s("text", { x: 10, y: (top + bottom) / 2, transform: `rotate(-90 10 ${(top + bottom) / 2})`, "text-anchor": "middle", class: "sv-t sv-small" }, t("GPIO: busy (wired OR)")));
      boards.forEach((_, k) => svg.append(s("line", { x1: 14, y1: y0(k) + BOX_H / 2, x2: boxX, y2: y0(k) + BOX_H / 2, class: "sv-gpio" })));
    }
    boards.forEach(({ b, info }, k) => {
      const y = y0(k), p = plans.get(b.name) || {}, v = values[k], L = live.get(b.name);
      const role = p.role || "single", master = role === "master";
      const ns = info.sample_ns || 0, win = info.window_ns || 0, pre = info.pre_ns || 0;
      const g = s("g", {});
      g.append(s("rect", { x: boxX, y, width: boxW, height: BOX_H, rx: 10, class: `sv-box${master ? " sv-master" : ""}${L && L.state === "error" ? " sv-bad" : ""}` }));
      g.append(s("text", { x: boxX + 12, y: y + 20, class: "sv-t sv-name" }, `${b.name} · ${b.model}`),
               s("text", { x: boxX + boxW - 12, y: y + 20, "text-anchor": "end", class: `sv-t sv-role${master ? " sv-accent" : ""}` }, t(ROLE[role] || role)));
      const facts = `${fmtInt(info.channels)} ${t("ch")} · ${ns ? fmtNum(1000 / ns, 0) : "?"} MS/s · ${t("window {w} µs, {p} µs before", { w: fmtNum(win / 1000, 3), p: fmtNum(pre / 1000, 3) })}`;
      g.append(s("text", { x: boxX + 12, y: y + 39, class: "sv-t sv-dim" }, fit(facts, boxW - 24)));
      const start = v.StartSource ? (v.StartSource === "SWcmd" ? t("the DAQ") : v.StartSource === "EncodedClkIn" ? t("the run signal on CLK-IN") : v.StartSource) : "";
      const trig = `${t("trigger")}: ${sourceText(v.AcqTriggerSource)}` + (start ? ` · ${t("start")}: ${start}` : "")
                   + (v.VetoSource || v.BoardVetoSource ? ` · ${t("veto")}: ${v.VetoSource || v.BoardVetoSource}` : "");
      g.append(s("text", { x: boxX + 12, y: y + 57, class: "sv-t" }, fit(trig, boxW - 24)));
      let state = t("no run yet");
      if (L) {
        const temps = Object.entries(L.monitors || {}).filter(([kk, vv]) => kk.startsWith("temp") && typeof vv === "number").map(([, vv]) => vv);
        const off = match && match.offsets ? match.offsets[b.name] : null;
        state = [t(L.state || "?"), `${fmtInt(L.events)} ${t("events")}`, L.rate ? `${fmtNum(L.rate, 1)} Hz` : null,
                 temps.length ? `${fmtNum(Math.max(...temps), 0)} °C` : null,
                 L.error_flags ? t("error flags 0x{f}", { f: Number(L.error_flags).toString(16) }) : null,
                 off ? t("Δt {d} ns", { d: `${off.mean_ns >= 0 ? "+" : "−"}${fmtNum(Math.abs(off.mean_ns), 0)}` }) : (match && match.reference === b.name ? t("time reference") : null)]
          .filter(Boolean).join(" · ");
      }
      g.append(s("circle", { cx: boxX + 16, cy: y + 74, r: 4, class: `sv-led sv-led-${L ? L.state : "none"}` }),
               s("text", { x: boxX + 26, y: y + 78, class: "sv-t sv-dim" }, fit(state, boxW - 40)));
      svg.append(g);
      // the cables to the next board
      if (k < boards.length - 1) {
        const yA = y + BOX_H, yB = y0(k + 1), mid = (yA + yB) / 2;
        const xClk = boxX + 40, xTrg = boxX + boxW - 40;
        if (chain) {
          svg.append(s("line", { x1: xClk, y1: yA, x2: xClk, y2: yB - 1, class: "sv-clk", "marker-end": "url(#sv-arrow-clk)" }));
          const nxt = plans.get(boards[k + 1].b.name) || {};
          svg.append(s("text", { x: xClk + 8, y: mid - 4, class: "sv-t sv-small" }, t("CLK-OUT → CLK-IN: clock + run")),
                     s("text", { x: xClk + 8, y: mid + 11, class: "sv-t sv-small sv-dim" },
                       t("run delay {r} ns · clock-out {c} ps", { r: fmtInt(nxt.run_delay_ns ?? 0), c: fmtNum(p.clock_out_delay_ps ?? 0, 0) })));
        }
        const out = v.TrgOutMode, inNext = values[k + 1].AcqTriggerSource || "";
        if (setup !== "logic_or" && out && out !== "Disabled" && inNext.includes("TrgIn")) {
          svg.append(s("line", { x1: xTrg, y1: yA, x2: xTrg, y2: yB - 1, class: "sv-trg", "marker-end": "url(#sv-arrow-trg)" }),
                     s("text", { x: xTrg - 8, y: mid - 4, "text-anchor": "end", class: "sv-t sv-small" }, t("TRG-OUT ({m}) → TRG-IN", { m: out })));
        }
      }
    });
    // the logic unit of setup "OR in a logic unit": every TRG-OUT in, its OR back into every TRG-IN
    if (setup === "logic_or" && boards.length) {
      const yU = y0(boards.length - 1) + BOX_H + 24;
      svg.append(s("rect", { x: boxX + boxW / 2 - 110, y: yU, width: 220, height: 28, rx: 6, class: "sv-ext" }),
                 s("text", { x: boxX + boxW / 2, y: yU + 18, "text-anchor": "middle", class: "sv-t" }, t("logic unit: OR of every TRG-OUT → every TRG-IN")));
      boards.forEach((_, k) => {
        const yy = y0(k) + BOX_H / 2, xr = boxX + boxW;
        svg.append(s("line", { x1: xr, y1: yy, x2: W - 3, y2: yy, class: "sv-trg" }));
      });
      svg.append(s("line", { x1: W - 3, y1: y0(0) + BOX_H / 2, x2: W - 3, y2: yU + 14, class: "sv-trg" }),
                 s("line", { x1: W - 3, y1: yU + 14, x2: boxX + boxW / 2 + 110, y2: yU + 14, class: "sv-trg", "marker-end": "url(#sv-arrow-trg)" }));
    }
    clear(host, svg);
    // the sync check, in words
    const lines = [];
    if (setup === "periodic") lines.push(cfg.trigger.periodic_source === "test_pulse" ? t("Each board triggers on its own test pulse.") : t("Software triggers from the DAQ."));
    if (match && match.applies) {
      const miss = Object.entries(match.missing || {}).filter(([, n]) => n).map(([n, k]) => `${n} ${fmtInt(k)}`).join(", ");
      lines.push(t("Sync check: {n} triggers on every board within {w} ns · {o} out of sync · missing: {m}",
                   { n: fmtInt(match.built), w: fmtInt(match.window_ns), o: fmtInt(match.out_of_sync), m: miss || t("none") }));
    } else if (match) lines.push(t("Sync check: not for this setup ({why})", { why: t(match.why || "") }));
    clear(foot, lines.map((x) => h("div", x)));
  }

  return { el, render };
}

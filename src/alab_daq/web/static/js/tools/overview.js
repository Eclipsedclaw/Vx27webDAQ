// Tool: one number per channel for all boards, as a board × channel heat map, live (from the newest event of each
// board) or for a whole .raw or ROOT file (from up to 200 events spread over it): the baseline (mean of the first
// 16 samples), the noise (σ of those samples) or the peak (the largest distance from the baseline). A scale legend
// says what the colors mean, every cell shows its value, and hovering a cell shows all three.
import { get } from "../api.js";
import { T, t } from "../i18n.js";
import { basename, chooseFile, clear, fail, fmtInt, fmtNum, h, hideTip, icon, segmented, setText, showTip, store } from "../ui.js";

const QUANTITIES = [["baseline", T("baseline (ADC)")], ["noise", T("noise σ (ADC)")], ["peak", T("peak against the baseline (ADC)")]];
const SEQUENTIAL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"];

function ramp(colors, f) {
  f = Math.min(Math.max(f, 0), 1) * (colors.length - 1);
  const i = Math.min(Math.floor(f), colors.length - 2), k = f - i;
  const a = parseInt(colors[i].slice(1), 16), b = parseInt(colors[i + 1].slice(1), 16);
  const mix = (s) => Math.round(((a >> s) & 255) + (((b >> s) & 255) - ((a >> s) & 255)) * k);
  return [mix(16), mix(8), mix(0)];
}

function inkOn([r, g, b]) {
  const lin = [r, g, b].map((v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  const L = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  return 1.05 / (L + 0.05) > (L + 0.05) / 0.0534 ? "#ffffff" : "#0b0b0b";
}

export default {
  id: "overview",
  title: T("Overview of all channels"),
  icon: "grid",
  summary: T("baseline, noise or peak of every channel of every board, as a heat map: live or for a file"),
  open: true,
  make(body, ctx) {
    const app = ctx.app;
    const sub = h("div.hint");
    let source = store("overview.source") === "file" && store("overview.file") ? "file" : "live";
    let file = store("overview.file") || "", data = null;
    let quantity = store("overview.quantity") || "baseline";
    if (!QUANTITIES.some((q) => q[0] === quantity)) quantity = "baseline";
    const sourceSeg = segmented([["live", t("Live")], ["file", t("File")]], source, (v) => setSource(v), { label: t("Source") });
    const fileName = h("span.chip");
    const openBtn = h("button.btn.btn-sm", { type: "button", onclick: pickFile }, icon("folder"), t("Open file…"));
    const qSel = h("select.input", { "aria-label": t("Show"), onchange: () => { quantity = qSel.value; store("overview.quantity", quantity); render(); } },
                   QUANTITIES.map(([k, title]) => h("option", { value: k, selected: k === quantity }, t(title))));
    const scale = h("div.scale");
    const table = h("table.heat.wide");
    body.append(h("div.row", h("span.label", t("Source")), sourceSeg, fileName, openBtn, h("span.sep"),
                  h("span.label", t("Show")), qSel, h("span.spacer")),
                sub, h("div.row", h("b.small", t("Boards and channels")), h("span.spacer"), scale), h("div.table-wrap", table));

    const COLS = 32;          // channels per row: a 64-channel board takes two rows

    function rowsOf(boards) {
      const out = [];
      for (const b of boards) {
        const n = b.channels || 0;
        for (let first = 0; first < Math.max(n, 1); first += COLS) out.push({ b, first, last: Math.min(n, first + COLS) - 1 });
      }
      return out;
    }

    function range(b) {         // each board its own color range: their ADCs differ (14 or 16 bit)
      const vals = ((b.summary && b.summary[quantity]) || []).filter((v) => v !== null && v !== undefined)
        .map((v) => (quantity === "peak" ? Math.abs(v) : v));
      return vals.length ? [Math.min(...vals), Math.max(...vals)] : [0, 0];
    }

    function render() {
      const boards = data ? data.boards : [];
      const head = h("tr", h("th.row-head", ""), Array.from({ length: COLS }, (_, c) => h("th", String(c))));
      const ranges = new Map(boards.map((b) => [b, range(b)]));
      const rows = rowsOf(boards).map(({ b, first, last }) => h("tr",
        h("th.row-head", { title: b.model || "" }, first === 0 ? `${b.name} · ${b.model || ""}` : "", h("span.hint", ` ${first}–${last}`)),
        Array.from({ length: COLS }, (_, k) => cell(b, first + k, ...ranges.get(b)))));
      clear(table, h("thead", head), h("tbody", rows.length ? rows : h("tr", h("td.muted", { colspan: String(COLS + 1) }, t("No data yet.")))));
      const bar = h("span.scale-bar");
      bar.style.background = `linear-gradient(90deg, ${SEQUENTIAL.join(", ")})`;
      clear(scale, h("span", t("lowest of the board")), bar, h("span", t("highest of the board")),
            quantity === "peak" ? h("span", t("(colored by size)")) : null);
      sourceSeg.set(source);
      fileName.classList.toggle("hidden", source !== "file" || !file);
      setText(fileName, basename(file));
      fileName.title = file;
      let text = source === "file" ? t("File {path}: from {n} of its {m} events", { path: basename(file), n: fmtInt(data && data.boards[0] && data.boards[0].summary ? data.boards[0].summary.events : 0), m: fmtInt(data ? data.events : 0) })
                                   : t("Live: from the newest event of each board (refreshed every 2 s while this tool is open)");
      if (source === "file" && data && data.settings && data.settings.note) text += " · " + t("note: {note}", { note: data.settings.note.replace(/\s*\n\s*/g, " · ") });
      setText(sub, text);
    }

    function fmt(v) {             // compact: no thousands separators, so 32 columns fit
      if (v === null || v === undefined) return "—";
      return quantity === "noise" ? v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2) : v.toFixed(Math.abs(v) >= 100 ? 0 : 1);
    }

    function cell(b, c, lo, hi) {
      const td = h("td");
      if (c >= (b.channels || 0)) { td.className = "off"; return td; }
      const s = b.summary;
      const v = s ? s[quantity][c] : null;
      if (v === null || v === undefined) {
        td.className = "off";
        td.textContent = s ? t("off") : "—";
      } else {
        td.textContent = fmt(v);
        const x = quantity === "peak" ? Math.abs(v) : v;
        const rgb = ramp(SEQUENTIAL, hi > lo ? (x - lo) / (hi - lo) : 0.5);
        td.style.background = `rgb(${rgb.join(",")})`;
        td.style.color = inkOn(rgb);
      }
      td.tabIndex = 0;
      const tip = () => h("div", h("div.tv", `${b.name} ch${c}`),
        s ? QUANTITIES.map(([k, title]) => h("div.tr", h("span", t(title)), h("b", s[k][c] === null || s[k][c] === undefined ? "—" : fmtNum(s[k][c], k === "noise" ? 2 : 1))))
          : h("div.text2", t("No data yet.")));
      td.addEventListener("pointermove", (e) => showTip(e, tip()));
      td.addEventListener("pointerleave", hideTip);
      td.addEventListener("focus", () => { const r = td.getBoundingClientRect(); showTip({ clientX: r.right, clientY: r.top }, tip()); });
      td.addEventListener("blur", hideTip);
      td.setAttribute("aria-label", `${b.name} ch${c}: ${td.textContent}`);
      return td;
    }

    async function loadLive() {
      try { data = await get("/api/live/overview"); } catch (err) { data = null; }
      if (source === "live") render();
    }

    async function setSource(v) {
      if (v === "file" && !file) { await pickFile(); return; }
      source = v;
      store("overview.source", v);
      if (v === "file") await openFile(file); else loadLive();
    }

    async function pickFile() {
      const path = await chooseFile({ title: t("Open a .raw or ROOT file") });
      if (path) await openFile(path); else render();
    }

    async function openFile(path) {
      setText(sub, t("Reading…"));
      try {
        data = await get("/api/files/overview", { path });
        file = path;
        source = "file";
        store("overview.file", path);
        store("overview.source", "file");
      } catch (err) {
        fail(err);
        source = "live";
        store("overview.source", "live");
        loadLive();
        return;
      }
      render();
    }

    ctx.on("overview-file", (path) => openFile(path));
    if (source === "file") openFile(file); else loadLive();
    const timer = setInterval(() => { if (source === "live") loadLive(); }, 2000);
    return {
      update(type) { if (type === "config" && source === "live") loadLive(); },
      unmount() { clearInterval(timer); hideTip(); },
    };
  },
};

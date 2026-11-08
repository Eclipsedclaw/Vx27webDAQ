// Building blocks of the page: elements, icons, numbers, toasts, dialogs, dividers, the file chooser.
import { get } from "./api.js";
import { t } from "./i18n.js";

// ---- elements ----
// h("div.card.fill", {onclick, title, ...attributes}, child, ...): text children become text nodes (never HTML).
export function h(spec, props, ...children) {
  if (props === null || typeof props !== "object" || props instanceof Node || Array.isArray(props)) {
    if (props !== undefined && props !== null) children.unshift(props);
    props = {};
  }
  const [tag, ...classes] = spec.split(".");
  const el = document.createElementNS(tag === "svg" || tag === "path" ? "http://www.w3.org/2000/svg" : "http://www.w3.org/1999/xhtml",
                                      tag || "div");
  if (classes.length) el.setAttribute("class", classes.join(" "));
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.setAttribute("class", [el.getAttribute("class"), v].filter(Boolean).join(" "));
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "value" && "value" in el) el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = Boolean(v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

// ---- icons (24 x 24, drawn with strokes) ----
const PATHS = {
  logo: "M3 13h3l2-6 3.5 12 3-9 1.5 4H21",
  run: "M8 5v14l11-7z", stop: "M7 7h10v10H7z",
  kill: "M8.5 3h7L21 8.5v7L15.5 21h-7L3 15.5v-7zM9 9l6 6M15 9l-6 6",
  link: "M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  file: "M7 3h7l5 5v13H7zM14 3v5h5",
  sun: "M12 7a5 5 0 1 0 0 10a5 5 0 0 0 0-10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  user: "M12 12a4 4 0 1 0 0-8a4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  users: "M9 11a4 4 0 1 0 0-8a4 4 0 0 0 0 8zM2 21a7 7 0 0 1 14 0M16 3.5a4 4 0 0 1 0 7.5M18 14a7 7 0 0 1 4 7",
  down: "M6 9l6 6 6-6", up: "M6 15l6-6 6 6", left: "M15 6l-6 6 6 6", right: "M9 6l6 6-6 6",
  expand: "M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7", shrink: "M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7",
  download: "M12 3v12M7 10l5 5 5-5M5 21h14", check: "M5 12l5 5L20 7",
  alert: "M12 3l10 18H2zM12 10v4M12 18h.01", info: "M12 21a9 9 0 1 0 0-18a9 9 0 0 0 0 18zM12 11v6M12 7h.01",
  gear: "M12 15a3 3 0 1 0 0-6a3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  reset: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5", plus: "M12 5v14M5 12h14",
  trash: "M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3",
  copy: "M8 8h12v12H8zM16 8V4H4v12h4", pause: "M8 5v14M16 5v14",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4", key: "M14 10a4 4 0 1 0-4 4l2 2h2v2h2v2h3v-3l-5-5",
  chart: "M4 20V4M4 20h16M8 16l4-5 3 3 5-7", grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
  tool: "M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6a3 3 0 0 0 0 6z",
  wave: "M2 12c2-6 4-6 6 0s4 6 6 0 4-6 6 0",
  vdash: "M12 2v4M12 10v4M12 18v4", hdash: "M2 12h4M10 12h4M18 12h4", ruler: "M3 17L17 3l4 4L7 21zM7 13l2 2M10 10l2 2M13 7l2 2",
  zoom: "M11 18a7 7 0 1 0 0-14a7 7 0 0 0 0 14zM21 21l-5-5M8 11h6",
  list: "M9 6h12M9 12h12M9 18h12M4 6h.01M4 12h.01M4 18h.01",
  board: "M7 2h10v20H7zM10 6h4M10 10h4M10 14h4M10 18h2",
  send: "M22 2L11 13M22 2l-7 20-4-9-9-4z",
};

export function icon(name, cls) {
  const svg = h("svg", { viewBox: "0 0 24 24", width: "16", height: "16", fill: "none", stroke: "currentColor", "stroke-width": "2",
                         "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: cls });
  svg.appendChild(h("path", { d: PATHS[name] || PATHS.info }));
  return svg;
}

// ---- numbers and times ----
export function fmtInt(n) { return n === null || n === undefined ? "—" : Math.round(n).toLocaleString("en-US"); }
export function fmtNum(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export function fmtCompact(n) {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return (n / 1e3).toFixed(1) + "K";
  return fmtInt(n);
}
export function fmtBytes(b) {
  if (b === null || b === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = b;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}
export function fmtDuration(s) {
  if (s === null || s === undefined) return "—";
  s = Math.max(0, Math.round(s));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
}
export function fmtClock(epoch) {
  if (!epoch) return "—";
  const d = new Date(epoch * 1000);
  return d.toLocaleString("sv-SE", { hour12: false }).slice(0, 19);
}
export function basename(p) { return (p || "").split("/").filter(Boolean).pop() || p || ""; }

// ---- small controls ----
export function segmented(options, value, onChange, { disabled = false, label } = {}) {
  const box = h("div.seg", { role: "radiogroup", "aria-label": label });
  const buttons = options.map(([v, text]) => {
    const b = h("button", { type: "button", role: "radio", disabled, onclick: () => { set(v); onChange(v); } }, text);
    b.dataset.value = String(v);
    return b;
  });
  append(box, buttons);
  function set(v) {
    for (const b of buttons) {
      const on = b.dataset.value === String(v);
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
    }
  }
  set(value);
  box.set = set;
  box.setDisabled = (d) => buttons.forEach((b) => { b.disabled = d; });
  return box;
}

export function toggle(text, checked, onChange, { disabled = false } = {}) {
  const input = h("input", { type: "checkbox", checked, disabled, onchange: () => onChange(input.checked) });
  const el = h("label.switch", input, h("span.track"), h("span", text));
  el.input = input;
  return el;
}

export function chip(text, kind = "", iconName) {
  return h(`span.chip${kind ? "." + kind : ""}`, iconName ? icon(iconName) : null, text);
}

export function roleChip(role) {
  return chip(t(role || "viewer"), `role-${role || "viewer"}`);
}

// ---- toasts ----
export function toast(message, kind = "info") {
  const box = document.getElementById("toasts");
  const el = h(`div.toast.${kind}`, icon(kind === "error" ? "alert" : kind === "ok" ? "check" : "info"), h("div", message));
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 8000 : 4000);
}

export function fail(err) { toast(err && err.message ? err.message : String(err), "error"); }

// ---- dialogs ----
// dialog({title, body, actions: [{label, kind, value, primary}]}) -> Promise of the chosen value (null: closed)
export function dialog({ title, body, actions = [{ label: t("Close"), value: null }], wide = false, onOpen }) {
  return new Promise((resolve) => {
    const last = document.activeElement;
    const foot = h("div.modal-foot");
    const modal = h(`div.modal${wide ? ".wide" : ""}`, { role: "dialog", "aria-modal": "true", "aria-label": title },
                    h("div.modal-head", h("h2", title)), h("div.modal-body", body), foot);
    const backdrop = h("div.backdrop", { onmousedown: (e) => { if (e.target === backdrop) done(null); } }, modal);
    function done(value) {
      backdrop.remove();
      document.removeEventListener("keydown", onKey, true);
      if (last && last.focus) last.focus();
      resolve(value);
    }
    function onKey(e) {
      if (e.key === "Escape") { e.stopPropagation(); done(null); }
    }
    for (const a of actions) {
      foot.appendChild(h(`button.btn${a.kind ? ".btn-" + a.kind : ""}`, {
        type: "button", onclick: async () => {
          if (a.run) {
            try { const v = await a.run(); if (v !== undefined) done(v); } catch (err) { fail(err); }
          } else done(a.value);
        },
      }, a.icon ? icon(a.icon) : null, a.label));
    }
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(backdrop);
    const first = modal.querySelector("input, select, textarea") || foot.querySelector(".btn-primary, .btn-danger, .btn");
    if (first) first.focus();
    if (onOpen) onOpen(modal, done);
  });
}

export function confirmDialog(title, lines, okLabel, kind = "primary") {
  const body = h("div.stack", lines.filter(Boolean).map((l) => (l instanceof Node ? l : h("p", { style: "margin:0" }, l))));
  return dialog({ title, body, actions: [{ label: t("Cancel"), value: false }, { label: okLabel || t("OK"), kind, value: true }] });
}

// ---- a divider between two sections, kept for this browser ----
export function splitter(a, b, { key, vertical = true, initial = 0.5, min = 80 } = {}) {
  const box = h(`div.split.${vertical ? "v" : "h"}`);
  const paneA = h("div.pane", a), paneB = h("div.pane", b);
  const handle = h("div.split-handle", { role: "separator", tabindex: "0", "aria-orientation": vertical ? "horizontal" : "vertical",
                                          title: t("Drag to resize; double-click for the default") });
  append(box, [paneA, handle, paneB]);
  let ratio = Number(store(`split.${key}`)) || initial;
  const apply = () => {
    paneA.style.flex = `${ratio} 1 0px`;
    paneB.style.flex = `${1 - ratio} 1 0px`;
  };
  apply();
  const size = () => (vertical ? box.clientHeight : box.clientWidth) - 14;
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    const start = vertical ? e.clientY : e.clientX, startRatio = ratio, total = size();
    const move = (ev) => {
      const d = (vertical ? ev.clientY : ev.clientX) - start;
      ratio = Math.min(1 - min / total, Math.max(min / total, startRatio + d / total));
      apply();
      window.dispatchEvent(new Event("resize"));
    };
    const up = () => {
      handle.classList.remove("dragging");
      handle.removeEventListener("pointermove", move);
      store(`split.${key}`, ratio.toFixed(3));
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up, { once: true });
  });
  handle.addEventListener("dblclick", () => { ratio = initial; apply(); store(`split.${key}`, null); window.dispatchEvent(new Event("resize")); });
  handle.addEventListener("keydown", (e) => {
    const step = { ArrowUp: -0.03, ArrowLeft: -0.03, ArrowDown: 0.03, ArrowRight: 0.03 }[e.key];
    if (step) { ratio = Math.min(0.92, Math.max(0.08, ratio + step)); apply(); store(`split.${key}`, ratio.toFixed(3)); e.preventDefault(); }
  });
  box.showSecond = (on) => {            // the second section on or off (off: the first takes all the room)
    handle.classList.toggle("hidden", !on);
    paneB.classList.toggle("hidden", !on);
    if (on) apply(); else paneA.style.flex = "1 1 0px";
  };
  return box;
}

// ---- per-browser memory (may be off: then nothing is remembered) ----
export function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(`alab.${key}`);
    if (value === null) localStorage.removeItem(`alab.${key}`);
    else localStorage.setItem(`alab.${key}`, String(value));
  } catch (e) { /* private window or storage off */ }
  return undefined;
}

export function clearLayout() {
  try {
    for (const k of Object.keys(localStorage)) if (k.startsWith("alab.split.")) localStorage.removeItem(k);
  } catch (e) { /* nothing to clear */ }
}

// ---- a tooltip that follows the pointer ----
let tip = null;
export function showTip(e, content) {
  if (!tip) { tip = h("div.tooltip", { role: "tooltip" }); document.body.appendChild(tip); }
  clear(tip, content);
  tip.style.display = "block";
  const pad = 14, w = tip.offsetWidth, hgt = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
  if (y + hgt > window.innerHeight - 8) y = e.clientY - hgt - pad;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
export function hideTip() { if (tip) tip.style.display = "none"; }

// ---- choosing a data file or folder on the server ----
// where every chooser opens unless told otherwise: the lab's data folder (/NAS/ALAB_DAQ_DATA, from the server)
let defaultFolder = "";
export function setDefaultFolder(path) { defaultFolder = path || ""; }

export async function chooseFile({ title, want = "file", kinds = ["root", "raw"], start } = {}) {
  let folder = start || defaultFolder || store("files.folder") || "";
  let chosen = null;
  const crumbs = h("div.crumbs"), list = h("ul.file-list", { style: "max-height: 52vh; min-height: 200px" });
  const roots = h("div.row");
  const filter = segmented([["all", t("ROOT and .raw")], ["root", "ROOT"], ["raw", ".raw"]], store("files.kind") || "all",
                           (v) => { store("files.kind", v); load(folder); });
  const body = h("div.stack", h("div.row", roots, h("span.spacer"), want === "file" ? filter : null), crumbs, list);
  async function load(dir, quiet = false) {
    let data;
    try { data = await get("/api/files/list", { dir }); } catch (err) { if (quiet) return load(""); fail(err); return; }
    folder = data.folder;
    store("files.folder", folder);
    clear(roots, data.roots.map((r) => h("button.btn.btn-sm", { type: "button", title: r.path, onclick: () => load(r.path) },
                                         icon("folder"), r.label === "data" ? `${t(r.label)} · ${basename(r.path)}` : t(r.label))));
    const parts = folder.split("/").filter(Boolean);
    clear(crumbs, parts.map((p, i) => [h("button", { type: "button", onclick: () => load("/" + parts.slice(0, i + 1).join("/")) }, p),
                                       i < parts.length - 1 ? "/" : ""]));
    const kind = filter.querySelector(".on")?.dataset.value || "all";
    const rows = [];
    if (data.parent) rows.push(h("li", { tabindex: "0", onclick: () => load(data.parent) }, icon("up"), h("span.name", "..")));
    for (const e of data.entries) {
      if (e.kind === "folder") {
        rows.push(h("li", { tabindex: "0", onclick: () => load(`${folder}/${e.name}`) }, icon("folder"), h("span.name", e.name)));
      } else if (want === "file" && kinds.includes(e.kind) && (kind === "all" || kind === e.kind)) {
        const li = h("li", { tabindex: "0", onclick: () => select(li, `${folder}/${e.name}`),
                             ondblclick: () => finish(`${folder}/${e.name}`) },
                     icon("file"), h("span.name", e.name), h("span.meta", fmtBytes(e.size)), h("span.meta", fmtClock(e.mtime)));
        rows.push(li);
      }
    }
    if (!rows.length || (rows.length === 1 && data.parent)) rows.push(h("li.muted", t("Nothing here")));
    clear(list, rows);
  }
  let finish = () => {};
  function select(li, path) {
    list.querySelectorAll("li.on").forEach((x) => x.classList.remove("on"));
    li.classList.add("on");
    chosen = path;
  }
  const result = dialog({
    title: title || t("Open a data file"), body, wide: true,
    actions: [{ label: t("Cancel"), value: null },
              { label: want === "file" ? t("Open") : t("Choose this folder"), kind: "primary",
                run: () => (want === "file" ? (chosen || undefined) : folder) }],
    onOpen: (_m, done) => { finish = done; },
  });
  load(folder, true);                      // a folder that is gone (or not a data folder): the server's default
  return result;
}

// ---- choosing any file or folder on the server (operators: the configuration, programs, folders) ----
// want: "file" (one that exists), "folder", or "save" (a file name in a folder: new, or one that is replaced
// after a second Save). filters: [[label, (entry) => bool], ...] for the files shown, the first at the start.
// start: where to begin (a file's path opens its folder with the file chosen); name: the name a save starts
// with. The path can also be typed or pasted; Enter goes there. Resolves to the path, or null.
export const CONF_FILES = [[".json", (e) => e.name.toLowerCase().endsWith(".json")], ["all", () => true]];
export function choosePath({ title, want = "file", filters = null, start = "", name = "", okLabel } = {}) {
  let folder = "", entries = [], more = 0, parent = null, hidden = store("browse.hidden") === "1", filter = 0;
  let replace = null;                       // save: the existing file that a second Save replaces
  let finish = () => {};
  const placesRow = h("div.row.wrap"), crumbs = h("div.crumbs");
  const list = h("ul.file-list", { style: "max-height: 44vh; min-height: 200px" });
  const pathInput = h("input.input.mono", { style: "width:100%", spellcheck: "false", autocomplete: "off",
                                            "aria-label": want === "folder" ? t("Folder") : t("Path on the server"),
                                            onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); accept(); } },
                                            oninput: () => { replace = null; showState(); } });
  const state = h("div.hint");
  const hiddenBox = toggle(t("hidden files"), hidden, (on) => { hidden = on; store("browse.hidden", on ? "1" : null); load(folder); });
  const label = (l) => (l === ".json" ? t(".json files") : l === "all" ? t("all files") : t(l));
  const filterSeg = filters && filters.length > 1
    ? segmented(filters.map(([l], i) => [String(i), label(l)]), "0", (v) => { filter = Number(v); render(); }, { label: t("Show") }) : null;
  const body = h("div.stack", h("div.row", placesRow, h("span.spacer"), filterSeg, hiddenBox), crumbs, list,
                 h("div.field", h("label", want === "folder" ? t("Folder") : t("Path on the server")), pathInput), state);
  const join = (dir, n) => (dir === "/" ? `/${n}` : `${dir}/${n}`);

  async function load(dir, { quiet = false } = {}) {
    let data;
    try { data = await get("/api/browse", { dir, hidden: hidden ? "1" : undefined }); } catch (err) { if (!quiet) fail(err); return null; }
    folder = data.folder;
    entries = data.entries;
    more = data.more;
    parent = data.parent;
    clear(placesRow, data.places.map((p) => h("button.btn.btn-sm", { type: "button", title: p.path, onclick: () => load(p.path) },
                                             icon(p.label === "computer" ? "grid" : "folder"),
                                             p.label === "data" ? `${t("data")} · ${basename(p.path)}` : t(p.label))));
    const parts = folder.split("/").filter(Boolean);
    clear(crumbs, h("button", { type: "button", onclick: () => load("/") }, "/"),
          parts.map((p, i) => [h("button", { type: "button", onclick: () => load("/" + parts.slice(0, i + 1).join("/")) }, p),
                               i < parts.length - 1 ? "/" : ""]));
    if (want === "folder") pathInput.value = folder;
    else if (data.file) pathInput.value = join(folder, data.file);
    else if (want === "save") pathInput.value = join(folder, basename(pathInput.value) || name || "");
    else pathInput.value = folder + (folder.endsWith("/") ? "" : "/");
    replace = null;
    render(data.file);
    return data;
  }

  function render(chosen) {
    const test = filters ? filters[filter][1] : () => true;
    const rows = [];
    if (parent) rows.push(h("li", { tabindex: "0", onclick: () => load(parent), onkeydown: enter(() => load(parent)) }, icon("up"), h("span.name", "..")));
    for (const e of entries) {
      const path = join(folder, e.name);
      if (e.kind === "folder") {
        rows.push(h("li", { tabindex: "0", onclick: () => load(path), onkeydown: enter(() => load(path)) }, icon("folder"), h("span.name", e.name)));
      } else if (want !== "folder" && test(e)) {
        const li = h("li", { tabindex: "0", onclick: () => pick(li, path), ondblclick: () => { pick(li, path); accept(); },
                             onkeydown: enter(() => { pick(li, path); accept(); }) },
                     icon("file"), h("span.name", e.name), h("span.meta", fmtBytes(e.size)), h("span.meta", fmtClock(e.mtime)));
        if (e.name === chosen) li.classList.add("on");
        rows.push(li);
      }
    }
    if (!rows.length || (rows.length === 1 && parent)) rows.push(h("li.muted", t("Nothing here")));
    if (more) rows.push(h("li.muted", t("{n} more not shown", { n: more })));
    clear(list, rows);
    showState();
  }

  function enter(fn) { return (e) => { if (e.key === "Enter") { e.preventDefault(); fn(); } }; }

  function pick(li, path) {
    list.querySelectorAll("li.on").forEach((x) => x.classList.remove("on"));
    li.classList.add("on");
    pathInput.value = path;
    replace = null;
    showState();
  }

  function showState() {                    // save: does the name exist in the folder shown?
    let text = "";
    if (want === "save") {
      const p = pathInput.value.trim(), dir = p.slice(0, p.lastIndexOf("/")) || "/", n = basename(p);
      const there = (dir === folder) && entries.find((e) => e.name === n);
      if (replace && replace === p) text = t("{name} exists: Save again to replace it", { name: n });
      else if (there && there.kind === "file") text = t("{name} exists and will be replaced", { name: n });
      else if (n && !p.endsWith("/")) text = t("a new file in {folder}", { folder: dir });
    }
    setText(state, text);
    state.classList.toggle("warn-text", Boolean(replace));
  }

  async function accept() {
    const p = pathInput.value.trim();
    if (!p) return;
    if (want === "folder") {
      const data = p === folder ? { folder, file: null } : await load(p);
      if (data && !data.file) finish(data.folder);
      else if (data) fail(new Error(t("{path} is a file, not a folder", { path: p })));
      return;
    }
    if (want === "file") {
      const data = await load(p);
      if (data && data.file) finish(join(data.folder, data.file));          // a folder: it is shown now, choose in it
      return;
    }
    // save: into an existing folder; a name that exists is replaced only after a second Save
    if (p.endsWith("/")) { await load(p); return; }
    const dir = p.slice(0, p.lastIndexOf("/")) || "/", n = basename(p);
    if (dir !== folder && !(await load(dir))) return;                        // its folder must exist (shown now)
    pathInput.value = join(folder, n);
    const there = entries.find((e) => e.name === n);
    if (there && there.kind === "folder") { await load(join(folder, n)); return; }
    if (there && replace !== join(folder, n)) { replace = join(folder, n); showState(); return; }
    finish(join(folder, n));
  }

  const result = dialog({
    title, body, wide: true,
    actions: [{ label: t("Cancel"), value: null },
              { label: okLabel || (want === "folder" ? t("Choose this folder") : want === "save" ? t("Save") : t("Open")), kind: "primary",
                run: async () => { await accept(); return undefined; } }],
    onOpen: (_m, done) => { finish = done; pathInput.focus(); },
  });
  if (want === "save" && name) pathInput.value = name;
  load(start || defaultFolder || "", { quiet: true }).then((d) => { if (!d) load(""); });      // a start that is gone: the default
  return result;
}

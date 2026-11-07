// The page: one connection to the server's live events, the state they keep up to date, the top bar,
// signing in and out, and the pages (#run, #config, ...).
import { get, post } from "./api.js";
import { loadLanguage, T, t } from "./i18n.js";
import { append, chip, clear, dialog, fail, fmtDuration, h, icon, roleChip, store, toast } from "./ui.js";
import { configPage } from "./pages/config.js";
import { runPage } from "./pages/run.js";
import { settingsPage } from "./pages/settings.js";
import { toolsPage } from "./pages/tools.js";
import { usersPage } from "./pages/users.js";

const PAGES = [
  { id: "run", title: T("Run"), icon: "run", make: runPage },
  { id: "config", title: T("Configuration"), icon: "sliders", make: configPage },
  { id: "tools", title: T("Tools"), icon: "tool", make: toolsPage },
  { id: "settings", title: T("Settings"), icon: "gear", make: settingsPage },
  { id: "users", title: T("Users"), icon: "users", make: usersPage, admin: true },
];
export const PHASES = { starting: T("Starting"), connecting: T("Connecting to the boards"),
                        configuring: T("Configuring the boards"), arming: T("Arming the boards"), running: T("Running"),
                        stopping: T("Stopping"), finished: T("Finished"), failed: T("Failed") };

// ---- the shared state ----
export const app = {
  me: null, state: null, connected: false, page: null, pageId: null,
  can(what) { return Boolean(this.me && this.me.can && this.me.can[what]); },
  phaseText(run) {
    if (!run) return t("Idle");
    if (run.active) return t(PHASES[run.phase] || run.phase);
    return t("Idle");
  },
};

function applyMessage(msg) {
  const s = app.state;
  switch (msg.type) {
    case "hello":
      app.me = msg.me;
      app.state = msg.state;
      return;
    case "run": {
      const old = s.run;
      const series = old && old.active && msg.run.active && old.run_dir === msg.run.run_dir ? old.mbps_series || [] : [];
      const p = msg.run.mbps_point;
      if (msg.run.active) {
        if (p && (!series.length || p[0] > series[series.length - 1][0])) series.push(p);
        msg.run.mbps_series = series;
      }
      s.run = msg.run;
      return;
    }
    case "log":
      s.log.push(...msg.lines);
      if (s.log.length > 20000) s.log.splice(0, s.log.length - 20000);
      return;
    case "log_reset": s.log = []; return;
    case "stats": s.stats = msg.stats; return;
    case "config": s.config = msg.config; return;
    case "settings": s.settings = msg.settings; return;
    case "roots": s.roots = msg.roots; return;
    case "boards": s.boards = msg.boards; return;
    case "root": s.root = msg.root; return;
    case "memory": s.memory = msg.memory; return;
    default:
  }
}

// ---- the live connection ----
let socket = null, retry = 0, firstHello = true;

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events`);
  socket = ws;
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    applyMessage(msg);
    if (msg.type === "hello") {
      retry = 0;
      setConnected(true);
      if (firstHello) { firstHello = false; start(); } else { renderTop(); route(true); }
      return;
    }
    if (msg.type === "root_done") notifyRoot(msg.result);
    updateTop(msg.type);
    if (app.page && app.page.update) app.page.update(msg.type, msg);
  };
  ws.onclose = () => {
    if (socket !== ws) return;
    setConnected(false);
    const wait = Math.min(15000, 500 * 2 ** retry++);
    setTimeout(connect, wait);
  };
}

function setConnected(on) {
  app.connected = on;
  const dot = document.querySelector(".conn");
  if (dot) {
    dot.classList.toggle("live", on);
    dot.classList.toggle("lost", !on);
    dot.title = on ? t("Live: connected to the server") : t("Connection lost: trying again");
  }
}

function notifyRoot(r) {
  if (!r) return;
  const name = (r.root || r.raw).split("/").pop();
  if (r.status === "ok") toast(t("ROOT file ready: {name}", { name }), "ok");
  else if (r.status !== "cancelled") toast(t("ROOT conversion of {name}: {status}", { name, status: t(r.status) }), "error");
}

// ---- the top bar ----
function renderTop() {
  const top = document.getElementById("topbar");
  const version = app.state.version;
  const nav = h("nav.nav", { "aria-label": t("Pages") });
  for (const p of PAGES) {
    if (p.admin && !app.can("admin")) continue;
    nav.appendChild(h("a", { href: `#${p.id}`, dataset: { page: p.id }, class: app.pageId === p.id ? "active" : "",
                             title: t(p.title), "aria-label": t(p.title) }, icon(p.icon), h("span.lbl", t(p.title))));
  }
  const lang = h("select.input", { "aria-label": t("Language"), style: "width:auto;height:30px",
                                    onchange: async () => { store("lang", lang.value); location.reload(); } },
                 h("option", { value: "en", selected: (store("lang") || "en") === "en" }, "English"),
                 h("option", { value: "zh", selected: store("lang") === "zh" }, "中文"));
  const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
               (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
  const themeBtn = h("button.btn.btn-ghost.btn-icon", { type: "button", title: dark ? t("Light theme") : t("Dark theme"),
                                                         "aria-label": dark ? t("Light theme") : t("Dark theme"),
                                                         onclick: () => setTheme(dark ? "light" : "dark") },
                     icon(dark ? "sun" : "moon"));
  clear(top,
        h("a.brand", { href: "#run" }, h("span.brand-mark", icon("logo")), h("span.brand-name", "ALAB DAQ"),
          h("span.brand-version", `v${version}`)),
        nav,
        h("div.topbar-right",
          h("a.status-pill", { href: "#run", id: "status-pill" }, h("span.conn"), h("span.txt", "")),
          h("span.mem", { id: "mem" }), lang, themeBtn, accountButton()));
  setConnected(app.connected);
  updateTop("run");
  updateTop("memory");
}

function updateTop(type) {
  if (type === "run" || type === "hello") {
    const pill = document.getElementById("status-pill");
    if (!pill) return;
    const r = app.state.run;
    let text = t("Idle"), cls = "";
    if (r && r.active) {
      text = `${app.phaseText(r)} · ${fmtDuration(r.t)}${r.duration ? ` / ${fmtDuration(r.duration)}` : ""}`;
      cls = r.phase === "running" ? "running" : "busy";
    } else if (r && r.last && r.last.phase === "failed") {
      text = t("Last run failed");
      cls = "failed";
    }
    pill.className = `status-pill ${cls}`;
    pill.querySelector(".txt").textContent = text;
    pill.title = text;
  }
  if (type === "memory" || type === "hello") {
    const el = document.getElementById("mem");
    const m = app.state.memory;
    if (el && m) {
      el.textContent = t("Memory {p}%", { p: Math.round(m.used_percent) });
      el.title = t("Memory of the server in use · {g} GiB free", { g: m.available_gib.toFixed(1) });
      el.classList.toggle("high", m.used_percent >= 85);
    }
  }
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  store("theme", theme);
  renderTop();
  if (app.page && app.page.restyle) app.page.restyle();
}

function accountButton() {
  const me = app.me;
  if (!me.user) {
    return h("button.btn.btn-sm", { type: "button", onclick: () => signIn() }, icon("user"), t("Sign in"));
  }
  const wrap = h("div.relative");
  const btn = h("button.btn.btn-sm", { type: "button", "aria-haspopup": "menu", onclick: (e) => { e.stopPropagation(); toggle(); } },
                icon("user"), h("span.acct-name", me.user), roleChip(me.role));
  let menu = null;
  function toggle() {
    if (menu) { menu.remove(); menu = null; return; }
    menu = h("div.menu", { role: "menu" },
             h("div.who", h("div", t("Signed in as {name}", { name: me.user })), h("div.row", { style: "margin-top:6px" }, roleChip(me.role))),
             h("button", { type: "button", role: "menuitem", onclick: () => { toggle(); changePassword(); } }, icon("key"), t("Change password")),
             h("button", { type: "button", role: "menuitem", onclick: () => signOut() }, icon("logout"), t("Sign out")));
    wrap.appendChild(menu);
    setTimeout(() => document.addEventListener("click", function off(e) {
      if (menu && !menu.contains(e.target)) { menu.remove(); menu = null; }
      document.removeEventListener("click", off);
    }), 0);
  }
  append(wrap, [btn]);
  return wrap;
}

export async function signIn(required = false) {
  const name = h("input.input", { autocomplete: "username", name: "username", "aria-label": t("User name") });
  const password = h("input.input", { type: "password", autocomplete: "current-password", name: "password", "aria-label": t("Password") });
  const body = h("form.stack", { onsubmit: (e) => e.preventDefault() },
                 h("div.field", h("label", t("User name")), name), h("div.field", h("label", t("Password")), password),
                 app.me && app.me.no_accounts ? h("div.notice.info", icon("info"),
                   t("No accounts yet: an admin makes the first one on the DAQ computer with  alab-daq-web user add NAME --role admin")) : null);
  password.addEventListener("keydown", (e) => { if (e.key === "Enter") submit.click(); });
  let submit;
  const done = dialog({
    title: t("Sign in to ALAB DAQ"), body,
    actions: [...(required ? [] : [{ label: t("Cancel"), value: null }]),
              { label: t("Sign in"), kind: "primary", icon: "user",
                run: async () => { await post("/api/login", { name: name.value, password: password.value }); return true; } }],
    onOpen: (m) => { submit = m.querySelector(".btn-primary"); },
  });
  if (await done) location.reload();
}

async function signOut() {
  try { await post("/api/logout"); } catch (err) { /* signed out anyway */ }
  location.reload();
}

async function changePassword() {
  const old = h("input.input", { type: "password", autocomplete: "current-password" });
  const neu = h("input.input", { type: "password", autocomplete: "new-password" });
  const again = h("input.input", { type: "password", autocomplete: "new-password" });
  const ok = await dialog({
    title: t("Change password"),
    body: h("form.stack", { onsubmit: (e) => e.preventDefault() }, h("div.field", h("label", t("Present password")), old),
            h("div.field", h("label", t("New password (at least 8 characters)")), neu),
            h("div.field", h("label", t("New password again")), again)),
    actions: [{ label: t("Cancel"), value: null },
              { label: t("Change password"), kind: "primary", run: async () => {
                if (neu.value !== again.value) throw new Error(t("The two new passwords differ"));
                await post("/api/password", { old: old.value, new: neu.value });
                return true;
              } }],
  });
  if (ok) toast(t("Password changed; other browsers were signed out"), "ok");
}

// ---- pages ----
const MOVED = { waveforms: "run", overview: "tools" };     // old names of parts of other pages

function route(force = false) {
  let id = (location.hash || "").slice(1) || store("page") || "run";
  if (id === "overview") store("tools.open.overview", "1");    // the Overview is the first tool now
  id = MOVED[id] || id;
  const def = PAGES.find((p) => p.id === id && (!p.admin || app.can("admin"))) || PAGES[0];
  id = def.id;
  if (location.hash && location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);   // e.g. the old #waveforms
  if (!force && app.pageId === id) return;
  if (app.page && app.page.unmount) app.page.unmount();
  app.pageId = id;
  store("page", id);
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === id));
  const main = document.getElementById("main");
  main.replaceChildren();
  document.title = `${t(def.title)} · ALAB DAQ`;
  try {
    app.page = def.make(main, app);
  } catch (err) {
    console.error(err);
    app.page = null;
    main.appendChild(h("div.notice.error", icon("alert"), String(err)));
  }
}

function start() {
  renderTop();
  route(true);
  window.addEventListener("hashchange", () => route());
}

async function boot() {
  await loadLanguage(store("lang") || "en");
  let me;
  try {
    me = await get("/api/me");
  } catch (err) {
    clear(document.getElementById("main"), h("div.notice.error", icon("alert"), err.message));
    return;
  }
  app.me = me;
  if (!me.role) {                 // no guests on this server: signing in comes first
    document.getElementById("topbar").appendChild(h("a.brand", h("span.brand-mark", icon("logo")),
                                                    h("span.brand-name", "ALAB DAQ")));
    clear(document.getElementById("main"),
          h("div.signin-page.card", h("div.card-head", h("h1.card-title", t("Sign in to ALAB DAQ"))),
            h("div.card-body.stack", h("p.text2", { style: "margin:0" }, t("This server lets only signed-in users in.")),
              h("button.btn.btn-primary", { type: "button", onclick: () => signIn(true) }, icon("user"), t("Sign in")))));
    signIn(true);
    return;
  }
  connect();
}

window.alab = app;     // for the console and the tests
window.alabSignIn = () => signIn();
boot();

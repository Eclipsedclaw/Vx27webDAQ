// The page: one connection to the server's live events, the state they keep up to date, the top bar,
// signing in and out, and the pages (#run, #config, ...).
import { get, post } from "./api.js";
import { LANGUAGES, loadLanguage, T, t } from "./i18n.js";
import { append, chip, clear, dialog, fail, fmtDuration, h, icon, roleChip, setDefaultFolder, store, toast } from "./ui.js";
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
  // the server's clock now (s): the time in its newest message plus the time since that message came, so that a
  // browser whose clock is off still counts the server's seconds (Kill 15 s after Stop, quiet boards)
  serverNow() { return Date.now() / 1000 + clockOffset; },
  // the edits a page still holds back (the Configuration page sends them 400 ms after the last key) go to the
  // server now; keepalive: the page is being closed, the request must outlive it
  flush(keepalive = false) {
    try { return Promise.resolve(this.page && this.page.flush ? this.page.flush(keepalive) : null); } catch (err) { return Promise.resolve(); }
  },
  // a reload (language, theme, signing in or out) after the page's edits have gone; waits at most 5 s for them
  async reload() {
    await Promise.race([this.flush().catch(() => {}), new Promise((ok) => setTimeout(ok, 5000))]);
    location.reload();
  },
};

let clockOffset = 0;            // the server's clock minus this browser's (s)
function stamp(obj) {           // a message's "now" (the server's clock when it was made) against this browser's clock
  if (obj && typeof obj.now === "number") clockOffset = obj.now - Date.now() / 1000;
}

// the MB/s plot of the Run page: at most about MBPS_POINTS points. The run going: past that many, every second
// point goes and from then on only every second point is kept (the step doubles), the newest one shown until the
// next is a step on; a finished run: thinned once
const MBPS_POINTS = 2000;
function thinned(series) {
  if (!series || series.length <= MBPS_POINTS) return series || [];
  const k = Math.ceil(series.length / MBPS_POINTS);
  return series.filter((_, i) => i % k === 0 || i === series.length - 1);
}

function keepMbps(run, old) {
  if (!run) return;
  if (!run.active) {
    if (run.last && run.last.mbps_series) run.last.mbps_series = thinned(run.last.mbps_series);
    return;
  }
  const same = old && old.active && old.run_dir === run.run_dir;
  let series = same && old.mbps_series ? old.mbps_series : (run.mbps_series || []).slice();
  let step = (same && old.mbps_step) || 1, version = (same && old.mbps_version) || 0, tail = Boolean(same && old.mbps_tail);
  const p = run.mbps_point, newest = series[series.length - 1];
  if (p && (!newest || p[0] > newest[0])) {                  // a new point (about one a second)
    if (tail) series.pop();                                   // the newest point so far was there to be shown only
    const kept = series[series.length - 1];
    tail = Boolean(kept) && p[0] - kept[0] < step - 0.5;      // less than a step on from the last one kept
    series.push(p);
    version += 1;
  }
  while (series.length > MBPS_POINTS) {
    const end = tail ? series.pop() : null;
    series = series.filter((_, i) => i % 2 === 0 || i === series.length - 1);
    if (end) series.push(end);
    step *= 2;
    version += 1;
  }
  // version: changes with every point, so the plot is drawn again only then
  Object.assign(run, { mbps_series: series, mbps_step: step, mbps_version: version, mbps_tail: tail });
}

function applyMessage(msg) {
  const s = app.state;
  switch (msg.type) {
    case "hello":
      app.me = msg.me;
      app.state = msg.state;
      stamp(msg.state.run);
      stamp(msg.state.sequence);
      keepMbps(msg.state.run, null);
      setDefaultFolder(msg.state.settings.default_folder);
      return;
    case "run": {
      stamp(msg.run);
      keepMbps(msg.run, s.run);
      s.run = msg.run;
      return;
    }
    case "log":
      s.log.push(...msg.lines);
      if (s.log.length > 20000) s.log.splice(0, s.log.length - 20000);
      return;
    case "log_reset": s.log = []; return;
    case "stats": s.stats = msg.stats; return;
    case "config":                     // never an older configuration than the one the page has (a late message)
      if (!s.config || !(msg.config.version < s.config.version)) s.config = msg.config;
      return;
    case "settings": s.settings = msg.settings; setDefaultFolder(msg.settings.default_folder); return;
    case "roots": s.roots = msg.roots; return;
    case "boards": s.boards = msg.boards; return;
    case "root": s.root = msg.root; return;
    case "memory": s.memory = msg.memory; return;
    case "sequence": stamp(msg.sequence); s.sequence = msg.sequence; return;
    default:
  }
}

// ---- the live connection ----
// The server says something at least every 5 s (the memory in use): after QUIET_MS of silence the connection is
// taken as dead (a laptop that slept, a stalled ssh tunnel never close it) and made again. Before connecting again
// the page asks who it is: a server that lets nobody in without signing in (after a restart, or a sign-out
// elsewhere: the server closes with code 4401) gets the sign-in instead of a retry every 15 s.
const QUIET_MS = 15000, SIGNED_OUT = 4401;
let socket = null, retry = 0, firstHello = true, heard = 0;

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events`);
  socket = ws;
  heard = Date.now();
  ws.onmessage = (ev) => {
    if (socket !== ws) return;
    heard = Date.now();
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
  ws.onclose = (ev) => {
    if (socket !== ws) return;
    socket = null;
    lost(ev.code);
  };
}

setInterval(() => {                        // silence: the connection is dead even if it was never closed
  if (!socket || Date.now() - heard < QUIET_MS) return;
  const ws = socket;
  socket = null;                           // its close, whenever it comes, is not waited for
  try { ws.close(); } catch (e) { /* closed already */ }
  lost(0);
}, 5000);

const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));
const backoff = () => Math.min(15000, 500 * 2 ** retry++);

async function lost(code) {
  setConnected(false);
  if (code !== SIGNED_OUT) await sleep(backoff());
  let me = null;
  try { me = await get("/api/me", undefined, { timeout: 10000 }); } catch (err) { me = null; }     // null: no answer yet
  if (me && !me.role) { app.me = me; showSignIn(); return; }       // signed out, and no guests here: sign in, no retry
  if (code === SIGNED_OUT) await sleep(backoff());
  connect();
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
  else if (r.status === "skipped") toast(`${t("ROOT conversion of {name}: {status}", { name, status: t(r.status) })}${r.message ? ` · ${r.message}` : ""}`);
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
  const lang = h("select.input.lang-pick", { "aria-label": t("Language"), style: "width:auto;height:30px",
                                              onchange: () => { store("lang", lang.value); app.reload(); } },
                 LANGUAGES.map(([v, name]) => h("option", { value: v, selected: (store("lang") || "en") === v }, name)));
  const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
               (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
  const themeBtn = h("button.btn.btn-ghost.btn-icon", { type: "button", title: dark ? t("Light theme") : t("Dark theme"),
                                                         "aria-label": dark ? t("Light theme") : t("Dark theme"),
                                                         onclick: () => setTheme(dark ? "light" : "dark") },
                     icon(dark ? "sun" : "moon"));
  clear(top,
        h("a.brand", { href: "#run" }, h("span.brand-mark", icon("logo")), h("span.brand-name", "Vx27webDAQ"),
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
                icon("user"), h("span.acct-name", me.user), h("span.acct-role", roleChip(me.role)));
  let menu = null;
  // a click anywhere outside the menu closes it; a click inside (its header) leaves it open, still closing on the next one
  function outside(e) { if (menu && !menu.contains(e.target)) close(); }
  function close() {
    if (!menu) return;
    menu.remove();
    menu = null;
    document.removeEventListener("click", outside);
  }
  function toggle() {
    if (menu) { close(); return; }
    menu = h("div.menu", { role: "menu" },
             h("div.who", h("div", t("Signed in as {name}", { name: me.user })), h("div.row", { style: "margin-top:6px" }, roleChip(me.role))),
             h("button", { type: "button", role: "menuitem", onclick: () => { close(); changePassword(); } }, icon("key"), t("Change password")),
             h("button", { type: "button", role: "menuitem", onclick: () => signOut() }, icon("logout"), t("Sign out")));
    wrap.appendChild(menu);
    setTimeout(() => { if (menu) document.addEventListener("click", outside); }, 0);
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
                   t("No accounts yet: an admin makes the first one on the DAQ computer with  vx27webdaq user add NAME --role admin")) : null);
  password.addEventListener("keydown", (e) => { if (e.key === "Enter") submit.click(); });
  let submit;
  const done = dialog({
    title: t("Sign in to Vx27webDAQ"), body,
    actions: [...(required ? [] : [{ label: t("Cancel"), value: null }]),
              { label: t("Sign in"), kind: "primary", icon: "user",
                run: async () => { await post("/api/login", { name: name.value, password: password.value }); return true; } }],
    onOpen: (m) => { submit = m.querySelector(".btn-primary"); },
  });
  if (await done) app.reload();
}

async function signOut() {
  await Promise.race([app.flush().catch(() => {}), sleep(5000)]);      // the edits still waiting go first
  try { await post("/api/logout"); } catch (err) { /* signed out anyway */ }
  location.reload();
}

// nobody may look in without signing in: the page gives way to the sign-in
function showSignIn() {
  if (app.page && app.page.unmount) { try { app.page.unmount(); } catch (err) { console.error(err); } }
  app.page = null;
  app.pageId = null;
  clear(document.getElementById("topbar"), h("a.brand", h("span.brand-mark", icon("logo")), h("span.brand-name", "Vx27webDAQ")));
  clear(document.getElementById("main"),
        h("div.signin-page.card", h("div.card-head", h("h1.card-title", t("Sign in to Vx27webDAQ"))),
          h("div.card-body.stack", h("p.text2", { style: "margin:0" }, t("This server lets only signed-in users in.")),
            h("button.btn.btn-primary", { type: "button", onclick: () => signIn(true) }, icon("user"), t("Sign in")))));
  signIn(true);
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
  if (!PAGES.some((p) => p.id === id)) {                        // not a page (e.g. #main): the page shown stays
    if (app.pageId && !force) { history.replaceState(null, "", `#${app.pageId}`); return; }
    id = store("page") || "run";
  }
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
  document.title = `${t(def.title)} · Vx27webDAQ`;
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
  // closing the tab or leaving the page: the edits still waiting go now (a request that outlives the page)
  window.addEventListener("pagehide", () => { app.flush(true).catch(() => {}); });
}

// "Skip to the page" (for the keyboard): to the page's content, without changing the address
document.querySelector(".skip")?.addEventListener("click", (e) => {
  e.preventDefault();
  document.getElementById("main").focus();
});

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
  if (!me.role) { showSignIn(); return; }     // no guests on this server: signing in comes first
  connect();
}

window.vx27 = app;     // for the console and the tests
window.vx27SignIn = () => signIn();
boot();

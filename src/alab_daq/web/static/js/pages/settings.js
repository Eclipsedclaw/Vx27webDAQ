// Settings page: the server's settings (operators change them, everyone can read them), this browser's
// choices (theme, language, layout), and your account.
import { put } from "../api.js";
import { T, t } from "../i18n.js";
import { CONF_FILES, chip, choosePath, clear, clearLayout, fail, h, icon, roleChip, segmented, store, toast, toggle } from "../ui.js";

const FIELDS = [
  ["config_file", T("configuration opened when the server starts with nothing being edited"), "file"],
  ["caen_lib_dir", T("folder of libCAEN_FELib.so and libCAEN_Dig2.so (caen_libs.sh builds them there)"), "folder"],
  ["runs_dir", T("save folder (run records and users' outputs)"), "folder"],
  ["data_dirs", T("more data folders users may open (one per line)"), "folders"],
  ["use_simulator", T("simulator"), "bool"], ["simulator_options", T("simulator options"), "text"],
  ["simulator_pulses", T("simulated signal"), "choice"],
  ["convert_to_root", T("Convert to ROOT after the run"), "bool"],
  ["build_events", T("After a run of several boards: one ROOT file of every board per trigger (RUN_built.root)"), "bool"],
  ["refresh_ms", T("refresh (ms)"), "int"],
];
const PICK = { config_file: CONF_FILES };

export function settingsPage(root, app) {
  const can = app.can("control");
  const page = h("div.page");
  root.appendChild(page);
  page.appendChild(h("div.page-head", h("h1", t("Settings"))));
  const values = { ...app.state.settings.values };
  const changes = {};
  const saveBtn = h("button.btn.btn-primary", { type: "button", disabled: true, onclick: save }, icon("check"), t("Save settings"));
  const grid = h("div.form-grid");

  function control(key, kind) {
    const v = values[key];
    const mark = (val) => { changes[key] = val; saveBtn.disabled = false; };
    if (kind === "choice") {
      const names = { "": t("random hits (the simulator options)"), staircase: t("pulse generator: one pulse per channel and record, channel k at k × 1 µs") };
      const el = h("select.input", { disabled: !can, onchange: () => mark(el.value) },
                   ((app.state.settings.choices || {})[key] || []).map((c) => h("option", { value: c }, names[c] || c)));
      el.value = String(v ?? "");
      return el;
    }
    if (kind === "bool") {
      const label = { use_simulator: t("use simulated boards instead of the real ones (no hardware)"),
                      build_events: t("also RUN_built.root: every board's event per trigger, in one file") }[key]
                    || t("after every run, its .raw files become .root files");
      return toggle(label, Boolean(v), (on) => mark(on), { disabled: !can });
    }
    if (kind === "folders") {
      const el = h("textarea.input", { rows: "3", disabled: !can, style: "width:min(640px,100%)", oninput: () => mark(el.value) });
      el.value = (v || []).join("\n");
      const add = h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: async () => {
        const lines = el.value.split("\n").map((x) => x.trim()).filter(Boolean);
        const p = await choosePath({ title: t("Add a data folder"), want: "folder", start: lines[lines.length - 1] || "" });
        if (p && !lines.includes(p)) { el.value = [...lines, p].join("\n"); mark(el.value); }
      } }, icon("plus"), t("Add folder…"));
      return [el, add];
    }
    const el = h("input.input", { value: String(v ?? ""), disabled: !can, type: kind === "int" ? "number" : "text",
                                  style: kind === "int" ? "width:120px" : "width:min(640px,100%)",
                                  oninput: () => mark(kind === "int" ? Number(el.value) : el.value),
                                  placeholder: key === "runs_dir" ? t("empty = {path}", { path: app.state.settings.runs_folder }) : "" });
    if (kind !== "int" && kind !== "text") el.classList.add("mono");
    if (kind !== "file" && kind !== "folder") return el;
    const pick = h("button.btn.btn-sm", { type: "button", disabled: !can, onclick: async () => {
      const start = el.value.trim() || (key === "runs_dir" ? app.state.settings.runs_folder : "");
      const p = await choosePath({ title: t(FIELDS.find((f) => f[0] === key)[1]), want: kind, filters: PICK[key] || null, start });
      if (p) { el.value = p; mark(p); }
    } }, icon("folder"), t("Choose…"));
    return [el, pick];
  }

  function hints(key) {
    if (key === "runs_dir") grid.append(h("span.hint.hint-cell", t("Every run gets a folder here (configuration, DAQ log, status, run.json); users' conversions go to users/<name>/.")));
    if (key === "simulator_options") grid.append(h("span.hint.hint-cell", t("--rate HZ: hits per second; --share P: the part of the channels a hit reaches; --amplitude COUNTS; --noise SIGMA; --seed N; --spacing-us US: the pulse generator's spacing (1 µs); --fail open:NAME, param:NAME:PARAM, read:NAME:N, nostop:NAME or unarmed:NAME imitates a failure of board NAME")));
    if (key === "simulator_pulses") grid.append(h("span.hint.hint-cell", t("the pulse generator: every channel of every board gets a full pulse at each hit (--rate a second, at regular times), negative on a VX2730 and positive on a VX2740, so the waveforms are easy to read; the boards line up in the combined view")));
    if (key === "caen_lib_dir") {
      const libs = app.state.settings.caen_libs || {};
      grid.append(h("span.hint.hint-cell", Object.entries(libs).map(([name, ok]) => [chip(`${name} ${ok ? t("found") : t("missing")}`, ok ? "good" : "warning", ok ? "check" : "alert"), " "]),
                    Object.values(libs).every(Boolean) ? "" : t("Without them only the simulator runs: build them with ./caen_libs.sh (from CAEN's open-source code, no sudo), or install CAEN FELib and CAEN Dig2 and give their folder.")));
    }
  }

  for (const [key, label, kind] of FIELDS) {
    grid.append(h("span.label", t(label)), h("div.row.nowrap", control(key, kind)));
    hints(key);
  }
  page.appendChild(h("div.card",
    h("div.card-head", h("h2.card-title", t("Server settings")), h("span.card-sub", t("for everyone who uses this server")), h("span.spacer"),
      can ? saveBtn : chip(t("read-only: operators change these"), "", "lock")),
    h("div.card-body", grid)));

  async function save() {
    try {
      app.state.settings = await put("/api/settings", { changes });
      for (const k of Object.keys(changes)) delete changes[k];
      saveBtn.disabled = true;
      toast(t("Settings saved"), "ok");
    } catch (err) { fail(err); }
  }

  // ---- this browser ----
  const theme = segmented([["system", t("System")], ["light", t("light")], ["dark", t("dark")]],
                          store("theme") || "system", (v) => {
                            if (v === "system") { document.documentElement.removeAttribute("data-theme"); store("theme", null); }
                            else { document.documentElement.setAttribute("data-theme", v); store("theme", v); }
                            location.reload();
                          }, { label: t("theme") });
  const lang = segmented([["en", "English"], ["zh", "中文"]], store("lang") || "en", (v) => { store("lang", v); location.reload(); },
                         { label: t("language") });
  page.appendChild(h("div.card",
    h("div.card-head", h("h2.card-title", t("This browser")), h("span.card-sub", t("kept in this browser only"))),
    h("div.card-body", h("div.form-grid",
      h("span.label", t("theme")), h("div", theme),
      h("span.label", t("language")), h("div", lang),
      h("span.label", t("layout")), h("div.row", h("button.btn.btn-sm", { type: "button", onclick: () => { clearLayout(); toast(t("Divider positions back to the defaults"), "ok"); } },
                                                     icon("reset"), t("Reset layout")),
                                      h("span.hint", t("Divider positions back to the defaults")))))));

  // ---- your account ----
  const me = app.me;
  page.appendChild(h("div.card",
    h("div.card-head", h("h2.card-title", t("Your account"))),
    h("div.card-body.row", me.user ? [h("span", t("Signed in as {name}", { name: me.user })), roleChip(me.role)]
                                   : [h("span.text2", t("Not signed in: you can view and use the tools.")),
                                      h("button.btn.btn-sm", { type: "button", onclick: () => window.alabSignIn() }, icon("user"), t("Sign in"))])));
  return {
    update(type) {
      if (type === "settings" && !Object.keys(changes).length) {
        Object.assign(values, app.state.settings.values);
        clear(grid);
        for (const [key, label, kind] of FIELDS) { grid.append(h("span.label", t(label)), h("div.row.nowrap", control(key, kind))); hints(key); }
      }
    },
  };
}

// Users page (admins): the accounts and their roles, whether guests may look in, and who did what.
import { del, get, post, put } from "../api.js";
import { t } from "../i18n.js";
import { clear, confirmDialog, dialog, fail, fmtClock, h, icon, roleChip, toast, toggle } from "../ui.js";

const ROLES = ["viewer", "operator", "admin"];

function randomPassword() {
  const abc = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(14));
  return [...bytes].map((b) => abc[b % abc.length]).join("");
}

export function usersPage(root, app) {
  const page = h("div.page");
  root.appendChild(page);
  page.appendChild(h("div.page-head", h("h1", t("Users")),
                     h("span.sub", t("viewer: watch and use the tools · operator: also control the DAQ · admin: also manage the accounts"))));

  // ---- guests ----
  const guests = toggle(t("People who are not signed in may look in, as viewers"), app.state.settings.values.guests, async (on) => {
    try { app.state.settings = await put("/api/settings", { changes: { guests: on } }); toast(on ? t("Guests may look in") : t("Only signed-in users may look in"), "ok"); }
    catch (err) { fail(err); guests.input.checked = !on; }
  });
  page.appendChild(h("div.card", h("div.card-head", h("h2.card-title", t("Who may look in"))),
                     h("div.card-body.stack", guests, h("span.hint", t("Guests can watch everything and use the tools, but never control the DAQ. Off: the page asks everyone to sign in.")))));

  // ---- accounts ----
  const body = h("tbody");
  const name = h("input.input", { placeholder: t("user name"), autocomplete: "off", style: "width:170px" });
  const role = h("select.input", ROLES.map((r) => h("option", { value: r }, t(r))));
  const password = h("input.input.mono", { placeholder: t("password (at least 8 characters)"), autocomplete: "new-password", style: "width:250px" });
  page.appendChild(h("div.card",
    h("div.card-head", h("h2.card-title", t("Accounts"))),
    h("div.card-body.stack",
      h("div.row", name, role, password,
        h("button.btn.btn-sm", { type: "button", onclick: () => { password.value = randomPassword(); } }, icon("key"), t("Make one")),
        h("button.btn.btn-sm.btn-primary", { type: "button", onclick: add }, icon("plus"), t("Add account")))),
    h("div.table-wrap", h("table.table", h("thead", h("tr", h("th", t("Name")), h("th", t("Role")), h("th", t("Created")),
                                                       h("th", t("Last sign-in")), h("th", ""))), body))));

  // ---- activity ----
  const actBody = h("tbody");
  page.appendChild(h("div.card",
    h("div.card-head", h("h2.card-title", t("Activity")), h("span.card-sub", t("who did what, newest first")), h("span.spacer"),
      h("button.btn.btn-sm", { type: "button", onclick: loadActivity }, icon("refresh"), t("Refresh"))),
    h("div.table-wrap", { style: "max-height:420px" },
      h("table.table", h("thead", h("tr", h("th", t("Time")), h("th", t("User")), h("th", t("What")), h("th", t("Detail")))), actBody))));

  function show(users) {
    clear(body, users.map((u) => {
      const sel = h("select.input", { "aria-label": t("Role"), style: "height:28px",
                                      onchange: async () => {
                                        try { show((await put(`/api/users/${encodeURIComponent(u.name)}`, { role: sel.value })).users); toast(t("{name} is {role} now", { name: u.name, role: t(sel.value) }), "ok"); }
                                        catch (err) { fail(err); sel.value = u.role; }
                                      } }, ROLES.map((r) => h("option", { value: r, selected: r === u.role }, t(r))));
      return h("tr",
        h("td", h("div.row.nowrap", h("b", u.name), u.name === app.me.user ? h("span.hint", t("(you)")) : null)),
        h("td", h("div.row.nowrap", sel, roleChip(u.role))),
        h("td.small.text2.num", u.created || "—"), h("td.small.text2.num", u.last_login || t("never")),
        h("td", h("div.row.nowrap",
          h("button.btn.btn-sm", { type: "button", onclick: () => newPassword(u.name) }, icon("key"), t("New password")),
          h("button.btn.btn-sm.btn-ghost", { type: "button", "aria-label": t("Remove {name}", { name: u.name }), title: t("Remove {name}", { name: u.name }),
                                             onclick: () => remove(u.name) }, icon("trash")))));
    }));
  }

  async function load() {
    try { show((await get("/api/users")).users); } catch (err) { fail(err); }
  }

  async function add() {
    try {
      show((await post("/api/users", { name: name.value, password: password.value, role: role.value })).users);
      toast(t("Added {name}: give them the password in person or by a private message", { name: name.value }), "ok");
      name.value = password.value = "";
      loadActivity();
    } catch (err) { fail(err); }
  }

  async function newPassword(user) {
    const input = h("input.input.mono", { value: randomPassword(), style: "width:100%" });
    const ok = await dialog({ title: t("New password for {name}", { name: user }),
                              body: h("div.stack", h("div.field", h("label", t("New password")), input),
                                      h("span.hint", t("{name} is signed out everywhere and signs in again with this password.", { name: user }))),
                              actions: [{ label: t("Cancel"), value: null }, { label: t("Set password"), kind: "primary",
                                run: async () => { await put(`/api/users/${encodeURIComponent(user)}`, { password: input.value }); return true; } }] });
    if (ok) toast(t("New password set for {name}", { name: user }), "ok");
  }

  async function remove(user) {
    if (!(await confirmDialog(t("Remove {name}", { name: user }), [t("The account and its sign-ins are removed. Its outputs stay in the save folder.")],
                              t("Remove"), "danger"))) return;
    try { show((await del(`/api/users/${encodeURIComponent(user)}`)).users); loadActivity(); } catch (err) { fail(err); }
  }

  async function loadActivity() {
    let rows;
    try { rows = (await get("/api/activity", { limit: 200 })).activity; } catch (err) { fail(err); return; }
    clear(actBody, rows.map((a) => h("tr", h("td.small.num.nowrap", fmtClock(a.time)), h("td.small", a.user), h("td.small", t(a.action)),
                                     h("td.small.text2.mono.break", a.detail || ""))));
  }

  load();
  loadActivity();
  return {
    update(type) { if (type === "settings") guests.input.checked = app.state.settings.values.guests; },
  };
}

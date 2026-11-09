// Tools page: every tool is a section with a title bar that opens and closes it (kept for this browser).
// The tools themselves are modules in js/tools/, listed in js/tools/index.js.
import { t } from "../i18n.js";
import { h, icon, setText, store } from "../ui.js";
import { TOOLS } from "../tools/index.js";

export function toolsPage(root, app) {
  const page = h("div.page");
  root.appendChild(page);
  const listeners = {};
  const ctx = {
    app,
    open: (id) => setOpen(id, true),
    on: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
    emit: (name, data) => { for (const fn of listeners[name] || []) fn(data); },
  };
  page.appendChild(h("div.page-head", h("h1", t("Tools")),
    h("span.sub", t("For everyone: the overview of all channels, data files and their ROOT conversion, and what each board says it is")),
    h("span.spacer"),
    h("button.btn.btn-sm", { type: "button", onclick: () => TOOLS.forEach((x) => setOpen(x.id, true)) }, t("Open all")),
    h("button.btn.btn-sm", { type: "button", onclick: () => TOOLS.forEach((x) => setOpen(x.id, false)) }, t("Close all"))));

  const sections = TOOLS.map((tool) => {
    const badge = h("span.tool-badge");
    const head = h("button.tool-head", { type: "button", "aria-expanded": "false", "aria-controls": `tool-${tool.id}`,
                                         onclick: () => setOpen(tool.id, !sec.open) },
                   icon("right", "chev"), icon(tool.icon), h("span.tool-title", t(tool.title)),
                   h("span.tool-summary", t(tool.summary)), badge);
    const body = h("div.tool-body", { id: `tool-${tool.id}`, role: "region", "aria-label": t(tool.title) });
    const el = h("section.card.tool", head, body);
    page.appendChild(el);
    const kept = store(`tools.open.${tool.id}`);
    const sec = { tool, el, head, body, badge, instance: null, open: false };
    sec.wantOpen = kept === null || kept === undefined ? Boolean(tool.open) : kept === "1";
    return sec;
  });

  function setOpen(id, open) {
    const sec = sections.find((s) => s.tool.id === id);
    if (!sec || sec.open === open) return;
    sec.open = open;
    store(`tools.open.${id}`, open ? "1" : "0");
    sec.el.classList.toggle("open", open);
    sec.head.setAttribute("aria-expanded", open ? "true" : "false");
    if (open && !sec.instance) {
      try {
        sec.instance = sec.tool.make(sec.body, ctx) || {};
      } catch (err) {
        console.error(err);
        sec.instance = {};
        sec.body.appendChild(h("div.notice.error", icon("alert"), String(err)));
      }
    }
    showBadges();
  }

  function showBadges() {
    for (const s of sections) setText(s.badge, s.tool.badge ? s.tool.badge(app) || "" : "");
  }

  for (const s of sections) if (s.wantOpen) setOpen(s.tool.id, true);
  showBadges();

  return {
    update(type, msg) {
      for (const s of sections) if (s.instance && s.instance.update) s.instance.update(type, msg);
      showBadges();
    },
    restyle() { for (const s of sections) if (s.instance && s.instance.restyle) s.instance.restyle(); },
    unmount() { for (const s of sections) if (s.instance && s.instance.unmount) s.instance.unmount(); },
  };
}

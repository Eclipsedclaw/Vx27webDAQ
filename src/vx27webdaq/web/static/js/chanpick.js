// A channel picker: one button per channel of a board, at most `max` chosen at a time (16: more plots are not
// readable). Quick choices: blocks of 16, the channels read out, the channels in the board trigger, none.
// Channels that are not read out are drawn faint (they can still be chosen: the plot then says so); a dot marks
// a channel that takes part in the board's own trigger.
import { t } from "./i18n.js";
import { clear, h, setText, toast } from "./ui.js";

export const MAX_SHOWN = 16;

// [0,1,2,3,7,12,13] -> "0–3, 7, 12–13"
export function rangesText(list) {
  const s = [...list].sort((a, b) => a - b), out = [];
  for (let i = 0; i < s.length;) {
    let j = i;
    while (j + 1 < s.length && s[j + 1] === s[j] + 1) j++;
    out.push(j > i ? `${s[i]}–${s[j]}` : `${s[i]}`);
    i = j + 1;
  }
  return out.join(", ");
}

// the channels a board shows before anyone chose: its first `max` channels that are read out (else 0, 1, ...)
export function defaultChannels(n, enabled, max = MAX_SHOWN) {
  const on = [];
  for (let c = 0; c < n && on.length < max; c++) if (!enabled || enabled[c]) on.push(c);
  if (!on.length) for (let c = 0; c < Math.min(n, max); c++) on.push(c);
  return on;
}

// onChange(list): called after every change, with the chosen channels in order
export function channelPicker({ onChange, label }) {
  let n = 0, enabled = null, trigger = null, chosen = new Set(), limit = MAX_SHOWN;
  const count = h("span.text2.small.num");
  const presets = h("div.row.wrap.chan-presets");
  const grid = h("div.chan-grid", { role: "group", "aria-label": label || t("Channels") });
  const el = h("div.chan-pick", h("div.row.wrap", count, h("span.spacer"), presets), grid);

  function emit() { show(); onChange([...chosen].sort((a, b) => a - b)); }

  function choose(list) {
    chosen = new Set(list.filter((c) => c >= 0 && c < n).slice(0, limit));
    emit();
  }

  function flip(c) {
    if (chosen.has(c)) chosen.delete(c);
    else if (chosen.size >= limit) {
      toast(t("At most {n} channels at a time: hide one first", { n: limit }), "info");
      return;
    } else chosen.add(c);
    emit();
  }

  function show() {
    setText(count, t("{k} of at most {n} shown · click a channel to show or hide it", { k: chosen.size, n: limit }));
    const full = chosen.size >= limit;
    for (const b of grid.children) {
      const c = Number(b.dataset.ch), on = chosen.has(c);
      b.classList.toggle("on", on);
      b.classList.toggle("full", full && !on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function build() {
    const btn = (text, title, list) => h("button.btn.btn-sm", { type: "button", title, onclick: () => choose(list()) }, text);
    const blocks = [];
    for (let a = 0; a < n; a += 16) {
      const b = Math.min(n, a + 16) - 1;
      blocks.push(btn(`${a}–${b}`, t("Channels {a} to {b}", { a, b }), () => Array.from({ length: b - a + 1 }, (_, i) => a + i)));
    }
    clear(presets, ...blocks,
      btn(t("Read out"), t("The first channels that are read out"), () => defaultChannels(n, enabled, limit)),
      trigger && trigger.some(Boolean)
        ? btn(t("In trigger"), t("The channels that take part in the board trigger"), () => trigger.map((x, c) => (x ? c : -1)).filter((c) => c >= 0))
        : null,
      btn(t("None"), t("Hide every channel"), () => []));
    clear(grid, Array.from({ length: n }, (_, c) => {
      const off = enabled && !enabled[c];
      const b = h("button.chan", { type: "button", onclick: () => flip(c),
                                   title: off ? t("ch{c}: not read out in this run", { c }) : trigger && trigger[c] ? t("ch{c}: in the board trigger", { c }) : `ch${c}` },
                  String(c));
      b.dataset.ch = String(c);
      if (off) b.classList.add("dim");
      if (trigger && trigger[c]) b.classList.add("trg");
      return b;
    }));
    grid.style.setProperty("--cols", n > 32 ? 16 : n > 16 ? 16 : n);
    show();
  }

  return {
    el,
    // n channels; enabled/trigger: per channel (or null); selected: the chosen ones; max: how many may be chosen
    set({ channels, enabledList = null, triggerList = null, selected = [], max = MAX_SHOWN }) {
      n = channels;
      enabled = enabledList;
      trigger = triggerList;
      limit = Math.max(0, max);
      chosen = new Set(selected.filter((c) => c >= 0 && c < n).slice(0, Math.max(limit, 0)));
      build();
    },
    selected: () => [...chosen].sort((a, b) => a - b),
  };
}

// English and Chinese. Every visible text goes through t(english, values); the Chinese texts are
// in i18n/zh.json, keyed by the English text (the tests check that none is missing).

let lang = "en";
let zh = {};
const missing = new Set();

export async function loadLanguage(choice) {
  lang = choice === "zh" ? "zh" : "en";
  if (lang === "zh" && !Object.keys(zh).length) {
    const v = document.body.dataset.version || "";
    const res = await fetch(`/static/i18n/zh.json?v=${v}`, { credentials: "same-origin" });
    zh = res.ok ? await res.json() : {};
  }
  document.documentElement.setAttribute("lang", lang === "zh" ? "zh-CN" : "en");
}

export function language() { return lang; }

// marks a text in a table, so that the tests find it; t() translates it where it is shown
export const T = (text) => text;

export function t(text, values) {
  let out = text;
  if (lang === "zh") {
    if (Object.prototype.hasOwnProperty.call(zh, text)) out = zh[text];
    else missing.add(text);
  }
  if (values) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in values ? String(values[k]) : m));
  return out;
}

export function missingTexts() { return [...missing].sort(); }

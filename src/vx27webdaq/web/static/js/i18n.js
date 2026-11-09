// English, Chinese and Japanese. Every visible text goes through t(english, values); the other languages' texts
// are in i18n/zh.json and i18n/ja.json, keyed by the English text (the tests check that none is missing).

export const LANGUAGES = [["en", "English"], ["zh", "中文"], ["ja", "日本語"]];     // the choices, in their own words
const HTML_LANG = { en: "en", zh: "zh-CN", ja: "ja" };

let lang = "en";
let texts = {};
const missing = new Set();

export async function loadLanguage(choice) {
  lang = HTML_LANG[choice] ? choice : "en";
  texts = {};
  if (lang !== "en") {
    const v = document.body.dataset.version || "";
    const res = await fetch(`/static/i18n/${lang}.json?v=${v}`, { credentials: "same-origin" });
    texts = res.ok ? await res.json() : {};
  }
  document.documentElement.setAttribute("lang", HTML_LANG[lang]);
}

export function language() { return lang; }

// marks a text in a table, so that the tests find it; t() translates it where it is shown
export const T = (text) => text;

export function t(text, values) {
  let out = text;
  if (lang !== "en") {
    if (Object.prototype.hasOwnProperty.call(texts, text)) out = texts[text];
    else missing.add(text);
  }
  if (values) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in values ? String(values[k]) : m));
  return out;
}

export function missingTexts() { return [...missing].sort(); }

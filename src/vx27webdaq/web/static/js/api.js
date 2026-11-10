// Talking to the server: JSON requests (changes carry the page's own header, which the server requires).
import { t } from "./i18n.js";

// a refused or failed request: the server's message, the HTTP status (0: no answer) and the server's whole JSON
// answer (data), e.g. the configuration as it is now with a 409 conflict
export class ApiError extends Error {
  constructor(message, status, data = null) { super(message); this.status = status; this.data = data; }
}

// an answer that has not come after this long will not come (a laptop that slept, a stalled ssh tunnel): the
// request is given up, so the page does not wait forever; slow calls (Send to boards, big files) give their own
export const TIME_OUT_MS = 30000;
export const SLOW_MS = 180000;
const KEEPALIVE_MOST = 60000;        // the browser sends at most 64 KiB of requests after the page has gone

// timeout: ms to wait for the answer; keepalive: the request still goes when the page is closed or reloaded
export async function api(path, { method = "GET", body, query, timeout = TIME_OUT_MS, keepalive = false } = {}) {
  let url = path;
  if (query) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
    const s = q.toString();
    if (s) url += "?" + s;
  }
  const stop = new AbortController();
  const opts = { method, credentials: "same-origin", headers: {}, signal: stop.signal };
  if (method !== "GET") opts.headers["X-VX27"] = "1";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (keepalive && (!opts.body || opts.body.length < KEEPALIVE_MOST)) opts.keepalive = true;
  const timer = setTimeout(() => stop.abort(), timeout);
  let res, text;
  try {
    res = await fetch(url, opts);
    text = await res.text();
  } catch (err) {
    throw new ApiError(stop.signal.aborted ? t("the server did not answer in time") : t("the server cannot be reached"), 0);
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = { error: text.slice(0, 200) }; }
  }
  if (!res.ok) throw new ApiError((data && data.error) || `${res.status} ${res.statusText}`, res.status, data);
  return data;
}

// opts: {timeout, keepalive}
export const get = (path, query, opts) => api(path, { query, ...opts });
export const post = (path, body = {}, opts) => api(path, { method: "POST", body, ...opts });
export const put = (path, body = {}, opts) => api(path, { method: "PUT", body, ...opts });
export const del = (path, opts) => api(path, { method: "DELETE", ...opts });

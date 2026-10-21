// Masking for every string that leaves the browser over the bridge. Loaded by background.js
// (importScripts) and by the Python tests through node, so it touches no chrome.* API.
// Deterministic: ck-browser compares masked snapshots with each other (markers, guards, page
// keys), so the same page and the same captured values always mask the same way.
"use strict";

const Mask = (() => {
  // A key, a token, a hash: a long run of base64/url-safe characters with a letter and a digit.
  // Node ids and the snapshot's own fields (e12, scroll_down, page keys) are numbers or short.
  const TOKEN = /[A-Za-z0-9+/=_-]{20,}/g;
  const URL_START = /^https?:\/\//i;
  // Opaque ids Chrome mints, which ck-browser matches verbatim; never page content.
  const KEEP = new Set(["objectId", "requestId"]);

  const hidden = length => `[hidden secret, ${length} chars]`;
  const secretLike = run => /[A-Za-z]/.test(run) && /[0-9]/.test(run);
  const tokens = text => text.replace(TOKEN, run => (secretLike(run) ? hidden(run.length) : run));

  function values(text, captured) {
    for (const value of captured) text = text.split(value).join(hidden(value.length));
    return text;
  }

  // A URL keeps its scheme, host and path (an account id in a dashboard path is how a session
  // knows where it is); its query and fragment are masked like any text.
  function string(text, captured) {
    text = values(text, captured);
    if (!URL_START.test(text)) return tokens(text);
    const cut = text.search(/[?#]/);
    return cut < 0 ? text : text.slice(0, cut) + tokens(text.slice(cut));
  }

  function deep(value, captured) {
    if (typeof value === "string") return string(value, captured);
    if (Array.isArray(value)) return value.map(item => deep(item, captured));
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [
        string(key, captured),
        KEEP.has(key) && typeof item === "string" ? item : deep(item, captured),
      ]));
    }
    return value;
  }

  // Whether page text holds a captured value or anything secret-like, URLs included.
  function shows(text, captured) {
    for (const value of captured) if (text.includes(value)) return true;
    return (text.match(TOKEN) || []).some(secretLike);
  }

  return { values, string, deep, shows };
})();

if (typeof module === "object") module.exports = Mask;

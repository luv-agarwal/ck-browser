// ck-browser: hand ONE tab of this everyday Chrome to `ck-browser run --target ext`.
// The human hands a tab over from the popup (or stops it there, or with Cancel on Chrome's
// "started debugging" bar); an agent may open a NEW tab on a domain the human allowed, never
// take an existing one. claim <=> debugger attached; the native port and the host stay up while
// Chrome runs. Chrome may still restart this worker: the claim is kept in chrome.storage.session
// and a new worker re-adopts it while the debugger is still attached. Anything that reaches the host's socket bypasses Python, so this file is the
// policy point. Everything it sends out is masked (mask.js); a captured secret goes to the host alone.
"use strict";

importScripts("mask.js");

const HOST = "com.ck_browser.host";
const DEFAULT_TITLE = chrome.runtime.getManifest().action.default_title;
const CLAIMED_TITLE = "ck-browser controls this tab — click to stop";
const CLAIMED_COLOR = "#d93025";
const PENDING_COLOR = "#f9ab00";

// Everything ck-browser sends to a tab it does not own (no Target.*/Browser.*).
const ALLOWED = new Set([
  "Page.enable", "Page.navigate", "Page.captureScreenshot",
  "Runtime.enable", "Runtime.evaluate", "Log.enable", "Network.enable",
  "Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText",
  "DOM.setFileInputFiles",
  "Emulation.setDeviceMetricsOverride", "Emulation.clearDeviceMetricsOverride",
  "Emulation.setFocusEmulationEnabled",
  // Candidate discovery: Chrome's accessibility tree, closed shadow roots, registering what they
  // name. Runtime.callFunctionOn has the power Runtime.evaluate already has.
  "Accessibility.enable", "Accessibility.getFullAXTree",
  "DOM.describeNode", "DOM.resolveNode", "Runtime.callFunctionOn", "Runtime.releaseObjectGroup",
]);

// The results whose raw form carries page content ck-browser never reads, cut to what it reads
// before masking: no AX value or description, no attributes or text from the described DOM.
const described = n => n && {
  nodeName: n.nodeName, backendNodeId: n.backendNodeId, shadowRootType: n.shadowRootType,
  children: n.children?.map(described), shadowRoots: n.shadowRoots?.map(described),
};
const RESULTS = {
  "Accessibility.getFullAXTree": r => ({
    nodes: (r.nodes || []).filter(n => !n.ignored).map(n => ({
      ignored: false, role: { value: n.role?.value }, name: { value: n.name?.value },
      backendDOMNodeId: n.backendDOMNodeId,
      properties: (n.properties || []).filter(p => p.name === "focusable")
        .map(p => ({ name: p.name, value: { value: p.value?.value } })),
    })),
  }),
  "DOM.describeNode": r => ({ node: described(r.node) }),
};

// The events Tab.drain_events reads, cut to the fields it reads: no headers, cookies or bodies
// ever leave the browser.
const EVENTS = {
  "Runtime.consoleAPICalled": p => ({
    type: p.type, args: (p.args || []).map(a => ({ value: a.value, description: a.description })),
  }),
  "Runtime.exceptionThrown": p => ({
    exceptionDetails: {
      text: p.exceptionDetails?.text,
      exception: { description: p.exceptionDetails?.exception?.description },
    },
  }),
  "Log.entryAdded": p => ({ entry: { level: p.entry.level, text: p.entry.text, url: p.entry.url } }),
  "Network.requestWillBeSent": p => ({
    requestId: p.requestId, request: { method: p.request.method, url: p.request.url },
  }),
  "Network.responseReceived": p => ({
    requestId: p.requestId, response: { status: p.response.status, url: p.response.url },
  }),
  "Network.loadingFailed": p => ({ requestId: p.requestId, errorText: p.errorText }),
};

const WITHHELD = "screenshot withheld: the page shows a secret";
const SERVICE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
// Printable ASCII without space, quotes or backslash: what the host's `security -i` line takes.
const SECRET_CHARS = /^[\x21\x23-\x26\x28-\x5B\x5D-\x7E]+$/;
// The host's three `security` calls take at most 60 s each.
const KEYCHAIN_TIMEOUT_MS = 200000;
const OWN_PAGES = chrome.runtime.getURL("");
const ADOPT_PROBE_MS = 2000;

// ponytail: these checks run in the page's main world, so a hostile page could spoof what they
// read (activeElement, elementFromPoint, attributes). Upgrade path: Page.createIsolatedWorld.
// They are also checks, not a sandbox: Runtime.evaluate stays allowed.
// Takes null (the focused element), {x, y} (a click) or {node} (an observed node's scripted write).
const PROBE = `((point) => {
  const CARD = /card.?num|cc.?num|cvc|cvv|csc|security.?code|expir|card.?holder/i;
  const FRAME = /^(IFRAME|FRAME)$/;
  const isField = e => e.matches?.(
    "input,textarea,select,[contenteditable]:not([contenteditable=false]),[role=textbox],[role=spinbutton]");
  const words = e => {
    const named = (e.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean)
      .map(id => e.getRootNode().getElementById?.(id)?.textContent);
    return [e.getAttribute("name"), e.id, e.getAttribute("aria-label"), e.getAttribute("placeholder"),
      ...[...(e.labels || [])].map(l => l.textContent), ...named].filter(Boolean).join(" ");
  };
  const card = e => isField(e) && ((e.getAttribute("autocomplete") || "").includes("cc-") || CARD.test(words(e)));
  // ck-browser registers the closed roots it reaches; they are checked like open ones.
  const shadow = e => (e && (e.shadowRoot || window.__ckPage?.closedRoots?.get(e))) || null;
  if (typeof point?.node === "number") {
    const e = window.__ckPage?.byId.get(point.node);
    if (!e?.isConnected) return "the element is gone";
    return card(e) || (e.tagName === "LABEL" && e.control && card(e.control))
      ? "the element is a payment-card field" : null;
  }
  if (!point) {
    let e = document.activeElement;
    while (e) {
      if (shadow(e)?.activeElement) e = shadow(e).activeElement;
      else if (FRAME.test(e.tagName)) {
        let inner = null;
        try { inner = e.contentDocument?.activeElement; } catch {}
        if (!inner) return "the focused element is a cross-origin frame";
        e = inner;
      } else break;
    }
    return e && card(e) ? "the focused element is a payment-card field" : null;
  }
  let hit = document.elementFromPoint(point.x, point.y);
  while (shadow(hit)) {
    const inner = shadow(hit).elementFromPoint(point.x, point.y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (hit && FRAME.test(hit.tagName)) return "the click lands on a frame";
  for (let n = hit; n; n = n.parentElement || n.getRootNode().host) {
    if (card(n) || (n.tagName === "LABEL" && n.control && card(n.control))) {
      return "the click lands on a payment-card field";
    }
  }
  return null;
})`;

// The value of an observed node, read for capture_secret; it never leaves this file but to the host.
const READ_VALUE = `((node) => {
  const e = window.__ckPage?.byId.get(node);
  if (!e?.isConnected) return null;
  return e.tagName === "INPUT" || e.tagName === "TEXTAREA" ? e.value : e.textContent;
})`;

// What a screenshot would show, and more: all text, every field's value, open shadow roots.
const PAGE_TEXT = `(() => {
  const parts = [document.body?.innerText || ""];
  const walk = root => {
    for (const e of root.querySelectorAll("*")) {
      if (e.tagName === "INPUT" || e.tagName === "TEXTAREA") parts.push(e.value);
      const inner = e.shadowRoot || window.__ckPage?.closedRoots?.get(e);
      if (inner) { parts.push(inner.textContent || ""); walk(inner); }
    }
  };
  walk(document);
  return parts.join("\\n");
})()`;

// What chrome.storage.local holds, as the human set it in the popup; these until they do.
const POLICY = { enabled: true, domains: [], pending: [] };
const DOMAIN = /^(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)*$/;
const MAX_PENDING = 20; // an agent asking in a loop cannot bury the human in requests
// Answered by this file with or without a claimed tab: they open or close one, or ask for or give
// back a domain.
const TABLESS = new Set(["ck.openTab", "ck.closeTab", "ck.allowDomain",
  "ck.revokeDomain"]);

let port = null; // to the host: opened at start and kept, so the worker and the host stay up
// {tabId, opened, title, tab: its hello's, captured: values read this claim, keychain: id -> settle,
// writes, adopted: taken over from a worker Chrome stopped}
let claim = null;
let queue = Promise.resolve();
let approvalTab = null; // approve.html, opened once for every pending request

// This worker in host.log: a new id under the same instance is a worker restart, a new instance
// another Chrome profile running the extension.
const WORKER = crypto.randomUUID().slice(0, 8);
// Every handler waits for the claim a previous worker left to be re-adopted or dropped.
const ready = start();
// A crashed host, or one not installed yet, comes back on the next alarm.
chrome.alarms.create("ck-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "ck-reconnect") ready.then(() => port || connect());
});

async function start() {
  const { claim: saved } = await chrome.storage.session.get("claim");
  // No other tab may still say it is controlled.
  const tabs = await chrome.tabs.query({});
  await Promise.all(tabs.filter(tab => tab.id !== saved?.tabId).map(tab => mark(tab.id, false)));
  showPending((await policy()).pending);
  connect();
  if (saved) await adopt(saved);
}

// The debugger outlives a stopped worker, so the human's tab may still be attached: then the claim
// goes on. Gone, it ends: never re-attached without a click, since the human may have pressed
// Cancel while no worker ran. A call to a tab not attached fails at once; one that hangs (a busy
// page, an alert) is attached.
async function adopt(saved) {
  try {
    const targets = await chrome.debugger.getTargets();
    if (!targets.some(target => target.tabId === saved.tabId && target.attached)) throw new Error("detached");
    const call = chrome.debugger.sendCommand({ tabId: saved.tabId }, "Runtime.evaluate", { expression: "0" });
    await Promise.race([call, new Promise(resolve => setTimeout(resolve, ADOPT_PROBE_MS))]);
  } catch {
    await chrome.storage.session.remove("claim");
    await mark(saved.tabId, false);
    post({ type: "detached", reason: "lost_in_worker_restart" });
    return;
  }
  claim = { ...saved, captured: new Set(saved.captured), keychain: new Map(), writes: 0, adopted: true };
  await mark(saved.tabId, true);
  post({ type: "hello", tab: claim.tab, adopted: true });
}

// What a new worker needs to go on with the claim. The captured values too, or the masks would
// let them through: session storage stays in memory and out of content scripts.
function save(current) {
  if (claim !== current) return; // released meanwhile: never bring it back
  const { tabId, opened, title, tab, captured } = current;
  return chrome.storage.session.set({ claim: { tabId, opened, title, tab, captured: [...captured] } });
}

// The popup and approve.html: the human's own pages, the only ones that set the policy.
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const fromPage = sender.tab && !String(sender.url).startsWith(OWN_PAGES);
  ready.then(() => (fromPage ? onTabMessage(message, sender.tab) : onPageMessage(message)))
    .then(reply, error => reply({ error: error.message }));
  return true; // the reply comes later
});

// A web page's tab sets nothing.
async function onTabMessage(message, tab) {
  throw new Error(`${message.type} is not a page's message`);
}


chrome.debugger.onDetach.addListener(async (source, reason) => {
  await ready;
  if (claim?.tabId === source.tabId) release(reason);
});

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (claim?.tabId !== source.tabId || source.sessionId || !EVENTS[method]) return;
  let masked;
  try {
    masked = Mask.deep(EVENTS[method](params), claim.captured);
  } catch {
    return; // fail closed: an event that cannot be masked is not sent
  }
  post({ method, params: masked });
});

// Chrome may reset a tab's badge when it navigates; the indicator must outlive navigations.
// An agent-opened tab stays on the allowed domains: a link, a redirect or an OAuth bounce off
// them ends the claim (fail closed). The tab stays open: it is the human's now.
chrome.tabs.onUpdated.addListener(async (tabId, change) => {
  await ready;
  const current = claim;
  if (current?.tabId !== tabId) return;
  if (change.status === "loading") mark(tabId, true);
  if (!current.opened || !change.url) return;
  const host = offList(change.url, (await policy()).domains);
  if (host !== null && claim === current) release("left_allowed_domains", { host });
});

function connect() {
  const current = chrome.runtime.connectNative(HOST);
  port = current;
  current.onMessage.addListener(message => onHostMessage(message));
  current.onDisconnect.addListener(() => {
    const why = chrome.runtime.lastError?.message || "closed without an error";
    console.warn("ck-browser: host:", why);
    if (port !== current) return;
    port = null;
    chrome.storage.session.set({ lastDisconnect: why }); // the next host logs it
    // Fail closed: no host, no claim. After start(), so a claim being re-adopted is released too.
    ready.then(() => release("host_disconnected"));
  });
  policy().then(postState);
}

// The policy, with who this worker is for host.log. `instance` names this profile's socket; the
// policy lives in chrome.storage.local, which Chrome keeps per profile, so each profile has its own.
async function postState(open) {
  const { instance = crypto.randomUUID().slice(0, 8) } = await chrome.storage.local.get("instance");
  await chrome.storage.local.set({ instance });
  const { lastDisconnect = null } = await chrome.storage.session.get("lastDisconnect");
  post({ type: "state", open, instance, worker: WORKER, lastDisconnect, profile: await profileInfo() });
  if (lastDisconnect && port) await chrome.storage.session.remove("lastDisconnect"); // told once
}

// The email this Chrome profile is signed in as, so an agent can pick the profile; never the gaia
// id. Null without it: no identity permission (an extension not reloaded since 0.5.0).
async function profileInfo() {
  return null;
}

// One at a time, like clicks: a claim is checked and taken without another in between.
function serial(task) {
  const result = queue.then(task);
  queue = result.catch(error => console.error("ck-browser:", error));
  return result;
}

async function toggle(tabId) {
  const previous = claim?.tabId;
  if (claim) await release("released_by_user");
  if (previous !== tabId) await take(tabId);
}

// Whether the tab is now claimed. A tab ck-browser just opened is still loading `url`: its hello
// names that url, since reading the tab would race the navigation.
async function take(tabId, { opened = false, url = null } = {}) {
  if (!port) connect();
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
  } catch (error) {
    console.warn("ck-browser: cannot attach to this tab:", error.message);
    return false;
  }
  const current = { tabId, opened, title: "", captured: new Set(), keychain: new Map(), writes: 0 };
  claim = current;
  await mark(tabId, true);
  try {
    let title = "";
    if (!url) {
      const { result } = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate",
        { expression: "[location.href, document.title]", returnByValue: true });
      [url, title] = result.value;
    }
    const tab = Mask.deep({ id: tabId, url, title, opened }, current.captured);
    current.title = tab.title;
    current.tab = tab;
    post({ type: "hello", tab });
    await save(current);
    return true;
  } catch (error) {
    console.warn("ck-browser: cannot read the tab:", error.message);
    if (claim === current) await release("unreadable_tab");
    return false;
  }
}

// The port stays open: the host outlives a claim. `detail` says more to the client (a host).
async function release(reason, detail = {}) {
  const current = claim;
  if (!current) return;
  claim = null;
  post({ type: "detached", reason, ...detail });
  await chrome.storage.session.remove("claim");
  await chrome.debugger.detach({ tabId: current.tabId }).catch(() => {});
  await mark(current.tabId, false);
}

async function onHostMessage(message) {
  await ready;
  // The host strips typed messages from clients, so a typed one is the host's own.
  if (message.type === "keychain_result") {
    claim?.keychain.get(message.id)?.(message);
    return;
  }
  if (message.type) return;
  const { id, method } = message;
  const params = message.params || {};
  const current = claim;
  const captured = current?.captured || new Set();
  let reply;
  try {
    reply = { id, result: await answer(current, method, params) };
  } catch (error) {
    let text = "the error could not be masked";
    try {
      const message = error.message || String(error);
      // Our own capture errors name the Keychain service, which reads like a token.
      text = error.trusted ? Mask.values(message, captured) : Mask.string(message, captured);
    } catch {}
    reply = { id, error: { message: text } };
  }
  // A tab call answered after its claim ended has no caller left to hear it.
  if (TABLESS.has(method) || claim === current) post(reply);
  // After the reply: the detached frame ends the client's reads.
  if (method === "ck.closeTab" && reply.result && claim === current) {
    await release("closed_by_agent");
    await chrome.tabs.remove(current.tabId).catch(() => {});
  }
}

// The result of one call, masked; a refusal, a failed call or a failed mask throws.
async function answer(current, method, params) {
  if (method === "ck.openTab") return serial(() => openTab(params));
  if (method === "ck.closeTab") return closeTab();
  if (method === "ck.allowDomain") return allowDomain(params);
  if (method === "ck.revokeDomain") return revokeDomain(params);
  if (!current) throw new Error("no tab is claimed");
  const { tabId } = current;
  if (method === "ck.captureSecret") return captureSecret(current, params);
  if (method === "ck.checkNode") return checkNode(current, params);
  const refusal = await refuse(current, method, params);
  if (refusal) throw new Error(refusal);
  if (method === "Page.captureScreenshot") {
    await checkScreenshot(current);
    return (await chrome.debugger.sendCommand({ tabId }, method, params)) || {};
  }
  const result = (await chrome.debugger.sendCommand({ tabId }, method, params)) || {};
  // A reducer that throws fails the call: the raw result is never sent.
  return Mask.deep(RESULTS[method] ? RESULTS[method](result) : result, current.captured);
}

// A new tab at an allowed http(s) URL, claimed without a click; the hello goes out first.
// An existing tab is only ever handed over by the human.
async function openTab({ url }) {
  if (claim) throw new Error("busy: a tab is already claimed");
  const open = await policy();
  if (!open.enabled) throw new Error("refused: agents may not open tabs; turn it on in the ck-browser popup");
  let scheme = null;
  try { scheme = new URL(url).protocol; } catch {}
  if (!["http:", "https:"].includes(scheme)) throw new Error("refused: only http(s) URLs can be opened");
  const host = offList(url, open.domains);
  if (host !== null) throw new Error(notAllowed(host));
  const tab = await chrome.tabs.create({ url, active: true });
  if (!(await take(tab.id, { opened: true, url }))) {
    await chrome.tabs.remove(tab.id).catch(() => {});
    throw new Error("could not take the tab ck-browser opened");
  }
  return { tabId: tab.id };
}

// Only a tab ck-browser opened; onHostMessage closes it once the reply is out.
function closeTab() {
  if (!claim) throw new Error("no tab is claimed");
  if (!claim.opened) throw new Error("refused: the human handed this tab over; it is stopped from the icon");
  return { closed: true };
}

// An agent asks; the human answers on approve.html or in the popup.
// A request already waiting changes nothing, so it never pulls the approval page forward again.
async function allowDomain({ domain }) {
  if (typeof domain !== "string") throw new Error("refused: a request needs a domain");
  const name = domain.trim().toLowerCase();
  if (name === "*") throw new Error("refused: any-domain is only set by the human in the popup");
  if (!DOMAIN.test(name)) throw new Error(`refused: ${domain} is not a domain like example.com or *.example.com`);
  // `*.com` would be every site under a public suffix; a bare single label (localhost) is one host.
  if (name.startsWith("*.") && !name.slice(2).includes(".")) {
    throw new Error(`refused: ${name} is too broad; name a domain like *.example.com`);
  }
  const open = await policy();
  if (allows(open.domains, name)) return { status: "allowed" };
  if (open.pending.includes(name)) return { status: "pending" };
  if (open.pending.length >= MAX_PENDING) throw new Error("refused: too many pending requests; answer them in the popup");
  await setPolicy({ pending: [...open.pending, name] });
  await showApproval();
  return { status: "pending" };
}

// An agent gives a domain back, `*` too: off the allowed and pending lists. A tab claimed on it
// stays claimed (the human stops it); an agent-opened one is released at its next navigation.
async function revokeDomain({ domain }) {
  if (typeof domain !== "string") throw new Error("refused: a revoke needs a domain");
  const name = domain.trim().toLowerCase();
  if (name !== "*" && !DOMAIN.test(name)) throw new Error(`refused: ${domain} is not a domain like example.com or *.example.com`);
  const open = await policy();
  if (!open.domains.includes(name) && !open.pending.includes(name)) return { status: "not_allowed" };
  await setPolicy({ domains: open.domains.filter(item => item !== name), pending: open.pending.filter(item => item !== name) });
  return { status: "revoked" };
}

async function showApproval() {
  if (approvalTab !== null) {
    try {
      await chrome.tabs.update(approvalTab, { active: true });
      return;
    } catch {} // the human closed it
  }
  approvalTab = (await chrome.tabs.create({ url: chrome.runtime.getURL("approve.html"), active: true })).id;
}

// `*` covers every host; `*.example.com` covers example.com and every subdomain; `example.com`
// covers that host only. `name` is a host or, for a request, a pattern itself.
function covers(pattern, name) {
  if (pattern === "*" || pattern === name) return true;
  if (!pattern.startsWith("*.")) return false;
  const apex = pattern.slice(2), bare = name.replace(/^\*\./, "");
  return bare === apex || bare.endsWith(`.${apex}`);
}

const allows = (domains, name) => domains.some(pattern => covers(pattern, name.toLowerCase()));

// Null for an http(s) URL on an allowed domain, else its host (the URL itself if it has none).
function offList(url, domains) {
  let target = null;
  try { target = new URL(url); } catch {}
  if (["http:", "https:"].includes(target?.protocol) && allows(domains, target.hostname)) return null;
  return target?.hostname || String(url);
}

const notAllowed = host => `refused: ${host} is not an allowed domain; ask with: bin/ck-browser ext allow ${host}`;

async function policy() {
  return { ...POLICY, ...(await chrome.storage.local.get(Object.keys(POLICY))) };
}

// Stored, shown and told to the host, which hands it to every client.
async function setPolicy(change) {
  await chrome.storage.local.set(change);
  const open = await policy();
  await showPending(open.pending);
  await postState(open);
  return open;
}

// The global badge; a claimed tab's own `CK` stays over it.
function showPending(pending) {
  return Promise.all([
    chrome.action.setBadgeText({ text: pending.length ? "?" : "" }),
    chrome.action.setBadgeBackgroundColor({ color: PENDING_COLOR }),
  ]).catch(() => {});
}

async function onPageMessage({ type, enabled, domain, tabId, origin, ids }) {
  // Queued on arrival, like a click, so it keeps its place against an agent's openTab.
  if (type === "handover") await serial(() => toggle(tabId));
  const open = await policy();
  const without = list => list.filter(item => item !== domain);
  if (type === "toggle") await setPolicy({ enabled: Boolean(enabled) });
  else if (type === "add") {
    // The human typed it: `*` is theirs to set.
    const name = String(domain).trim().toLowerCase();
    if (name !== "*" && !DOMAIN.test(name)) throw new Error(`${domain} is not a domain like example.com or *.example.com`);
    await setPolicy({ domains: [...new Set([...open.domains, name])], pending: open.pending.filter(item => item !== name) });
  } else if (type === "remove") await setPolicy({ domains: without(open.domains) });
  else if (type === "allow" && open.pending.includes(domain)) {
    await setPolicy({ domains: [...new Set([...open.domains, domain])], pending: without(open.pending) });
  } else if (type === "deny") await setPolicy({ pending: without(open.pending) });
  return { open: await policy(), claim: claim && { tabId: claim.tabId, title: claim.title, opened: claim.opened } };
}


// A scripted write (a native select's value) dispatches no input event for refuse() to check,
// so ck-browser asks first. Answered by this file, never forwarded to chrome.debugger.
async function checkNode(current, params) {
  const { node } = params;
  if (!Number.isInteger(node)) throw new Error("refused: a check needs an observed node");
  const reason = await probe(current.tabId, { node });
  if (reason) throw new Error(`refused: ${reason}`);
  return {};
}
// A secret on the page is never photographed: the PNG would carry it past every mask.
async function checkScreenshot(current) {
  let text = null;
  try {
    const { result, exceptionDetails } = await chrome.debugger.sendCommand({ tabId: current.tabId },
      "Runtime.evaluate", { expression: PAGE_TEXT, returnByValue: true });
    if (!exceptionDetails && typeof result.value === "string") text = result.value;
  } catch {}
  if (text === null) throw new Error("screenshot withheld: the page could not be checked");
  if (Mask.shows(text, current.captured)) throw new Error(WITHHELD);
}

// An error this file or the host words, never holding the value: only captured values are masked.
const trusted = message => Object.assign(new Error(message), { trusted: true });

// Read an observed node's value here and hand it to the host for the Keychain; ck-browser gets
// its length. Answered by this file, never forwarded to chrome.debugger.
async function captureSecret(current, params) {
  const { node, service } = params;
  if (!Number.isInteger(node) || typeof service !== "string" || !SERVICE.test(service)) {
    throw trusted("refused: a capture needs an observed node and a Keychain service name");
  }
  const { result, exceptionDetails } = await chrome.debugger.sendCommand({ tabId: current.tabId },
    "Runtime.evaluate", { expression: `${READ_VALUE}(${node})`, returnByValue: true });
  if (exceptionDetails || typeof result.value !== "string") {
    throw trusted("refused: the element is gone or holds no text");
  }
  const value = result.value.trim();
  if (value.length < 8 || value.length > 4096) {
    throw trusted(`refused: the value is ${value.length} characters; a Keychain capture takes 8 to 4096`);
  }
  if (!SECRET_CHARS.test(value)) throw trusted("refused: the value has characters a Keychain capture does not take");
  current.captured.add(value);
  await save(current);
  const written = await keychainWrite(current, service, value);
  if (!written.ok) throw trusted(written.error || "the Keychain write failed");
  return { service, length: value.length, stored: true };
}

function keychainWrite(current, service, value) {
  const id = ++current.writes;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      current.keychain.delete(id);
      reject(trusted(`the Keychain write for ${service} did not answer in time`));
    }, KEYCHAIN_TIMEOUT_MS);
    current.keychain.set(id, message => {
      clearTimeout(timer);
      current.keychain.delete(id);
      resolve(message);
    });
    post({ type: "keychain_write", id, service, value });
  });
}

async function refuse(current, method, params) {
  if (!ALLOWED.has(method)) return `${method} is not allowed over the extension bridge`;
  if (method === "Page.navigate") {
    let scheme = null;
    try { scheme = new URL(params.url).protocol; } catch {}
    if (!["http:", "https:"].includes(scheme)) return "refused: only http(s) URLs can be opened";
    // The human gave a handed-over tab; an opened one only goes where they allowed.
    const host = current.opened ? offList(params.url, (await policy()).domains) : null;
    return host === null ? null : notAllowed(host);
  }
  let point;
  if (method === "Input.insertText" || (method === "Input.dispatchKeyEvent" && params.text)) point = null;
  else if (method === "Input.dispatchMouseEvent" && params.type === "mousePressed") point = { x: params.x, y: params.y };
  else return null;
  const reason = await probe(current.tabId, point);
  return reason ? `refused: ${reason}` : null;
}

// PROBE's reason to refuse, or null. Fail closed: a target that cannot be checked is not typed
// into, clicked or written.
async function probe(tabId, arg) {
  try {
    const { result, exceptionDetails } = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate",
      { expression: `${PROBE}(${JSON.stringify(arg)})`, returnByValue: true });
    if (!exceptionDetails) return result.value;
  } catch {}
  return "the target could not be checked";
}

function post(message) {
  try {
    port?.postMessage(message);
  } catch {} // the port is already gone; its onDisconnect releases the claim
}

// Issued together, so a later mark always lands after an earlier one. An unclaimed tab falls
// back to the global badge: `?` while a domain request waits.
function mark(tabId, claimed) {
  return Promise.all([
    chrome.action.setBadgeText({ tabId, text: claimed ? "CK" : null }),
    chrome.action.setBadgeBackgroundColor({ tabId, color: claimed ? CLAIMED_COLOR : PENDING_COLOR }),
    chrome.action.setTitle({ tabId, title: claimed ? CLAIMED_TITLE : DEFAULT_TITLE }),
  ]).catch(() => {}); // the tab was closed
}

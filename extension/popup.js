// The popup: hand the active tab over or stop it, the switch, the allowlist and the requests
// waiting for the human. background.js owns the policy; each control is one message to it, and the
// popup redraws from its reply.
"use strict";

const $ = id => document.getElementById(id);
let active = null; // the tab the popup was opened over

async function send(message) {
  const reply = await chrome.runtime.sendMessage(message);
  $("error").textContent = reply.error || "";
  if (!reply.error) render(reply);
}

function render({ open, claim }) {
  const here = claim !== null && claim.tabId === active?.id;
  $("status").textContent = claim
    ? `Controlling: ${claim.title || "a tab"}${claim.opened ? " (opened by ck-browser)" : ""}`
    : "No tab is controlled.";
  $("handover").textContent = here ? "Stop controlling" : "Hand over this tab";
  $("handover").className = here ? "stop" : "";
  $("handover").disabled = !active;
  $("enabled").checked = open.enabled;
  $("requests").hidden = !open.pending.length;
  list($("pending"), open.pending, domain => [["Allow", { type: "allow", domain }], ["Deny", { type: "deny", domain }]]);
  list($("domains"), open.domains, domain => [["✕", { type: "remove", domain }]]);
}

// One row per domain, its buttons each sending one message.
function list(ul, domains, buttons) {
  if (!domains.length) {
    const empty = document.createElement("li");
    empty.className = "none";
    empty.textContent = "none";
    ul.replaceChildren(empty);
    return;
  }
  ul.replaceChildren(...domains.map(domain => {
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = domain;
    row.append(name, ...buttons(domain).map(([label, message]) => {
      const button = document.createElement("button");
      button.textContent = label;
      button.addEventListener("click", () => send(message));
      return button;
    }));
    return row;
  }));
}

$("handover").addEventListener("click", () => send({ type: "handover", tabId: active.id }));
$("enabled").addEventListener("change", event => send({ type: "toggle", enabled: event.target.checked }));
$("add").addEventListener("submit", event => {
  event.preventDefault();
  const domain = $("domain").value.trim();
  if (!domain) return;
  $("domain").value = "";
  send({ type: "add", domain });
});


chrome.tabs.query({ active: true, currentWindow: true }).then(async ([tab]) => {
  active = tab || null;
  await send({ type: "state" });
});

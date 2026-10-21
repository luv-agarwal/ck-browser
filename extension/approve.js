// The page background.js opens when an agent asks for a domain: Allow or Deny each request.
// It redraws whenever the policy changes (the popup may answer too) and closes once none is left.
"use strict";

async function send(message) {
  const { open } = await chrome.runtime.sendMessage(message);
  if (!open.pending.length) {
    window.close();
    return;
  }
  document.getElementById("pending").replaceChildren(...open.pending.map(domain => {
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = domain;
    row.append(name, ...[["Allow", "allow"], ["Deny", "deny"]].map(([label, type]) => {
      const button = document.createElement("button");
      button.textContent = label;
      button.className = type;
      button.addEventListener("click", () => send({ type, domain }));
      return button;
    }));
    return row;
  }));
}

chrome.storage.onChanged.addListener(() => send({ type: "state" }));
send({ type: "state" });

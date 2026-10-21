# Optional: drive your own Chrome

By default ck-browser runs in its own Chrome. With this extension, a run can
instead drive one tab of your everyday Chrome (`--target ext`), for flows
that need your real logins. It is macOS only today.

## Install

1. Open `chrome://extensions`, turn on Developer mode, click **Load
   unpacked** and pick this `extension/` folder. The extension is not on the
   Chrome Web Store.
2. Run `bin/ck-browser ext install`. It writes Chrome's native-messaging host
   manifest and a small host wrapper, so the extension can talk to
   ck-browser.

Chrome derives an unpacked extension's ID from the folder it was loaded
from. `ext install` computes that ID itself. If the bridge does not
connect, copy the ID shown at `chrome://extensions` and run:

```sh
bin/ck-browser ext install --extension-id <id>
```

If you move this folder, load it again and re-run `ext install`. Load it in
every Chrome profile you want to drive; after an update, click the
extension's reload button there.

## Why it asks for the `debugger` permission

ck-browser reads pages and clicks through the Chrome DevTools Protocol. In
your own Chrome the only way to reach a tab's DevTools Protocol from an
extension is `chrome.debugger`. While a tab is driven, Chrome shows its
"started debugging this browser" bar; Cancel on that bar stops the run.

## Giving it a tab

- Hand over a tab: click the extension icon, then **Hand over this tab**.
  A red `CK` badge shows the tab is claimed; **Stop controlling** releases
  it.
- Or allow domains: in the popup, add a domain (`example.com`,
  `*.example.com` for it and every subdomain). A script whose `url:` is on
  an allowed domain then opens and claims its own tab, and that tab is kept
  to the allowed domains. `bin/ck-browser ext allow <domain>` asks you for a
  domain through an approval page in Chrome.

Then:

```sh
bin/ck-browser ext status
bin/ck-browser run steps.yaml --out /tmp/run --target ext
```

A run never closes the tab. `bin/ck-browser ext close` closes a tab
ck-browser opened; a handed-over tab is yours to stop.

## capture_secret

`capture_secret: { field: "...", keychain_service: <name> }` (with
`--target ext` only) reads a value the page shows, such as an API key
revealed once, and stores it in the macOS login Keychain through
`security`. Jev only sees it masked; run.json records its length, never the
value. It never overwrites an existing item. macOS only.

## Limits (not a sandbox)

- Masking: every string leaving the browser (snapshots, page text,
  console and network events, the tab's title and URL) has captured values
  and secret-like tokens (runs of 20+ `[A-Za-z0-9+/=_-]` with a letter and a
  digit) replaced by `[hidden secret, N chars]`. A page that shows one is not
  photographed. URL paths are never masked.
- The extension refuses DevTools methods outside its allowlist, navigation
  to anything but http(s), and typing or clicking into frames or
  payment-card fields.
- `Runtime.evaluate` stays allowed, so anything that reaches a profile's
  bridge socket can run any JavaScript in its claimed tab. The real boundary
  is the 0600 socket in a 0700 directory and the same OS user.
- The refusal and capture probes run in the page's main world, so a hostile
  page can spoof them.
- A closed shadow root ck-browser registered sits in the page's main world
  until the page unloads, so the page's own scripts can reach into it:
  closed mode is encapsulation, not a security boundary, and it is weaker
  while a run is on the tab.
- Before a capture, a value that does not look like a token (punctuation,
  under 20 characters, letters-only or digits-only) is unmasked.
- An allowed domain means a run may act as you on that site whenever Chrome
  is open.

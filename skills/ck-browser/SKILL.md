---
name: ck-browser
description: Runs a web flow written as plain-language YAML steps in a real headless Chrome and reports pass or fail per step, with a JSON result. Use it to verify a login or form flow, click through a UI and check that a page shows some text, smoke-test a web app after a change, or drive a page without writing selectors. Triggers include requests to "check the login still works", "test this form in a browser", "click through the signup flow", "verify the page shows", "smoke-test the web app" and "run a browser check".
compatibility: Requires Python 3.11+ and Chrome or Chromium. Jev decisions need TYPESAFE_API_KEY; --stub runs an offline label matcher without it.
license: MIT
allowed-tools: Bash(ck-browser:*), Bash(uvx:*), Bash(uv:*)
---

# ck-browser

ck-browser runs the steps of a web flow in its own Chrome and reports pass or fail for each step.
Each decision a step needs (which element, has the page settled, is the text there) goes to Jev,
typesafe.ai's decision model. You write labels the way a user reads them, never selectors.

## Install

Run it without installing:

```sh
uvx --from git+https://github.com/luv-agarwal/ck-browser ck-browser doctor
```

Install it as a command:

```sh
uv tool install git+https://github.com/luv-agarwal/ck-browser
# or
pipx install git+https://github.com/luv-agarwal/ck-browser
```

From a clone, use `bin/ck-browser` in place of `ck-browser`.

`ck-browser doctor` reports whether Chrome starts, whether the Jev key is present, and whether
`ffmpeg` (for `--video`) is on PATH.

## Workflow

1. Write `steps.yaml` with an optional `url:` and a `steps:` list, one `verb: argument` per step.
2. Run it:

   ```sh
   ck-browser run steps.yaml --out /tmp/ck-run --json
   ```

3. Read the JSON on stdout. For each step check `status` (`pass`, `fail` or `skipped`) and
   `error`. A pick shows the chosen `target` with its `label`, `role` and probability `p`, plus
   the other `candidates`. A failed pick adds `near_misses`: the page elements whose words came
   closest, with why each was not offered. Screenshots are in `/tmp/ck-run`, at checkpoints and
   on failure.
   With `--video`, `filmstrip.html` in `--out` shows each step's frame next to its verdict, and
   run.json's `video` names the `run.webm` (or why there is none). For a person to watch; read
   the JSON yourself.
4. Fix the failing step (its label, a missing `expect`, a dropdown that needs opening first) and
   run again.

Exit codes: `0` all steps passed, `1` a step failed, `2` a script, Chrome or Jev error. A run that
dies early still writes `run.json`, with top-level `error` set.

## Example

A login and logout on a public practice site with published demo credentials:

```yaml
url: https://the-internet.herokuapp.com/login
steps:
  - fill: { field: "Username", value: "tomsmith" }
  - fill: { field: "Password", value: "SuperSecretPassword!" }
  - click: "Login"
  - expect: "You logged into a secure area!"
  - checkpoint: secure-area
  - click: "Logout"
  - expect: "You logged out of the secure area!"
```

## Verbs

| verb | argument | what happens |
| --- | --- | --- |
| `open` | URL | navigate, wait for load |
| `click` | label | Jev picks a clickable element; click it |
| `fill` | `{field, value}` | Jev picks the input; type the value (password-like fields masked) |
| `select` | `{field, value}` | Jev picks the dropdown; the option is chosen by its text |
| `upload` | `{field, path}` | Jev picks the file input; set the file |
| `hover` | label | Jev picks the element; move the mouse to it |
| `drag` | `{from, to}` | two Jev picks, then press, move and release |
| `press` | key name (`Enter`, `Escape`), letter or digit, modifiers (`Shift+Tab`) | key event, no decision |
| `type` | text, or `{text, secret: true}` | types into whatever has focus, no decision |
| `scroll` | `up` or `down` | one viewport, no decision |
| `wait` | milliseconds | sleep, no decision |
| `expect` | text | Jev judges the page text; pass at p ≥ 0.6 (`--expect-threshold`) |
| `expect_count` | `{label: n}` | pass when the number of matching elements is n |
| `checkpoint` | name | screenshot `NN-<name>.png`, console errors, failed requests |
| `capture_secret` | `{field, keychain_service}` | `--target ext` only (macOS) |

Labels are matched by Jev, not literally. A "none of these" option is always offered, so a label
that is not on the page fails with "no matching element" and the top candidates, instead of a
wrong click.

## Writing good steps

- Put an `expect` after every navigation or dialog. It doubles as the wait.
- Expect text that only the new state shows. Text already on the page passes whether or not the
  action worked.
- `expect` only proves presence. There is no negative assertion.
- Name labels the way a user reads them: the button text, the field label or placeholder.
- Open a custom dropdown in its own step and `expect` the option before you click it.
- Lower `--step-timeout` (default 60 seconds) while you draft a script, so a miss fails fast.

## Useful flags

| flag | meaning |
| --- | --- |
| `--json` | print run.json to stdout instead of the text report |
| `--step-timeout <s>` | seconds per step (default 60) |
| `--headed` | show the Chrome window when launching it |
| `--stub` | offline label matcher instead of Jev |
| `--video` | record the run: `run.webm` (needs `ffmpeg` on PATH, else JPEG frames in `frames/`) plus a per-step `filmstrip.html` |
| `--profile-dir <dir>` | a separate Chrome profile, for parallel runs that must not share cookies |

## Key and privacy

- Set `TYPESAFE_API_KEY`. Without a key, `--stub` runs the flow with an offline label matcher
  (no network, no calibrated probabilities).
- Each decision sends Jev the page URL and title, the page text, each candidate's label and
  current field value, the whole step script, the current step, and the last 5 actions.
- No screenshots or pixels are sent.
- The value of a `fill` step whose field looks like a password, secret, token, PIN or OTP is
  masked, and so is a `type` step with `secret: true`. Other values the page shows go to Jev
  unmasked.
- `--out` holds `jev-trace.jsonl` with the page text of every decision. It is created private
  (0700); keep it out of shared directories.

## Limits

- No visual judgment: it reads the DOM, never pixels.
- No `alert` or `confirm` dialogs, no frames, no network mocking, one tab per run.
- It is not a sandbox. A step file has the power of a script: `open` can load `file://` pages and
  `upload` can send any file you can read. Run only pages and step files you trust.

## doctor and stop

- `ck-browser doctor` starts or reuses ck-browser's Chrome and reports its port, profile,
  whether the key is present, and `ffmpeg: found <version>` or `ffmpeg: not found (video falls
  back to frames)`.
- `ck-browser stop` closes that Chrome. An idle watchdog also closes it after 5 minutes without
  a client; the next run starts it again.
- ck-browser uses its own Chrome profile under `~/.ck-browser` and never attaches to your
  everyday Chrome. Logins in its profile persist between runs.

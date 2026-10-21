# ck-browser

[![CI](https://github.com/luv-agarwal/ck-browser/actions/workflows/ci.yml/badge.svg)](https://github.com/luv-agarwal/ck-browser/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
[![MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Run browser checks written as plain-language steps. Python 3.11+ standard
library only, no packages to install.

## What it is

You write the steps of a flow in a `steps.yaml` ("fill Username", "click
Login", "expect You logged into a secure area!"). ck-browser runs them in a
real Chrome and reports pass or fail for each step.

Every decision a step needs (which element to act on, has the page
settled, is the text there) goes to Jev, typesafe.ai's decision model. Jev
reads a DOM snapshot of the page, never pixels, and returns a calibrated
probability for each candidate element. Usually one Jev call per decision:
a drag picks twice (source and target), and settling can ask again.

Each run writes `run.json` (every step, its target, the probabilities,
timings and cost), screenshots at checkpoints and on failure, and a Jev
trace (`jev-trace.jsonl`). With `--video` it also records the run as
`run.webm` and writes `filmstrip.html`, one frame per step.

Why use it: on three real web-app flows, run with the same step scripts
and passing 9/9 on both tools, ck-browser was 11-17x faster and about
200-380x cheaper per run (about 15 s and $0.002 against 171-257 s and
$0.31-0.54) than an LLM agent clicking through chrome-devtools-axi with one
model turn per step. In real agent verification sessions it also took about
4x fewer browser calls (44 → 10) and about 3x fewer output tokens
(8.7k → 3.1k) per verification. The gain comes from one open CDP
connection, one page read per step and one Jev decision of about 180 ms,
where the agent pays a model turn and a new process for every step. The
comparison is with that agent loop, not with driving CDP yourself.

## Install

You need Python 3.11+ and Chrome or Chromium. ck-browser is not on PyPI;
install it from GitHub.

- Run it without installing:

  ```sh
  uvx --from git+https://github.com/luv-agarwal/ck-browser ck-browser doctor
  ```

- Install it as a command:

  ```sh
  uv tool install git+https://github.com/luv-agarwal/ck-browser
  # or
  pipx install git+https://github.com/luv-agarwal/ck-browser
  ```

- Clone it:

  ```sh
  git clone https://github.com/luv-agarwal/ck-browser && cd ck-browser && bin/ck-browser doctor
  ```

The examples are in the clone only.
So is the optional extension (`extension/`).

`ck-browser --version` prints the installed version.

## 60-second quickstart

You need Python 3.11+ and Chrome or Chromium.

```sh
git clone https://github.com/luv-agarwal/ck-browser && cd ck-browser
export TYPESAFE_API_KEY=...
bin/ck-browser doctor
bin/ck-browser run examples/login.yaml --out /tmp/run
```

No key yet? Add `--stub` to `run`: an offline label matcher stands in for
Jev, so you can try the flow format without a key.

## The Jev key

Jev is typesafe.ai's decision model. typesafe.ai currently says: "Try our
first System One Model, Jev, in early access."

- Get a key: typesafe's quickstart says "Get an API key from the TypeSafe
  console" (https://console.typesafe.ai/keys). Then
  `export TYPESAFE_API_KEY=...`.
- Access is early access and is up to typesafe.ai. If you cannot create a
  key, ask them via https://typesafe.ai/contact. Meanwhile `--stub` runs the
  flow with an offline label matcher (no network, no calibrated
  probabilities).
- Pricing and limits: see https://docs.typesafe.ai.
- What is sent: each decision sends a request to
  https://api.typesafe.ai/v1/systemone. It holds:
  - the page URL and title;
  - the page text from a DOM snapshot;
  - each candidate element's label, role and current value, so the values
    in form fields are sent too;
  - the whole step script as text, plus the current step and its value;
  - the last 5 actions.

  No screenshots or pixels are sent. The script masks the value of a `fill`
  step whose field name looks like a password, secret, token, PIN or OTP,
  and of a `type` step with `secret: true`.
  Apart from that, the default target masks only `type=password` inputs.
  Tokens or personal data that the page shows in its text, URL or fields
  go to Jev unmasked, and into `jev-trace.jsonl` in `--out`. ck-browser
  reads the key from the `TYPESAFE_API_KEY` environment variable only.

## steps.yaml reference

A script is a small YAML subset (or JSON with the same shape): an optional
`url:` to open first, then `steps:`, one `verb: argument` per step.

```yaml
url: https://example.com/login
steps:
  - fill: { field: "Email", value: "me@example.com" }
  - click: "Continue"
  - expect: "Welcome back"
  - checkpoint: signed-in
```

| verb | argument | what happens |
| --- | --- | --- |
| `open` | URL | navigate, wait for load |
| `click` | label or description | Jev picks the element among clickable ones; click at its centre |
| `fill` | `{field, value}` | Jev picks the input by label or placeholder; type the value. Password-like fields are masked in all output |
| `select` | `{field, value}` | Jev picks the dropdown; the option is chosen by its text. Works on native selects, and on custom ones by clicking the control and then the option it shows |
| `upload` | `{field, path}` | Jev picks the file input; sets the file. `path` must exist |
| `hover` | label | Jev picks the element; move the mouse to its centre |
| `drag` | `{from, to}` | two Jev picks, then press, move and release |
| `press` | key name (`Escape`, `Enter`, `ArrowDown`), a letter or digit, modifiers (`Shift+Tab`) | key event to the page, no decision |
| `type` | text, or `{text, secret: true}` | types into whatever has focus, no decision |
| `scroll` | `up` / `down` | one viewport, no decision |
| `wait` | milliseconds | sleep, no decision |
| `expect` | text | Jev judges the page text; pass when p(verbatim) ≥ `--expect-threshold` (0.6) |
| `expect_count` | `{label: n}` | Jev marks each candidate as an instance or not; pass when the count matches |
| `checkpoint` | name | screenshot `NN-<name>.png`, plus console errors and failed requests |
| `capture_secret` | `{field, keychain_service}` | `--target ext` only (macOS): stores a value the page shows into the Keychain |

Labels are matched by Jev, not literally: describe the element the way a
user reads it. A "none of these" option is always offered, so a label that
is not on the page fails with "no matching element" and the top candidates,
instead of a wrong click.

Tips:

- Put an `expect` after every navigation or dialog: it doubles as the wait.
- Expect text that only the new state shows. Text already on the page
  passes whether or not the action worked.
- `expect` only proves presence; there is no negative assertion.

Command line:

```
bin/ck-browser run <steps.yaml> --out <dir> [flags]
bin/ck-browser doctor        # our Chrome and the key present? ffmpeg for --video?
bin/ck-browser stop          # close our Chrome
```

| flag | meaning |
| --- | --- |
| `--json` | print run.json instead of the text report |
| `--step-timeout <s>` | seconds per step (default 60) |
| `--headed` | show the Chrome window when launching it |
| `--stub` | offline label matcher instead of Jev |
| `--video` | record the run: `run.webm` (needs `ffmpeg` on PATH, else JPEG frames in `frames/`) plus a per-step `filmstrip.html` |
| `--chrome-port <n>` | fixed CDP port when launching our Chrome (default: Chrome picks a free one); ck-browser then writes `DevToolsActivePort` itself, after the owner check |
| `--profile-dir <dir>` | Chrome profile dir, for parallel runs that must not share cookies |

`--out` is created private (0700). It holds the Jev trace with the full
page text of every decision, so keep it out of shared directories.

Exit codes: `0` all steps passed, `1` a step failed, `2` script, Chrome or
Jev error.

`--video` records the tab with Chrome's screencast. Chrome sends a frame
only when the page paints, so the video keeps the run's real timing,
pauses included. With `ffmpeg` on PATH (VP9, else VP8) the frames become
`run.webm`; without it they stay as `frames/00001.jpg…` with
`frames/index.json` (`file`, `ts`, `duration_s` per frame). Every step that
ran gets a frame: its checkpoint or failure screenshot, else a new
`NN-<step>.png`. `filmstrip.html` shows the video and one row per step
(text, verdict, time, error, frame). It is a single file with no scripts or
remote assets; open it from `--out`. A video problem never fails the run.

## run.json

Trimmed example:

```json
{
  "script_path": "examples/login.yaml",
  "url": "https://the-internet.herokuapp.com/login",
  "started_at": "2026-10-18T10:00:00+00:00",
  "error": null,
  "steps": [
    {
      "n": 3, "step": "click: Login", "verb": "click", "status": "pass", "error": null,
      "target": {"index": 4, "label": "Login", "role": "button", "p": 0.97},
      "candidates": [{"label": "Login", "p": 0.97}, {"label": "Logout", "p": 0.02}],
      "none_p": 0.01, "fallback": null,
      "screenshot": null, "console_errors": [], "failed_requests": [],
      "ms": 812, "timed_out": false, "frame": null
    }
  ],
  "totals": {
    "wall_ms": 6400, "steps": 7, "passed": 7, "failed": 0,
    "jev_requests": 6, "jev_ms": 2100, "input_tokens": 9800, "output_tokens": 0,
    "cdp_calls": 140, "cost_usd": 0.000412
  },
  "chrome": {"port": 53817, "ws": "ws://127.0.0.1:53817/devtools/browser/..."},
  "ext_profile": null,
  "settings": {"settle_tries": 20, "step_timeout": 60},
  "model": "jev-latest",
  "video": null, "filmstrip": null
}
```

A step's `status` is `pass`, `fail` or `skipped` (steps after a failure). A
failed pick adds `near_misses`: the page elements whose words came closest,
with why each was not offered. A run that dies early still writes run.json,
with top-level `error` set.

With `--video`, `video` is `{path, reason, encoder, frames, frames_dir,
duration_s}`: `path` is the `run.webm`, or null with a `reason` (`ffmpeg
not found`, `ffmpeg failed: …`, `no frames`) and the frames in
`frames_dir`. `filmstrip` is the page's path. Each step's `frame` is its
picture; `frame_withheld: true` means none was taken because the page may
show a secret (`--target ext`).

## Use it from your coding agent

Install the skill for Claude Code, Codex, Cursor, Gemini CLI, Copilot and
other agents:

```sh
npx skills add luv-agarwal/ck-browser
```

Or paste this prompt into your agent:

```
Install ck-browser with `uv tool install git+https://github.com/luv-agarwal/ck-browser` and run `ck-browser doctor`. Write a steps.yaml for the flow I describe, run it with `ck-browser run steps.yaml --out /tmp/ck-run --json`, and read the JSON result. If a step fails, use its error, candidates and near_misses to fix the step, and rerun.
```

Or add this to your AGENTS.md or CLAUDE.md:

```markdown
## Browser checks

- Verify web flows (logins, forms, a page that must show some text) with ck-browser.
- Write the flow as steps.yaml and run `ck-browser run steps.yaml --out /tmp/ck-run --json`.
- In the JSON, each step has a `status`, the picked `target` with its `p`, and `near_misses` when a pick failed. Screenshots are in `--out`.
- Exit code 0 means every step passed, 1 a step failed, 2 a script, Chrome or Jev error.
- Without TYPESAFE_API_KEY, add `--stub` to run an offline label matcher.
```

For agents and CI: `--json` prints run.json to stdout and silences the
step lines. The exit code tells the result (`0`, `1` or `2`, as above), and
run.json and the screenshots stay in `--out`. A failed pick lists its
`near_misses`, so the step can be fixed without opening the page.

## Its own Chrome

ck-browser launches and reuses its own Chrome, with its own profile under
`CK_BROWSER_HOME` (default `~/.ck-browser`). It refuses port 9222, the
usual port of your own Chrome: it never attaches to your everyday browser.
Logins in its profile persist between runs. An idle watchdog closes this
Chrome after 5 minutes without a client; the next run starts it again.

Security of this Chrome:

- Chrome picks a free debugging port and writes it into the profile.
  ck-browser connects only after it checks that the process listening on
  that port is the Chrome that owns the profile, running as you. The
  debugging port has no authentication: while this Chrome runs, any local
  process that finds the port can drive it.
- The profile is created private (0700). Chrome runs with
  `--use-mock-keychain --password-store=basic`, so its cookies and saved
  passwords are encrypted with a fixed key. File permissions are the only
  protection for them: anyone who can read the profile can read your
  logins. Do not loosen its permissions, and do not copy the profile.
- The profile turns off Chrome's leaked-password check. A breached demo
  password would otherwise open a dialog that blocks all input. This
  applies to this profile only, never to your own Chrome.

| variable | meaning |
| --- | --- |
| `TYPESAFE_API_KEY` | the Jev key |
| `TYPESAFE_MODEL` | Jev model name (default `jev-latest`) |
| `CK_BROWSER_HOME` | ck-browser's own files (default `~/.ck-browser`); the Chrome profile is `<home>/chrome-profile` |
| `CK_BROWSER_PROFILE_DIR` | override the Chrome profile dir |
| `CK_BROWSER_CHROME` | path to the Chrome binary |
| `CK_BROWSER_PLUGINS` | comma-separated module names to load as plugins (optional) |

## Limits

- Custom dropdowns built from scratch are weak. A step that names an option
  it has not seen yet can pick the wrong element, and a miss can cost up to
  `--step-timeout` (60 s by default). Open the dropdown in its own step and
  `expect` the option first, or lower `--step-timeout` while you write the
  script.
- It is a step runner, not a sandbox. Run it only on pages and scripts you
  trust. Treat a step file like a script, because it has the same power:
  `open` can load `file://` pages, whose text then goes to Jev, and
  `upload` can send any file you can read to a site.
- No visual judgment, no `alert`/`confirm` dialogs, no frames, no network
  mocking, one tab per run.

## Optional: drive your own Chrome

`--target ext` (macOS) drives one tab of your everyday Chrome through a
small extension, for flows that need your real logins. It is the optional
part of this release; see [extension/README.md](extension/README.md).
`--video` records no screencast there (its frames would skip the
extension's check for a page that shows a secret); the filmstrip still has
each step's frame.

## Licence

MIT, see [LICENSE](LICENSE).

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security:
[SECURITY.md](SECURITY.md). Agent guide: [AGENTS.md](AGENTS.md).

Jev is typesafe.ai's model and service. This project is not affiliated with
typesafe.ai.

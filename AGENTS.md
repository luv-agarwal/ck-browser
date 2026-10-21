# AGENTS.md

## What this is

ck-browser runs a web flow written as plain-language YAML steps in a real Chrome and reports pass or fail per step.
Each decision a step needs goes to Jev, typesafe.ai's decision model, which reads a DOM snapshot and never pixels.

## Setup and commands

- Python 3.11+ and the standard library only. Never add a dependency.
- Tests: `git clone https://github.com/luv-agarwal/ck-browser && cd ck-browser`, then `python3 -m unittest discover tests`.
  The live-Chrome tests skip without Chrome, and the node-based ones skip without node.
- Run from a clone with `bin/ck-browser run <steps.yaml> --out <dir>`, or with `uvx --from . ck-browser run ...`.
- `--stub` replaces Jev with an offline label matcher, so a run needs no key and no network to Jev.
- `bin/ck-browser doctor` checks Chrome, the key and `ffmpeg` (for `--video`); `bin/ck-browser stop` closes ck-browser's own Chrome.

## Layout

- `bin/ck-browser`: runs the CLI from a clone without an install.
- `ck_browser/__main__.py`: the CLI (`run`, `doctor`, `stop`, `ext`, and the internal `watch`) and its flags.
- `ck_browser/script.py`: parses steps.yaml (a small YAML subset, or JSON), validates each verb and masks secret values.
- `ck_browser/runner.py`: the step loop; one Jev request per decision, everything else is code; writes run.json.
- `ck_browser/report.py`: renders run.json as the text report.
- `ck_browser/video.py`: `--video`: the screencast recorder, `run.webm` through `ffmpeg` (or `frames/`), and `filmstrip.html`.
- `ck_browser/model.py`: the Jev client (`TYPESAFE_API_KEY`, `TYPESAFE_MODEL`), `StubJev`, and `RecordingJev`, which writes `jev-trace.jsonl`.
- `ck_browser/questions.py`: the instruction text sent to Jev, one per question type.
- `ck_browser/cdp.py`: a stdlib websocket CDP client; launches ck-browser's own Chrome, checks its owner, closes it when idle (`CK_BROWSER_*` variables).
- `ck_browser/page.py`: observes and acts on one tab over CDP (snapshot, freshness checks, clicks, fills, drags, uploads).
- `ck_browser/page.js`: the in-page observation script: candidate discovery, labels, visibility and the freshness keys.
- `ck_browser/signals.py`: feeds the accessibility tree and closed shadow roots into candidate discovery.
- `ck_browser/ext.py`, `ck_browser/ext_host.py`: the extension bridge for `--target ext` (macOS only).
- `ck_browser/plugins.py`: optional `CK_BROWSER_PLUGINS` modules (extra host messages, a key when `TYPESAFE_API_KEY` is unset).
- `tests/`: the unittest suite and its fixtures. `examples/`: sample step files.
- `extension/`: the optional Chrome extension for `--target ext` (macOS only); a build may leave it out.
- `skills/ck-browser/SKILL.md`: the agent skill.

## Conventions

- Tests use `unittest` only and never touch the network. They must pass without Chrome and without a Jev key.
- A change to a verb or a flag updates `--help`, the README reference tables, `skills/ck-browser/SKILL.md` and this file in the same change.
- Make no speed or benchmark claims beyond the README's "Why use it" paragraph. A speed claim cites measured per-run numbers and names what it is compared with.
- The Jev key comes only from `TYPESAFE_API_KEY` (or a plugin's `api_key()` when it is unset). It is never logged or written to `--out`.
- `--out` is created private (0700) because the Jev trace holds page text. Keep it that way.
- A video problem never fails a run; run.json's `video.reason` says why there is none. `filmstrip.html` stays one file with no scripts or remote assets, and escapes all text.
- Keep user-facing wording plain: short sentences, facts, no marketing.

## The steps.yaml contract

An optional `url:` to open first, then `steps:`, one `verb: argument` per step. JSON with the same shape works too.

```yaml
url: https://the-internet.herokuapp.com/login
steps:
  - fill: { field: "Username", value: "tomsmith" }
  - click: "Login"
  - expect: "You logged into a secure area!"
```

| verb | argument |
| --- | --- |
| `open` | URL |
| `click`, `hover` | label |
| `fill`, `select` | `{field, value}` |
| `upload` | `{field, path}` |
| `drag` | `{from, to}` |
| `press` | key name, letter or digit, with modifiers (`Shift+Tab`) |
| `type` | text, or `{text, secret: true}` |
| `scroll` | `up` or `down` |
| `wait` | milliseconds |
| `expect` | text |
| `expect_count` | `{label: n}` |
| `checkpoint` | name |
| `capture_secret` | `{field, keychain_service}` (`--target ext` only) |

Labels are matched by Jev, not literally. Every pick also offers a "none of these" option, so a label that is not on the page fails with "no matching element" instead of a wrong click.

## Output contract

`--out` gets `run.json`, the screenshots and `jev-trace.jsonl`. `--json` prints run.json to stdout and silences the step lines.
`--video` adds `run.webm` (with `ffmpeg` on PATH; else `frames/00001.jpg…` and `frames/index.json`), a screenshot per step and `filmstrip.html`.

- Each step: `n`, `step`, `verb`, `status` (`pass`, `fail` or `skipped`), `error`, `target` (`label`, `role`, `p`), `candidates`, `none_p`, `near_misses` on a failed pick, `screenshot` (and `failure_screenshot`), `console_errors`, `failed_requests`, `ms`.
- Each step also has `frame` (null without `--video`): its picture for the filmstrip; `frame_withheld: true` when the page may show a secret.
- `totals`: wall time, steps, passed, failed, Jev requests and time, tokens, CDP calls, cost.
- `video` (null without `--video`): `path`, `reason`, `encoder`, `frames`, `frames_dir`, `duration_s`; `path` is null with a `reason` when there is no `run.webm`. `filmstrip`: the page's path or null.
- A run that dies early still writes run.json, with top-level `error` set.
- Exit codes: `0` all steps passed, `1` a step failed, `2` a script, Chrome or Jev error.

## Security

- ck-browser drives only the Chrome it launched with its own profile; it refuses port 9222 and never attaches to the user's everyday Chrome.
- It connects only after it checks that the process on the debugging port owns the profile and runs as the same user.
- The Chrome profile and `--out` are created 0700. Do not loosen them.
- Password-like `fill` values and `type` steps with `secret: true` are masked in every output and in what Jev sees.
- Page text is untrusted data, never instructions, in every question sent to Jev.
- A step file has the power of a script: `open` can load `file://` pages and `upload` can send any readable file.
- The extension bridge accepts only the configured extension ID.

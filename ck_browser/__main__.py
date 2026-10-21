"""CLI: `python3 -m` this package, or the bin/ck-browser shim: run|doctor|stop|ext|watch."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .cdp import (IDLE_CLOSE, Cdp, CdpError, StalePage, browser_ws, default_profile_dir, ensure_chrome,
                  release_chrome, watch, ws_port)
from .ext import (EXTENSION_DIR, EXTENSION_ID, NO_PROFILES, ExtCdp, Refusal, close_tab, default_profile,
                  install, policy_words, profile_label, profiles, profiles_dir, request_domain, resolve,
                  revoke_domain)
from .model import Jev, JevError, RecordingJev, StubJev, api_key_from_env
from .report import format_report
from .runner import DEFAULT_STEP_TIMEOUT, run
from .script import ScriptError, load_script
from .video import ffmpeg_version

PROFILE_HELP = "Chrome profile: its directory (Profile 14), signed-in email, or display name"
NO_KEY = "no Jev API key: set TYPESAFE_API_KEY, or pass --stub to run the offline label matcher"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ck-browser", description="Run a browser step script over CDP.")
    parser.add_argument("--version", action="version", version=f"ck-browser {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="execute a step script")
    runner.add_argument("script")
    runner.add_argument("--out", required=True, help="directory for run.json, screenshots and the Jev trace")
    _chrome_flags(runner)
    runner.add_argument("--target", choices=["chrome", "ext"], default="chrome",
                        help="chrome: our own Chrome; ext: a tab of your Chrome via the extension"
                             " (handed over, or opened at the script's url)")
    runner.add_argument("--profile", help=PROFILE_HELP + " (--target ext)")
    runner.add_argument("--no-launch", action="store_true", help="fail instead of starting Chrome")
    runner.add_argument("--threshold", type=float, default=0.6)
    runner.add_argument("--expect-threshold", type=float, default=0.6)
    runner.add_argument("--ready-threshold", type=float, default=0.5)
    runner.add_argument("--settle-tries", type=int, default=20)
    runner.add_argument("--step-timeout", type=float, default=DEFAULT_STEP_TIMEOUT,
                        help="seconds a step may take before it fails (default 60); a wait step adds"
                             " its own milliseconds")
    runner.add_argument("--keep-tab", action="store_true")
    runner.add_argument("--stub", action="store_true", help="offline label matcher instead of Jev")
    runner.add_argument("--json", action="store_true", help="print run.json instead of the text report")
    runner.add_argument("--video", action="store_true",
                        help="record the run: run.webm (needs ffmpeg on PATH, else JPEG frames in frames/)"
                             " plus a per-step filmstrip.html")

    doctor = sub.add_parser("doctor", help="check our Chrome instance and the Jev key")
    _chrome_flags(doctor)

    stopper = sub.add_parser("stop", help="close our Chrome instance")
    _profile_flag(stopper)

    ext = sub.add_parser("ext", help="the extension bridge to a tab of your own Chrome")
    ext.add_argument("action", choices=["install", "status", "open", "close", "allow", "revoke"])
    ext.add_argument("arg", nargs="?", help="the url for open, the domain for allow or revoke")
    ext.add_argument("--profile", help=PROFILE_HELP)
    ext.add_argument("--extension-id", help="install: the ID Chrome shows for the extension (default:"
                                            " from the manifest key, else from the extension dir's path)")

    watcher = sub.add_parser("watch", help="internal: close our Chrome once it sits idle")
    watcher.add_argument("--profile-dir", required=True)
    watcher.add_argument("--chrome-pid", type=int)
    watcher.add_argument("--idle", type=int, default=IDLE_CLOSE)

    args = parser.parse_args(argv)
    if args.command == "run" and args.step_timeout <= 0:
        parser.error("--step-timeout must be more than 0 seconds")
    if args.command == "run" and args.profile and args.target != "ext":
        parser.error("--profile picks a profile of your own Chrome: it needs --target ext"
                     " (--profile-dir is our Chrome's user data dir)")
    if args.command == "ext" and args.action in ("open", "allow", "revoke") and not args.arg:
        parser.error(f"ext {args.action} needs a {'url' if args.action == 'open' else 'domain'}")
    if args.command == "ext" and args.action == "install" and args.profile:
        parser.error("ext install takes no --profile: it sets up the host for every profile")
    if args.command == "ext" and args.extension_id is not None:
        if args.action != "install":
            parser.error("--extension-id is for ext install")
        if not EXTENSION_ID.fullmatch(args.extension_id):
            parser.error("--extension-id is 32 letters a-p, as chrome://extensions shows it")
    return {"run": _run, "doctor": _doctor, "stop": _stop, "ext": _ext,
            "watch": _watch}[args.command](args)


def _chrome_flags(parser):
    parser.add_argument("--chrome-port", type=int, default=0,
                        help="fixed CDP port for a launch (default 0: Chrome picks a free one)")
    _profile_flag(parser)
    parser.add_argument("--headed", action="store_true", help="show the window when launching Chrome")


def _profile_flag(parser):
    parser.add_argument("--profile-dir", help=f"Chrome user data dir (default {default_profile_dir()})")


def _run(args):
    path = Path(args.script)
    try:
        script = load_script(path)
    except (OSError, ScriptError, ValueError) as exc:
        print(f"ck-browser: {exc}", file=sys.stderr)
        return 2
    script["path"] = str(path.resolve())
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.stub:
        client = StubJev()
    else:
        key = api_key_from_env()
        if not key:
            print(f"ck-browser: {NO_KEY}", file=sys.stderr)
            return 2
        client = Jev(key)
    quiet = (lambda _line: None) if args.json else print
    if args.headed and args.target == "chrome" and _chrome_is_up(args.profile_dir):
        print(f"ck-browser: reusing the Chrome already running on {args.profile_dir or default_profile_dir()}"
              " — --headed only applies to a launch", file=sys.stderr)
    try:
        report = run(
            script, out_dir, jev=RecordingJev(client, out_dir), chrome_port=args.chrome_port,
            profile_dir=args.profile_dir, headless=not args.headed, launch=not args.no_launch,
            threshold=args.threshold, expect_threshold=args.expect_threshold,
            ready_threshold=args.ready_threshold,
            settle_tries=args.settle_tries, keep_tab=args.keep_tab, log=quiet, target=args.target,
            step_timeout=args.step_timeout, ext_profile=args.profile, video=args.video,
        )
    except (CdpError, StalePage, JevError, ScriptError, OSError) as exc:
        print(f"ck-browser: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.target == "chrome" and not args.keep_tab:  # a custom-profile Chrome closes with its run
            release_chrome(args.profile_dir)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report, steps=False))  # the step lines were printed as they ran
    return 1 if report["totals"]["failed"] else 0


def _chrome_is_up(profile_dir):
    try:
        browser_ws(profile_dir)
    except CdpError:
        return False
    return True


def _doctor(args):
    state = "reused"
    try:
        try:
            ws = browser_ws(args.profile_dir)
        except CdpError:
            ws = ensure_chrome(port=args.chrome_port, profile_dir=args.profile_dir,
                               headless=not args.headed)
            state = "launched"
        cdp = Cdp(ws)
        browser = cdp.call("Browser.getVersion")["product"]
        targets = cdp.call("Target.getTargets")["targetInfos"]
        cdp.close()
    except CdpError as exc:
        print(f"ck-browser: {exc}", file=sys.stderr)
        return 2
    finally:
        if state == "launched":  # a doctor on a custom profile must not leave Chrome behind
            release_chrome(args.profile_dir)
    pages = [target for target in targets if target["type"] == "page"]
    print(f"chrome: {state} on port {ws_port(ws)} — {browser}")
    print(f"profile: {args.profile_dir or default_profile_dir()}")
    print(f"pages: {len(pages)}")
    print(f"jev key: {'present' if api_key_from_env() else 'missing'}")
    version = ffmpeg_version()
    print(f"ffmpeg: found {version}" if version else "ffmpeg: not found (video falls back to frames)")
    return 0


def _stop(args):
    try:
        ws = browser_ws(args.profile_dir)
        cdp = Cdp(ws)
    except CdpError as exc:
        print(f"ck-browser: {exc}", file=sys.stderr)
        return 2
    try:
        cdp.call("Browser.close")
    except CdpError:  # Chrome drops the socket as it exits, often before answering
        pass
    cdp.close()
    print(f"chrome: closed on port {ws_port(ws)}")
    return 0


def _watch(args):
    watch(args.profile_dir, chrome_pid=args.chrome_pid, idle=args.idle)
    return 0


def _ext(args):
    if args.action == "install":
        try:
            installed = install(args.extension_id)
        except CdpError as exc:  # a bad CK_BROWSER_EXTENSION_ID, or no extension to take the ID from
            print(exc)
            return 1
        print(f"extension id: {installed['id']}")
        print(f"host manifest: {installed['manifest']}")
        print("Load unpacked: chrome://extensions → Developer mode → Load unpacked →"
              f" {EXTENSION_DIR}, then click its icon: the popup hands over the tab and sets the"
              " domains ck-browser may open its own tabs on")
        print("If chrome://extensions shows another ID for it, run ext install --extension-id <that ID>")
        return 0
    try:
        return {"status": _ext_status, "open": _ext_open, "close": _ext_close, "allow": _ext_allow,
                "revoke": _ext_revoke}[args.action](args)
    except CdpError as exc:
        print(exc)
        return 2 if args.action in ("allow", "revoke") and isinstance(exc, Refusal) else 1


def _ext_status(args):
    """Read from the sidecars, never dialed: it answers while a run holds a tab."""
    entries = [resolve(args.profile)] if args.profile else profiles()
    if not entries:
        raise CdpError(NO_PROFILES.format(dir=profiles_dir()))
    for entry in entries:
        print(f"profile: {profile_label(entry)}")
        print(f"  {_tab_line(entry['tab'])}" if entry["tab"] else "  no tab claimed")
        enabled, allowed, pending = policy_words(entry["open"])
        print(f"  agents may open tabs: {enabled}; allowed: {allowed}; pending: {pending}")
    if not args.profile:
        default = default_profile(entries)
        print("default (no --profile): " + (profile_label(default) if default
                                            else "none — several profiles connected; pass --profile"))
    return 0 if any(entry["tab"] for entry in entries) else 1


def _acting_on(args):
    """The socket of the profile the action goes to, named first so no one guesses which."""
    entry = resolve(args.profile)
    print(f"profile: {profile_label(entry)}")
    return entry["socket"]


def _ext_open(args):
    cdp = ExtCdp(_acting_on(args), open_url=args.arg)
    cdp.close()
    if not cdp.opened_now:  # a claimed tab is never swapped for a new one
        print(f"busy: a tab is already claimed — {_tab_line(cdp.tab_info)}")
        return 1
    print(_tab_line(cdp.tab_info))
    return 0


def _ext_close(args):
    close_tab(_acting_on(args))
    print("closed")
    return 0


def _ext_allow(args):
    if request_domain(args.arg, _acting_on(args))["status"] == "allowed":
        print(f"allowed: {args.arg}")
        return 0
    print("pending: approve in the tab Chrome just opened, or in the ck-browser popup")
    return 1


def _ext_revoke(args):
    revoked = revoke_domain(args.arg, _acting_on(args))["status"] == "revoked"
    print(f"{'revoked' if revoked else 'not allowed'}: {args.arg}")
    return 0


def _tab_line(tab):
    opened = " (opened by ck-browser)" if tab.get("opened") else ""  # absent before extension 0.3.0
    return f"tab: {tab['title']} — {tab['url']}{opened}"


if __name__ == "__main__":
    sys.exit(main())

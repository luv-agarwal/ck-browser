"""ck-browser extension bridge: framing, the native-messaging host, ExtCdp/ExtTab, install."""

import base64
import contextlib
import hashlib
import io
import json
import os
import pwd
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ck_browser import __main__ as cli
from ck_browser import ext as ext_module
from ck_browser import page as page_module
from ck_browser import runner as runner_module
from ck_browser.cdp import CdpError
from ck_browser.ext import (BUSY, CAPTURE_TIMEOUT, EXISTS, EXTENSION_DIR, HOST_MODULE, LOADED,
                                      MAX_FRAME, NO_BRIDGE, NO_PROFILES, NO_TAB, ROOT, UNKNOWN_PROFILE,
                                      ExtCdp, ExtTab, FrameError, bridge_state, close_tab, extension_id,
                                      extension_origin, our_extension_id, profiles, read_frame,
                                      request_domain, resolve, revoke_domain, unpacked_extension_id,
                                      write_frame)
from ck_browser.model import RecordingJev, StubJev
from ck_browser.page import TARGET_JS, act
from ck_browser.runner import run

EXTENSION_ID = "cellfbmommdcpmpdgmejbfoiejncgeck"
TAB = {"id": 7, "url": "https://shop.example/cart", "title": "Cart"}
OPEN = {"enabled": True, "domains": ["shop.example", "*.cloudflare.com"], "pending": []}
HELLO = {"type": "hello", "tab": TAB, "open": OPEN}
OPENED = {"id": 9, "url": "https://shop.example/new", "title": "", "opened": True}
REFUSED = "refused: evil.example is not an allowed domain; ask with: bin/ck-browser ext allow evil.example"
NODE = shutil.which("node")
# What a revealed key could look like: punctuation (no quote or backslash) so no masking rule
# but the captured-value one hides it; it must never show up outside the fake Keychain.
VALUE = "Zq8!vR2#xK9$mW4%tB7&yN3(pL6)"
SERVICE = "backup-r2-secret-access-key"
ACCOUNT = pwd.getpwuid(os.getuid()).pw_name
EMAIL = "alex@example.com"
# Chrome's Local State: profile directory -> display name and the email it is signed in as.
LOCAL_STATE = {"profile": {"info_cache": {
    "Profile 14": {"name": "work.example", "user_name": "Alex@Example.com"},
    "Profile 6": {"name": "Personal", "user_name": "alex.home@example.com"},
    "Default": {"name": "Person 1", "user_name": ""},
}}}
WORK = 'Profile 14 "work.example" <alex@example.com>'

# Stands in for /usr/bin/security: logs argv and stdin, keeps items as files, echoes its input.
FAKE_SECURITY = """#!{python}
import json, os, sys
home = os.environ["FAKE_KEYCHAIN"]
args = sys.argv[1:]
with open(os.path.join(home, "argv.log"), "a") as log:
    log.write(json.dumps(args) + "\\n")
def item(words):
    return os.path.join(home, "item-" + words[words.index("-a") + 1] + "-" + words[words.index("-s") + 1])
if args == ["-i"]:
    data = sys.stdin.read()
    with open(os.path.join(home, "stdin.log"), "a") as log:
        log.write(data)
    print("security> " + data)
    for words in (line.split() for line in data.splitlines()):
        if words[:1] == ["add-generic-password"] and not os.environ.get("FAKE_KEYCHAIN_DROP"):
            with open(item(words), "w") as handle:
                handle.write(words[words.index("-w") + 1])
    sys.exit(0)
if args[:1] == ["find-generic-password"]:
    if not os.path.exists(item(args)):
        sys.exit(44)
    if "-w" in args:
        print(open(item(args)).read())
    sys.exit(0)
sys.exit(1)
"""


def temp_dir(test):
    # Under /tmp: a unix socket path must stay under ~104 bytes on macOS.
    path = Path(tempfile.mkdtemp(prefix="ckx-", dir="/tmp"))
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


def fake_keychain(test, existing=False, drop=False):
    """(home, env for the host): a fake `security` whose items live under `home`."""
    home = temp_dir(test)
    fake = home / "security"
    fake.write_text(FAKE_SECURITY.format(python=sys.executable))
    fake.chmod(0o700)
    if existing:
        (home / f"item-{ACCOUNT}-{SERVICE}").write_text("an older key")
    env = {"CK_BROWSER_SECURITY": str(fake), "FAKE_KEYCHAIN": str(home)}
    if drop:
        env["FAKE_KEYCHAIN_DROP"] = "1"
    return home, env


def fake_chrome(test, local_state=LOCAL_STATE):
    """A Chrome user-data dir holding `local_state` as its Local State."""
    path = temp_dir(test)
    (path / "Local State").write_text(json.dumps(local_state))
    return path


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class Trickle:
    """A binary stream that hands out at most three bytes per read."""

    def __init__(self, data):
        self.data = data

    def readinto(self, view):
        n = min(3, len(view), len(self.data))
        view[:n], self.data = self.data[:n], self.data[n:]
        return n


# The id of the keychain write FakeExtension sends as a barrier: the host refuses its value at
# once, without running security, and only after every frame sent before it.
BARRIER = -1


class FakeExtension:
    """One Chrome profile's end of the native port: spawns the host, says hello, answers every call.
    `instance` is the profile's id; `email` the account it is signed in as (None: unknown)."""

    def __init__(self, test, answer=None, origin=None, env=None, dir=None, instance="i1", email=None):
        self.dir = dir or temp_dir(test)
        self.instance = instance
        self.email = email
        self.socket = self.dir / "profiles" / f"{instance}.sock"
        self.sidecar = self.dir / "profiles" / f"{instance}.json"
        self.calls = []
        self.answer = answer or (lambda call: {"id": call["id"], "result": {"echo": call["method"]}})
        self.lock = threading.Lock()
        self.bound = threading.Event()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", HOST_MODULE, origin or extension_origin()], cwd=ROOT,
            # Never the real Keychain: a host that tried to write without a fake fails to run it.
            # Nor the real Local State: no profile is known unless a test makes a fake Chrome dir.
            env={**os.environ, "CK_BROWSER_BRIDGE_DIR": str(self.dir),
                 "CK_BROWSER_SECURITY": "/nonexistent/ck-browser-test-security",
                 "CK_BROWSER_CHROME_DIR": "/nonexistent/ck-browser-test-chrome", **(env or {})},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        test.addCleanup(self.stop)
        threading.Thread(target=self._serve, daemon=True).start()

    def send(self, message):
        with self.lock:
            write_frame(self.proc.stdin, message)

    def _serve(self):
        try:
            while (call := read_frame(self.proc.stdout)) is not None:
                if call.get("type") == "keychain_result" and call["id"] == BARRIER:
                    self.bound.set()
                    continue
                self.calls.append(call)
                reply = self.answer(call)
                if reply is not None:
                    self.send(reply)
        except (OSError, ValueError):
            pass  # stopped under us

    def state(self, open=OPEN, **extra):
        """What background.js posts: the policy, the profile's instance and identity."""
        profile = None if self.email is None else {"email": self.email}
        return {"type": "state", "open": open, "instance": self.instance, "worker": "w1",
                "lastDisconnect": None, "profile": profile, **extra}

    def ready(self, tab=TAB):
        """The policy, then a claimed tab."""
        self.send(self.state())
        self.send({"type": "hello", "tab": tab})
        return self._bound()

    def idle(self, open=OPEN):
        """The policy and no claimed tab."""
        self.send(self.state(open))
        return self._bound()

    def _bound(self):
        """Once the host answers the barrier it listens and holds what was sent before it."""
        self.send({"type": "keychain_write", "id": BARRIER, "service": "ck-browser-test", "value": "x"})
        if not self.bound.wait(10):
            self.proc.kill()
            raise AssertionError(f"host never bound {self.socket}: {self.proc.stderr.read()!r}")
        return self

    def stop(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


# -- framing -------------------------------------------------------------------


class FrameTest(unittest.TestCase):
    def test_a_large_frame_survives_a_socket_that_delivers_it_in_pieces(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        message = {"id": 1, "result": {"data": "x" * (200 * 1024)}}
        buffer = io.BytesIO()
        write_frame(buffer, message)
        frame = buffer.getvalue()
        self.assertEqual(frame[:4], struct.pack("=I", len(frame) - 4))  # native byte order

        def trickle():
            for start in range(0, len(frame), 1000):
                left.sendall(frame[start:start + 1000])

        threading.Thread(target=trickle, daemon=True).start()
        self.assertEqual(read_frame(right), message)

    def test_frames_read_from_a_stream_of_short_reads_and_end_cleanly(self):
        buffer = io.BytesIO()
        write_frame(buffer, {"type": "hello", "tab": TAB})
        write_frame(buffer, {"id": 2, "result": {}})
        stream = Trickle(buffer.getvalue())
        self.assertEqual(read_frame(stream), {"type": "hello", "tab": TAB})
        self.assertEqual(read_frame(stream), {"id": 2, "result": {}})
        self.assertIsNone(read_frame(stream))

    def test_a_truncated_frame_is_an_error_not_an_end(self):
        buffer = io.BytesIO()
        write_frame(buffer, {"id": 3, "result": {}})
        with self.assertRaises(FrameError):
            read_frame(Trickle(buffer.getvalue()[:-2]))
        with self.assertRaises(FrameError):
            read_frame(Trickle(b"\x01\x00"))

    def test_oversize_frames_are_refused_both_ways(self):
        with self.assertRaises(FrameError):
            read_frame(Trickle(struct.pack("=I", MAX_FRAME + 1)))
        buffer = io.BytesIO()
        with self.assertRaises(FrameError) as caught:
            write_frame(buffer, {"expression": "x" * 2000}, limit=1000)
        self.assertIn("over the 1000-byte limit", str(caught.exception))
        self.assertEqual(buffer.getvalue(), b"")  # nothing half-sent


class ExtensionIdTest(unittest.TestCase):
    def test_the_id_is_the_first_128_bits_of_the_key_hash_spelled_a_to_p(self):
        key = json.loads((EXTENSION_DIR / "manifest.json").read_text()).get("key")
        if key is None:
            self.skipTest("this extension build has no manifest key: UnpackedIdTest covers its ID")
        digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
        self.assertEqual(extension_id(key), digest.translate(str.maketrans("0123456789abcdef",
                                                                           "abcdefghijklmnop")))
        self.assertEqual(extension_id(key), EXTENSION_ID)
        self.assertEqual(extension_origin(), f"chrome-extension://{EXTENSION_ID}/")


# -- the host, end to end over real pipes and a real socket ----------------------


class HostTest(unittest.TestCase):
    def connect(self, ext):
        cdp = ExtCdp(ext.socket, timeout=5)
        self.addCleanup(cdp.close)
        return cdp

    def test_the_cached_hello_reaches_each_client_and_calls_round_trip(self):
        ext = FakeExtension(self).ready()
        first = self.connect(ext)
        self.assertEqual(first.tab_info, TAB)
        self.assertEqual(first.call("Runtime.evaluate", {"expression": "1"}), {"echo": "Runtime.evaluate"})
        self.assertEqual(ext.calls, [{"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1"}}])
        first.close()
        self.assertTrue(wait_for(lambda: self._connects(ext)))  # the hello is cached, not used up

    def _connects(self, ext):
        try:
            cdp = ExtCdp(ext.socket, timeout=5)
        except CdpError:
            return False
        cdp.close()
        return cdp.tab_info == TAB

    def test_an_error_reply_raises_and_events_are_kept_without_a_session(self):
        ext = FakeExtension(self, answer=lambda call: {
            "id": call["id"], "error": {"message": "refused: the click lands on a frame"}}).ready()
        cdp = self.connect(ext)
        with self.assertRaises(CdpError) as caught:
            cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 1, "y": 2})
        self.assertEqual(str(caught.exception),
                         "Input.dispatchMouseEvent: refused: the click lands on a frame")
        ext.send({"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [{"value": "x"}]}})
        self.assertTrue(wait_for(lambda: cdp.events))
        [event] = cdp.take_events(None)
        self.assertEqual((event["method"], event["sessionId"]), ("Runtime.consoleAPICalled", None))
        self.assertIsNone(cdp.closed)

    def test_call_many_pipelines_over_the_bridge_and_matches_replies_out_of_order(self):
        held = []

        def answer(call):  # nothing is answered until every request has arrived, then last first
            held.append(call)
            if len(held) == 3:
                for c in reversed(held):
                    ext.send({"id": c["id"], "error": {"message": "No node with given id found"}}
                             if c["params"]["backendNodeId"] == 2 else
                             {"id": c["id"], "result": {"node": c["params"]["backendNodeId"]}})

        ext = FakeExtension(self, answer=answer).ready()
        cdp = self.connect(ext)
        results = cdp.call_many("DOM.resolveNode", [{"backendNodeId": n} for n in (1, 2, 3)])
        self.assertEqual((results[0], results[2]), ({"node": 1}, {"node": 3}))
        self.assertIsInstance(results[1], CdpError)
        self.assertEqual(str(results[1]), "DOM.resolveNode: No node with given id found")
        self.assertIsNone(cdp.closed)

    def test_the_human_stopping_fails_the_pending_call(self):
        ext = FakeExtension(self, answer=lambda call: {"type": "detached", "reason": "canceled_by_user"}).ready()
        cdp = self.connect(ext)
        started = time.perf_counter()
        with self.assertRaises(CdpError) as caught:
            cdp.call("Page.navigate", {"url": "https://shop.example/"})
        self.assertLess(time.perf_counter() - started, 4)
        self.assertEqual(str(caught.exception), "Page.navigate: the human stopped the run (canceled_by_user)")
        with self.assertRaises(CdpError):
            cdp.call("Runtime.evaluate")

    def test_a_second_client_is_busy_and_a_vanished_one_frees_the_tab(self):
        ext = FakeExtension(self, answer=lambda call: None).ready()
        raw = socket.socket(socket.AF_UNIX)
        raw.connect(str(ext.socket))
        self.assertEqual(read_frame(raw), HELLO)
        with self.assertRaises(CdpError) as caught:
            ExtCdp(ext.socket)
        self.assertEqual(str(caught.exception), BUSY)
        write_frame(raw, {"id": 1, "method": "Page.navigate", "params": {"url": "https://a.example/"}})
        raw.close()  # gone mid-call, its answer never coming
        self.assertTrue(wait_for(lambda: self._connects(ext)))

    def test_a_frame_over_chromes_1mb_limit_is_answered_not_sent(self):
        ext = FakeExtension(self).ready()
        cdp = self.connect(ext)
        with self.assertRaises(CdpError) as caught:
            cdp.call("Runtime.evaluate", {"expression": "x" * (1024 * 1024)})
        self.assertIn("over the 1048576-byte limit (Chrome's native-messaging limit)", str(caught.exception))
        self.assertEqual(cdp.call("Page.enable"), {"echo": "Page.enable"})
        self.assertEqual([call["method"] for call in ext.calls], ["Page.enable"])

    def test_permissions_and_cleanup_on_stdin_eof(self):
        ext = FakeExtension(self).ready()
        self.assertEqual(stat.S_IMODE(ext.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(ext.socket.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(ext.socket.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(ext.sidecar.stat().st_mode), 0o600)
        cdp = self.connect(ext)
        ext.proc.stdin.close()  # Chrome quit
        self.assertEqual(ext.proc.wait(5), 0)
        self.assertFalse(ext.socket.exists())
        self.assertFalse(ext.sidecar.exists())
        self.assertTrue(wait_for(lambda: cdp.closed))
        self.assertEqual(cdp.closed, "the bridge closed: Chrome or the extension went away")

    def test_a_caller_other_than_our_extension_is_refused(self):
        ext = FakeExtension(self, origin="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/")
        self.assertEqual(ext.proc.wait(10), 2)
        self.assertIn(b"refusing caller", ext.proc.stderr.read())
        self.assertFalse(ext.socket.exists())


class ProfileHostTest(unittest.TestCase):
    """Each Chrome profile's host binds its own socket, named after the profile's instance."""

    def _state(self, path):
        try:
            return bridge_state(path)
        except CdpError:
            return None

    def test_two_profiles_are_reachable_at_once_each_with_its_own_tab(self):
        first = FakeExtension(self, instance="aaaa1111").ready()
        second = FakeExtension(self, dir=first.dir, instance="bbbb2222").ready(OPENED)
        one, two = ExtCdp(first.socket, timeout=5), ExtCdp(second.socket, timeout=5)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        self.assertEqual((one.tab_info, two.tab_info), (TAB, OPENED))
        self.assertEqual(two.call("Page.enable"), {"echo": "Page.enable"})
        self.assertEqual(first.calls, [])  # the other profile's tab is never touched
        self.assertEqual([call["method"] for call in second.calls], ["Page.enable"])
        self.assertEqual(sorted(path.name for path in (first.dir / "profiles").iterdir()),
                         ["aaaa1111.json", "aaaa1111.sock", "bbbb2222.json", "bbbb2222.sock"])

    def test_a_host_binds_on_its_first_valid_state_and_keeps_that_instance(self):
        ext = FakeExtension(self)
        ext.send({"type": "hello", "tab": TAB})  # an adopted claim may come before the state
        ext.send({**ext.state(), "instance": "../escape"})
        ext.send({**ext.state(), "instance": None})
        ext._bound()
        self.assertEqual(list((ext.dir / "profiles").iterdir()), [])
        ext.send(ext.state())
        ext.send({**ext.state(), "instance": "other"})  # a host serves one profile
        ext.bound.clear()
        ext._bound()
        self.assertEqual(sorted(path.name for path in (ext.dir / "profiles").iterdir()), ["i1.json", "i1.sock"])
        self.assertEqual(bridge_state(ext.socket), {"tab": TAB, "open": OPEN})
        log = (ext.dir / "host.log").read_text()
        self.assertIn("state ignored: instance '../escape' is not 1-36 letters, digits or dashes", log)
        self.assertIn("state ignored: instance 'None' is not 1-36 letters, digits or dashes", log)
        self.assertIn(f"instance i1: listening on {ext.socket}", log)
        self.assertIn("state ignored: instance other, but this host serves i1", log)

    def test_a_reconnected_profiles_new_host_keeps_its_socket_when_the_old_one_stops(self):
        old = FakeExtension(self).ready()
        new = FakeExtension(self, dir=old.dir).idle()  # the extension's port reopened
        self.assertEqual(json.loads(new.sidecar.read_text())["pid"], new.proc.pid)
        old.send({"type": "detached", "reason": "host_disconnected"})  # heard before its stdin ends
        old.proc.stdin.close()
        self.assertEqual(old.proc.wait(5), 0)
        self.assertEqual(bridge_state(new.socket), {"tab": None, "open": OPEN})
        self.assertEqual(json.loads(new.sidecar.read_text())["pid"], new.proc.pid)
        self.assertIn(f"pid {old.proc.pid} superseded: a newer host serves instance i1",
                      (old.dir / "host.log").read_text())
        new.proc.stdin.close()
        self.assertEqual(new.proc.wait(5), 0)
        self.assertEqual(list((old.dir / "profiles").iterdir()), [])

    def test_the_sidecar_names_the_profile_from_local_state_and_follows_the_claim(self):
        chrome = fake_chrome(self)
        ext = FakeExtension(self, email=EMAIL, env={"CK_BROWSER_CHROME_DIR": str(chrome)}).ready()
        self.assertEqual(json.loads(ext.sidecar.read_text()), {
            "instance": "i1", "pid": ext.proc.pid, "socket": str(ext.socket), "email": EMAIL,
            "profile_dir": "Profile 14", "name": "work.example", "tab": TAB, "open": OPEN})
        self.assertEqual(stat.S_IMODE(ext.sidecar.stat().st_mode), 0o600)
        ext.send({"type": "detached", "reason": "released_by_user"})
        self.assertTrue(wait_for(lambda: json.loads(ext.sidecar.read_text())["tab"] is None))
        ext.send({"type": "hello", "tab": OPENED})
        self.assertTrue(wait_for(lambda: json.loads(ext.sidecar.read_text())["tab"] == OPENED))
        moved = {"enabled": False, "domains": [], "pending": ["x.example"]}
        ext.send(ext.state(moved))
        self.assertTrue(wait_for(lambda: json.loads(ext.sidecar.read_text())["open"] == moved))
        self.assertEqual([path.name for path in (ext.dir / "profiles").iterdir() if path.suffix == ".tmp"], [])

    def test_an_unknown_identity_leaves_the_profile_null(self):
        chrome = fake_chrome(self)
        for email, instance in ((None, "nobody"), ("", "signedout"), ("else@example.com", "elsewhere")):
            ext = FakeExtension(self, email=email, instance=instance,
                                env={"CK_BROWSER_CHROME_DIR": str(chrome)}).idle()
            entry = json.loads(ext.sidecar.read_text())
            self.assertEqual((entry["email"], entry["profile_dir"], entry["name"]),
                             (email or None, None, None))
        corrupt = temp_dir(self)
        (corrupt / "Local State").write_text("{not json")
        ext = FakeExtension(self, email=EMAIL, env={"CK_BROWSER_CHROME_DIR": str(corrupt)}).idle()
        self.assertEqual(json.loads(ext.sidecar.read_text())["profile_dir"], None)

    def test_a_dead_hosts_socket_file_is_replaced(self):
        dir = temp_dir(self)
        (dir / "profiles").mkdir()
        (dir / "profiles" / "i1.sock").write_text("left by a host that was killed")
        ext = FakeExtension(self, dir=dir).ready()
        self.assertEqual(bridge_state(ext.socket)["tab"], TAB)

    def test_the_log_records_the_worker_the_claim_and_why_the_host_stopped(self):
        ext = FakeExtension(self)
        ext.send(ext.state(lastDisconnect="Native host has exited."))
        ext.send(ext.state())  # logged once
        ext.send({"type": "hello", "tab": TAB, "adopted": True})
        ext._bound()
        ext.send({"type": "detached", "reason": "canceled_by_user"})
        ext.proc.stdin.close()
        self.assertEqual(ext.proc.wait(5), 0)
        lines = [line.split(" ", 4)[4] for line in (ext.dir / "host.log").read_text().splitlines()]
        self.assertEqual(lines, [
            "start: parent pid " + str(os.getpid()),
            "extension: instance i1 worker w1; its previous host port closed: Native host has exited.",
            f"instance i1: listening on {ext.socket}",
            "claim: tab 7 re-adopted after a worker restart",
            "release: canceled_by_user", "stop: stdin closed: the extension's port is gone"])
        self.assertEqual(stat.S_IMODE((ext.dir / "host.log").stat().st_mode), 0o600)

    def test_the_log_rolls_over_past_its_cap(self):
        dir = temp_dir(self)
        (dir / "host.log").write_text("x" * (ext_module.LOG_MAX + 1))
        with mock.patch.dict(os.environ, {"CK_BROWSER_BRIDGE_DIR": str(dir)}), \
                contextlib.redirect_stderr(io.StringIO()):
            ext_module.log("after the roll")
        self.assertEqual((dir / "host.log.1").stat().st_size, ext_module.LOG_MAX + 1)
        self.assertTrue((dir / "host.log").read_text().endswith(f"pid {os.getpid()} after the roll\n"))


class ResolveTest(unittest.TestCase):
    """Which profile a client drives: read from the sidecars alone, never another than the one named."""

    def setUp(self):
        self.bridge = temp_dir(self)
        (self.bridge / "profiles").mkdir()
        env = mock.patch.dict(os.environ, {"CK_BROWSER_BRIDGE_DIR": str(self.bridge),
                                           "CK_BROWSER_CHROME_DIR": str(fake_chrome(self))})
        env.start()
        self.addCleanup(env.stop)

    def sidecar(self, instance, directory=None, name=None, email=None, tab=None, pid=None, socket_file=True):
        path = self.bridge / "profiles" / f"{instance}.sock"
        if socket_file:
            path.write_text("")  # only its presence is read
        entry = {"instance": instance, "pid": pid or os.getpid(), "socket": str(path), "email": email,
                 "profile_dir": directory, "name": name, "tab": tab, "open": OPEN}
        (self.bridge / "profiles" / f"{instance}.json").write_text(json.dumps(entry))
        return entry

    def several(self):
        return (self.sidecar("work", "Profile 14", "work.example", EMAIL),
                self.sidecar("pers", "Profile 6", "Personal", "alex.home@example.com"),
                self.sidecar("ab12cd34"))

    def error(self, profile=None):
        with self.assertRaises(CdpError) as caught:
            resolve(profile)
        return str(caught.exception)

    CONNECTED = (f'connected: {WORK}; Profile 6 "Personal" <alex.home@example.com>;'
                 f" instance ab12cd34 {UNKNOWN_PROFILE}")

    def test_none_connected(self):
        self.assertEqual(self.error(), NO_PROFILES.format(dir=self.bridge / "profiles"))
        self.assertIn("reload the extension at chrome://extensions", self.error())

    def test_the_only_connected_profile_is_the_default_but_never_stands_in_for_another(self):
        work = self.sidecar("work", "Profile 14", "work.example", EMAIL)
        self.assertEqual(resolve(), work)
        self.assertEqual(self.error("Profile 6"), 'Chrome profile "Profile 6" is not connected (never falling'
                         f' back to another profile); connected: {WORK} — Profile 6 "Personal" exists but its'
                         " ck-browser extension is not connected: open a window of that profile and install or"
                         " reload the extension there")

    def test_several_without_a_flag_list_every_connected_profile(self):
        self.several()
        self.assertEqual(self.error(), "several Chrome profiles are connected: pass --profile"
                         f" <directory|email|name>; {self.CONNECTED}")

    def test_several_with_exactly_one_claimed_tab_pick_it(self):
        _work, pers, _unknown = self.several()
        pers = self.sidecar("pers", "Profile 6", "Personal", "alex.home@example.com", tab=TAB)
        self.assertEqual(resolve(), pers)
        self.sidecar("ab12cd34", tab=OPENED)
        self.assertIn("several Chrome profiles are connected", self.error())

    def test_the_flag_matches_directory_email_name_or_instance_in_any_case(self):
        work, pers, unknown = self.several()
        for name in ("Profile 14", "profile 14", EMAIL.upper(), "Work.Example", "WORK"):
            self.assertEqual(resolve(name), work, name)
        self.assertEqual(resolve("personal"), pers)
        self.assertEqual(resolve("AB12CD34"), unknown)

    def test_an_unknown_profile_never_falls_back_and_names_one_that_exists_unconnected(self):
        self.several()
        self.assertEqual(self.error("Travel"), 'Chrome profile "Travel" is not connected (never falling back to'
                         f" another profile); {self.CONNECTED}")
        self.assertTrue(self.error("person 1").endswith(
            ' — Default "Person 1" exists but its ck-browser extension is not connected: open a window of'
            " that profile and install or reload the extension there"))

    def test_a_name_two_connected_profiles_share_asks_for_the_directory_or_email(self):
        self.sidecar("one", "Profile 1", "Work", "a@example.com")
        self.sidecar("two", "Profile 2", "Work", "b@example.com")
        self.assertEqual(self.error("work"), '--profile "work" matches several connected profiles:'
                         ' Profile 1 "Work" <a@example.com>; Profile 2 "Work" <b@example.com>;'
                         " use the directory or email")

    def test_a_dead_host_or_a_missing_socket_is_skipped_and_left_alone(self):
        work = self.sidecar("work", "Profile 14", "work.example", EMAIL)
        self.sidecar("dead", "Profile 6", "Personal", "alex.home@example.com", pid=99999999)
        self.sidecar("nosock", socket_file=False)
        self.assertEqual(profiles(), [work])
        self.assertEqual(resolve(), work)
        self.assertTrue((self.bridge / "profiles" / "dead.json").exists())
        self.assertIn("Profile 6", self.error("Profile 6"))  # stale is not connected

    def test_every_entry_point_dials_the_resolved_profile(self):
        ext = FakeExtension(self, dir=self.bridge, instance="work", email=EMAIL,
                            answer=lambda call: {"id": call["id"], "result": {"status": "revoked"}}).idle()
        self.assertEqual(bridge_state(profile="work"), {"tab": None, "open": OPEN})
        self.assertEqual(revoke_domain("shop.example", profile="work"), {"status": "revoked"})
        self.assertEqual(ext.calls, [{"id": 0, "method": "ck.revokeDomain", "params": {"domain": "shop.example"}}])
        with self.assertRaises(CdpError):
            bridge_state(profile="Profile 6")


class KeychainHostTest(unittest.TestCase):
    """The host writes what the extension captured; the value goes nowhere else."""

    def write(self, **keychain):
        home, env = fake_keychain(self, **keychain)
        ext = FakeExtension(self, answer=lambda call: None, env=env).ready()
        client = socket.socket(socket.AF_UNIX)
        self.addCleanup(client.close)
        client.connect(str(ext.socket))
        self.assertEqual(read_frame(client), HELLO)
        ext.send({"type": "keychain_write", "id": 1, "service": SERVICE, "value": VALUE})
        self.assertTrue(wait_for(lambda: any(c.get("type") == "keychain_result" for c in ext.calls), 15))
        [result] = [c for c in ext.calls if c.get("type") == "keychain_result"]
        client.settimeout(0.5)
        with self.assertRaises(TimeoutError):  # the client never hears of it
            read_frame(client)
        ext.proc.stdin.close()
        ext.proc.wait(5)
        self.assertNotIn(VALUE.encode(), ext.proc.stderr.read())
        argv = [json.loads(line) for line in (home / "argv.log").read_text().splitlines()]
        stdin = (home / "stdin.log").read_text() if (home / "stdin.log").exists() else None
        return result, argv, stdin

    def test_a_captured_value_goes_to_security_on_stdin_and_is_read_back(self):
        result, argv, stdin = self.write()
        self.assertEqual(result, {"type": "keychain_result", "id": 1, "ok": True})
        find = ["find-generic-password", "-a", ACCOUNT, "-s", SERVICE]
        self.assertEqual(argv, [find, ["-i"], [*find, "-w"]])
        self.assertNotIn(VALUE, json.dumps(argv))
        self.assertEqual(stdin, f"add-generic-password -a {ACCOUNT} -s {SERVICE} -w {VALUE}\n")

    def test_an_existing_item_is_never_overwritten(self):
        result, argv, stdin = self.write(existing=True)
        self.assertEqual(result, {"type": "keychain_result", "id": 1, "ok": False,
                                  "error": EXISTS.format(service=SERVICE, account=ACCOUNT)})
        self.assertIn(f"security delete-generic-password -a {ACCOUNT} -s {SERVICE}", result["error"])
        self.assertEqual(argv, [["find-generic-password", "-a", ACCOUNT, "-s", SERVICE]])
        self.assertIsNone(stdin)

    def test_a_write_the_keychain_did_not_keep_is_an_error_whatever_security_exits_with(self):
        result, _argv, _stdin = self.write(drop=True)
        self.assertEqual(result, {"type": "keychain_result", "id": 1, "ok": False,
                                  "error": f"the Keychain did not store {SERVICE}"})

    def test_a_value_security_cannot_take_is_refused_before_it_runs(self):
        home, env = fake_keychain(self)
        ext = FakeExtension(self, answer=lambda call: None, env=env).ready()
        ext.send({"type": "keychain_write", "id": 2, "service": SERVICE, "value": "has a space in it"})
        self.assertTrue(wait_for(lambda: ext.calls, 15))
        self.assertEqual(ext.calls, [{"type": "keychain_result", "id": 2, "ok": False, "error":
                                      "refused: the value has characters a Keychain capture does not take"}])
        self.assertFalse((home / "argv.log").exists())

    def test_a_client_cannot_forge_a_typed_message(self):
        ext = FakeExtension(self).ready()
        client = socket.socket(socket.AF_UNIX)
        self.addCleanup(client.close)
        client.connect(str(ext.socket))
        read_frame(client)  # the hello
        write_frame(client, {"type": "keychain_result", "id": 1, "ok": True})
        self.assertEqual(read_frame(client), {"id": 1, "error": {"message": "only CDP calls go over the bridge"}})
        self.assertEqual(ext.calls, [])


class ExtCdpTest(unittest.TestCase):
    def test_no_socket_means_the_profiles_host_went_away(self):
        path = temp_dir(self) / "i1.sock"
        with self.assertRaises(CdpError) as caught:
            ExtCdp(path)
        self.assertEqual(str(caught.exception), NO_BRIDGE.format(path=path, state="absent"))
        self.assertIn("reload the extension at chrome://extensions", str(caught.exception))

    def test_a_socket_left_by_a_dead_host_reads_as_stale(self):
        path = temp_dir(self) / "i1.sock"
        dead = socket.socket(socket.AF_UNIX)
        dead.bind(str(path))
        dead.close()
        with self.assertRaises(CdpError) as caught:
            ExtCdp(path)
        self.assertEqual(str(caught.exception), NO_BRIDGE.format(path=path, state="stale"))


def opens(call):
    """A fake extension's answer to ck.openTab: the new tab's hello, then the reply."""
    if call.get("method") == "ck.openTab":
        return ({"id": call["id"], "error": {"message": REFUSED}} if "evil" in call["params"]["url"]
                else {"id": call["id"], "result": {"tabId": OPENED["id"]}})
    return {"id": call["id"], "result": {"echo": call["method"]}}


class OpenTabTest(unittest.TestCase):
    """No tab claimed: the host says idle, and ck-browser may ask the extension for a new tab."""

    def extension(self):
        def answer(call):
            if call.get("method") == "ck.openTab" and "evil" not in call["params"]["url"]:
                ext.send({"type": "hello", "tab": OPENED})
            return opens(call)

        ext = FakeExtension(self, answer=answer).idle()
        return ext

    def state(self, ext):
        try:
            return bridge_state(ext.socket)
        except CdpError:  # busy: the previous client's hang-up is not seen yet
            return None

    def connects(self, ext, into):
        try:
            into.append(ExtCdp(ext.socket, timeout=5, open_url="https://shop.example/new"))
        except CdpError:  # busy, as above
            return False
        return True

    def test_an_idle_host_says_so_with_the_policy(self):
        ext = FakeExtension(self).idle()
        raw = socket.socket(socket.AF_UNIX)
        self.addCleanup(raw.close)
        raw.connect(str(ext.socket))
        self.assertEqual(read_frame(raw), {"type": "idle", "open": OPEN})

    def test_without_a_url_the_error_names_both_ways_and_the_policy(self):
        ext = FakeExtension(self).idle({"enabled": False, "domains": [], "pending": ["x.example"]})
        with self.assertRaises(CdpError) as caught:
            ExtCdp(ext.socket)
        self.assertEqual(str(caught.exception), NO_TAB.format(enabled="off", domains="none"))
        self.assertIn("choose Hand over", str(caught.exception))
        self.assertIn("give the script a url", str(caught.exception))
        self.assertEqual(ext.calls, [])

    def test_with_a_url_the_extension_opens_a_tab_and_later_clients_get_it_until_detached(self):
        ext = self.extension()
        cdp = ExtCdp(ext.socket, timeout=5, open_url="https://shop.example/new")
        self.addCleanup(cdp.close)
        self.assertTrue(cdp.opened_now)
        self.assertEqual(cdp.tab_info, OPENED)
        self.assertEqual(cdp.open, OPEN)
        self.assertEqual(ext.calls, [{"id": 0, "method": "ck.openTab", "params": {"url": "https://shop.example/new"}}])
        self.assertEqual(cdp.call("Page.enable"), {"echo": "Page.enable"})
        cdp.close()
        self.assertTrue(wait_for(lambda: self.state(ext) == {"tab": OPENED, "open": OPEN}))
        later = []
        self.assertTrue(wait_for(lambda: self.connects(ext, later)))
        self.assertFalse(later[0].opened_now)  # the claimed tab, not a second one
        later[0].close()
        ext.send({"type": "detached", "reason": "released_by_user"})
        self.assertTrue(wait_for(lambda: self.state(ext) == {"tab": None, "open": OPEN}))

    def test_a_refused_open_raises_the_extensions_own_words(self):
        ext = self.extension()
        with self.assertRaises(CdpError) as caught:
            ExtCdp(ext.socket, open_url="https://evil.example/")
        self.assertEqual(str(caught.exception), REFUSED)

    def test_allow_domain_and_close_tab_are_relayed(self):
        ext = FakeExtension(self, answer=lambda call: {"id": call["id"], "result": {"status": "pending"}}
                            if call["method"] == "ck.allowDomain" else opens(call)).ready(OPENED)
        self.assertEqual(request_domain("x.example", ext.socket), {"status": "pending"})
        self.assertEqual(ext.calls, [{"id": 0, "method": "ck.allowDomain", "params": {"domain": "x.example"}}])
        self.assertTrue(wait_for(lambda: self.state(ext) is not None))
        close_tab(ext.socket)
        self.assertEqual(ext.calls[-1], {"id": 0, "method": "ck.closeTab", "params": {}})

    def test_close_on_an_idle_bridge_asks_nothing(self):
        ext = FakeExtension(self).idle()
        with self.assertRaises(CdpError) as caught:
            close_tab(ext.socket)
        self.assertEqual(str(caught.exception), "no tab is claimed; nothing to close")
        self.assertEqual(ext.calls, [])

    def test_a_tab_that_left_the_allowed_domains_fails_the_run_naming_the_host(self):
        ext = FakeExtension(self, answer=lambda call: {
            "type": "detached", "reason": "left_allowed_domains", "host": "accounts.google.com"}).ready(OPENED)
        cdp = ExtCdp(ext.socket, timeout=5)
        self.addCleanup(cdp.close)
        with self.assertRaises(CdpError) as caught:
            cdp.call("Runtime.evaluate")
        self.assertEqual(str(caught.exception), "Runtime.evaluate: the tab left the allowed domains, so the"
                         " extension released it (ask with: bin/ck-browser ext allow accounts.google.com)")


# -- the human's tab ----------------------------------------------------------------


class RecordingCdp:
    """Answers like the claimed tab: a 1440x789 viewport, or a stopped run when `fail`."""

    calls = 0
    opened_now = False

    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []
        self.closed = False

    def call(self, method, params=None, session_id=None, timeout=None):
        self.sent.append((method, params or {}, session_id))
        if self.fail:
            raise CdpError(f"{method}: the human stopped the run (canceled_by_user)")
        if method != "Runtime.evaluate":
            return {}
        if params["expression"] == "[innerWidth, innerHeight]":
            return {"result": {"value": [1440, 789]}}
        return {"result": {"value": {"url": "https://shop.example/cart", "title": "Cart", "text": "Cart",
                                     "actions": [], "marker": ["m"], "scroll": {"y": 0}}}}

    def take_events(self, session_id):
        return []

    def close(self):
        self.closed = True


class ExtTabTest(unittest.TestCase):
    def test_open_uses_the_real_viewport_and_restores_it_instead_of_overriding(self):
        cdp = RecordingCdp()
        tab = ExtTab.open(cdp)
        self.assertEqual((tab.width, tab.height), (1440, 789))
        self.assertEqual([method for method, _, _ in cdp.sent],
                         ["Page.enable", "Runtime.enable", "Log.enable", "Network.enable",
                          "Runtime.evaluate", "Emulation.setFocusEmulationEnabled"])
        self.assertEqual({session for _, _, session in cdp.sent}, {None})
        cdp.sent.clear()
        tab.set_viewport(1440, 5000)
        tab.set_viewport(1440, 789)
        self.assertEqual([method for method, _, _ in cdp.sent],
                         ["Emulation.setDeviceMetricsOverride", "Emulation.clearDeviceMetricsOverride"])

    def test_close_hands_the_tab_back_and_never_closes_it(self):
        cdp = RecordingCdp()
        tab = ExtTab.open(cdp)
        cdp.fail = True
        cdp.sent.clear()
        tab.close()  # a tab the human already took back must not fail cleanup
        self.assertEqual(cdp.sent, [("Emulation.clearDeviceMetricsOverride", {}, None),
                                    ("Emulation.setFocusEmulationEnabled", {"enabled": False}, None)])

    def test_a_withheld_screenshot_is_none_and_any_other_failure_still_raises(self):
        class Shy(RecordingCdp):
            error = "screenshot withheld: the page shows a secret"

            def call(self, method, params=None, session_id=None, timeout=None):
                if method == "Page.captureScreenshot":
                    raise CdpError(f"{method}: {self.error}")
                return super().call(method, params, session_id, timeout)

        out = temp_dir(self)
        cdp = Shy()
        tab = ExtTab.open(cdp)
        self.assertIsNone(tab.screenshot_png(out / "a.png"))
        cdp.error = "screenshot withheld: the page could not be checked"
        self.assertIsNone(tab.screenshot_png(out / "a.png"))
        self.assertFalse((out / "a.png").exists())
        cdp.error = "Target closed"
        with self.assertRaises(CdpError):
            tab.screenshot_png(out / "a.png")

    def test_capture_secret_asks_the_extension_and_waits_for_the_keychain(self):
        class Capturing(RecordingCdp):
            def call(self, method, params=None, session_id=None, timeout=None):
                if method == "ck.captureSecret":
                    self.sent.append((method, params, timeout))
                    return {"service": params["service"], "length": 64, "stored": True}
                return super().call(method, params, session_id, timeout)

        cdp = Capturing()
        tab = ExtTab.open(cdp)
        self.assertEqual(tab.capture_secret(6, SERVICE), {"service": SERVICE, "length": 64, "stored": True})
        self.assertEqual(cdp.sent[-1], ("ck.captureSecret", {"node": 6, "service": SERVICE},
                                        CAPTURE_TIMEOUT))
        with self.assertRaises(ValueError):
            tab.capture_secret("6", SERVICE)

    def test_check_write_asks_the_extension_about_the_observed_node(self):
        cdp = RecordingCdp()
        tab = ExtTab.open(cdp)
        tab.check_write(6)
        self.assertEqual(cdp.sent[-1], ("ck.checkNode", {"node": 6}, None))
        with self.assertRaises(ValueError):
            tab.check_write("6")
        self.assertEqual(len([call for call in cdp.sent if call[0] == "ck.checkNode"]), 1)

    def test_a_select_is_checked_before_its_scripted_write_and_a_refusal_stops_it(self):
        class Tab:
            def __init__(self, refusal=None):
                self.refusal, self.done, self.after_input = refusal, [], None

            def check_write(self, node):
                self.done.append(("check_write", node))
                if self.refusal:
                    raise CdpError(f"ck.checkNode: {self.refusal}")

            def evaluate(self, expression, await_promise=False, command_line_api=False):
                self.done.append(("write", json.loads(expression[len(TARGET_JS) + 1:-1])["value"]))
                return {"x": 1, "y": 2}

        select = {"id": "e1", "node": 5, "kind": "select", "role": "combobox", "label": "Expiry month → 04",
                  "value": "04"}
        with mock.patch.object(page_module, "fresh", return_value=True):
            tab = Tab()
            act(tab, select, {})
            self.assertEqual(tab.done, [("check_write", 5), ("write", "04")])
            tab = Tab("refused: the element is a payment-card field")
            with self.assertRaisesRegex(CdpError, "payment-card field"):
                act(tab, select, {})
        self.assertEqual(tab.done, [("check_write", 5)])

    def test_run_on_the_ext_target_reports_the_bridge_and_always_hands_back(self):
        out = temp_dir(self)
        cdp, asked, lines = RecordingCdp(), [], []
        entry = {"instance": "work", "pid": 1, "socket": "/tmp/work.sock", "email": EMAIL,
                 "profile_dir": "Profile 14", "name": "work.example", "tab": TAB, "open": OPEN}
        with mock.patch.object(runner_module, "resolve", lambda profile: asked.append(profile) or entry), \
                mock.patch.object(runner_module, "ExtCdp", lambda path, open_url=None: asked.append(path) or cdp):
            run({"steps": []}, out, jev=object(), target="ext", keep_tab=True, log=lines.append,
                ext_profile="profile 14")
        self.assertEqual(asked, ["profile 14", "/tmp/work.sock"])
        self.assertEqual(lines[0], f"profile: {WORK}")
        report = json.loads((out / "run.json").read_text())
        self.assertEqual(report["chrome"]["ws"], "ext:/tmp/work.sock")
        self.assertEqual(report["ext_profile"], {"profile_dir": "Profile 14", "name": "work.example",
                                                 "email": EMAIL, "instance": "work"})
        self.assertEqual(cdp.sent[-1][0], "Emulation.setFocusEmulationEnabled")
        self.assertNotIn("Target.closeTarget", [method for method, _, _ in cdp.sent])
        self.assertTrue(cdp.closed)


    def test_a_tab_the_extension_opened_at_the_url_is_not_navigated_again(self):
        class Opened(RecordingCdp):
            opened_now = True

            def call(self, method, params=None, session_id=None, timeout=None):
                if method == "Runtime.evaluate" and params["expression"] == LOADED:
                    self.sent.append((method, params, session_id))
                    return {"result": {"value": "complete"}}
                return super().call(method, params, session_id, timeout)

        out = temp_dir(self)
        cdp, asked = Opened(), []
        entry = {"instance": "i1", "socket": "/tmp/i1.sock", "email": None, "profile_dir": None, "name": None}
        with mock.patch.object(runner_module, "resolve", lambda profile: entry), \
                mock.patch.object(runner_module, "ExtCdp", lambda path, open_url=None: asked.append(open_url) or cdp):
            run({"url": "https://shop.example/new", "steps": []}, out, jev=object(), target="ext",
                log=lambda _line: None)
        self.assertEqual(asked, ["https://shop.example/new"])
        methods = [(method, (params or {}).get("expression")) for method, params, _ in cdp.sent]
        self.assertNotIn("Page.navigate", [method for method, _ in methods])
        self.assertEqual(methods[0], ("Runtime.evaluate", LOADED))  # before the enables and the viewport read


class CaptureOverTheBridgeTest(unittest.TestCase):
    def test_a_capture_run_leaves_no_trace_of_the_value_in_its_output(self):
        """ck-browser run -> host -> a fake extension that reads VALUE and hands it to the host,
        which writes it with a fake security. Only the fake Keychain may ever hold it."""
        home, env = fake_keychain(self)
        url = "https://dash.cloudflare.com/0123456789abcdef0123456789abcdef/r2/api-tokens"
        rect = {"x": 10, "y": 10, "w": 300, "h": 20}
        page = {"url": url, "title": "R2", "text": "Secret Access Key\n[hidden secret, 40 chars]",
                "actions": [{"id": "e1", "node": 5, "kind": "click", "role": "text", "rect": rect,
                             "label": "Secret Access Key", "value": ""},
                            {"id": "e2", "node": 6, "kind": "click", "role": "text", "rect": rect,
                             "label": "[hidden secret, 40 chars]", "value": ""},
                            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}],
                "marker": ["m"], "page_key": ["k"], "guards": {}, "scroll": {"y": 0, "height": 800}}
        written = threading.Event()

        def capture(call):
            ext.send({"type": "keychain_write", "id": 1, "service": call["params"]["service"], "value": VALUE})
            written.wait(15)
            [result] = [c for c in ext.calls if c.get("type") == "keychain_result"]
            ext.send({"id": call["id"], "result": {"service": SERVICE, "length": len(VALUE), "stored": True}}
                     if result["ok"] else {"id": call["id"], "error": {"message": result["error"]}})

        def answer(call):
            if call.get("type") == "keychain_result":
                written.set()
                return None
            method = call["method"]
            if method == "ck.captureSecret":
                threading.Thread(target=capture, args=(call,), daemon=True).start()
                return None
            if method == "Page.captureScreenshot":
                return {"id": call["id"], "error": {"message": "screenshot withheld: the page shows a secret"}}
            if method == "Runtime.evaluate":
                expression = call["params"]["expression"]
                value = [1280, 800] if expression == "[innerWidth, innerHeight]" else page
                return {"id": call["id"], "result": {"result": {"value": value}}}
            return {"id": call["id"], "result": {}}

        ext = FakeExtension(self, answer=answer, env=env).ready()
        out = temp_dir(self)
        script = {"steps": [
            {"verb": "capture_secret", "arg": {"field": "the hidden secret", "keychain_service": SERVICE},
             "raw": f"capture_secret: {{field: the hidden secret, keychain_service: {SERVICE}}}"},
            {"verb": "checkpoint", "arg": "keys", "raw": "checkpoint: keys"},
        ]}
        lines = []
        with mock.patch.dict(os.environ, {"CK_BROWSER_BRIDGE_DIR": str(ext.dir)}):
            report = run(script, out, jev=RecordingJev(StubJev(), out), target="ext", log=lines.append)
        capture, checkpoint = report["steps"]
        self.assertEqual(capture["status"], "pass", capture["error"])
        self.assertEqual(capture["secret"], {"service": SERVICE, "length": len(VALUE), "stored": True})
        self.assertEqual(capture["observations"]["url"], url)  # the account id stays readable
        self.assertEqual((checkpoint["screenshot"], checkpoint["screenshot_withheld"]), (None, True))
        [call] = [c for c in ext.calls if c.get("method") == "ck.captureSecret"]
        self.assertEqual(call["params"], {"node": 6, "service": SERVICE})
        self.assertEqual(lines[0], f"profile: instance i1 {UNKNOWN_PROFILE}")
        self.assertIn(f"captured → keychain {SERVICE} ({len(VALUE)} chars)", lines[1])
        self.assertIn(VALUE, (home / "stdin.log").read_text())  # it really was written
        self.assertEqual(sorted(path.name for path in out.iterdir()), ["jev-trace.jsonl", "run.json"])
        for path in out.iterdir():
            self.assertNotIn(VALUE.encode(), path.read_bytes(), path)
        self.assertNotIn(VALUE, "\n".join(lines))
        ext.proc.stdin.close()
        ext.proc.wait(5)
        self.assertNotIn(VALUE.encode(), ext.proc.stderr.read())


@unittest.skipUnless(NODE, "node is not on PATH: the extension's masking runs in JavaScript")
class MaskTest(unittest.TestCase):
    """extension/mask.js, run through node: what every string leaving the browser becomes."""

    def mask(self, *cases, how="deep"):
        """Each case is (value, captured values); returns what Mask[how] makes of each."""
        script = ("const Mask = require(process.argv[1]);"
                  "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
                  f"process.stdout.write(JSON.stringify(cases.map(([v, c]) => Mask.{how}(v, new Set(c)))));")
        result = subprocess.run([NODE, "-e", script, str(EXTENSION_DIR / "mask.js")], input=json.dumps(cases),
                                capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    def test_the_extension_scripts_parse(self):
        for name in ("background.js", "mask.js", "popup.js", "approve.js"):
            subprocess.run([NODE, "--check", str(EXTENSION_DIR / name)], check=True, timeout=30)

    def test_secret_like_tokens_are_hidden_and_words_and_short_ids_are_not(self):
        self.assertEqual(self.mask(
            ("key AKIAabcdefghij0123456789 here", []),
            ("abcdefghijklmnopqrstuvwxyz 123456789012345678901234 e12 scroll_down", []),
            ("see https://dash.cloudflare.com/0123456789abcdef0123456789abcdef/r2", []),
        ), ["key [hidden secret, 24 chars] here",
            "abcdefghijklmnopqrstuvwxyz 123456789012345678901234 e12 scroll_down",
            "see https://dash.cloudflare.[hidden secret, 39 chars]"])  # in text, a path is a token

    def test_a_url_keeps_its_path_and_loses_secrets_in_its_query_and_fragment(self):
        account = "0123456789abcdef0123456789abcdef"
        [masked] = self.mask((f"https://dash.cloudflare.com/{account}/r2/overview"
                              "?token=abcdefghij0123456789xyz&page=2#k=0123456789abcdefghijk", []))
        self.assertEqual(masked, f"https://dash.cloudflare.com/{account}/r2/overview"
                                 "?[hidden secret, 29 chars]&page=2#[hidden secret, 23 chars]")

    def test_a_captured_value_is_hidden_everywhere_even_in_a_url_path(self):
        self.assertEqual(self.mask((f"the key: {VALUE}.", [VALUE]),
                                   (f"https://x.example/{VALUE}/edit", [VALUE])),
                         [f"the key: [hidden secret, {len(VALUE)} chars].",
                          f"https://x.example/[hidden secret, {len(VALUE)} chars]/edit"])

    def test_chromes_own_ids_and_every_non_string_pass_through(self):
        [masked] = self.mask(({"objectId": "abcdefghij0123456789abc", "requestId": "ABCDEF0123456789ABCDEF0123456789",
                               "result": {"value": ["abcdefghij0123456789abc", 7, True, None, 1.5]}}, []))
        self.assertEqual(masked, {"objectId": "abcdefghij0123456789abc",
                                  "requestId": "ABCDEF0123456789ABCDEF0123456789",
                                  "result": {"value": ["[hidden secret, 23 chars]", 7, True, None, 1.5]}})

    def test_a_snapshot_without_secrets_passes_unchanged(self):
        """Markers, page keys, guards and action ids are compared verbatim by the runner."""
        semantics = [{"node": 12, "role": "button", "label": "Checkout", "kind": "click", "value": "", "id": "e1"}]
        snapshot = {
            "url": "https://shop.example/cart?page=2", "title": "Cart", "text": "Cart\nCheckout", "w": 1280,
            "h": 800, "scroll": {"y": 0, "height": 800}, "omitted_actions": 0,
            "actions": [{**semantics[0], "rect": {"x": 1, "y": 2, "w": 3, "h": 4}},
                        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
                        {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}],
            "marker": [1717171717171.123, "https://shop.example/cart?page=2", 0, 0, 1280, 800, "Cart",
                       "Cart\nCheckout", semantics, [[12, "", None, -1, False, False]]],
            "page_key": [1717171717171.123, "https://shop.example/cart?page=2", 0, 0, 1280, 800,
                         [[12, "", None, -1, False, False]]],
            "guards": {"12": [12, "button", "Checkout", None, None, None, None, False, None, None, None,
                              None, None, "Cart Checkout"]},
        }
        self.assertEqual(self.mask((snapshot, [])), [snapshot])

    def test_masking_is_deterministic_and_keeps_node_ids_for_capture(self):
        """The runner compares a masked marker with a later masked one, and capture_secret names a
        node by the id the masked snapshot carried."""
        token = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
        snapshot = {"actions": [{"id": "e1", "node": 6, "role": "text", "label": token[:40]}],
                    "guards": {"6": [6, "text", token[:40], None]}, "text": f"Secret Access Key\n{token}"}
        once, again = self.mask((snapshot, []), (snapshot, []))
        self.assertEqual(once, again)
        self.assertEqual(once["actions"][0], {"id": "e1", "node": 6, "role": "text",
                                              "label": "[hidden secret, 40 chars]"})
        self.assertEqual(list(once["guards"]), ["6"])
        self.assertEqual(self.mask((once, [])), [once])  # masking a masked value changes nothing
        # Once captured, the value masks the same way it did as a token: markers stay stable.
        self.assertEqual(self.mask((snapshot, [token])), [once])

    def test_shows_finds_a_captured_value_or_a_token_anywhere(self):
        self.assertEqual(self.mask(("Your cart\nCheckout", []), (f"key {VALUE}", [VALUE]),
                                   ("https://x.example/?t=abcdefghij0123456789abc", []), how="shows"),
                         [False, True, True])


# Loads background.js in a vm context with a stub `chrome`, then either answers each of `calls`
# through `answer()` or plays `steps` against the running worker. Every Runtime.evaluate (PROBE)
# runs in a tiny fake page: `elements` are observed nodes by id, `active` the focused one,
# `closed` maps a host to the closed root whose activeElement it names; `body` is the page's
# innerText and `closed_text` maps a host to the text of its closed root; `broken` makes the
# evaluate itself fail, `unattachable` the attach; `attached` lists the tabs getTargets says the
# debugger is attached to. Other methods get `raw`. A step is
# ["host", message] (from the host over the port), ["page", message] (from the popup),
# ["disconnect"] (the host dies), ["alarm"], ["detach", tabId] (Cancel on Chrome's bar),
# ["updated", [tabId, changeInfo]], ["eval", expression] or ["together", [step, ...]] (all
# started in the same tick); each records what went to the host, what chrome.* was asked, the claim
# and the storage (local, and session as `session`) after it. `session` seeds session storage: what
# a worker Chrome stopped left behind. `page.identity` is what chrome.identity.getProfileUserInfo
# answers; without it there is no chrome.identity (an extension without the permission).
BACKGROUND_HARNESS = r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const dir = process.argv[1];
const { page, calls, steps, storage, session: seeded } = JSON.parse(fs.readFileSync(0, "utf8"));
const els = {};
for (const [id, s] of Object.entries(page.elements || {})) els[id] = {
  tagName: s.tag, id: "", isConnected: s.connected !== false, labels: [],
  matches: sel => sel.split(",").includes(s.tag.toLowerCase()),
  getAttribute: name => (s.attrs || {})[name] ?? null, getRootNode: () => ({}),
};
const closed = new WeakMap();
for (const [host, inner] of Object.entries(page.closed || {})) closed.set(els[host], { activeElement: els[inner] });
for (const [host, text] of Object.entries(page.closed_text || {}))
  closed.set(els[host], { textContent: text, querySelectorAll: () => [] });
const document = { activeElement: els[page.active] || null, title: "Cart" };
if ("body" in page) Object.assign(document, { body: { innerText: page.body }, querySelectorAll: () => Object.values(els) });
const pageContext = vm.createContext({
  window: { __ckPage: { byId: new Map(Object.entries(els).map(([id, e]) => [Number(id), e])), closedRoots: closed } },
  document, location: { href: "https://shop.example/cart" },
});
let raw = {}, sent = [], log = [], nextTab = 100;
const copy = value => JSON.parse(JSON.stringify(value));
const event = () => { const fns = []; return { fns, addListener: fn => fns.push(fn) }; };
const store = { ...(storage || {}) }, session = { ...(seeded || {}) }, ports = [], gone = new Set();
const pick = (from, keys) => copy(Object.fromEntries([].concat(keys).filter(key => key in from).map(key => [key, from[key]])));
const onChanged = event(), onMessage = event(), onAlarm = event(), onDetach = event(), onUpdated = event();
const onCommand = event();
const chrome = {
  runtime: {
    getManifest: () => ({ action: { default_title: "ck" } }),
    getURL: name => `chrome-extension://ck/${name}`,
    onMessage,
    connectNative: host => {
      const port = { posted: [], onMessage: event(), onDisconnect: event(), postMessage(m) {
        if (this.dead) throw new Error("Attempting to use a disconnected port object");
        this.posted.push(copy(m)); log.push(["post", copy(m)]);
      } };
      ports.push(port);
      log.push(["connectNative", host]);
      return port;
    },
  },
  storage: {
    local: {
      get: async keys => pick(store, keys),
      set: async items => { Object.assign(store, copy(items)); onChanged.fns.forEach(fn => fn(copy(items), "local")); },
    },
    session: {
      get: async keys => pick(session, keys),
      set: async items => { Object.assign(session, copy(items)); },
      remove: async key => { delete session[key]; },
    },
    onChanged,
  },
  alarms: { create: (name, info) => log.push(["alarms.create", name, info]), onAlarm },
  tabs: {
    query: async () => [], onUpdated,
    create: async props => { log.push(["tabs.create", props]); return { id: nextTab++ }; },
    remove: async id => { log.push(["tabs.remove", id]); gone.add(id); },
    update: async (id, props) => {
      if (gone.has(id)) throw new Error(`No tab with id: ${id}`);
      log.push(["tabs.update", id, props]);
      return { id };
    },
    get: async id => ({ id, url: (page.urls || {})[id] || "https://shop.example/cart" }),
    captureVisibleTab: async (windowId, options) => {
      log.push(["captureVisibleTab", windowId, options]);
      if (page.no_capture) throw new Error("cannot capture");
      return "data:image/png;base64,iVBORw0KGgo=";
    },
  },
  commands: { onCommand },
  ...(page.identity ? { identity: { getProfileUserInfo: async details => {
    log.push(["identity", details]);
    return page.identity;
  } } } : {}),
  scripting: {
    executeScript: async details => {
      if (details.files) return log.push(["executeScript", details]);
      log.push(["readText", details.target.tabId]); // a function run in the tab: its text
      if (page.unreadable) throw new Error("Cannot access contents of the page");
      return [{ result: page.text ?? "" }];
    },
  },
  action: {
    setBadgeText: async details => log.push(["badge", details]),
    setBadgeBackgroundColor: async details => log.push(["badgeColor", details]),
    setTitle: async () => {}, onClicked: event(),
  },
  debugger: {
    onDetach, onEvent: event(),
    attach: async target => {
      if (page.unattachable) throw new Error("Cannot access a chrome:// URL");
      log.push(["attach", target.tabId]);
    },
    detach: async target => log.push(["detach", target.tabId]),
    getTargets: async () => (page.attached || []).map(tabId => ({ tabId, attached: true, type: "page" })),
    sendCommand: async (target, method, params) => {
      sent.push(method);
      if (method !== "Runtime.evaluate") return raw;
      if (page.broken) throw new Error("Cannot access the tab");
      return { result: { value: vm.runInContext(params.expression, pageContext) } };
    },
  },
};
const crypto = { randomUUID: () => "c0ffee00-0000-4000-8000-000000000000" };
const context = vm.createContext({ chrome, crypto, console: { ...console, warn() {}, error() {} }, URL, setTimeout, clearTimeout });
context.importScripts = name => vm.runInContext(fs.readFileSync(path.join(dir, name), "utf8"), context);
vm.runInContext(fs.readFileSync(path.join(dir, "background.js"), "utf8"), context);
const settle = () => new Promise(resolve => setTimeout(resolve, 5));
const port = () => ports[ports.length - 1];
async function play([kind, arg]) {
  if (kind === "host") return Promise.all(port().onMessage.fns.map(fn => fn(copy(arg))));
  if (kind === "page") return new Promise(resolve => onMessage.fns[0](copy(arg), {}, resolve));
  // A message with its sender: [message, sender], e.g. pick.js in a tab.
  if (kind === "from") return new Promise(resolve => onMessage.fns[0](copy(arg[0]), copy(arg[1]), resolve));
  if (kind === "command") return Promise.all(onCommand.fns.map(fn => fn(arg[0], arg[1])));
  if (kind === "disconnect") { port().dead = true; port().onDisconnect.fns.forEach(fn => fn()); return; }
  if (kind === "alarm") { onAlarm.fns.forEach(fn => fn({ name: "ck-reconnect" })); return; }
  if (kind === "detach") { onDetach.fns.forEach(fn => fn({ tabId: arg }, "canceled_by_user")); return; }
  if (kind === "eval") return vm.runInContext(arg, context);
  if (kind === "updated") return Promise.all(onUpdated.fns.map(fn => fn(arg[0], arg[1])));
  if (kind === "together") return Promise.all(arg.map(play));
  if (kind === "later") return settle().then(() => play(arg)); // after the other steps' awaits
  throw new Error(`unknown step ${kind}`);
}
(async () => {
  if (steps) {
    await settle();
    const record = reply => ({
      reply, log, ports: ports.length, storage: copy(store), session: copy(session),
      claim: vm.runInContext("claim && { tabId: claim.tabId, opened: claim.opened }", context),
      posted: log.filter(([what]) => what === "post").map(([, message]) => message),
    });
    const boot = record(null), out = [];
    for (const step of steps) {
      log = [];
      const reply = await play(step);
      await settle();
      out.push(record(["page", "from", "eval", "together"].includes(step[0]) ? reply : null));
    }
    process.stdout.write(JSON.stringify({ boot, out }));
    return;
  }
  const out = [];
  for (const [method, params, result] of calls) {
    raw = result; sent = [];
    try {
      out.push({ result: await context.answer({ tabId: 1, captured: new Set() }, method, params), sent });
    } catch (error) {
      out.push({ error: error.message, sent });
    }
  }
  process.stdout.write(JSON.stringify({ allowed: vm.runInContext("[...ALLOWED]", context), out }));
})();
"""


@unittest.skipUnless(NODE, "node is not on PATH: the extension's policy runs in JavaScript")
class BackgroundTest(unittest.TestCase):
    """extension/background.js through node: what the bridge allows, what it cuts, what it refuses."""

    def answer(self, *calls, page=None):
        """Each call is (method, params, raw chrome.debugger result); returns (allowed, replies)."""
        result = subprocess.run([NODE, "-e", BACKGROUND_HARNESS, str(EXTENSION_DIR)],
                                input=json.dumps({"page": page or {}, "calls": calls}),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        return data["allowed"], data["out"]

    def test_a_secret_inside_a_registered_closed_root_withholds_the_screenshot(self):
        page = {"body": "Account settings", "elements": {"1": {"tag": "X-VAULT"}}}
        shot = ("Page.captureScreenshot", {}, {"data": "cG5n"})
        _, [shown] = self.answer(shot, page=page)
        self.assertNotIn("error", shown)
        # Stripe's published test key, assembled at run time so no key-shaped literal sits in this file.
        secret = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
        _, [withheld] = self.answer(shot, page={**page, "closed_text": {"1": f"key {secret}"}})
        self.assertEqual(withheld["error"], "screenshot withheld: the page shows a secret")

    def test_the_discovery_methods_are_allowed(self):
        allowed, out = self.answer(("Runtime.callFunctionOn", {"functionDeclaration": "x"}, {"result": {}}))
        for method in ("Accessibility.enable", "Accessibility.getFullAXTree", "DOM.describeNode",
                       "DOM.resolveNode", "Runtime.callFunctionOn", "Runtime.releaseObjectGroup"):
            self.assertIn(method, allowed)
        self.assertEqual(out, [{"result": {"result": {}}, "sent": ["Runtime.callFunctionOn"]}])

    def test_the_ax_tree_keeps_only_role_name_node_and_focusable_of_nodes_not_ignored(self):
        raw = {"nodes": [
            {"nodeId": "1", "ignored": True, "role": {"type": "role", "value": "none"}, "backendDOMNodeId": 3},
            {"nodeId": "2", "ignored": False, "role": {"type": "role", "value": "textbox"},
             "chromeRole": {"type": "internalRole", "value": 47},
             "name": {"type": "computedString", "value": "Card number", "sources": [{"type": "attribute"}]},
             "value": {"type": "string", "value": "4242 4242 4242 4242"},
             "description": {"type": "computedString", "value": "Ends in 4242"},
             "properties": [{"name": "focusable", "value": {"type": "booleanOrUndefined", "value": True}},
                            {"name": "editable", "value": {"type": "token", "value": "plaintext"}}],
             "childIds": ["3"], "backendDOMNodeId": 9, "frameId": "F"},
            {"nodeId": "3", "ignored": False, "role": {"type": "role", "value": "generic"}, "backendDOMNodeId": 10},
        ]}
        _, out = self.answer(("Accessibility.getFullAXTree", {}, raw))
        self.assertEqual(out[0]["result"], {"nodes": [
            {"ignored": False, "role": {"value": "textbox"}, "name": {"value": "Card number"},
             "backendDOMNodeId": 9, "properties": [{"name": "focusable", "value": {"value": True}}]},
            {"ignored": False, "role": {"value": "generic"}, "name": {}, "backendDOMNodeId": 10, "properties": []},
        ]})
        self.assertNotIn("4242", json.dumps(out))

    def test_the_described_dom_keeps_structure_and_closed_roots_two_deep_and_no_content(self):
        def root(*children, mode="closed"):
            return {"nodeName": "#document-fragment", "backendNodeId": 0, "shadowRootType": mode,
                    "nodeValue": "", "children": list(children)}

        button = {"nodeName": "BUTTON", "backendNodeId": 7, "attributes": ["data-token", "s3cr3t"],
                  "children": [{"nodeName": "#text", "backendNodeId": 8, "nodeValue": "Pay s3cr3t"}]}
        inner = {"nodeName": "PAY-BUTTON", "backendNodeId": 6, "attributes": ["id", "pay"],
                 "shadowRoots": [{**root(button), "backendNodeId": 5}]}
        raw = {"node": {
            "nodeName": "#document", "backendNodeId": 1, "documentURL": "https://shop.example/?t=s3cr3t",
            "children": [{"nodeName": "CHECKOUT-FORM", "backendNodeId": 2, "attributes": ["class", "s3cr3t"],
                          "shadowRoots": [{**root(inner), "backendNodeId": 4}],
                          "pseudoElements": [{"nodeName": "::before", "backendNodeId": 11}],
                          "templateContent": {"nodeName": "#document-fragment", "backendNodeId": 12},
                          "contentDocument": {"nodeName": "#document", "backendNodeId": 13}}],
        }}
        _, out = self.answer(("DOM.describeNode", {"objectId": "o", "depth": -1, "pierce": True}, raw))
        self.assertEqual(out[0]["result"], {"node": {
            "nodeName": "#document", "backendNodeId": 1,
            "children": [{"nodeName": "CHECKOUT-FORM", "backendNodeId": 2, "shadowRoots": [
                {"nodeName": "#document-fragment", "backendNodeId": 4, "shadowRootType": "closed", "children": [
                    {"nodeName": "PAY-BUTTON", "backendNodeId": 6, "shadowRoots": [
                        {"nodeName": "#document-fragment", "backendNodeId": 5, "shadowRootType": "closed",
                         "children": [{"nodeName": "BUTTON", "backendNodeId": 7, "children": [
                             {"nodeName": "#text", "backendNodeId": 8}]}]}]}]}]}],
        }})
        self.assertNotIn("s3cr3t", json.dumps(out))

    def test_a_result_that_cannot_be_cut_fails_the_call_instead_of_going_out_raw(self):
        _, out = self.answer(("Accessibility.getFullAXTree", {}, {"nodes": [None, {"value": {"value": "4242"}}]}))
        self.assertIn("error", out[0])
        self.assertNotIn("4242", json.dumps(out))

    def test_check_node_refuses_a_payment_card_field_and_is_never_forwarded(self):
        page = {"elements": {"5": {"tag": "SELECT", "attrs": {"autocomplete": "cc-exp-month"}},
                             "6": {"tag": "SELECT", "attrs": {"name": "country"}},
                             "7": {"tag": "SELECT", "attrs": {"name": "size"}, "connected": False}}}
        _, out = self.answer(("ck.checkNode", {"node": 5}, {}), ("ck.checkNode", {"node": 6}, {}),
                             ("ck.checkNode", {"node": 7}, {}), ("ck.checkNode", {"node": "5"}, {}),
                             page=page)
        self.assertEqual(out, [
            {"error": "refused: the element is a payment-card field", "sent": ["Runtime.evaluate"]},
            {"result": {}, "sent": ["Runtime.evaluate"]},
            {"error": "refused: the element is gone", "sent": ["Runtime.evaluate"]},
            {"error": "refused: a check needs an observed node", "sent": []},
        ])

    def test_check_node_fails_closed_when_the_page_cannot_be_probed(self):
        page = {"elements": {"6": {"tag": "SELECT", "attrs": {"name": "country"}}}, "broken": True}
        _, out = self.answer(("ck.checkNode", {"node": 6}, {}), page=page)
        self.assertEqual(out[0]["error"], "refused: the target could not be checked")

    def test_typing_into_a_card_field_inside_a_closed_root_is_refused(self):
        page = {"elements": {"1": {"tag": "CARD-FORM"}, "2": {"tag": "INPUT", "attrs": {"name": "cardnumber"}}},
                "active": "1", "closed": {"1": "2"}}
        _, out = self.answer(("Input.insertText", {"text": "4242"}, {}), page=page)
        self.assertEqual(out, [{"error": "refused: the focused element is a payment-card field",
                                "sent": ["Runtime.evaluate"]}])


def host_call(id, method, **params):
    return ["host", {"id": id, "method": method, "params": params}]


def handover(tab_id):
    return ["page", {"type": "handover", "tabId": tab_id}]


NO_POLICY = {"enabled": True, "domains": [], "pending": []}
# The harness's crypto.randomUUID, cut to the ids background.js keeps.
WORKER_ID = "c0ffee00"


def state(open=NO_POLICY, last_disconnect=None, profile=None):
    return {"type": "state", "open": open, "instance": WORKER_ID, "worker": WORKER_ID,
            "lastDisconnect": last_disconnect, "profile": profile}


@unittest.skipUnless(NODE, "node is not on PATH: the extension's policy runs in JavaScript")
class TabPolicyTest(unittest.TestCase):
    """background.js through node: the resident port, agent-opened tabs and the domain allowlist."""

    URL = "https://shop.example/new"

    def drive(self, *steps, storage=None, page=None, session=None):
        """(boot, one record per step): see BACKGROUND_HARNESS."""
        result = subprocess.run([NODE, "-e", BACKGROUND_HARNESS, str(EXTENSION_DIR)],
                                input=json.dumps({"page": page or {}, "steps": steps, "storage": storage or {},
                                                  "session": session or {}}),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        return data["boot"], data["out"]

    def test_the_worker_connects_the_host_once_and_tells_it_the_policy(self):
        boot, _ = self.drive()
        self.assertEqual(boot["ports"], 1)
        self.assertEqual(boot["posted"], [state()])
        self.assertIn(["alarms.create", "ck-reconnect", {"periodInMinutes": 0.5}], boot["log"])

    def needs_profile_emails(self):
        """A build without page tags drops chrome.identity: its profiles are labelled without an email."""
        if "identity" not in json.loads((EXTENSION_DIR / "manifest.json").read_text())["permissions"]:
            self.skipTest("this extension build does not read the profile's email")

    def test_without_the_identity_permission_the_profile_is_null(self):
        boot, _ = self.drive()  # no chrome.identity: an extension not reloaded since 0.5.0, or one built without it
        self.assertEqual(boot["posted"], [state(profile=None)])

    def test_the_state_names_the_profiles_email_and_never_its_gaia_id(self):
        self.needs_profile_emails()
        boot, _ = self.drive(page={"identity": {"email": EMAIL, "id": "113377991122"}})
        self.assertEqual(boot["posted"], [state(profile={"email": EMAIL})])
        self.assertIn(["identity", {"accountStatus": "ANY"}], boot["log"])
        self.assertNotIn("113377991122", json.dumps(boot["posted"]))
        boot, _ = self.drive(page={"identity": {"email": "", "id": ""}})  # not signed in to Chrome
        self.assertEqual(boot["posted"], [state(profile={"email": ""})])

    def test_the_manifest_asks_for_the_profiles_email(self):
        self.needs_profile_emails()
        manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
        self.assertIn("identity", manifest["permissions"])
        self.assertIn("identity.email", manifest["permissions"])

    def test_revoke_domain_takes_it_off_both_lists_and_tells_the_host(self):
        _, out = self.drive(host_call(1, "ck.revokeDomain", domain="Shop.Example"),
                            host_call(2, "ck.revokeDomain", domain="asked.example"),
                            host_call(3, "ck.revokeDomain", domain="*"),
                            host_call(4, "ck.revokeDomain", domain="never.example"),
                            host_call(5, "ck.revokeDomain", domain="shop.example/path"),
                            storage={"domains": ["shop.example", "*", "other.example"],
                                     "pending": ["asked.example"]})
        policy = {"enabled": True, "domains": ["*", "other.example"], "pending": ["asked.example"]}
        self.assertEqual(out[0]["posted"], [state(policy), {"id": 1, "result": {"status": "revoked"}}])
        self.assertEqual(out[1]["storage"]["pending"], [])
        self.assertIn(["badge", {"text": ""}], out[1]["log"])
        self.assertEqual(out[2]["storage"]["domains"], ["other.example"])  # `*` is revocable
        self.assertEqual(out[3]["posted"], [{"id": 4, "result": {"status": "not_allowed"}}])
        self.assertEqual(out[4]["posted"], [{"id": 5, "error": {"message":
                         "refused: shop.example/path is not a domain like example.com or *.example.com"}}])

    def test_revoke_domain_leaves_a_claimed_tab_claimed(self):
        _, [_, revoked] = self.drive(host_call(1, "ck.openTab", url=self.URL),
                                     host_call(2, "ck.revokeDomain", domain="shop.example"),
                                     storage={"domains": ["shop.example"]})
        self.assertEqual(revoked["posted"][-1], {"id": 2, "result": {"status": "revoked"}})
        self.assertEqual(revoked["claim"], {"tabId": 100, "opened": True})

    def test_open_tab_opens_claims_and_says_hello_before_the_reply(self):
        _, [opened] = self.drive(host_call(4, "ck.openTab", url=self.URL),
                                 storage={"domains": ["shop.example"]})
        log = opened["log"]
        self.assertEqual(log[0], ["tabs.create", {"url": self.URL, "active": True}])
        self.assertIn(["attach", 100], log)
        self.assertIn(["badge", {"tabId": 100, "text": "CK"}], log)
        self.assertEqual(opened["posted"], [
            {"type": "hello", "tab": {"id": 100, "url": self.URL, "title": "", "opened": True}},
            {"id": 4, "result": {"tabId": 100}},
        ])
        self.assertEqual(opened["claim"], {"tabId": 100, "opened": True})

    def test_open_tab_refusals_open_nothing(self):
        _, out = self.drive(
            host_call(1, "ck.openTab", url="file:///etc/passwd"),
            host_call(2, "ck.openTab", url="javascript:alert(1)"),
            host_call(3, "ck.openTab", url="https://evil.example/"),
            ["page", {"type": "toggle", "enabled": False}],
            host_call(4, "ck.openTab", url=self.URL),
            ["page", {"type": "toggle", "enabled": True}],
            handover(5),
            host_call(6, "ck.openTab", url=self.URL),
            storage={"domains": ["shop.example"]})
        errors = {step["posted"][-1]["id"]: step["posted"][-1]["error"]["message"]
                  for step in out if step["posted"] and "id" in step["posted"][-1]}
        self.assertEqual(errors, {
            1: "refused: only http(s) URLs can be opened",
            2: "refused: only http(s) URLs can be opened",
            3: "refused: evil.example is not an allowed domain; ask with: bin/ck-browser ext allow evil.example",
            4: "refused: agents may not open tabs; turn it on in the ck-browser popup",
            6: "busy: a tab is already claimed",
        })
        self.assertNotIn("tabs.create", [entry[0] for step in out for entry in step["log"]])
        self.assertEqual(out[-1]["claim"], {"tabId": 5, "opened": False})

    def test_a_tab_that_cannot_be_attached_is_closed_again(self):
        _, [opened] = self.drive(host_call(1, "ck.openTab", url=self.URL),
                                 storage={"domains": ["shop.example"]}, page={"unattachable": True})
        self.assertIn(["tabs.remove", 100], opened["log"])
        self.assertEqual(opened["posted"], [{"id": 1, "error": {"message": "could not take the tab ck-browser opened"}}])
        self.assertIsNone(opened["claim"])

    def test_the_allowlist_matches_exact_hosts_apex_and_subdomains_or_anything(self):
        cases = [(["exact.example"], "exact.example", True), (["exact.example"], "a.exact.example", False),
                 (["*.wild.example"], "wild.example", True), (["*.wild.example"], "a.b.wild.example", True),
                 (["*.wild.example"], "notwild.example", False), (["*"], "any.example", True),
                 ([], "shop.example", False), (["Shop.example"], "shop.example", False)]
        _, [result] = self.drive(["eval", json.dumps([[d, h] for d, h, _ in cases]) +
                                  ".map(([domains, host]) => allows(domains, host))"])
        self.assertEqual(result["reply"], [allowed for _, _, allowed in cases])
        _, [opened] = self.drive(host_call(1, "ck.openTab", url="https://deep.wild.example/x"),
                                 storage={"domains": ["*.wild.example"]})
        self.assertEqual(opened["posted"][-1], {"id": 1, "result": {"tabId": 100}})

    def test_close_tab_closes_only_a_tab_ck_browser_opened(self):
        _, out = self.drive(host_call(1, "ck.closeTab"), handover(5), host_call(2, "ck.closeTab"))
        self.assertEqual(out[0]["posted"], [{"id": 1, "error": {"message": "no tab is claimed"}}])
        self.assertEqual(out[2]["posted"], [{"id": 2, "error": {"message":
                         "refused: the human handed this tab over; it is stopped from the icon"}}])
        self.assertEqual(out[2]["claim"], {"tabId": 5, "opened": False})
        _, [_, closed] = self.drive(host_call(1, "ck.openTab", url=self.URL), host_call(2, "ck.closeTab"),
                                    storage={"domains": ["shop.example"]})
        # The reply first: the detached frame ends the client's reads.
        self.assertEqual(closed["posted"], [{"id": 2, "result": {"closed": True}},
                                            {"type": "detached", "reason": "closed_by_agent"}])
        self.assertIn(["detach", 100], closed["log"])
        self.assertIn(["tabs.remove", 100], closed["log"])
        self.assertIsNone(closed["claim"])

    def test_allow_domain_asks_the_human_once_on_the_approval_page(self):
        _, [first, again, second] = self.drive(host_call(1, "ck.allowDomain", domain="New.Example"),
                                               host_call(2, "ck.allowDomain", domain="new.example"),
                                               host_call(3, "ck.allowDomain", domain="*.other.example"))
        self.assertEqual(first["storage"]["pending"], ["new.example"])
        self.assertEqual(second["storage"]["pending"], ["new.example", "*.other.example"])
        self.assertIn(["badge", {"text": "?"}], first["log"])
        self.assertIn(["tabs.create", {"url": "chrome-extension://ck/approve.html", "active": True}], first["log"])
        self.assertEqual(first["posted"], [
            state({"enabled": True, "domains": [], "pending": ["new.example"]}),
            {"id": 1, "result": {"status": "pending"}}])
        # A new request brings the same page forward; one already waiting leaves it be.
        self.assertNotIn("tabs.create", [entry[0] for entry in second["log"]])
        self.assertIn(["tabs.update", 100, {"active": True}], second["log"])
        self.assertEqual(again["log"], [["post", {"id": 2, "result": {"status": "pending"}}]])

    def test_allow_domain_answers_at_once_or_refuses(self):
        _, out = self.drive(host_call(1, "ck.allowDomain", domain="a.shop.example"),
                            host_call(2, "ck.allowDomain", domain="*"),
                            host_call(3, "ck.allowDomain", domain="shop.example/path"),
                            storage={"domains": ["*.shop.example"]})
        self.assertEqual([step["posted"][-1] for step in out], [
            {"id": 1, "result": {"status": "allowed"}},
            {"id": 2, "error": {"message": "refused: any-domain is only set by the human in the popup"}},
            {"id": 3, "error": {"message": "refused: shop.example/path is not a domain like example.com or *.example.com"}},
        ])
        self.assertEqual(out[-1]["storage"], {"domains": ["*.shop.example"], "instance": WORKER_ID})
        self.assertNotIn("tabs.create", [entry[0] for step in out for entry in step["log"]])

    def test_the_popup_answers_requests_and_edits_the_policy_telling_the_host_each_time(self):
        _, out = self.drive(["page", {"type": "allow", "domain": "a.example"}],
                            ["page", {"type": "deny", "domain": "b.example"}],
                            ["page", {"type": "toggle", "enabled": False}],
                            ["page", {"type": "add", "domain": "*"}],
                            ["page", {"type": "remove", "domain": "a.example"}],
                            ["page", {"type": "state"}],
                            storage={"pending": ["a.example", "b.example"]})
        self.assertEqual([step["reply"]["open"] for step in out], [
            {"enabled": True, "domains": ["a.example"], "pending": ["b.example"]},
            {"enabled": True, "domains": ["a.example"], "pending": []},
            {"enabled": False, "domains": ["a.example"], "pending": []},
            {"enabled": False, "domains": ["a.example", "*"], "pending": []},
            {"enabled": False, "domains": ["*"], "pending": []},
            {"enabled": False, "domains": ["*"], "pending": []},
        ])
        for step in out[:-1]:
            self.assertEqual(step["posted"], [state(step["reply"]["open"])])
        self.assertEqual(out[-1]["posted"], [])
        self.assertIn(["badge", {"text": "?"}], out[0]["log"])
        self.assertIn(["badge", {"text": ""}], out[1]["log"])
        self.assertIsNone(out[0]["reply"]["claim"])

    def test_handover_from_the_popup_claims_the_tab_and_again_stops(self):
        _, [taken, stopped] = self.drive(handover(5), handover(5))
        self.assertEqual(taken["posted"], [{"type": "hello", "tab": {
            "id": 5, "url": "https://shop.example/cart", "title": "Cart", "opened": False}}])
        self.assertEqual(taken["reply"]["claim"], {"tabId": 5, "title": "Cart", "opened": False})
        self.assertEqual(stopped["posted"], [{"type": "detached", "reason": "released_by_user"}])
        self.assertIsNone(stopped["reply"]["claim"])
        self.assertIn(["badge", {"tabId": 5, "text": None}], stopped["log"])  # back to the global badge

    def test_an_opened_tab_navigates_only_within_the_allowed_domains(self):
        _, out = self.drive(host_call(1, "ck.openTab", url=self.URL),
                            host_call(2, "Page.navigate", url="https://evil.example/steal"),
                            host_call(3, "Page.navigate", url="https://shop.example/other"),
                            storage={"domains": ["shop.example"]})
        self.assertEqual(out[1]["posted"], [{"id": 2, "error": {"message":
                         "refused: evil.example is not an allowed domain; ask with: bin/ck-browser ext allow evil.example"}}])
        self.assertEqual(out[2]["posted"], [{"id": 3, "result": {}}])
        self.assertEqual(out[2]["claim"], {"tabId": 100, "opened": True})
        _, [_, handed] = self.drive(handover(5), host_call(2, "Page.navigate", url="https://evil.example/"),
                                    storage={"domains": ["shop.example"]})
        self.assertEqual(handed["posted"], [{"id": 2, "result": {}}])  # the human gave that tab

    def test_an_opened_tab_that_leaves_the_allowed_domains_is_released_not_closed(self):
        _, out = self.drive(host_call(1, "ck.openTab", url=self.URL),
                            ["updated", [100, {"status": "loading", "url": "https://shop.example/next"}]],
                            ["updated", [100, {"url": "https://accounts.google.com/o/oauth2"}]],
                            storage={"domains": ["shop.example"]})
        self.assertEqual(out[1]["posted"], [])
        self.assertEqual(out[1]["claim"], {"tabId": 100, "opened": True})
        left = out[2]
        self.assertEqual(left["posted"], [{"type": "detached", "reason": "left_allowed_domains",
                                           "host": "accounts.google.com"}])
        self.assertIsNone(left["claim"])
        self.assertIn(["detach", 100], left["log"])
        self.assertNotIn("tabs.remove", [entry[0] for entry in left["log"]])
        _, [_, odd] = self.drive(host_call(1, "ck.openTab", url=self.URL),
                                 ["updated", [100, {"url": "chrome://settings/"}]], storage={"domains": ["shop.example"]})
        self.assertEqual(odd["posted"][0]["reason"], "left_allowed_domains")
        _, [_, handed] = self.drive(handover(5), ["updated", [5, {"url": "https://evil.example/"}]],
                                    storage={"domains": ["shop.example"]})
        self.assertEqual(handed["posted"], [])
        self.assertEqual(handed["claim"], {"tabId": 5, "opened": False})

    def test_allow_domain_refuses_bad_or_broad_patterns_and_a_flood(self):
        _, out = self.drive(["host", {"id": 1, "method": "ck.allowDomain", "params": {"domain": ["a.example"]}}],
                            host_call(2, "ck.allowDomain", domain="*.com"),
                            host_call(3, "ck.allowDomain", domain="localhost"),
                            host_call(4, "ck.allowDomain", domain="*.co.uk"),
                            host_call(5, "ck.allowDomain", domain="one-more.example"),
                            storage={"pending": [f"p{n}.example" for n in range(18)]})
        self.assertEqual([step["posted"][-1] for step in out], [
            {"id": 1, "error": {"message": "refused: a request needs a domain"}},
            {"id": 2, "error": {"message": "refused: *.com is too broad; name a domain like *.example.com"}},
            {"id": 3, "result": {"status": "pending"}},
            {"id": 4, "result": {"status": "pending"}},
            {"id": 5, "error": {"message": "refused: too many pending requests; answer them in the popup"}},
        ])
        self.assertEqual(out[-1]["storage"]["pending"][-2:], ["localhost", "*.co.uk"])

    def test_open_tab_and_a_handover_in_the_same_tick_take_turns(self):
        open_tab = host_call(1, "ck.openTab", url=self.URL)
        # The handover first: the agent's request sees busy and opens nothing.
        _, [both] = self.drive(["together", [handover(5), open_tab]], storage={"domains": ["shop.example"]})
        self.assertEqual(both["claim"], {"tabId": 5, "opened": False})
        self.assertIn({"id": 1, "error": {"message": "busy: a tab is already claimed"}}, both["posted"])
        self.assertNotIn("tabs.create", [entry[0] for entry in both["log"]])
        # The agent first: its tab is released (never closed) and the human's taken.
        _, [both] = self.drive(["together", [open_tab, handover(5)]], storage={"domains": ["shop.example"]})
        self.assertEqual(both["claim"], {"tabId": 5, "opened": False})
        log = [entry[:2] for entry in both["log"] if entry[0] in ("attach", "detach", "tabs.remove")]
        self.assertEqual(log, [["attach", 100], ["detach", 100], ["attach", 5]])

    def test_a_lost_host_releases_the_claim_and_the_alarm_reconnects(self):
        _, [_, lost, reconnected, again] = self.drive(handover(5), ["disconnect"], ["alarm"], ["alarm"])
        self.assertIsNone(lost["claim"])
        self.assertIn(["detach", 5], lost["log"])
        self.assertEqual(lost["ports"], 1)
        self.assertEqual(reconnected["ports"], 2)
        self.assertEqual(reconnected["posted"], [state(last_disconnect="closed without an error")])
        self.assertNotIn("lastDisconnect", reconnected["session"])  # told once
        self.assertEqual(again["ports"], 2)  # a live port is kept

    SAVED = {"tabId": 5, "opened": False, "title": "Cart", "captured": [],
             "tab": {"id": 5, "url": "https://shop.example/cart", "title": "Cart", "opened": False}}

    def test_a_claim_is_kept_in_session_storage_until_it_ends(self):
        _, [taken, released] = self.drive(handover(5), ["detach", 5])
        self.assertEqual(taken["session"], {"claim": self.SAVED})
        self.assertEqual(released["session"], {})
        self.assertIsNone(released["claim"])

    def test_a_restarted_worker_re_adopts_a_claim_whose_tab_is_still_attached(self):
        boot, [call] = self.drive(host_call(1, "Runtime.evaluate", expression="document.title"),
                                  session={"claim": self.SAVED}, page={"attached": [5]})
        self.assertEqual(boot["claim"], {"tabId": 5, "opened": False})
        self.assertCountEqual(boot["posted"], [state(), {"type": "hello", "tab": self.SAVED["tab"], "adopted": True}])
        self.assertIn(["badge", {"tabId": 5, "text": "CK"}], boot["log"])
        self.assertNotIn("attach", [entry[0] for entry in boot["log"]])  # no click, no new attach
        self.assertNotIn(["badge", {"tabId": 5, "text": None}], boot["log"])
        self.assertEqual(call["posted"], [{"id": 1, "result": {"result": {"value": "Cart"}}}])

    def test_a_restarted_worker_drops_a_claim_whose_debugger_is_gone(self):
        boot, _ = self.drive(session={"claim": self.SAVED}, page={"attached": [6]})
        self.assertIsNone(boot["claim"])
        self.assertCountEqual(boot["posted"], [state(), {"type": "detached", "reason": "lost_in_worker_restart"}])
        self.assertIn(["badge", {"tabId": 5, "text": None}], boot["log"])
        self.assertNotIn("attach", [entry[0] for entry in boot["log"]])  # never re-taken without a click
        self.assertEqual(boot["session"], {})

    def test_a_captured_value_survives_a_worker_restart_so_it_stays_masked(self):
        saved = {**self.SAVED, "captured": [VALUE]}
        _, [call] = self.drive(host_call(1, "Runtime.evaluate", expression=json.dumps(f"key {VALUE}")),
                               session={"claim": saved}, page={"attached": [5]})
        self.assertNotIn(VALUE, json.dumps(call["posted"]))


# -- setup and the CLI ----------------------------------------------------------------


BAKED = ("CK_BROWSER_HOME", "CK_BROWSER_PROFILE_DIR", "CK_BROWSER_PLUGINS", "CK_BROWSER_EXTENSION_ID")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.env = {"CK_BROWSER_BRIDGE_DIR": str(self.home / "bridge"), "CK_BROWSER_NM_DIR": str(self.home / "nm")}

    def install(self, *args, **env):
        """`ext install` with only `env` of the variables the wrapper bakes; its stdout."""
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()) as out:
            for name in BAKED:
                os.environ.pop(name, None)
            os.environ.update(env)
            self.assertEqual(cli.main(["ext", "install", *args]), 0)
        return out.getvalue()

    def baked(self):
        """What the wrapper's export lines set, as /bin/sh reads them."""
        exports = "".join(line + "\n" for line in (self.home / "bridge" / "host").read_text().splitlines()
                          if line.startswith("export "))
        shown = subprocess.run(["/bin/sh", "-c", exports + "env"], capture_output=True, text=True, timeout=10,
                               env={"PATH": "/usr/bin:/bin"})
        return {name: value for name, _, value in (line.partition("=") for line in shown.stdout.splitlines())
                if name in BAKED}

    def test_install_writes_a_pinned_wrapper_and_a_manifest_for_our_extension_only(self):
        out = self.install()
        with mock.patch.dict(os.environ):
            os.environ.pop("CK_BROWSER_EXTENSION_ID", None)
            ours = our_extension_id()  # EXTENSION_ID while the manifest has its key
        manifest_path = self.home / "nm" / "com.ck_browser.host.json"
        wrapper = self.home / "bridge" / "host"
        self.assertEqual(json.loads(manifest_path.read_text()), {
            "name": "com.ck_browser.host",
            "description": "ck-browser bridge to one handed-over tab",
            "path": str(wrapper), "type": "stdio",
            "allowed_origins": [f"chrome-extension://{ours}/"],
        })
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o700)
        script = wrapper.read_text()
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertIn(f"cd {shlex.quote(str(ROOT))} && exec {shlex.quote(sys.executable)} -m {HOST_MODULE} \"$@\"",
                      script)
        self.assertEqual(self.baked(), {"CK_BROWSER_EXTENSION_ID": ours})
        self.assertIn(f"extension id: {ours}", out)
        self.assertIn(f"host manifest: {manifest_path}", out)
        self.assertIn(f"Load unpacked → {EXTENSION_DIR}", out)
        # The wrapper really starts the host: a wrong caller is turned away by it.
        result = subprocess.run([str(wrapper), "chrome-extension://wrong/"], capture_output=True,
                                timeout=10, env={"PATH": "/usr/bin:/bin", "CK_BROWSER_BRIDGE_DIR": str(self.home / "bridge")})
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"refusing caller", result.stderr)

    def test_the_wrapper_bakes_the_home_profile_plugins_and_id_set_at_install(self):
        """Chrome starts the host without our env: what is set at install is the host's too, paths absolute."""
        chosen = "abcdefghijklmnopabcdefghijklmnop"
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.home)
        with contextlib.redirect_stderr(io.StringIO()):  # a.b and c do not import: logged, not fatal
            out = self.install("--extension-id", chosen, CK_BROWSER_HOME="ck home's",
                               CK_BROWSER_PROFILE_DIR=str(self.home / "profile"), CK_BROWSER_PLUGINS="a.b, c")
        self.assertEqual(self.baked(), {"CK_BROWSER_HOME": os.path.join(os.getcwd(), "ck home's"),
                                        "CK_BROWSER_PROFILE_DIR": str(self.home / "profile"),
                                        "CK_BROWSER_PLUGINS": "a.b, c", "CK_BROWSER_EXTENSION_ID": chosen})
        self.assertEqual(json.loads((self.home / "nm" / "com.ck_browser.host.json").read_text())
                         ["allowed_origins"], [f"chrome-extension://{chosen}/"])
        self.assertIn(f"extension id: {chosen}", out)
        # The baked ID is the one the host lets in.
        with mock.patch.dict(os.environ, {"CK_BROWSER_EXTENSION_ID": chosen}):
            self.assertEqual(extension_origin(), f"chrome-extension://{chosen}/")

    def test_a_bad_extension_id_or_one_outside_install_is_refused(self):
        for argv in (["ext", "install", "--extension-id", "not-an-id"],
                     ["ext", "status", "--extension-id", "abcdefghijklmnopabcdefghijklmnop"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                cli.main(argv)
            self.assertEqual(caught.exception.code, 2, argv)
        self.assertFalse((self.home / "bridge").exists())

    def failed_install(self, *args, **env):
        """`ext install` that must fail: its stdout."""
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()) as out:
            for name in BAKED:
                os.environ.pop(name, None)
            os.environ.update(env)
            self.assertEqual(cli.main(["ext", "install", *args]), 1)
        return out.getvalue()

    def test_a_bad_extension_id_in_the_env_is_refused_like_the_flag(self):
        out = self.failed_install(CK_BROWSER_EXTENSION_ID="not-an-id")
        self.assertIn("'not-an-id'", out)
        self.assertIn("32 letters a-p", out)
        self.assertFalse((self.home / "bridge").exists())
        # The host turns every caller away rather than crash on it.
        with mock.patch.dict(os.environ, {**self.env, "CK_BROWSER_EXTENSION_ID": "not-an-id"}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ext_module.host_main(["chrome-extension://not-an-id/"]), 2)
        self.assertIn("refusing caller", err.getvalue())

    def test_without_the_extension_install_needs_an_explicit_id(self):
        missing = self.home / "no-extension"
        with mock.patch.object(ext_module, "EXTENSION_DIR", missing):
            out = self.failed_install()
            self.assertIn(f"no extension at {missing}", out)
            self.assertIn("--extension-id", out)
            with mock.patch.dict(os.environ):
                os.environ.pop("CK_BROWSER_EXTENSION_ID", None)
                with self.assertRaisesRegex(CdpError, "no extension at"):
                    our_extension_id()
            chosen = "abcdefghijklmnopabcdefghijklmnop"
            self.assertIn(f"extension id: {chosen}", self.install("--extension-id", chosen))
            self.assertIn(f"extension id: {chosen}", self.install(CK_BROWSER_EXTENSION_ID=chosen))


class LayoutTest(unittest.TestCase):
    """The package finds its root, extension and modules from where it sits, whatever its name."""

    def test_root_is_the_directory_holding_the_top_level_package(self):
        package = ext_module.__package__
        self.assertEqual(ROOT, Path(ext_module.__file__).resolve().parents[len(package.split("."))])
        self.assertTrue((ROOT / package.split(".")[0]).is_dir())
        self.assertEqual(EXTENSION_DIR, ROOT / "extension")
        self.assertTrue((EXTENSION_DIR / "manifest.json").is_file())
        self.assertEqual(HOST_MODULE, f"{package}.ext_host")

    def test_the_package_runs_from_root_without_pythonpath(self):
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        for module in (ext_module.__package__, HOST_MODULE):
            result = subprocess.run([sys.executable, "-c", f"import importlib; importlib.import_module({module!r})"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


class UnpackedIdTest(unittest.TestCase):
    """An extension without a manifest key: Chrome names it after the path it was loaded from."""

    def spelled(self, path):
        digest = hashlib.sha256(os.path.realpath(path).encode()).hexdigest()[:32]
        return digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))

    def test_the_id_is_the_first_128_bits_of_the_real_path_hash_spelled_a_to_p(self):
        root = temp_dir(self)
        (root / "extension").mkdir()
        (root / "link").symlink_to(root / "extension")
        self.assertEqual(unpacked_extension_id(root / "extension"), self.spelled(root / "extension"))
        self.assertEqual(unpacked_extension_id(root / "link"), unpacked_extension_id(root / "extension"))
        self.assertRegex(unpacked_extension_id(root / "extension"), r"^[a-p]{32}$")

    def test_without_a_key_install_and_the_host_use_the_unpacked_id_unless_one_is_chosen(self):
        root = temp_dir(self)
        (root / "manifest.json").write_text(json.dumps({"name": "ck-browser"}))
        with mock.patch.object(ext_module, "EXTENSION_DIR", root), mock.patch.dict(os.environ):
            os.environ.pop("CK_BROWSER_EXTENSION_ID", None)
            self.assertEqual(our_extension_id(), self.spelled(root))
            self.assertEqual(our_extension_id("abcdefghijklmnopabcdefghijklmnop"), "abcdefghijklmnopabcdefghijklmnop")
            os.environ["CK_BROWSER_EXTENSION_ID"] = "ponmlkjihgfedcbaponmlkjihgfedcba"
            self.assertEqual(extension_origin(), "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba/")


class ExtCliTest(unittest.TestCase):
    POLICY = "agents may open tabs: on; allowed: shop.example, *.cloudflare.com; pending: none"
    LABEL = f"instance i1 {UNKNOWN_PROFILE}"  # FakeExtension's profile: no email, no Local State
    ACTING = f"profile: {LABEL}"

    def ext(self, bridge, *args, chrome="/nonexistent/ck-browser-test-chrome"):
        with mock.patch.dict(os.environ, {"CK_BROWSER_BRIDGE_DIR": str(bridge), "CK_BROWSER_CHROME_DIR": str(chrome)}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["ext", *args])
        return code, out.getvalue().strip()

    def test_status_without_a_bridge(self):
        bridge = temp_dir(self)
        self.assertEqual(self.ext(bridge, "status"), (1, NO_PROFILES.format(dir=bridge / "profiles")))

    def test_status_idle_names_the_policy(self):
        ext = FakeExtension(self).idle()
        self.assertEqual(self.ext(ext.dir, "status"), (1, f"profile: {self.LABEL}\n  no tab claimed\n"
                                                          f"  {self.POLICY}\ndefault (no --profile): {self.LABEL}"))

    def test_status_names_the_handed_over_tab(self):
        ext = FakeExtension(self).ready()
        self.assertEqual(self.ext(ext.dir, "status"), (0, f"profile: {self.LABEL}\n  tab: Cart — https://shop.example/cart\n"
                                                          f"  {self.POLICY}\ndefault (no --profile): {self.LABEL}"))

    def test_status_names_a_tab_ck_browser_opened(self):
        ext = FakeExtension(self).ready(OPENED)
        self.assertIn("\n  tab:  — https://shop.example/new (opened by ck-browser)\n", self.ext(ext.dir, "status")[1])

    def test_status_answers_while_a_run_holds_the_tab(self):
        ext = FakeExtension(self, answer=lambda call: None).ready()
        cdp = ExtCdp(ext.socket, timeout=5)
        self.addCleanup(cdp.close)
        self.assertEqual(self.ext(ext.dir, "status")[0], 0)  # read from the sidecar, never BUSY

    def test_status_lists_every_profile_and_the_default(self):
        chrome = fake_chrome(self)
        env = {"CK_BROWSER_CHROME_DIR": str(chrome)}
        work = FakeExtension(self, instance="work", email=EMAIL, env=env).idle()
        FakeExtension(self, dir=work.dir, instance="ab12cd34", env=env).idle()
        unknown = f"instance ab12cd34 {UNKNOWN_PROFILE}"
        blocks = (f"profile: {WORK}\n  no tab claimed\n  {self.POLICY}\n"
                  f"profile: {unknown}\n  no tab claimed\n  {self.POLICY}\n")
        self.assertEqual(self.ext(work.dir, "status", chrome=chrome), (
            1, blocks + "default (no --profile): none — several profiles connected; pass --profile"))
        work.send({"type": "hello", "tab": TAB})
        self.assertTrue(wait_for(lambda: self.ext(work.dir, "status", chrome=chrome) == (
            0, blocks.replace("no tab claimed", "tab: Cart — https://shop.example/cart", 1)
            + f"default (no --profile): {WORK}")))
        self.assertEqual(self.ext(work.dir, "status", "--profile", "ab12cd34", chrome=chrome),
                         (1, f"profile: {unknown}\n  no tab claimed\n  {self.POLICY}"))
        self.assertEqual(self.ext(work.dir, "status", "--profile", "profile 14", chrome=chrome),
                         (0, f"profile: {WORK}\n  tab: Cart — https://shop.example/cart\n  {self.POLICY}"))
        code, out = self.ext(work.dir, "open", "https://shop.example/new", "--profile", "Personal", chrome=chrome)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith('Chrome profile "Personal" is not connected (never falling back'), out)

    def test_open_close_and_a_refused_open(self):
        def answer(call):
            if call.get("method") == "ck.openTab" and "evil" not in call["params"]["url"]:
                ext.send({"type": "hello", "tab": OPENED})
            return opens(call)

        ext = FakeExtension(self, answer=answer).idle()
        self.assertEqual(self.ext(ext.dir, "open", "https://evil.example/"), (1, f"{self.ACTING}\n{REFUSED}"))
        self.assertEqual(self.ext(ext.dir, "open", "https://shop.example/new"),
                         (0, f"{self.ACTING}\ntab:  — https://shop.example/new (opened by ck-browser)"))
        self.assertTrue(wait_for(lambda: self.ext(ext.dir, "close") == (0, f"{self.ACTING}\nclosed")))
        self.assertEqual(ext.calls[-1]["method"], "ck.closeTab")

    def test_open_while_a_tab_is_claimed_opens_nothing(self):
        ext = FakeExtension(self).ready()
        self.assertEqual(self.ext(ext.dir, "open", "https://shop.example/new"),
                         (1, f"{self.ACTING}\nbusy: a tab is already claimed — tab: Cart — https://shop.example/cart"))
        self.assertEqual(ext.calls, [])

    def test_allow_reports_allowed_pending_or_refused(self):
        def answer(call):
            domain = call["params"]["domain"]
            if domain == "*":
                return {"id": call["id"], "error": {"message": "refused: any-domain is only set by the human in the popup"}}
            return {"id": call["id"], "result": {"status": "allowed" if domain == "shop.example" else "pending"}}

        ext = FakeExtension(self, answer=answer).idle()
        self.assertEqual(self.ext(ext.dir, "allow", "shop.example"), (0, f"{self.ACTING}\nallowed: shop.example"))
        self.assertTrue(wait_for(lambda: self.ext(ext.dir, "allow", "new.example") == (
            1, f"{self.ACTING}\npending: approve in the tab Chrome just opened, or in the ck-browser popup")))
        self.assertTrue(wait_for(lambda: self.ext(ext.dir, "allow", "*") == (
            2, f"{self.ACTING}\nrefused: any-domain is only set by the human in the popup")))

    def test_revoke_reports_revoked_not_allowed_or_refused(self):
        def answer(call):
            domain = call["params"]["domain"]
            if "/" in domain:
                return {"id": call["id"], "error": {"message": f"refused: {domain} is not a domain"}}
            return {"id": call["id"], "result": {"status": "revoked" if domain == "shop.example" else "not_allowed"}}

        ext = FakeExtension(self, answer=answer).idle()
        self.assertEqual(self.ext(ext.dir, "revoke", "shop.example"), (0, f"{self.ACTING}\nrevoked: shop.example"))
        self.assertTrue(wait_for(lambda: self.ext(ext.dir, "revoke", "other.example") == (
            0, f"{self.ACTING}\nnot allowed: other.example")))
        self.assertTrue(wait_for(lambda: self.ext(ext.dir, "revoke", "a/b") == (
            2, f"{self.ACTING}\nrefused: a/b is not a domain")))
        self.assertEqual(ext.calls[0], {"id": 0, "method": "ck.revokeDomain", "params": {"domain": "shop.example"}})

    def test_allow_and_close_without_a_bridge_exit_1(self):
        bridge = temp_dir(self)
        absent = NO_PROFILES.format(dir=bridge / "profiles")
        self.assertEqual(self.ext(bridge, "allow", "shop.example"), (1, absent))
        self.assertEqual(self.ext(bridge, "close"), (1, absent))
        self.assertEqual(self.ext(bridge, "revoke", "shop.example"), (1, absent))

    def test_run_passes_the_target_and_profile_through(self):
        out = temp_dir(self)
        script = out / "steps.yaml"
        script.write_text("steps:\n  - click: Save\n")
        seen = {}

        def fake_run(*_args, **kwargs):
            seen.update(kwargs)
            raise CdpError("no tab handed over")

        with mock.patch.object(cli, "run", fake_run), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["run", str(script), "--out", str(out), "--stub", "--target", "ext",
                                       "--profile", "Profile 14"]), 2)
        self.assertEqual((seen["target"], seen["ext_profile"]), ("ext", "Profile 14"))

    def test_profile_needs_the_ext_target_and_install_takes_none(self):
        for argv in (["run", "x.yaml", "--out", "/tmp/x", "--profile", "Profile 14"],
                     ["ext", "install", "--profile", "Profile 14"], ["ext", "revoke"]):
            with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
                cli.main(argv)
            self.assertEqual(caught.exception.code, 2, argv)
        self.assertIn("ext revoke needs a domain", err.getvalue())


if __name__ == "__main__":
    unittest.main()

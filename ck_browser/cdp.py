"""CDP over a stdlib websocket: launch a dedicated Chrome, drive one owned tab.

Never the human's browser. A websocket to the default profile's debugging port makes
Chrome 144+ pop up "Allow remote debugging?" and interrupts whoever is using it, so
this module only talks to a Chrome that `ensure_chrome` started with its own --user-data-dir.
The profile dir, not a port, is that Chrome's identity: it listens on a port it picked
itself, and `browser_ws` only trusts the process that owns the profile and listens there,
so whatever else binds a port is never driven. Logins persist in the profile dir. The shared
instance (the default profile) stays up between runs and a watchdog closes it after
IDLE_CLOSE seconds without a client; any other instance closes when the ck-browser process
that used it exits.
"""

import base64
import http.client
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Chrome's own default: a websocket there would be the human's browser, never ours.
CHROME_DEFAULT_PORT = 9222
LAUNCH_TIMEOUT = 30
CONNECT_TIMEOUT = 10
# CDP says nothing between commands, so recv treats this as idle, not dead; only call() has a
# deadline for answers. sendall gets it as its deadline: a peer that stops reading fails the send.
IDLE_TIMEOUT = 30
# Seconds without a client before the watchdog closes our Chrome, and how often it looks.
IDLE_CLOSE = 300
WATCH_POLL = 15
# Both live inside the profile dir: when a client last used this Chrome, and its watchdog.
LAST_USE = "ck-last-use"
WATCHDOG_PID = "ck-watchdog.pid"
# Chrome writes both into its user data dir and may leave them behind when it exits: the port
# it listens on and its browser path, and a symlink to <hostname>-<pid> of the browser process.
ACTIVE_PORT = "DevToolsActivePort"
SINGLETON_LOCK = "SingletonLock"
# The importable package name (whatever this copy is installed as) and the directory holding it:
# subprocesses run `-m PACKAGE` from ROOT, and the extension lives at ROOT/extension.
PACKAGE = __package__
ROOT = Path(__file__).resolve().parents[len(PACKAGE.split("."))]

MAC_CHROMES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
PATH_CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

NO_CHROME = (
    "no Chrome binary found: install Google Chrome or Chromium, or point CK_BROWSER_CHROME"
    " at the executable"
)

# Profile prefs set before every launch. Chrome checks a submitted password against known
# breaches and, on a hit (every published demo password), shows a dialog over the tab that
# silently drops all input to the page until it is closed — CDP cannot close it.
PROFILE_PREFS = {"profile": {"password_manager_leak_detection": False}}

LAUNCHED = []
# Resolved profile dir -> the ck-last-use mtime this process last wrote.
_LAST_TOUCH = {}


class CdpError(RuntimeError):
    """The browser connection or a CDP command failed."""


class StepTimeout(CdpError):
    """The step's deadline passed: no call may wait past it."""


class PageCrashed(CdpError):
    """The tab's renderer is gone: nothing on its session will ever answer."""


class StalePage(ValueError):
    """The page an action was decided on has moved on, or its document was replaced."""


def home():
    """Where ck-browser keeps its state: CK_BROWSER_HOME, default ~/.ck-browser."""
    return Path(os.environ.get("CK_BROWSER_HOME") or Path.home() / ".ck-browser").expanduser()


def default_profile_dir():
    """Our Chrome's user data dir: CK_BROWSER_PROFILE_DIR, else <home>/chrome-profile."""
    return Path(os.environ.get("CK_BROWSER_PROFILE_DIR") or home() / "chrome-profile").expanduser()


def browser_ws(profile_dir=None):
    """Browser websocket URL of the Chrome that owns `profile_dir`, verified, else CdpError.

    Both files may be stale, so the pid in SingletonLock must be alive, ours, running on this
    --user-data-dir, and listening on the port DevToolsActivePort names.
    """
    profile_dir = Path(profile_dir or default_profile_dir()).resolve()
    try:
        port, path = (profile_dir / ACTIVE_PORT).read_text().splitlines()[:2]
        port = int(port)
    except (OSError, ValueError):
        raise _refuse(profile_dir, f"no readable {ACTIVE_PORT}") from None
    if not path.startswith("/devtools/browser/"):
        raise _refuse(profile_dir, f"{ACTIVE_PORT} names no browser path")
    if port == CHROME_DEFAULT_PORT:
        raise _refuse(profile_dir, f"it names port {CHROME_DEFAULT_PORT}, Chrome's default: never ours")
    _verify_owner(profile_dir, port)
    return f"ws://127.0.0.1:{port}{path}"


def _refuse(profile_dir, why):
    return CdpError(f"no verified Chrome for profile {profile_dir}: {why}")


def _verify_owner(profile_dir, port):
    """CdpError unless the pid in SingletonLock is alive, ours, on this --user-data-dir and listening on `port`."""
    try:
        pid = int(os.readlink(profile_dir / SINGLETON_LOCK).rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        raise _refuse(profile_dir, f"no readable {SINGLETON_LOCK}") from None
    if pid <= 0 or not _alive(pid):
        raise _refuse(profile_dir, f"pid {pid} is not running")
    found = subprocess.run(["ps", "-p", str(pid), "-ww", "-o", "uid=,command="],
                           capture_output=True, text=True).stdout.strip()
    uid, _, command = found.partition(" ")
    if uid != str(os.getuid()):
        raise _refuse(profile_dir, f"pid {pid} is not running as this user")
    if f" --user-data-dir={profile_dir} " not in f" {command.strip()} ":
        raise _refuse(profile_dir, f"pid {pid} is not a Chrome on this profile dir")
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    # ponytail: no lsof, no listener check; the pid's owner and profile dir are still verified.
    if lsof:
        listening = subprocess.run([lsof, "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                   capture_output=True, text=True).stdout.split()
        if str(pid) not in listening:
            raise _refuse(profile_dir, f"pid {pid} is not listening on port {port}")


def _adopt_port(profile_dir, port):
    """Browser websocket URL of our Chrome on a fixed `port`, recorded in DevToolsActivePort, else CdpError.

    Chrome writes that file only for --remote-debugging-port=0. For a fixed port we write it, once
    the owner is verified on that port, so everything else keeps reading the one file.
    """
    profile_dir = Path(profile_dir).resolve()
    if port in (0, CHROME_DEFAULT_PORT):
        raise _refuse(profile_dir, f"port {port} is never adopted")
    _verify_owner(profile_dir, port)  # before trusting whatever answers on the port
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as answer:
            path = urllib.parse.urlsplit(json.load(answer)["webSocketDebuggerUrl"]).path
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        raise _refuse(profile_dir, f"port {port} serves no /json/version") from None
    if not path.startswith("/devtools/browser/"):
        raise _refuse(profile_dir, f"port {port} names no browser path")
    with open(os.open(profile_dir / ACTIVE_PORT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
        out.write(f"{port}\n{path}\n")
    return f"ws://127.0.0.1:{port}{path}"


def _find_chrome(profile_dir, port):
    """browser_ws, then for a fixed port the Chrome on it that wrote no DevToolsActivePort."""
    try:
        return browser_ws(profile_dir)
    except CdpError:
        if not port:
            raise
    return _adopt_port(profile_dir, port)


def ws_port(ws):
    """The port in a browser websocket URL; None for one without (an ext: socket)."""
    return urllib.parse.urlsplit(ws).port


def chrome_binary():
    """Path to the Chrome/Chromium executable to launch."""
    override = os.environ.get("CK_BROWSER_CHROME")
    if override:
        return override
    for path in MAC_CHROMES if sys.platform == "darwin" else []:
        if Path(path).exists():
            return path
    for name in PATH_CHROMES:
        found = shutil.which(name)
        if found:
            return found
    raise CdpError(NO_CHROME)


def is_shared(profile_dir):
    """The default instance every run reuses: the Chrome on the default profile dir."""
    return Path(profile_dir or default_profile_dir()).resolve() == default_profile_dir().resolve()


def mark_used(profile_dir):
    """Touch the profile's last-use file and remember the mtime this process wrote."""
    path = Path(profile_dir) / LAST_USE
    path.touch()
    mtime = path.stat().st_mtime_ns
    _LAST_TOUCH[str(Path(profile_dir).resolve())] = mtime
    return mtime


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True  # PermissionError too: it exists, owned by someone else


def _is_watchdog(pid):
    """A live pid that is still a ck-browser watchdog, not a stranger that reused the pid."""
    if pid <= 0 or not _alive(pid):
        return False
    found = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return f"{PACKAGE} watch" in found.stdout


def ensure_chrome(port=0, profile_dir=None, headless=True, launch=True):
    """Browser websocket URL for our own Chrome on `profile_dir`, starting it if it is not up yet.

    A launch listens on `port`; 0 lets Chrome pick a free one.
    """
    if port == CHROME_DEFAULT_PORT:
        raise CdpError(f"port {CHROME_DEFAULT_PORT} is Chrome's default remote-debugging port;"
                       " ck-browser only drives its own instance — pick another port")
    profile_dir = Path(profile_dir or default_profile_dir()).resolve()
    # Its cookies sit under a mock keychain: only the dir's permissions keep them private.
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    try:
        ws = _find_chrome(profile_dir, port)
    except CdpError:
        if not launch:
            raise
    else:
        # A Chrome launched by an older ck-browser has no watchdog yet; this gives it one.
        mark_used(profile_dir)
        _ensure_watchdog(profile_dir, None)
        return ws
    set_profile_prefs(profile_dir)
    (profile_dir / ACTIVE_PORT).unlink(missing_ok=True)  # a stale one names a dead port
    command = [
        chrome_binary(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # Throwaway automation profiles: a locked or unreachable OS keychain must
        # not leave cookies memory-only (lost on relaunch). macOS / Linux switches.
        "--use-mock-keychain",
        "--password-store=basic",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--window-size=1280,900",
    ]
    if headless:
        command.append("--headless=new")
    command.append("about:blank")
    log_path = profile_dir / "chrome.log"
    try:
        with _open_log(profile_dir) as log:
            chrome = subprocess.Popen(command, start_new_session=True, stdout=log, stderr=log)
    except OSError as exc:
        raise CdpError(f"could not start {command[0]}: {exc}") from None
    # Kept in LAUNCHED: the child outlives us on purpose, and an unreferenced
    # Popen warns about the running subprocess when it is collected.
    LAUNCHED.append(chrome)
    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.1)
        try:
            ws = _find_chrome(profile_dir, port)
        except CdpError:
            continue
        # Only once CDP is up: a watchdog that finds no endpoint takes Chrome for gone and exits.
        mark_used(profile_dir)
        _ensure_watchdog(profile_dir, chrome.pid)
        return ws
    _stop_launch(chrome)
    raise CdpError(f"Chrome did not serve CDP for {profile_dir} within {LAUNCH_TIMEOUT}s; see {log_path};"
                   " the late Chrome was stopped")


def _stop_launch(chrome):
    """SIGTERM a launched Chrome's process group, SIGKILL it after 5s: it must not keep SingletonLock."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(chrome.pid, sig)  # its own group: launched with start_new_session
        except ProcessLookupError:
            return
        try:
            chrome.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _open_log(profile_dir):
    """chrome.log for appending, readable by this user only."""
    path = Path(profile_dir) / "chrome.log"
    return open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")


def set_profile_prefs(profile_dir):
    """Merge PROFILE_PREFS into the profile's Preferences; only before a launch, as a running
    Chrome writes its own copy over the file. Unreadable or damaged prefs are left to Chrome."""
    path = Path(profile_dir) / "Default" / "Preferences"
    try:
        prefs = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return
    if not isinstance(prefs, dict):
        return
    for section, values in PROFILE_PREFS.items():
        if not isinstance(prefs.get(section), dict):
            prefs[section] = {}
        prefs[section].update(values)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs))
    except OSError:
        pass


def _ensure_watchdog(profile_dir, chrome_pid):
    """Start the process that closes our Chrome once it sits idle, unless one is watching already.

    A fresh launch always gets its own: the previous watchdog may still be sleeping on the Chrome
    that just died, and exits on its next look without touching the new pidfile.
    """
    pidfile = Path(profile_dir) / WATCHDOG_PID
    try:
        pid = int(pidfile.read_text())
    except (OSError, ValueError):
        pid = 0
    if not chrome_pid and _is_watchdog(pid):
        return
    # Absolute: the watchdog's cwd is ROOT, not ours.
    command = [sys.executable, "-m", PACKAGE, "watch", "--profile-dir", os.path.abspath(profile_dir)]
    if chrome_pid:
        command += ["--chrome-pid", str(chrome_pid)]
    try:
        with _open_log(profile_dir) as log:
            # From ROOT and with ROOT on PYTHONPATH, so `-m PACKAGE` imports whatever the
            # caller's cwd, PYTHONSAFEPATH included.
            path = os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH"))))
            watchdog = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log, cwd=ROOT,
                                        env={**os.environ, "PYTHONPATH": path})
        LAUNCHED.append(watchdog)  # outlives us on purpose, as Chrome does
        pidfile.write_text(str(watchdog.pid))
    except OSError:
        pass  # the run works without one; Chrome then stays up until `ck-browser stop`


def _attached_pages(cdp):
    """Page targets some client is driving right now."""
    targets = cdp.call("Target.getTargets")["targetInfos"]
    return [target for target in targets if target["type"] == "page" and target["attached"]]


def _close_browser(cdp):
    try:
        cdp.call("Browser.close")
    except CdpError:  # Chrome drops the socket as it exits, often before answering
        pass


def release_chrome(profile_dir=None):
    """Close our Chrome when this process was its last user and no client is driving it.

    The shared instance is left to its watchdog. True when Browser.close was sent.
    """
    profile_dir = Path(profile_dir or default_profile_dir())
    if is_shared(profile_dir):
        return False
    try:
        mtime = (profile_dir / LAST_USE).stat().st_mtime_ns
    except OSError:
        return False
    if mtime != _LAST_TOUCH.get(str(profile_dir.resolve())):
        return False  # another ck-browser used it since, or we never did
    try:
        cdp = Cdp(browser_ws(profile_dir), timeout=5)
    except (CdpError, OSError):
        return False
    try:
        if _attached_pages(cdp):
            return False
        _close_browser(cdp)
    except CdpError:
        return False
    finally:
        cdp.close()
    return True


def watch(profile_dir, chrome_pid=None, idle=IDLE_CLOSE, poll=WATCH_POLL,
          clock=time.time, sleep=time.sleep):
    """Close our Chrome on `profile_dir` once no client has used it for `idle` seconds.

    Returns "closed", or "gone" when Chrome went away first. Each look is a short-lived
    connection, so the watchdog never counts as a client itself.
    """
    # Safe by construction: browser_ws only finds the Chrome running on our own profile dir,
    # never on Chrome's default port 9222.
    profile_dir = Path(profile_dir)
    last_use = profile_dir / LAST_USE
    # An attached page counts as use in memory only: touching ck-last-use here would make the
    # run that owns the page see a newer mtime and skip release_chrome on exit.
    seen = 0.0
    try:
        while True:
            if chrome_pid and not _alive(chrome_pid):
                return "gone"
            try:
                cdp = Cdp(browser_ws(profile_dir), timeout=5)
                try:
                    if _attached_pages(cdp):
                        seen = clock()
                    if not last_use.exists():
                        last_use.touch()
                    if clock() - max(seen, last_use.stat().st_mtime) >= idle:
                        _close_browser(cdp)
                        break
                finally:
                    cdp.close()
            except (CdpError, OSError):
                return "gone"
            sleep(poll)
        for _ in range(10):
            if not chrome_pid or not _alive(chrome_pid):
                return "closed"
            sleep(0.5)
        try:
            os.kill(chrome_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return "closed"
    finally:
        pidfile = profile_dir / WATCHDOG_PID
        try:
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass


class WebSocket:
    """Minimal RFC 6455 text client — enough for CDP's multi-megabyte frames."""

    def __init__(self, url):
        scheme, _, rest = url.partition("://")
        if scheme != "ws":
            raise CdpError(f"unsupported websocket scheme {scheme!r}")
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=CONNECT_TIMEOUT)
        self.buffer = b""
        self.lock = threading.Lock()
        self._handshake(hostport, "/" + path)
        self.sock.settimeout(IDLE_TIMEOUT)

    def _handshake(self, hostport, path):
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("connection closed during websocket handshake")
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise CdpError(f"websocket upgrade refused: {status}")

    def _read(self, count):
        while len(self.buffer) < count:
            try:
                chunk = self.sock.recv(max(4096, count - len(self.buffer)))
            except TimeoutError:
                continue  # an idle connection, not a broken one
            if not chunk:
                raise CdpError("connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _send(self, opcode, payload):
        """One client frame. RFC 6455: every frame a client sends is masked, pongs included."""
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += struct.pack("!B", length | 0x80)
        elif length < 65536:
            header += b"\xfe" + struct.pack("!H", length)
        else:
            header += b"\xff" + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        with self.lock:
            self.sock.sendall(header + mask + masked)

    def send(self, text):
        self._send(0x1, text.encode())

    def _frame(self):
        first, second = self._read(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        mask = self._read(4) if second & 0x80 else None
        payload = self._read(length) if length else b""
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return bool(first & 0x80), first & 0x0F, payload

    def recv(self):
        """Next complete text message, reassembling continuations and answering pings."""
        opcode, chunks = None, []
        while True:
            final, code, payload = self._frame()
            if code == 0x8:
                raise CdpError("connection closed")
            if code == 0x9:
                self._send(0xA, payload)
                continue
            if code == 0xA:
                continue
            if code != 0x0:
                opcode, chunks = code, []
            chunks.append(payload)
            if final:
                break
        if opcode != 0x1:
            return None
        return b"".join(chunks).decode(errors="replace")

    def close(self):
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        self.sock.close()


class Cdp:
    """One websocket to the browser; a reader thread fans messages out to callers and events."""

    def __init__(self, ws_url, timeout=30):
        self.ws = WebSocket(ws_url)
        self._start(timeout)

    def _start(self, timeout):
        self.timeout = timeout
        self.calls = 0
        self.events = []
        self.closed = None
        # A time.monotonic() no call waits past (the runner's step deadline), and the method whose
        # wait it last cut.
        self.deadline = None
        self.waiting_on = None
        self._id = 0
        self._pending = {}
        # Sessions whose renderer crashed (Inspector.targetCrashed): their calls fail at once.
        self._crashed = set()
        # (method, session id) -> a handler that takes those events instead of self.events (on_event).
        self._hooks = {}
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _receive(self):
        """The next message, or None for a frame that carries none."""
        raw = self.ws.recv()
        return None if raw is None else json.loads(raw)

    def _send(self, request):
        try:
            self.ws.send(json.dumps(request))
        except OSError as exc:  # a half-sent frame breaks the stream: no later frame can be read
            self._fail(f"send: {exc}")
            raise CdpError(f"{request['method']}: send: {exc}") from None

    def _read_loop(self):
        while True:
            try:
                message = self._receive()
                if message is None:
                    continue
            except CdpError as exc:  # the transport says why it ended
                self._fail(str(exc))
                return
            except Exception as exc:  # thread boundary: anything here means no answers will come
                self._fail(f"reader: {exc!r}")
                return
            if "id" in message:
                with self._lock:
                    slot = self._pending.get(message["id"])
                if slot:
                    slot["message"] = message
                    slot["event"].set()
            elif "method" in message:
                session_id = message.get("sessionId")
                with self._lock:
                    hook = self._hooks.get((message["method"], session_id))
                if hook is not None:
                    try:
                        hook(message.get("params") or {})
                    except Exception:  # thread boundary: a handler's fault must not end every call's answers
                        pass
                    continue
                waiters = []
                with self._lock:
                    self.events.append({
                        "method": message["method"],
                        "params": message.get("params") or {},
                        "sessionId": session_id,
                        "ts": time.time(),
                    })
                    if message["method"] == "Inspector.targetCrashed" and session_id:
                        self._crashed.add(session_id)
                        waiters = [slot for slot in self._pending.values() if slot["session"] == session_id]
                for slot in waiters:
                    slot["event"].set()

    def _fail(self, reason):
        """The reader is gone: waiting calls must fail now instead of hanging on their event.

        Under the lock calls register under: a call either sees `closed` or is woken here.
        """
        with self._lock:
            self.closed = reason
            waiters = list(self._pending.values())
        for slot in waiters:
            slot["event"].set()

    def _check(self, method, session_id):
        """A dead transport says why it died, even past the step deadline; a crashed page says so."""
        if self.closed:
            raise CdpError(f"{method}: {self.closed}")
        if session_id in self._crashed:
            raise PageCrashed(f"{method}: the page crashed (renderer gone)")

    def _unanswered(self, method, session_id):
        """A slot woken without its answer: the page crashed or the reader died."""
        if session_id in self._crashed:
            return PageCrashed(f"{method}: the page crashed (renderer gone)")
        return CdpError(f"{method}: {self.closed or 'connection closed'}")

    def _wait(self, method, timeout):
        """(seconds the call may wait, whether the step deadline rather than `timeout` set them)."""
        wait = timeout or self.timeout
        if self.deadline is None:
            return wait, False
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise StepTimeout(f"{method}: no answer before the step deadline")
        return min(wait, left), left < wait

    def _timed_out(self, method, wait, by_deadline):
        if by_deadline:
            self.waiting_on = method
            return StepTimeout(f"{method}: no answer before the step deadline")
        return CdpError(f"{method}: no answer in {wait:g}s")

    def call(self, method, params=None, session_id=None, timeout=None):
        self._check(method, session_id)
        wait, by_deadline = self._wait(method, timeout)
        with self._lock:
            # The reader died or the page crashed since: only the slots registered before are woken.
            self._check(method, session_id)
            self._id += 1
            call_id = self._id
            slot = {"event": threading.Event(), "message": None, "session": session_id}
            self._pending[call_id] = slot
            self.calls += 1
        request = {"id": call_id, "method": method, "params": params or {}}
        if session_id:
            request["sessionId"] = session_id
        try:
            self._send(request)
            if not slot["event"].wait(wait):
                raise self._timed_out(method, wait, by_deadline)
        finally:
            with self._lock:
                self._pending.pop(call_id, None)
        message = slot["message"]
        if message is None:
            raise self._unanswered(method, session_id)
        if "error" in message:
            raise CdpError(f"{method}: {message['error'].get('message', message['error'])}")
        return message.get("result") or {}

    def call_many(self, method, params_list, session_id=None, timeout=None):
        """One method over many params, pipelined: every request goes out before any reply is awaited.

        Returns each item's result, or the CdpError for that item; raises only when the transport
        is closed, the page crashed or the step deadline passes, as `call` does.
        """
        self._check(method, session_id)
        wait, by_deadline = self._wait(method, timeout)
        slots = []
        with self._lock:
            self._check(method, session_id)
            for params in params_list:
                self._id += 1
                slots.append((self._id, {"event": threading.Event(), "message": None, "session": session_id},
                              params))
                self._pending[self._id] = slots[-1][1]
                self.calls += 1
        out = []
        try:
            for call_id, _slot, params in slots:
                request = {"id": call_id, "method": method, "params": params or {}}
                if session_id:
                    request["sessionId"] = session_id
                self._send(request)
            deadline = time.monotonic() + wait
            for _call_id, slot, _params in slots:
                if not slot["event"].wait(max(0, deadline - time.monotonic())):
                    timed_out = self._timed_out(method, wait, by_deadline)
                    if by_deadline:
                        raise timed_out
                    out.append(timed_out)
                    continue
                message = slot["message"]
                if message is None:
                    raise self._unanswered(method, session_id)
                if "error" in message:
                    out.append(CdpError(f"{method}: {message['error'].get('message', message['error'])}"))
                else:
                    out.append(message.get("result") or {})
        finally:
            with self._lock:
                for call_id, _slot, _params in slots:
                    self._pending.pop(call_id, None)
        return out

    def send_only(self, method, params=None, session_id=None):
        """Fire and forget, for cleanup: no slot and no wait (the reader drops the answer to an id
        nobody waits on), and never an error."""
        with self._lock:
            if self.closed or session_id in self._crashed:
                return
            self._id += 1
            call_id = self._id
            self.calls += 1
        request = {"id": call_id, "method": method, "params": params or {}}
        if session_id:
            request["sessionId"] = session_id
        try:
            self._send(request)
        except (CdpError, OSError):
            pass

    def on_event(self, method, session_id, handle):
        """`handle(params)` takes this session's `method` events in the reader thread, never kept in
        self.events (a screencast's frames would pile up there). It runs before the next message is
        read, so it must be quick; it may send_only, never call (the answer would wait on itself)."""
        with self._lock:
            self._hooks[(method, session_id)] = handle

    def off_event(self, method, session_id):
        with self._lock:
            self._hooks.pop((method, session_id), None)

    def peek_events(self, session_id):
        """This session's events, left in place for take_events."""
        with self._lock:
            return [e for e in self.events if e["sessionId"] == session_id]

    def take_events(self, session_id):
        with self._lock:
            mine = [e for e in self.events if e["sessionId"] == session_id]
            self.events = [e for e in self.events if e["sessionId"] != session_id]
        return mine

    def close(self):
        self.ws.close()


class Tab:
    """An owned background tab: a CDP session plus the console/network events it produced."""

    def __init__(self, cdp, target_id, session_id, width=1280, height=800):
        self.cdp = cdp
        self.target_id = target_id
        self.session_id = session_id
        self.width = width
        self.height = height
        self.after_input = None
        self._requests = {}

    @classmethod
    def open(cls, cdp, width=1280, height=800):
        target_id = cdp.call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
        session_id = cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        tab = cls(cdp, target_id, session_id, width, height)
        tab.set_viewport(width, height)
        # Keep rAF/menus rendering in a background tab, without bringing it to the front.
        tab.call("Emulation.setFocusEmulationEnabled", enabled=True)
        for domain in ("Page", "Runtime", "Log", "Network"):
            tab.call(domain + ".enable")
        return tab

    def call(self, method, **params):
        return self.cdp.call(method, params, session_id=self.session_id)

    def call_many(self, method, params_list):
        return self.cdp.call_many(method, params_list, session_id=self.session_id)

    def send_only(self, method, **params):
        self.cdp.send_only(method, params, session_id=self.session_id)

    def set_viewport(self, width, height):
        """Resize the emulated viewport; `self.width`/`self.height` stay the configured size."""
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=False)

    def evaluate(self, expression, await_promise=False, command_line_api=False):
        # The command-line API gives the page look getEventListeners for this one evaluate.
        params = {"includeCommandLineAPI": True} if command_line_api else {}
        response = self.call(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=await_promise,
            **params,
        )
        if response.get("exceptionDetails"):
            raise StalePage("the page threw while evaluating a script (its document was likely replaced)")
        return response.get("result", {}).get("value")

    def navigate(self, url, timeout=20):
        error = self.call("Page.navigate", url=url).get("errorText")
        if error:
            raise CdpError(f"navigate {url}: {error}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except StalePage:
                pass  # expected mid-navigation; the document is being replaced
            time.sleep(0.02)
        raise CdpError(f"navigation to {url} did not complete in {timeout}s")

    def dismiss_dialog(self):
        """A JavaScript dialog blocks the page's renderer, so every evaluate waits on it: dismiss
        the open one and say what it was and whether it went; None when no dialog is open."""
        opened = None
        for event in self.cdp.peek_events(self.session_id):
            if event["method"] == "Page.javascriptDialogOpening":
                opened = event["params"]
            elif event["method"] == "Page.javascriptDialogClosed":
                opened = None
        if opened is None:
            return None
        what = f'a JavaScript {opened.get("type") or "dialog"} is open: "{opened.get("message", "")}"'
        try:
            self.call("Page.handleJavaScriptDialog", accept=False)
        except CdpError:  # the extension refuses it: the human's tab keeps its dialog
            return what + " (still open)"
        return what + " (dismissed)"

    def screenshot_png(self, path):
        data = self.call("Page.captureScreenshot", format="png")["data"]
        Path(path).write_bytes(base64.b64decode(data))
        return str(path)

    def check_write(self, node):
        """A scripted write may go ahead: our own Chrome has no policy to ask (ExtTab's has)."""

    def drain_events(self):
        """Console errors and failed requests seen since the last drain."""
        console_errors, failed_requests = [], []
        for event in self.cdp.take_events(self.session_id):
            method, params = event["method"], event["params"]
            if method == "Runtime.consoleAPICalled" and params.get("type") in {"error", "assert"}:
                parts = [str(a.get("value", a.get("description", ""))) for a in params.get("args") or []]
                console_errors.append({"level": "error", "text": " ".join(p for p in parts if p)})
            elif method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails") or {}
                exception = details.get("exception") or {}
                text = " ".join(t for t in (details.get("text"), exception.get("description")) if t)
                console_errors.append({"level": "exception", "text": text})
            elif method == "Log.entryAdded":
                entry = params.get("entry") or {}
                if entry.get("level") == "error":
                    text = " ".join(t for t in (entry.get("text"), entry.get("url")) if t)
                    console_errors.append({"level": "error", "text": text})
            elif method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                self._requests[params.get("requestId")] = {
                    "method": request.get("method", ""), "url": request.get("url", ""),
                }
            elif method == "Network.responseReceived":
                response = params.get("response") or {}
                status = response.get("status") or 0
                if status >= 400:
                    request = self._requests.get(params.get("requestId"), {})
                    failed_requests.append({
                        "method": request.get("method", ""),
                        "url": request.get("url") or response.get("url", ""),
                        "status": status,
                    })
            elif method == "Network.loadingFailed" and params.get("errorText") != "net::ERR_ABORTED":
                request = self._requests.get(params.get("requestId"), {})
                failed_requests.append({
                    "method": request.get("method", ""), "url": request.get("url", ""),
                    "status": None, "error": params.get("errorText", ""),
                })
        return {"console_errors": console_errors, "failed_requests": failed_requests}

    def close(self):
        if self.target_id:
            self.cdp.call("Target.closeTarget", {"targetId": self.target_id})
            self.target_id = None

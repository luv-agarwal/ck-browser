"""Drive one tab of the human's own Chrome: one they handed over in the ck-browser extension's
popup, or a new one the extension opened for ck-browser on a domain they allowed.

ck-browser run  <->  unix socket <bridge_dir>/profiles/<instance>.sock  <->  this module's host
(spawned by Chrome via connectNative)  <->  native messaging on stdio  <->  extension/background.js
<->  chrome.debugger on the claimed tab. Both hops carry native messaging's framing. The host lives
as long as the extension's port, i.e. while Chrome runs, and outlives each claim; stdin EOF ends
it. Every Chrome profile running the extension starts its own host, which binds a socket named
after the profile's instance id once the extension's first state names it, and keeps a sidecar
<instance>.json beside it (who the profile is: its email from chrome.identity, its directory and
name from Chrome's Local State; its claimed tab; its policy). A client lists profiles from the
sidecars alone and dials the one it picked, never another (resolve). Each host writes what it saw
to host.log in bridge_dir. The extension is the policy point (allowed methods, payment fields,
frames, masking, which tabs it opens); the host relays, and writes what the extension cannot: a
captured secret into the login Keychain. A typed message the core does not know goes to the
CK_BROWSER_PLUGINS plugin that handles it (plugins.py), and the host sends back its answer.
"""

import base64
import hashlib
import json
import os
import pwd
import re
import shlex
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import plugins
from .cdp import CONNECT_TIMEOUT, PACKAGE, ROOT, Cdp, CdpError, StalePage, Tab, home
from .script import KEYCHAIN_SERVICE

EXTENSION_DIR = ROOT / "extension"
HOST_NAME = "com.ck_browser.host"
HOST_MODULE = f"{PACKAGE}.ext_host"
DEFAULT_CHROME_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
DEFAULT_NM_DIR = DEFAULT_CHROME_DIR / "NativeMessagingHosts"

HEADER = struct.Struct("=I")  # native messaging: length in native byte order, then UTF-8 JSON
MAX_FRAME = 64 * 1024 * 1024
# Chrome drops a native host that sends it a message over 1 MB.
MAX_TO_EXTENSION = 1024 * 1024

BUSY = "busy: another ck-browser run holds the tab"
HANDOFF_WAIT = 0.5  # seconds a new client waits for one that is hanging up
LOG_MAX = 256 * 1024  # host.log rolls over to host.log.1 past this
# A new tab starts on about:blank, whose readyState is already complete.
LOADED = 'location.href === "about:blank" ? "blank" : document.readyState'
# The extension's own refusals of a screenshot, while the page shows a secret or cannot be checked.
WITHHELD = "screenshot withheld:"
# Each `security` call; a locked Keychain asks the human for its password meanwhile. The
# extension waits for all three calls, ck-browser for the extension.
SECURITY_TIMEOUT = 60
CAPTURE_TIMEOUT = 240
# Printable ASCII without space, quotes or backslash: a token `security -i` reads as it is.
SECRET_VALUE = re.compile(r"[\x21\x23-\x26\x28-\x5b\x5d-\x7e]{8,4096}")
KEYCHAIN_ACCOUNT = re.compile(r"[A-Za-z0-9._@-]{1,255}")
EXISTS = ("keychain item {service} already exists — refusing to overwrite; delete it yourself"
          " (security delete-generic-password -a {account} -s {service}) to replace it")
NO_BRIDGE = ("no live ck-browser bridge at {path} ({state}): its Chrome profile's host just went away;"
             " reload the extension at chrome://extensions, or restart Chrome")
NO_PROFILES = ("no Chrome profile has the ck-browser extension connected (no live bridge in {dir}): start"
               " Chrome, or reload the extension at chrome://extensions")
UNKNOWN_PROFILE = "(profile unknown: not signed in to Chrome, or reload the ck-browser extension)"
# Typed messages the core answers itself; any other goes to a plugin.
CORE_TYPES = ("hello", "detached", "state", "keychain_write")
EXTENSION_ID = re.compile(r"[a-p]{32}")
BAD_EXTENSION_ID = ("extension ID {id!r} (from --extension-id or CK_BROWSER_EXTENSION_ID) is not 32 letters a-p,"
                    " as chrome://extensions shows it")
NO_EXTENSION = ("no extension at {dir} (this copy was built without it): run ext install --extension-id <the ID"
                " chrome://extensions shows for the extension you loaded>, or set CK_BROWSER_EXTENSION_ID")
# The per-profile id background.js keeps in chrome.storage.local; it names the socket file.
INSTANCE = re.compile(r"[A-Za-z0-9-]{1,36}")
NO_TAB = ("no tab to drive: click the ck-browser icon on a tab and choose Hand over, or give"
          " the script a url so ck-browser opens its own tab (agents may open tabs: {enabled};"
          " allowed domains: {domains})")


class FrameError(ValueError):
    """A native-messaging frame was truncated or over its size limit."""


class Refusal(CdpError):
    """The extension answered no: its own words say why and what to do."""


def bridge_dir():
    """Where the hosts keep host.log and profiles/: CK_BROWSER_HOME; CK_BROWSER_BRIDGE_DIR redirects it (tests)."""
    return Path(os.environ.get("CK_BROWSER_BRIDGE_DIR") or home())


def profiles_dir():
    """One socket and one sidecar per Chrome profile whose extension has a host."""
    return bridge_dir() / "profiles"


def chrome_dir():
    """The human's Chrome user-data dir, which holds Local State; CK_BROWSER_CHROME_DIR redirects it (tests)."""
    return Path(os.environ.get("CK_BROWSER_CHROME_DIR") or DEFAULT_CHROME_DIR)


def chrome_profiles():
    """[(directory, display name, email)] from Chrome's Local State; [] when it cannot be read."""
    try:
        cache = json.loads((chrome_dir() / "Local State").read_text())["profile"]["info_cache"]
        return [(directory, info.get("name") or "", info.get("user_name") or "")
                for directory, info in cache.items()]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []  # identity is a label: a missing or odd Local State leaves it unknown


def chrome_profile(email):
    """(directory, display name) of the profile signed in as `email`, or (None, None)."""
    if email:
        for directory, name, user in chrome_profiles():
            if user.casefold() == email.casefold():
                return directory, name
    return None, None


def write_frame(target, obj, limit=MAX_FRAME):
    """One frame to a socket or a binary stream; nothing is written when it is over `limit`."""
    data = json.dumps(obj, separators=(",", ":")).encode()
    if len(data) > limit:
        raise FrameError(f"message of {len(data)} bytes is over the {limit}-byte limit")
    frame = HEADER.pack(len(data)) + data
    if isinstance(target, socket.socket):
        target.sendall(frame)
    else:
        target.write(frame)
        target.flush()


def read_frame(source):
    """The next frame from a socket or a binary stream, or None when it ends between frames."""
    header = _read_exactly(source, HEADER.size)
    if not header:
        return None
    if len(header) < HEADER.size:
        raise FrameError("stream ended inside a frame header")
    (length,) = HEADER.unpack(header)
    if length > MAX_FRAME:
        raise FrameError(f"frame of {length} bytes is over the {MAX_FRAME}-byte limit")
    data = _read_exactly(source, length)
    if len(data) < length:
        raise FrameError(f"stream ended {length - len(data)} bytes short of a frame")
    return json.loads(data)


def _read_exactly(source, count):
    """Up to `count` bytes, fewer only at end of stream."""
    buffer = bytearray(count)
    view = memoryview(buffer)
    read_into = source.recv_into if isinstance(source, socket.socket) else source.readinto
    got = 0
    while got < count:
        n = read_into(view[got:])
        if not n:
            break
        got += n
    return buffer[:got]


def _spelled(data):
    """Chrome's ID spelling: sha256 of `data`, first 128 bits, each hex digit d as the letter 'a'+d."""
    return "".join(chr(ord("a") + int(digit, 16)) for digit in hashlib.sha256(data).hexdigest()[:32])


def extension_id(key):
    """Chrome's ID for an extension with this manifest `key` (the DER public key, base64)."""
    return _spelled(base64.b64decode(key))


def unpacked_extension_id(path):
    """Chrome's ID for an unpacked extension without a `key`: from its directory's absolute real
    path, UTF-8. Loading the same directory from another path gives another ID."""
    return _spelled(os.path.realpath(path).encode())


def our_extension_id(chosen=None):
    """`chosen`, else CK_BROWSER_EXTENSION_ID, else the manifest key's ID, else the unpacked ID of
    EXTENSION_DIR. CdpError for a malformed ID, or for no ID and no extension to derive one from."""
    chosen = chosen or os.environ.get("CK_BROWSER_EXTENSION_ID")
    if chosen:
        if not EXTENSION_ID.fullmatch(chosen):
            raise CdpError(BAD_EXTENSION_ID.format(id=chosen))
        return chosen
    try:
        key = json.loads((EXTENSION_DIR / "manifest.json").read_text()).get("key")
    except FileNotFoundError:
        raise CdpError(NO_EXTENSION.format(dir=EXTENSION_DIR)) from None
    return extension_id(key) if key else unpacked_extension_id(EXTENSION_DIR)


def extension_origin():
    return f"chrome-extension://{our_extension_id()}/"


# -- the native-messaging host -------------------------------------------------


def log(line):
    """To stderr, which Chrome swallows, and to host.log, which outlives the host. Never a value."""
    print(f"ck-browser host: {line}", file=sys.stderr, flush=True)  # stdout carries only frames
    path = bridge_dir() / "host.log"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX:
            path.replace(path.with_name("host.log.1"))
        with open(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as out:
            out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} {line}\n")
    except OSError:
        pass  # the log is for diagnosis; the bridge never fails over it


def host_main(argv):
    """Chrome starts the host with the caller's origin as the first argument."""
    origin = argv[0] if argv else ""
    try:
        ours = extension_origin()
    except CdpError as exc:
        log(f"refusing caller {origin!r}: {exc}")
        return 2
    if origin != ours:
        log(f"refusing caller {origin!r}: only {ours} may start this host")
        return 2
    return Host(sys.stdin.buffer, sys.stdout.buffer, profiles_dir()).serve()


class Host:
    """Relays one ck-browser client at a time to the extension; the claimed tab's hello and the
    extension's policy are cached for each. Serves one Chrome profile, on its own socket."""

    def __init__(self, stdin, stdout, directory):
        self.stdin = stdin
        self.stdout = stdout
        self.directory = Path(directory)
        self.hello = None  # the claimed tab's, None while no tab is claimed
        self.open = None  # the policy the extension last sent: {enabled, domains, pending}
        self.email = None  # the profile's, as the extension last sent it
        self.client = None
        self.reader = None  # the client's loop
        self.lock = threading.Lock()  # guards `client` and every write to it
        self.freed = threading.Condition(self.lock)  # a client's loop let go of `client`
        self.out_lock = threading.Lock()
        self.instance = None  # the profile's, from the first valid state; names the socket
        self.socket = None
        self.inode = None  # the socket file this host bound: a newer host's replaces it
        self.listener = None
        self.worker = None  # (instance, worker) the extension last named in its state

    def serve(self):
        for directory in (self.directory.parent, self.directory):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        log(f"start: parent pid {os.getppid()}")
        reason = "stdin closed: the extension's port is gone"
        try:
            while (message := read_frame(self.stdin)) is not None:
                self._from_extension(message)
        except (OSError, ValueError) as exc:
            reason = f"stdin failed: {exc}"
            raise
        finally:
            log(f"stop: {reason}")
            with self.lock:
                listener, self.listener = self.listener, None
                client, reader, self.client = self.client, self.reader, None
                self.freed.notify_all()
            if listener is not None:
                listener.close()
                self._let_go()
            if client is not None:
                # The client's loop closes the socket once it reads the end: a close from here
                # would fail its read mid-call, and a thread that logs while the interpreter
                # shuts down aborts it.
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                reader.join(timeout=2)
        return 0

    def _bind(self, instance):
        """Listen on the profile's socket. Whatever is at the path goes first: a dead host's, or
        the one this profile's previous port started, still exiting; the extension talks only to
        its newest port. Under self.lock."""
        self.instance = instance
        self.socket = self.directory / f"{instance}.sock"
        self.socket.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.socket))
        self.socket.chmod(0o600)
        self.inode = self.socket.stat().st_ino
        listener.listen()
        self.listener = listener
        threading.Thread(target=self._accept_loop, args=(listener,), daemon=True).start()
        log(f"instance {instance}: listening on {self.socket}")

    def _ours(self):
        """Whether the socket file is still the one this host bound."""
        try:
            return self.socket.stat().st_ino == self.inode
        except FileNotFoundError:
            return False

    def _sidecar(self):
        return self.directory / f"{self.instance}.json"

    def _write_sidecar(self):
        """Who this profile is, its claimed tab and its policy, for clients that list profiles
        without dialing. Written whole (tmp, then rename). Under self.lock, once bound."""
        if not self._ours():
            log(f"superseded: a newer host serves instance {self.instance}; its sidecar is left alone")
            return
        directory, name = chrome_profile(self.email)
        entry = {"instance": self.instance, "pid": os.getpid(), "socket": str(self.socket),
                 "email": self.email, "profile_dir": directory, "name": name,
                 "tab": None if self.hello is None else self.hello.get("tab"), "open": self.open}
        path = self._sidecar()
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
                json.dump(entry, out)
            os.replace(tmp, path)
        except OSError as exc:
            log(f"sidecar: {exc}")

    def _let_go(self):
        """Remove the socket and the sidecar, unless a newer host for this profile owns them now."""
        if self._ours():
            self.socket.unlink(missing_ok=True)
        path = self._sidecar()
        try:
            if json.loads(path.read_text()).get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass  # gone, or mid-write by the newer host: theirs either way

    def _from_extension(self, message):
        if message.get("type") == "keychain_write":  # the value never reaches a client
            threading.Thread(target=self._keychain, args=(message,), daemon=True).start()
            return
        kind = message.get("type")
        if isinstance(kind, str) and kind not in CORE_TYPES and (found := plugins.handler(kind, log)):
            # Answered here, never relayed to a client.
            threading.Thread(target=self._plugin, args=(*found, message), daemon=True).start()
            return
        with self.lock:
            kind = message.get("type")
            if kind == "hello":
                self.hello = message
                how = "re-adopted after a worker restart" if message.get("adopted") else "claimed"
                log(f"claim: tab {message.get('tab', {}).get('id')} {how}")
            elif kind == "detached":
                self.hello = None
                log(f"release: {message.get('reason')}")
            elif kind == "state" and not self._state(message):
                return
            if self.instance is not None and kind in ("hello", "detached", "state"):
                self._write_sidecar()
            if self.client is not None:
                try:
                    write_frame(self.client, message)
                except OSError as exc:  # the client is gone; its loop drops it
                    log(f"client: {exc}")

    def _state(self, message):
        """Take the extension's state; False when it is ignored. The first valid instance binds
        the socket and stays: a host serves one profile. Under self.lock."""
        instance = message.get("instance")
        if not (isinstance(instance, str) and INSTANCE.fullmatch(instance)):
            log(f"state ignored: instance {str(instance)[:40]!r} is not 1-36 letters, digits or dashes")
            return False
        if self.instance is not None and instance != self.instance:
            log(f"state ignored: instance {instance}, but this host serves {self.instance}")
            return False
        self.open = message["open"]
        self.email = (message.get("profile") or {}).get("email") or None
        self._log_worker(message)
        if self.instance is None:
            self._bind(instance)
        return True

    def _log_worker(self, message):
        """Once per worker: a new worker id under the instance is a service-worker restart."""
        worker = (message.get("instance"), message.get("worker"))
        if worker == self.worker:
            return
        self.worker = worker
        line = f"extension: instance {worker[0]} worker {worker[1]}"
        if message.get("lastDisconnect"):
            line += f"; its previous host port closed: {message['lastDisconnect']}"
        log(line)

    def _accept_loop(self, listener):
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with self.lock:
                # A client that just hung up is dropped by its own loop a moment later: a dial
                # right behind it (`ext status` then `ext close`) waits for that, not BUSY.
                self.freed.wait_for(lambda: self.client is None, timeout=HANDOFF_WAIT)
                if self.client is not None:
                    try:
                        write_frame(conn, {"type": "error", "error": BUSY})
                    except OSError:
                        pass
                    conn.close()
                    continue
                self.client = conn
                first = ({**self.hello, "open": self.open} if self.hello is not None
                         else {"type": "idle", "open": self.open})
                log(f"client connected: {first['type']}")
                try:
                    write_frame(conn, first)
                except OSError as exc:
                    log(f"client: {exc}")
                self.reader = threading.Thread(target=self._client_loop, args=(conn,), daemon=True)
                self.reader.start()

    def _client_loop(self, conn):
        try:
            while (message := read_frame(conn)) is not None:
                self._to_extension(conn, message)
        except (OSError, ValueError) as exc:
            log(f"client: {exc}")
        finally:
            with self.lock:
                if self.client is conn:
                    self.client = None
                    self.freed.notify_all()
            conn.close()

    def _keychain(self, message):
        error = keychain_write(message["service"], message["value"])
        reply = {"type": "keychain_result", "id": message["id"], "ok": error is None}
        if error is not None:
            reply["error"] = error
        try:
            with self.out_lock:
                write_frame(self.stdout, reply)
        except OSError as exc:  # the extension is gone
            log(f"extension: {exc}")

    def _plugin(self, handle, reply_type, message):
        reply = {"type": reply_type, "id": message.get("id")}
        try:
            reply["result"] = handle(message)
        except Exception as exc:  # any failure is the extension's answer; the host keeps serving
            log(f"plugin {message['type']}: {exc!r}")
            reply["error"] = str(exc) or type(exc).__name__
        try:
            with self.out_lock:
                try:
                    write_frame(self.stdout, reply, limit=MAX_TO_EXTENSION)
                except FrameError as exc:
                    write_frame(self.stdout, {"type": reply_type, "id": reply["id"],
                                              "error": f"{exc} (Chrome's native-messaging limit)"})
        except OSError as exc:  # the extension is gone
            log(f"extension: {exc}")

    def _to_extension(self, conn, message):
        if "type" in message:  # typed messages are the host's own: a client cannot forge a result
            with self.lock:
                write_frame(conn, {"id": message.get("id"),
                                   "error": {"message": "only CDP calls go over the bridge"}})
            return
        try:
            with self.out_lock:
                write_frame(self.stdout, message, limit=MAX_TO_EXTENSION)
        except FrameError as exc:
            with self.lock:
                write_frame(conn, {"id": message.get("id"),
                                   "error": {"message": f"{exc} (Chrome's native-messaging limit)"}})


def security_binary():
    """macOS's Keychain tool; CK_BROWSER_SECURITY swaps in a fake (tests never touch the Keychain)."""
    return os.environ.get("CK_BROWSER_SECURITY") or "/usr/bin/security"


def keychain_write(service, value):
    """Add `value` to the login Keychain as a new item; None when it is stored, else why not.

    Never overwrites. The value reaches `security -i` on stdin, never argv (`ps` shows argv to
    every user), and is in no returned or logged text; what security prints is discarded, since
    it can echo its input. Its exit code says little, so a read-back decides.
    """
    account = pwd.getpwuid(os.getuid()).pw_name
    if not (isinstance(service, str) and KEYCHAIN_SERVICE.fullmatch(service)
            and KEYCHAIN_ACCOUNT.fullmatch(account)):
        return "refused: the service or account name has characters a Keychain capture does not take"
    if not (isinstance(value, str) and SECRET_VALUE.fullmatch(value)):
        return "refused: the value has characters a Keychain capture does not take"
    find = ["find-generic-password", "-a", account, "-s", service]
    try:
        if _security(find).returncode == 0:
            return EXISTS.format(service=service, account=account)
        # Every token is one run of characters with no space, quote or backslash: none needs quoting.
        _security(["-i"], f"add-generic-password -a {account} -s {service} -w {value}\n")
        stored = _security([*find, "-w"])
    except subprocess.TimeoutExpired:
        return (f"the Keychain did not answer within {SECURITY_TIMEOUT} s; check"
                f" `security find-generic-password -a {account} -s {service}`")
    except OSError as exc:
        return f"could not run {security_binary()}: {exc.strerror}"
    if stored.returncode != 0:
        return f"the Keychain did not store {service}"
    if stored.stdout.removesuffix("\n") != value:
        return (f"keychain item {service} does not hold the captured value; delete it"
                f" (security delete-generic-password -a {account} -s {service}) and capture again")
    return None


def _security(args, stdin=None):
    return subprocess.run([security_binary(), *args], input=stdin, capture_output=True, text=True,
                          timeout=SECURITY_TIMEOUT)


def _hang_up(conn):
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()


# -- the ck-browser side --------------------------------------------------------


def policy_words(open):
    """("on"|"off", allowed domains, pending ones) as `ext status` and NO_TAB word them."""
    if open is None:  # the extension has not sent its policy
        return ("unknown",) * 3
    names = lambda items: ", ".join(items) or "none"
    return "on" if open["enabled"] else "off", names(open["domains"]), names(open["pending"])


def _alive(pid):
    if not (isinstance(pid, int) and pid > 0):  # kill(0) would reach our own process group
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # someone else's process, but alive
    return True


def profiles():
    """The connected profiles' sidecars, read without dialing (a run may hold a tab): each one's
    host is alive and its socket is there. Stale files are skipped, not removed."""
    entries = []
    for path in profiles_dir().glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            if _alive(entry["pid"]) and Path(entry["socket"]).exists():
                entries.append(entry)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # removed meanwhile by its exiting host, or not a sidecar
    return sorted(entries, key=lambda e: (e["profile_dir"] is None, e["profile_dir"] or "", e["instance"]))


def profile_label(entry):
    """Profile 14 "Work" <alex@example.com>, or the instance when that is unknown."""
    if entry["profile_dir"]:
        return f'{entry["profile_dir"]} "{entry["name"]}" <{entry["email"]}>'
    if entry["email"]:  # signed in, but not to a profile of the Chrome whose Local State was read
        return f'instance {entry["instance"]} <{entry["email"]}>'
    return f'instance {entry["instance"]} {UNKNOWN_PROFILE}'


def default_profile(entries):
    """The profile driven without --profile: the only one, else the only one with a claimed tab."""
    if len(entries) == 1:
        return entries[0]
    claimed = [entry for entry in entries if entry["tab"]]
    return claimed[0] if len(claimed) == 1 else None


def resolve(profile=None):
    """The connected profile's sidecar to drive. `profile` (its directory, name, email or instance,
    any case) names exactly one, never falling back to another: the wrong profile is the wrong
    account. CdpError says why none was picked and what is connected."""
    entries = profiles()
    if not entries:
        raise CdpError(NO_PROFILES.format(dir=profiles_dir()))
    connected = "; ".join(map(profile_label, entries))
    if profile is None:
        chosen = default_profile(entries)
        if chosen is None:
            raise CdpError("several Chrome profiles are connected: pass --profile <directory|email|name>;"
                           f" connected: {connected}")
        return chosen
    wanted = profile.casefold()
    names = lambda values: {value.casefold() for value in values if value}
    matches = [e for e in entries if wanted in names((e["profile_dir"], e["name"], e["email"], e["instance"]))]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise CdpError(f'--profile "{profile}" matches several connected profiles:'
                       f' {"; ".join(map(profile_label, matches))}; use the directory or email')
    error = (f'Chrome profile "{profile}" is not connected (never falling back to another profile);'
             f" connected: {connected}")
    known = [(directory, name) for directory, name, email in chrome_profiles()
             if wanted in names((directory, name, email))]
    if known:
        directory, name = known[0]
        error += (f' — {directory} "{name}" exists but its ck-browser extension is not connected: open a'
                  " window of that profile and install or reload the extension there")
    raise CdpError(error)


def _socket(path, profile):
    """`path` as given (tests), else the socket of the profile resolve() picks."""
    return Path(path) if path else Path(resolve(profile)["socket"])


def _dial(path):
    """A socket to the host and its first frame: the claimed tab's hello, or idle."""
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect(str(path))
        first = read_frame(sock)
    except FileNotFoundError:
        sock.close()
        raise CdpError(NO_BRIDGE.format(path=path, state="absent")) from None
    except ConnectionRefusedError:
        sock.close()
        raise CdpError(NO_BRIDGE.format(path=path, state="stale")) from None
    except (OSError, ValueError) as exc:
        sock.close()
        raise CdpError(f"bridge {path}: no hello from the extension ({exc})") from None
    if first is None or first.get("type") not in ("hello", "idle"):
        sock.close()
        raise CdpError((first or {}).get("error") or f"bridge {path} closed before the extension said hello")
    return sock, first


def _ask(sock, method, params):
    """One call before any reader runs: (its result, the hello that came on the way or None).
    A refusal raises its own words: they say what to do."""
    hello = None
    try:
        write_frame(sock, {"id": 0, "method": method, "params": params})
        while (message := read_frame(sock)) is not None and message.get("id") != 0:
            if message.get("type") == "hello":
                hello = message
            elif message.get("type") == "detached":
                hello = None
    except (OSError, ValueError) as exc:
        raise CdpError(f"{method}: {exc}") from None
    if message is None:
        raise CdpError(f"{method}: the bridge closed before the extension answered")
    if "error" in message:
        raise Refusal(message["error"].get("message", str(message["error"])))
    return message.get("result") or {}, hello


def bridge_state(path=None, profile=None):
    """{tab, open}: the claimed tab (None when idle) and the extension's policy."""
    sock, first = _dial(_socket(path, profile))
    _hang_up(sock)
    return {"tab": first.get("tab"), "open": first.get("open")}


def request_domain(domain, path=None, profile=None):
    """Ask the extension to let ck-browser open tabs on `domain`: {"status": "allowed"|"pending"};
    pending waits for the human in Chrome."""
    sock, _first = _dial(_socket(path, profile))
    try:
        return _ask(sock, "ck.allowDomain", {"domain": domain})[0]
    finally:
        _hang_up(sock)


def revoke_domain(domain, path=None, profile=None):
    """Take `domain` off the profile's allowed and pending lists: {"status": "revoked"|"not_allowed"}.
    A tab already claimed stays claimed."""
    sock, _first = _dial(_socket(path, profile))
    try:
        return _ask(sock, "ck.revokeDomain", {"domain": domain})[0]
    finally:
        _hang_up(sock)


def close_tab(path=None, profile=None):
    """Close the tab ck-browser opened; the extension refuses one the human handed over."""
    sock, first = _dial(_socket(path, profile))
    try:
        if first["type"] == "idle":
            raise CdpError("no tab is claimed; nothing to close")
        _ask(sock, "ck.closeTab", {})  # answered before the detached frame that follows it
    finally:
        _hang_up(sock)


class ExtCdp(Cdp):
    """Cdp's interface over a profile's bridge socket: calls go to the claimed tab, never Target.*.

    With no tab claimed and an `open_url`, the extension opens a new tab there and claims it
    (`opened_now`); it refuses unless the human lets agents open tabs on that domain."""

    def __init__(self, path=None, timeout=30, open_url=None, profile=None):
        self.sock, hello = _dial(_socket(path, profile))
        self.open = hello.get("open")
        self.opened_now = hello["type"] == "idle"
        if self.opened_now:
            try:
                hello = self._open_tab(open_url)
            except CdpError:
                self.sock.close()
                raise
        self.tab_info = hello["tab"]
        # A unix socket does not die silently: a gone host reads as end of stream.
        self.sock.settimeout(None)
        self._start(timeout)

    def _open_tab(self, url):
        if url is None:
            enabled, domains, _pending = policy_words(self.open)
            raise CdpError(NO_TAB.format(enabled=enabled, domains=domains))
        _result, hello = _ask(self.sock, "ck.openTab", {"url": url})
        if hello is None:
            raise CdpError(f"the extension opened {url} but never claimed the tab")
        return hello

    def _receive(self):
        message = read_frame(self.sock)
        if message is None:
            raise CdpError("the bridge closed: Chrome or the extension went away")
        if message.get("type") == "detached":
            if message.get("reason") == "left_allowed_domains":
                host = message.get("host")
                raise CdpError(f"the tab left the allowed domains, so the extension released it"
                               f" (ask with: bin/ck-browser ext allow {host})")
            raise CdpError(f"the human stopped the run ({message.get('reason')})")
        return None if "type" in message else message

    def _send(self, request):
        write_frame(self.sock, request)

    def close(self):
        _hang_up(self.sock)


class ExtTab(Tab):
    """The human's tab: its real viewport, no Target.* session, never closed by us."""

    @classmethod
    def open(cls, cdp, wait=False):
        """`wait`: the extension just opened the tab, so it is mid-navigation; let it load first."""
        tab = cls(cdp, None, None)
        if wait:
            tab.wait_loaded()
        for domain in ("Page", "Runtime", "Log", "Network"):
            tab.call(domain + ".enable")
        tab.width, tab.height = tab.evaluate("[innerWidth, innerHeight]")
        # Keep rAF/menus rendering while the human looks at another tab.
        tab.call("Emulation.setFocusEmulationEnabled", enabled=True)
        return tab

    def wait_loaded(self, timeout=20):
        """The tab the extension just opened is loading the script's url: wait as navigate()
        does after its Page.navigate, past the about:blank a new tab starts on."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate(LOADED) == "complete":
                    return
            except StalePage:
                pass  # expected mid-navigation; the document is being replaced
            time.sleep(0.02)
        raise CdpError(f"the tab ck-browser opened did not finish loading in {timeout}s")

    def screenshot_png(self, path):
        """None when the extension withholds it: a page showing a secret is never photographed."""
        try:
            return super().screenshot_png(path)
        except CdpError as exc:
            if WITHHELD in str(exc):
                return None
            raise

    def capture_secret(self, node, service):
        """The extension reads the node's value and the host stores it in the Keychain;
        {service, length, stored} comes back, never the value."""
        if type(node) is not int:
            raise ValueError(f"not an observed node number: {node!r}")
        return self.cdp.call("ck.captureSecret", {"node": node, "service": service},
                             timeout=CAPTURE_TIMEOUT)

    def check_write(self, node):
        """The extension refuses (CdpError) a scripted write on a payment-card field: a native
        select's value is set by script, so no input event reaches its input check."""
        if type(node) is not int:
            raise ValueError(f"not an observed node number: {node!r}")
        self.cdp.call("ck.checkNode", {"node": node})

    def set_viewport(self, width, height):
        """Back at the tab's own size, drop the override: the human gets their viewport back."""
        if (width, height) == (self.width, self.height):
            self.call("Emulation.clearDeviceMetricsOverride")
        else:
            super().set_viewport(width, height)

    def close(self):
        for method, params in (("Emulation.clearDeviceMetricsOverride", {}),
                               ("Emulation.setFocusEmulationEnabled", {"enabled": False})):
            try:
                self.call(method, **params)
            except CdpError:
                pass  # the human already took the tab back


# -- setup ------------------------------------------------------------------------


def nm_dir():
    """Chrome's per-user NativeMessagingHosts dir; CK_BROWSER_NM_DIR redirects it (tests)."""
    return Path(os.environ.get("CK_BROWSER_NM_DIR") or DEFAULT_NM_DIR)


def _baked_env(extension):
    """The export lines the wrapper carries: Chrome starts the host without our env, so the home,
    profile dir and plugins set now are the host's too (and the paths plugins name in WRAPPER_ENV),
    and so is the extension ID it lets in."""
    paths = ("CK_BROWSER_HOME", "CK_BROWSER_PROFILE_DIR", *plugins.wrapper_env())
    # Absolute: the host's cwd is ROOT, not ours.
    values = {name: os.path.abspath(os.path.expanduser(os.environ[name])) for name in paths
              if os.environ.get(name)}
    values["CK_BROWSER_PLUGINS"] = os.environ.get("CK_BROWSER_PLUGINS")
    values["CK_BROWSER_EXTENSION_ID"] = extension
    return "".join(f"export {name}={shlex.quote(value)}\n" for name, value in values.items() if value)


def install(extension=None):
    """Write the host wrapper and Chrome's host manifest for the extension `extension` names (default
    our_extension_id()); returns {id, manifest, wrapper}."""
    extension = our_extension_id(extension)
    directory = bridge_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    wrapper = directory / "host"
    # Chrome's PATH is minimal and its python3 may be too old: pin this interpreter. From ROOT, so
    # `-m HOST_MODULE` imports.
    wrapper.write_text(
        "#!/bin/sh\n# Chrome's native-messaging host for ck-browser; written by `ck-browser ext install`.\n"
        f"{_baked_env(extension)}cd {shlex.quote(str(ROOT))}"
        f" && exec {shlex.quote(sys.executable)} -m {HOST_MODULE} \"$@\"\n"
    )
    wrapper.chmod(stat.S_IRWXU)
    origin = f"chrome-extension://{extension}/"
    manifest = nm_dir() / f"{HOST_NAME}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "ck-browser bridge to one handed-over tab",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [origin],
    }, indent=2) + "\n")
    return {"id": origin.removeprefix("chrome-extension://").rstrip("/"), "manifest": manifest,
            "wrapper": wrapper}

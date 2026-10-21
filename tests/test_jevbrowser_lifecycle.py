"""ck-browser's Chrome lifecycle: who closes our Chrome and when, without a real Chrome."""

import contextlib
import io
import json
import os
import shutil
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ck_browser import check_python
from ck_browser import __main__ as cli
from ck_browser import cdp as cdp_module
from ck_browser.cdp import (LAST_USE, ROOT, WATCHDOG_PID, CdpError, default_profile_dir, ensure_chrome,
                                      is_shared, mark_used, release_chrome, watch)

PORT = 9444
WS = "ws://127.0.0.1:50123/devtools/browser/x"


class FakeCdp:
    """Stands in for Cdp: answers Target.getTargets from `pages`, records every call."""

    instances = []

    def __init__(self, pages):
        self.pages = pages
        self.methods = []
        self.closed = False

    def __call__(self, ws_url, timeout=30):
        FakeCdp.instances.append(self)
        return self

    def call(self, method, params=None):
        self.methods.append(method)
        if method == "Browser.getVersion":
            return {"product": "Chrome/155"}
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "attached": attached} for attached in self.pages]}
        if method == "Browser.close":
            raise CdpError("Browser.close: connection closed")  # Chrome drops the socket as it exits
        return {}

    def close(self):
        self.closed = True


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ck-browser-profile-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.last_use = self.dir / LAST_USE
        FakeCdp.instances = []

    def fake_browser(self, pages=(), reachable=True):
        fake = FakeCdp(list(pages))
        resolve = (mock.patch.object(cdp_module, "browser_ws", return_value=WS)
                   if reachable else
                   mock.patch.object(cdp_module, "browser_ws", side_effect=CdpError("no verified Chrome")))
        stack = contextlib.ExitStack()
        stack.enter_context(resolve)
        stack.enter_context(mock.patch.object(cdp_module, "Cdp", fake))
        self.addCleanup(stack.close)
        return fake

    def age_last_use(self, seconds):
        old = time.time() - seconds
        os.utime(self.last_use, (old, old))


class IsSharedTest(unittest.TestCase):
    def test_the_default_profile_is_shared(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CK_BROWSER_PROFILE_DIR", None)
            self.assertTrue(is_shared(None))
        self.assertTrue(is_shared(default_profile_dir()))

    def test_another_profile_dir_is_not(self):
        self.assertFalse(is_shared("/tmp/ck-browser-ci"))

    def test_the_default_profile_follows_the_profile_dir_then_the_home(self):
        with mock.patch.dict(os.environ, {"CK_BROWSER_PROFILE_DIR": "/tmp/ck-browser-ci",
                                          "CK_BROWSER_HOME": "/tmp/ck-home"}):
            self.assertEqual(default_profile_dir(), Path("/tmp/ck-browser-ci"))
            self.assertTrue(is_shared(None))
            os.environ.pop("CK_BROWSER_PROFILE_DIR")
            self.assertEqual(default_profile_dir(), Path("/tmp/ck-home/chrome-profile"))
            os.environ.pop("CK_BROWSER_HOME")
            self.assertEqual(default_profile_dir(), Path.home() / ".ck-browser" / "chrome-profile")


class ReleaseTest(LifecycleCase):
    def test_the_shared_instance_is_left_to_its_watchdog(self):
        with mock.patch.object(cdp_module, "Cdp", side_effect=AssertionError("connected")):
            self.assertFalse(release_chrome(default_profile_dir()))

    def test_our_last_use_with_no_client_closes_chrome(self):
        fake = self.fake_browser(pages=[False])
        mark_used(self.dir)
        self.assertTrue(release_chrome(self.dir))
        self.assertEqual(fake.methods, ["Target.getTargets", "Browser.close"])
        self.assertTrue(fake.closed)

    def test_an_attached_page_keeps_it_open(self):
        fake = self.fake_browser(pages=[False, True])
        mark_used(self.dir)
        self.assertFalse(release_chrome(self.dir))
        self.assertEqual(fake.methods, ["Target.getTargets"])
        self.assertTrue(fake.closed)

    def test_a_newer_use_by_someone_else_keeps_it_open(self):
        fake = self.fake_browser(pages=[False])
        mtime = mark_used(self.dir)
        os.utime(self.last_use, ns=(mtime, mtime + 1_000_000))
        self.assertFalse(release_chrome(self.dir))
        self.assertEqual(FakeCdp.instances, [])
        self.assertEqual(fake.methods, [])

    def test_a_chrome_we_never_used_is_left_alone(self):
        self.fake_browser(pages=[False])
        self.last_use.touch()
        self.assertFalse(release_chrome(self.dir))
        self.assertEqual(FakeCdp.instances, [])

    def test_an_unreachable_endpoint_is_false(self):
        self.fake_browser(reachable=False)
        mark_used(self.dir)
        self.assertFalse(release_chrome(self.dir))


class WatchTest(LifecycleCase):
    def run_watch(self, **kwargs):
        sleeps = []
        result = watch(self.dir, clock=time.time, sleep=sleeps.append, **kwargs)
        return result, sleeps

    def test_an_attached_page_counts_as_use(self):
        fake = self.fake_browser(pages=[True])
        self.last_use.touch()
        self.age_last_use(1000)
        polls = []

        def sleep(_seconds):  # one look, then Chrome goes away
            polls.append(_seconds)
            cdp_module.browser_ws.side_effect = CdpError("no verified Chrome")

        self.assertEqual(watch(self.dir, idle=300, poll=15, sleep=sleep), "gone")
        self.assertEqual(polls, [15])
        # Use is kept in memory: a touch would make the run that owns the page skip its release.
        self.assertLess(self.last_use.stat().st_mtime, time.time() - 900)
        self.assertNotIn("Browser.close", fake.methods)

    def test_idle_past_the_threshold_closes_chrome(self):
        fake = self.fake_browser(pages=[False])
        self.last_use.touch()
        self.age_last_use(400)
        result, sleeps = self.run_watch(idle=300)
        self.assertEqual(result, "closed")
        self.assertEqual(fake.methods, ["Target.getTargets", "Browser.close"])
        self.assertEqual(sleeps, [])
        self.assertTrue(fake.closed)

    def test_a_fake_clock_drives_the_idle_time(self):
        fake = self.fake_browser(pages=[False])
        self.last_use.touch()
        now = [time.time() + 1]  # just past the touch, so 20 polls make 300s
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        self.assertEqual(watch(self.dir, idle=300, poll=15, clock=lambda: now[0], sleep=sleep), "closed")
        self.assertEqual(len(sleeps), 20)  # 300s of 15s polls, then the close
        self.assertEqual(fake.methods[-1], "Browser.close")

    def test_a_chrome_that_outlives_browser_close_gets_sigterm(self):
        self.fake_browser(pages=[False])
        self.last_use.touch()
        self.age_last_use(400)
        with mock.patch.object(cdp_module.os, "kill") as kill:
            result, sleeps = self.run_watch(chrome_pid=4321)
        self.assertEqual(result, "closed")
        self.assertEqual(sleeps, [0.5] * 10)
        self.assertEqual(kill.call_args_list[-1], mock.call(4321, signal.SIGTERM))

    def test_a_dead_chrome_pid_is_gone_without_a_close(self):
        self.fake_browser(pages=[False])
        self.last_use.touch()
        self.age_last_use(400)
        with mock.patch.object(cdp_module.os, "kill", side_effect=ProcessLookupError):
            result, _sleeps = self.run_watch(chrome_pid=4321)
        self.assertEqual(result, "gone")
        self.assertEqual(FakeCdp.instances, [])

    def test_a_gone_endpoint_ends_the_watch(self):
        self.fake_browser(reachable=False)
        self.assertEqual(self.run_watch()[0], "gone")

    def test_port_9222_is_never_acted_on(self):
        (self.dir / "DevToolsActivePort").write_text("9222\n/devtools/browser/x\n")
        with mock.patch.object(cdp_module, "Cdp", side_effect=AssertionError("connected to 9222")):
            self.assertEqual(watch(self.dir), "gone")

    def test_the_pidfile_is_removed_on_exit_only_when_it_is_ours(self):
        self.fake_browser(reachable=False)
        pidfile = self.dir / WATCHDOG_PID
        pidfile.write_text(str(os.getpid()))
        self.run_watch()
        self.assertFalse(pidfile.exists())
        pidfile.write_text("99999999")
        self.run_watch()
        self.assertEqual(pidfile.read_text(), "99999999")


class EnsureChromeWatchdogTest(LifecycleCase):
    def test_a_reused_chrome_gets_one_watchdog(self):
        spawned = mock.Mock(pid=4242)
        with mock.patch.object(cdp_module, "browser_ws", return_value=WS), \
                mock.patch.object(cdp_module.subprocess, "Popen", return_value=spawned) as popen, \
                mock.patch.object(cdp_module, "_is_watchdog", lambda pid: pid == 4242), \
                mock.patch.object(cdp_module, "LAUNCHED", []) as launched:
            self.assertEqual(ensure_chrome(profile_dir=self.dir), WS)
            self.assertEqual(ensure_chrome(profile_dir=self.dir), WS)
        self.assertEqual(popen.call_count, 1)
        command = popen.call_args.args[0]
        self.assertEqual(command[1:], ["-m", "ck_browser", "watch",
                                       "--profile-dir", str(self.dir)])  # no --chrome-pid on reuse
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["cwd"], ROOT)  # `-m` imports from there, whatever our cwd
        self.assertEqual((self.dir / WATCHDOG_PID).read_text(), "4242")
        self.assertEqual(launched, [spawned])
        self.assertTrue(self.last_use.exists())

    def test_a_relative_profile_dir_reaches_the_watchdog_absolute(self):
        # The watchdog runs from ROOT: a relative --profile-dir would name another dir there.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.dir.parent)
        with mock.patch.object(cdp_module, "browser_ws", return_value=WS), \
                mock.patch.object(cdp_module.subprocess, "Popen", return_value=mock.Mock(pid=4242)) as popen, \
                mock.patch.object(cdp_module, "LAUNCHED", []), \
                mock.patch.dict(os.environ, {"PYTHONPATH": "/elsewhere"}):
            ensure_chrome(profile_dir=self.dir.name)
        command = popen.call_args.args[0]
        passed = command[command.index("--profile-dir") + 1]
        self.assertTrue(os.path.isabs(passed), passed)
        self.assertEqual(os.path.realpath(passed), os.path.realpath(self.dir))
        # ROOT on PYTHONPATH too, so `-m` imports even under PYTHONSAFEPATH; the caller's path kept.
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONPATH"], f"{ROOT}{os.pathsep}/elsewhere")

    def test_a_dead_watchdog_is_replaced(self):
        (self.dir / WATCHDOG_PID).write_text("99999999")
        with mock.patch.object(cdp_module, "browser_ws", return_value=WS), \
                mock.patch.object(cdp_module.subprocess, "Popen", return_value=mock.Mock(pid=5151)) as popen, \
                mock.patch.object(cdp_module.os, "kill", side_effect=ProcessLookupError), \
                mock.patch.object(cdp_module, "LAUNCHED", []):
            ensure_chrome(profile_dir=self.dir)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual((self.dir / WATCHDOG_PID).read_text(), "5151")

    def test_a_fresh_launch_replaces_a_watchdog_still_sleeping_on_the_old_chrome(self):
        (self.dir / WATCHDOG_PID).write_text("4242")  # alive, watching a Chrome that just died
        chrome, watchdog = mock.Mock(pid=7000), mock.Mock(pid=7001)
        with mock.patch.object(cdp_module, "browser_ws",
                               side_effect=[CdpError("no verified Chrome"), WS]), \
                mock.patch.object(cdp_module, "chrome_binary", return_value="/bin/chrome"), \
                mock.patch.object(cdp_module.subprocess, "Popen", side_effect=[chrome, watchdog]) as popen, \
                mock.patch.object(cdp_module, "_is_watchdog", return_value=True), \
                mock.patch.object(cdp_module, "LAUNCHED", []):
            self.assertEqual(ensure_chrome(profile_dir=self.dir), WS)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args.args[0][-2:], ["--chrome-pid", "7000"])
        self.assertEqual((self.dir / WATCHDOG_PID).read_text(), "7001")

    def test_a_stranger_that_reused_the_pid_is_no_watchdog(self):
        self.assertFalse(cdp_module._is_watchdog(os.getpid()))
        self.assertFalse(cdp_module._is_watchdog(0))

    def test_a_failed_spawn_does_not_fail_the_run(self):
        with mock.patch.object(cdp_module, "browser_ws", return_value=WS), \
                mock.patch.object(cdp_module.subprocess, "Popen", side_effect=OSError("no python")):
            self.assertEqual(ensure_chrome(profile_dir=self.dir), WS)
        self.assertFalse((self.dir / WATCHDOG_PID).exists())


class ProfilePrefsTest(LifecycleCase):
    """Chrome's password-breach dialog drops all input to the tab: a launch turns the check off."""

    def setUp(self):
        super().setUp()
        self.prefs = self.dir / "Default" / "Preferences"

    def launch(self):
        with mock.patch.object(cdp_module, "browser_ws",
                               side_effect=[CdpError("no verified Chrome"), WS]), \
                mock.patch.object(cdp_module, "chrome_binary", return_value="/bin/chrome"), \
                mock.patch.object(cdp_module.subprocess, "Popen", return_value=mock.Mock(pid=7000)) as popen, \
                mock.patch.object(cdp_module, "_ensure_watchdog"), \
                mock.patch.object(cdp_module, "LAUNCHED", []):
            ensure_chrome(profile_dir=self.dir)
        return popen.call_args.args[0]

    def test_a_launch_keeps_cookies_off_the_os_keychain(self):
        """A locked macOS keychain left os_crypt off: cookies lived in memory only (#cl-598)."""
        command = self.launch()
        self.assertIn("--use-mock-keychain", command)
        self.assertIn("--password-store=basic", command)

    def test_a_launch_on_a_new_profile_writes_the_pref(self):
        self.launch()
        self.assertEqual(json.loads(self.prefs.read_text()),
                         {"profile": {"password_manager_leak_detection": False}})

    def test_a_launch_keeps_the_profiles_other_prefs(self):
        self.prefs.parent.mkdir()
        self.prefs.write_text(json.dumps({"profile": {"name": "Person 1", "password_manager_leak_detection": True},
                                          "intl": {"accept_languages": "en"}}))
        self.launch()
        self.assertEqual(json.loads(self.prefs.read_text()),
                         {"profile": {"name": "Person 1", "password_manager_leak_detection": False},
                          "intl": {"accept_languages": "en"}})

    def test_damaged_prefs_are_left_to_chrome(self):
        self.prefs.parent.mkdir()
        self.prefs.write_text("{not json")
        self.launch()
        self.assertEqual(self.prefs.read_text(), "{not json")

    def test_a_running_chrome_is_not_touched(self):
        """It writes its own copy over the file: a change now would be lost, and is not made."""
        with mock.patch.object(cdp_module, "browser_ws", return_value=WS), \
                mock.patch.object(cdp_module, "_ensure_watchdog"):
            ensure_chrome(profile_dir=self.dir)
        self.assertFalse(self.prefs.exists())


class FixedPortTest(LifecycleCase):
    """Chrome writes no DevToolsActivePort for a fixed port: ck-browser writes it, after the owner check."""

    PID = 4242
    BROWSER = "/devtools/browser/abc"

    def setUp(self):
        super().setUp()
        self.ps = f"  {os.getuid()} /bin/chrome --remote-debugging-port={PORT} --user-data-dir={self.dir}\n"
        self.fetched = []

    def lock(self):
        os.symlink(f"host.local-{self.PID}", self.dir / "SingletonLock")

    def owner(self):
        def run(command, **_kwargs):
            return mock.Mock(stdout=self.ps if command[0] == "ps" else f"{self.PID}\n")

        def urlopen(url, timeout):
            self.fetched.append(url)
            return io.BytesIO(json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{PORT}{self.BROWSER}"}).encode())

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(cdp_module, "_alive", lambda pid: pid == self.PID))
        stack.enter_context(mock.patch.object(cdp_module.shutil, "which", return_value="/usr/sbin/lsof"))
        stack.enter_context(mock.patch.object(cdp_module.subprocess, "run", run))
        stack.enter_context(mock.patch.object(cdp_module.urllib.request, "urlopen", urlopen))
        stack.enter_context(mock.patch.object(cdp_module, "_ensure_watchdog"))
        stack.enter_context(mock.patch.object(cdp_module, "LAUNCHED", []))
        return stack

    def test_a_launch_on_a_fixed_port_writes_the_active_port_file(self):
        def popen(command, **_kwargs):
            self.lock()  # Chrome takes the profile once it starts
            return mock.Mock(pid=self.PID)

        with self.owner(), \
                mock.patch.object(cdp_module, "chrome_binary", return_value="/bin/chrome"), \
                mock.patch.object(cdp_module.subprocess, "Popen", side_effect=popen) as launched:
            self.assertEqual(ensure_chrome(port=PORT, profile_dir=self.dir), f"ws://127.0.0.1:{PORT}{self.BROWSER}")
        self.assertIn(f"--remote-debugging-port={PORT}", launched.call_args.args[0])
        self.assertEqual(self.fetched, [f"http://127.0.0.1:{PORT}/json/version"])
        active = self.dir / "DevToolsActivePort"
        self.assertEqual(active.read_text(), f"{PORT}\n{self.BROWSER}\n")
        self.assertEqual(active.stat().st_mode & 0o777, 0o600)

    def test_a_fixed_port_chrome_already_up_without_the_file_is_reused(self):
        """Launching a second Chrome there would abort on SingletonLock."""
        self.lock()
        with self.owner(), \
                mock.patch.object(cdp_module.subprocess, "Popen", side_effect=AssertionError("launched")):
            self.assertEqual(ensure_chrome(port=PORT, profile_dir=self.dir), f"ws://127.0.0.1:{PORT}{self.BROWSER}")
        self.assertEqual((self.dir / "DevToolsActivePort").read_text(), f"{PORT}\n{self.BROWSER}\n")

    def test_an_unverified_owner_is_never_asked_or_recorded(self):
        self.lock()
        self.ps = f"  {os.getuid()} /bin/chrome --user-data-dir={self.dir}-other\n"
        with self.owner(), self.assertRaises(CdpError) as caught:
            cdp_module._adopt_port(self.dir, PORT)
        self.assertEqual(str(caught.exception),
                         f"no verified Chrome for profile {self.dir}: pid {self.PID} is not a Chrome on this profile dir")
        self.assertEqual(self.fetched, [])
        self.assertFalse((self.dir / "DevToolsActivePort").exists())

    def test_port_0_never_adopts(self):
        with mock.patch.object(cdp_module, "browser_ws", side_effect=[CdpError("no verified Chrome"), WS]), \
                mock.patch.object(cdp_module, "_adopt_port", side_effect=AssertionError("adopted")), \
                mock.patch.object(cdp_module, "chrome_binary", return_value="/bin/chrome"), \
                mock.patch.object(cdp_module.subprocess, "Popen", return_value=mock.Mock(pid=7000)), \
                mock.patch.object(cdp_module, "_ensure_watchdog"), \
                mock.patch.object(cdp_module, "LAUNCHED", []):
            self.assertEqual(ensure_chrome(port=0, profile_dir=self.dir), WS)


class LaunchTimeoutTest(LifecycleCase):
    """A Chrome that never serves CDP is stopped: left up, it holds SingletonLock against every relaunch."""

    def launch_that_times_out(self, chrome, gone=False):
        with mock.patch.object(cdp_module, "browser_ws", side_effect=CdpError("no verified Chrome")), \
                mock.patch.object(cdp_module, "chrome_binary", return_value="/bin/chrome"), \
                mock.patch.object(cdp_module.subprocess, "Popen", return_value=chrome), \
                mock.patch.object(cdp_module, "LAUNCHED", []), \
                mock.patch.object(cdp_module, "LAUNCH_TIMEOUT", 0), \
                mock.patch.object(cdp_module.os, "killpg", side_effect=ProcessLookupError if gone else None) as killpg, \
                self.assertRaises(CdpError) as caught:
            ensure_chrome(profile_dir=self.dir)
        self.assertEqual(str(caught.exception),
                         f"Chrome did not serve CDP for {self.dir} within 0s; see {self.dir / 'chrome.log'};"
                         " the late Chrome was stopped")
        return killpg

    def test_the_launched_group_gets_sigterm_then_sigkill(self):
        chrome = mock.Mock(pid=7000)
        chrome.wait.side_effect = cdp_module.subprocess.TimeoutExpired("chrome", 5)
        killpg = self.launch_that_times_out(chrome)
        self.assertEqual(killpg.call_args_list, [mock.call(7000, signal.SIGTERM), mock.call(7000, signal.SIGKILL)])
        self.assertEqual(chrome.wait.call_args_list, [mock.call(timeout=5)] * 2)

    def test_a_group_already_gone_is_left_alone(self):
        chrome = mock.Mock(pid=7000)
        killpg = self.launch_that_times_out(chrome, gone=True)
        self.assertEqual(killpg.call_args_list, [mock.call(7000, signal.SIGTERM)])
        chrome.wait.assert_not_called()

class CliReleaseTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="ck-browser-cli-"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.script = self.out / "steps.yaml"
        self.script.write_text("steps:\n  - click: Save\n")

    def main(self, *extra):
        return cli.main(["run", str(self.script), "--out", str(self.out), "--stub",
                         "--chrome-port", str(PORT), "--profile-dir", str(self.out), *extra])

    def test_release_runs_even_when_run_raises(self):
        with mock.patch.object(cli, "run", side_effect=RuntimeError("boom")), \
                mock.patch.object(cli, "release_chrome") as release:
            with self.assertRaises(RuntimeError):
                self.main()
        release.assert_called_once_with(str(self.out))

    def test_release_runs_after_a_reported_failure(self):
        with mock.patch.object(cli, "run", side_effect=CdpError("lost")), \
                mock.patch.object(cli, "release_chrome") as release, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.main(), 2)
        release.assert_called_once_with(str(self.out))

    def test_the_extension_target_releases_nothing(self):
        with mock.patch.object(cli, "run", return_value={"totals": {"failed": 0}}), \
                mock.patch.object(cli, "format_report", return_value=""), \
                mock.patch.object(cli, "release_chrome") as release, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.main("--target", "ext"), 0)
        release.assert_not_called()

    def test_keep_tab_leaves_chrome_up(self):
        with mock.patch.object(cli, "run", return_value={"totals": {"failed": 0}}), \
                mock.patch.object(cli, "format_report", return_value=""), \
                mock.patch.object(cli, "release_chrome") as release, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.main("--keep-tab"), 0)
        release.assert_not_called()

    def test_doctor_releases_the_chrome_it_launched_even_on_failure(self):
        with mock.patch.object(cli, "browser_ws", side_effect=CdpError("no verified Chrome")), \
                mock.patch.object(cli, "ensure_chrome", return_value=WS) as launch, \
                mock.patch.object(cli, "Cdp", side_effect=CdpError("gone")), \
                mock.patch.object(cli, "release_chrome") as release, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["doctor", "--chrome-port", str(PORT), "--profile-dir", "/tmp/p"]), 2)
        self.assertEqual(launch.call_args.kwargs["port"], PORT)
        release.assert_called_once_with("/tmp/p")

    def test_doctor_leaves_a_reused_chrome_alone(self):
        fake = FakeCdp([False])
        with mock.patch.object(cli, "browser_ws", return_value=WS), \
                mock.patch.object(cli, "Cdp", fake), \
                mock.patch.object(cli, "release_chrome") as release, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["doctor"]), 0)
        release.assert_not_called()
        self.assertIn("chrome: reused on port 50123 — Chrome/155", out.getvalue())

    def test_stop_closes_the_chrome_on_the_profile_dir(self):
        fake = FakeCdp([])
        with mock.patch.object(cli, "browser_ws", return_value=WS) as lookup, \
                mock.patch.object(cli, "Cdp", fake), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["stop", "--profile-dir", "/tmp/p"]), 0)
        lookup.assert_called_once_with("/tmp/p")
        self.assertEqual(fake.methods, ["Browser.close"])
        self.assertEqual(out.getvalue(), "chrome: closed on port 50123\n")

    def test_watch_subcommand_calls_watch(self):
        with mock.patch.object(cli, "watch", return_value="gone") as watcher:
            self.assertEqual(cli.main(["watch", "--profile-dir", "/tmp/p", "--chrome-pid", "77"]), 0)
        watcher.assert_called_once_with("/tmp/p", chrome_pid=77, idle=cdp_module.IDLE_CLOSE)


class VersionTest(unittest.TestCase):
    def test_an_old_python_exits_with_what_to_run_instead(self):
        with self.assertRaises(SystemExit) as caught:
            check_python((3, 9, 6), "/x/python3")
        message = str(caught.exception.code)
        for part in ("3.11", "3.9", "/x/python3", "python3.12 -m ck_browser"):
            self.assertIn(part, message)

    def test_a_supported_python_passes(self):
        for info in ((3, 11, 0), (3, 14, 0)):
            self.assertIsNone(check_python(info, "/x/python3"))

    def test_version_flag_prints_the_package_version(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as caught:
            cli.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(out.getvalue(), "ck-browser 0.1.0\n")


if __name__ == "__main__":
    unittest.main()

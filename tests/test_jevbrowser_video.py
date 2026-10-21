"""ck-browser --video: the screencast recorder, its ffmpeg and frames outputs, the filmstrip, the wiring."""

import base64
import contextlib
import io
import json
import os
import queue
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ck_browser import __main__ as cli
from ck_browser import video as video_module
from ck_browser.cdp import Cdp, CdpError, Tab, ensure_chrome
from ck_browser.ext import ExtTab
from ck_browser.model import StubJev
from ck_browser.report import format_report, step_line
from ck_browser.runner import Driver, run
from ck_browser.video import NO_SCREENCAST, Recorder, filmstrip, result
from tests.test_jevbrowser import FakeDriver, action, make_page, script_of

PROFILE_DIR = os.environ.get("CK_BROWSER_PROFILE_DIR")


def frame_event(n, ts, session="cast-1"):
    return {"data": base64.b64encode(f"jpeg-{n}".encode()).decode(), "sessionId": session,
            "metadata": {"timestamp": ts}}


class HookCdp:
    """What a Recorder touches of a Cdp: hooks, fire-and-forget sends and calls, all recorded."""

    def __init__(self, refuse=False):
        self.hooks = {}
        self.sent = []
        self.methods = []
        self.refuse = refuse

    def on_event(self, method, session_id, handle):
        self.hooks[(method, session_id)] = handle

    def off_event(self, method, session_id):
        self.hooks.pop((method, session_id), None)

    def send_only(self, method, params=None, session_id=None):
        self.sent.append((method, params, session_id))

    def call(self, method, params=None, session_id=None, timeout=None):
        self.methods.append((method, params, session_id))
        if self.refuse:
            raise CdpError(f"{method}: not allowed")
        return {}

    def feed(self, params, session_id="s"):
        handle = self.hooks.get(("Page.screencastFrame", session_id))
        if handle:
            handle(params)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="ck-browser-video-"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)


class RecorderTest(TempCase):
    def recorder(self, refuse=False, width=1280, height=800):
        cdp = HookCdp(refuse=refuse)
        recorder = Recorder(Tab(cdp, "t", "s", width, height), self.out)
        return recorder, cdp

    def test_frames_are_acked_kept_in_order_and_stop_ends_them(self):
        recorder, cdp = self.recorder()
        recorder.start()
        self.assertEqual(cdp.methods, [("Page.startScreencast", {
            "format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 1}, "s")])
        cdp.feed(frame_event(1, 100.0))
        cdp.feed(frame_event(2, 100.4, session="cast-2"))
        self.assertEqual(cdp.sent, [("Page.screencastFrameAck", {"sessionId": "cast-1"}, "s"),
                                    ("Page.screencastFrameAck", {"sessionId": "cast-2"}, "s")])
        self.assertEqual([(path.name, ts) for path, ts in recorder.frames],
                         [("00001.jpg", 100.0), ("00002.jpg", 100.4)])
        self.assertEqual(recorder.frames[1][0].read_bytes(), b"jpeg-2")
        self.assertEqual(recorder.dir.parent, self.out)
        self.assertEqual(recorder.dir.stat().st_mode & 0o777, 0o700)
        recorder.stop()
        recorder.stop()  # idempotent
        self.assertEqual(cdp.hooks, {})
        self.assertEqual([m for m, _p, _s in cdp.methods], ["Page.startScreencast", "Page.stopScreencast"])
        recorder._frame(frame_event(3, 101.0))  # one already on its way when stop() ran
        self.assertEqual(len(recorder.frames), 2)

    def test_each_frame_lasts_until_the_next_and_the_last_until_the_run_ended(self):
        recorder, _cdp = self.recorder()
        recorder.frames = [(Path("a"), 100.0), (Path("b"), 100.5), (Path("c"), 100.5)]
        recorder.ended = 100.52
        self.assertEqual([round(d, 3) for d in recorder.durations()], [0.5, 0.01, 0.1])
        recorder.ended = 102.5
        self.assertEqual([round(d, 3) for d in recorder.durations()], [0.5, 0.01, 2.0])

    def test_without_ffmpeg_the_frames_and_their_index_stay(self):
        recorder, cdp = self.recorder()
        recorder.start()
        for n, ts in enumerate((10.0, 10.25, 11.0), 1):
            cdp.feed(frame_event(n, ts))
        recorder.stop()
        recorder.ended = 11.5
        temp = recorder.dir
        with mock.patch.object(video_module.shutil, "which", return_value=None):
            made = recorder.finish()
        frames = self.out / "frames"
        self.assertEqual(made, {"path": None, "reason": "ffmpeg not found", "encoder": None, "frames": 3,
                                "frames_dir": str(frames), "duration_s": 1.5})
        self.assertFalse(temp.exists())
        self.assertEqual(sorted(p.name for p in frames.iterdir()),
                         ["00001.jpg", "00002.jpg", "00003.jpg", "index.json"])
        self.assertEqual(json.loads((frames / "index.json").read_text()), [
            {"file": "00001.jpg", "ts": 10.0, "duration_s": 0.25},
            {"file": "00002.jpg", "ts": 10.25, "duration_s": 0.75},
            {"file": "00003.jpg", "ts": 11.0, "duration_s": 0.5}])
        self.assertIn("video: none — ffmpeg not found; 3 frames in " + str(frames) + "/", format_report(
            {**empty_report(), "video": made}))

    def test_a_failed_encode_keeps_the_frames_and_says_why(self):
        recorder, cdp = self.recorder()
        recorder.start()
        cdp.feed(frame_event(1, 5.0))
        recorder.stop()
        failed = subprocess.CompletedProcess([], 1, "", "noise\nUnknown encoder 'libvpx-vp9'\n")
        with mock.patch.object(video_module.shutil, "which", return_value="/bin/ffmpeg"), \
                mock.patch.object(video_module, "_encoder", return_value="libvpx-vp9"), \
                mock.patch.object(video_module.subprocess, "run", return_value=failed):
            made = recorder.finish()
        self.assertEqual(made["reason"], "ffmpeg failed: noise Unknown encoder 'libvpx-vp9'")
        self.assertEqual(made["frames"], 1)
        self.assertEqual(sorted(p.name for p in (self.out / "frames").iterdir()), ["00001.jpg", "index.json"])
        self.assertFalse((self.out / "run.webm").exists())

    def test_no_frames_and_a_refused_screencast(self):
        recorder, _cdp = self.recorder()
        recorder.start()
        recorder.stop()
        self.assertEqual(recorder.finish(), result(reason="no frames"))
        self.assertEqual([p.name for p in self.out.iterdir()], [])
        refused, cdp = self.recorder(refuse=True)
        refused.start()
        self.assertEqual(cdp.hooks, {})
        refused.stop()
        self.assertEqual([m for m, _p, _s in cdp.methods], ["Page.startScreencast"])  # nothing to stop
        self.assertEqual(refused.finish()["reason"],
                         "no frames (screencast refused: Page.startScreencast: not allowed)")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not on PATH")
    def test_ffmpeg_encodes_the_frames_with_their_real_timing(self):
        recorder, _cdp = self.recorder(width=64, height=48)
        # Two sizes, as a whole-page look sends: one is scaled into the video's size.
        for n, (colour, size) in enumerate((("red", "64x48"), ("blue", "64x96")), 1):
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            f"color=c={colour}:s={size}", "-frames:v", "1", "-update", "1",
                            str(recorder.dir / f"{n:05d}.jpg")], check=True, timeout=60)
        recorder.frames = [(recorder.dir / "00001.jpg", 50.0), (recorder.dir / "00002.jpg", 50.6)]
        recorder.ended = 51.0
        made = recorder.finish()
        self.assertEqual({**made, "encoder": None}, result(path=str(self.out / "run.webm"), frames=2, duration_s=1.0))
        self.assertIn(made["encoder"], ("libvpx-vp9", "libvpx"))
        self.assertFalse(recorder.dir.exists())
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
                                "-of", "json", made["path"]], capture_output=True, text=True, check=True, timeout=60)
        info = json.loads(probe.stdout)
        self.assertAlmostEqual(float(info["format"]["duration"]), 1.0, delta=0.1)  # frames sit on a 1/25 s tick
        self.assertEqual((info["streams"][0]["width"], info["streams"][0]["height"]), (64, 48))


class QueueCdp(Cdp):
    """A Cdp whose messages come from a queue and whose sends are recorded: no socket."""

    def __init__(self):
        self.inbox = queue.Queue()
        self.sent = []
        self.ws = self
        self._start(5)

    def _receive(self):
        return self.inbox.get()

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        pass


def until(condition, seconds=2):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition never held")
        time.sleep(0.01)


class CdpHookTest(unittest.TestCase):
    def test_a_hooked_event_skips_the_event_list_and_a_raising_hook_keeps_the_reader(self):
        cdp = QueueCdp()
        taken = []
        cdp.on_event("Page.screencastFrame", "s", taken.append)
        frame = {"method": "Page.screencastFrame", "params": {"sessionId": "c1"}, "sessionId": "s"}
        other = {**frame, "sessionId": "other-session"}
        cdp.inbox.put(frame)
        cdp.inbox.put(other)
        until(lambda: len(cdp.events) == 1)
        self.assertEqual(taken, [{"sessionId": "c1"}])
        self.assertEqual(cdp.events[0]["sessionId"], "other-session")

        def boom(_params):
            raise KeyError("data")

        cdp.on_event("Page.screencastFrame", "s", boom)
        cdp.inbox.put(frame)
        cdp.inbox.put({"method": "Page.loadEventFired", "params": {}, "sessionId": "s"})
        until(lambda: len(cdp.events) == 2)
        self.assertIsNone(cdp.closed)
        cdp.off_event("Page.screencastFrame", "s")
        cdp.inbox.put(frame)
        until(lambda: len(cdp.events) == 3)
        self.assertEqual(cdp.events[2]["method"], "Page.screencastFrame")

    def test_a_recorder_acks_from_the_reader_thread(self):
        cdp = QueueCdp()
        out = Path(tempfile.mkdtemp(prefix="ck-browser-video-"))
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        recorder = Recorder(Tab(cdp, "t", "s"), out)
        cdp.on_event("Page.screencastFrame", "s", recorder._frame)
        cdp.inbox.put({"method": "Page.screencastFrame", "params": frame_event(1, 3.0), "sessionId": "s"})
        until(lambda: recorder.frames)
        self.assertEqual(cdp.sent[-1]["method"], "Page.screencastFrameAck")
        self.assertEqual((cdp.sent[-1]["params"], cdp.sent[-1]["sessionId"]), ({"sessionId": "cast-1"}, "s"))
        self.assertEqual(cdp.events, [])


def empty_report(**extra):
    totals = {"passed": 0, "failed": 0, "steps": 0, "wall_ms": 1, "jev_requests": 0, "jev_ms": 0,
              "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}
    return {"totals": totals, "settings": {"settle_tries": 2}, "steps": [], **extra}


def record(n, step, status="pass", **extra):
    return {"n": n, "step": step, "verb": step.split(":")[0], "status": status, "error": None, "target": None,
            "ms": 12, "console_errors": [], "failed_requests": [], "screenshot": None,
            "failure_screenshot": None, "frame": None, **extra}


class FilmstripTest(TempCase):
    def test_one_escaped_row_per_step_with_relative_frames(self):
        report = empty_report(script_path="/tmp/<flows>/x.yaml", url="https://example.com/?a=1&b=<2>",
                              started_at="2026-10-21T00:00:00+00:00", error="Chrome went <away>",
                              video=result(path=str(self.out / "run.webm"), frames=4, duration_s=2.0,
                                           encoder="libvpx-vp9"))
        report["steps"] = [
            record(1, 'click: "<b>Save</b> & close"', frame=str(self.out / "01-click-save.png")),
            record(2, "expect: Saved", status="fail", error="no <match>", frame=None, frame_withheld=True),
            record(3, "checkpoint: end", status="skipped"),
        ]
        path = filmstrip(report, self.out)
        self.assertEqual(path, str(self.out / "filmstrip.html"))
        page = Path(path).read_text()
        self.assertIn('<video controls src="run.webm"></video>', page)
        self.assertIn('<img src="01-click-save.png"', page)
        self.assertIn("click: &quot;&lt;b&gt;Save&lt;/b&gt; &amp; close&quot;", page)
        self.assertIn("/tmp/&lt;flows&gt;/x.yaml", page)
        self.assertIn("https://example.com/?a=1&amp;b=&lt;2&gt;", page)
        self.assertIn("Chrome went &lt;away&gt;", page)
        self.assertIn("no &lt;match&gt;", page)
        self.assertIn("frame withheld: the page may show a secret", page)
        self.assertIn('<span class="skipped">skipped</span>', page)
        self.assertEqual(page.count("<tr><td>"), 3)
        for outside in ("<script", "<link", "http://", "src=\"/", "src=\"https"):
            self.assertNotIn(outside, page)

    def test_without_a_video_it_says_why(self):
        report = empty_report(script_path=None, url=None, started_at="t", error=None,
                              video=result(reason="ffmpeg not found", frames=2, frames_dir=str(self.out / "frames")))
        page = Path(filmstrip(report, self.out)).read_text()
        self.assertNotIn("<video", page)
        self.assertIn("no video: ffmpeg not found", page)


class FakeRecorder:
    def __init__(self, log, made):
        self.log = log
        self.made = made

    def start(self):
        self.log.append("start")

    def stop(self):
        self.log.append("stop")

    def finish(self):
        self.log.append("finish")
        return self.made


class RecordingDriver(FakeDriver):
    """A FakeDriver with a screencast: the recorder's calls land in `acted` with the driver's own."""

    def recorder(self, out_dir):
        self.acted.append(("recorder", Path(out_dir).name, None))
        self.made = result(path=str(Path(out_dir) / "run.webm"), frames=9, duration_s=1.5, encoder="libvpx-vp9")
        return FakeRecorder(self.acted, self.made)


class RunWithVideoTest(TempCase):
    def page(self):
        return make_page([action(1, "click", "Save")], text="Saved")

    def test_every_step_gets_a_frame_and_the_run_a_filmstrip(self):
        driver = RecordingDriver([self.page()])
        script = script_of({"click": "Save"}, {"checkpoint": "after"}, {"click": "Missing thing"},
                           {"expect": "never"}, url="http://fake/")
        report = run(script, self.out, jev=StubJev(), driver=driver, log=lambda _line: None, video=True,
                     settle_tries=2)
        self.assertEqual(driver.acted[:3], [("recorder", self.out.name, None), "start", ("navigate", "http://fake/")])
        self.assertEqual(driver.acted[-2:], ["stop", "finish"])  # FakeDriver closes nothing: no cdp
        steps = report["steps"]
        self.assertEqual([s["status"] for s in steps], ["pass", "pass", "fail", "skipped"])
        self.assertEqual(steps[0]["frame"], str(self.out / "01-click-save.png"))
        self.assertEqual(steps[1]["frame"], steps[1]["screenshot"])
        self.assertEqual(steps[2]["frame"], steps[2]["failure_screenshot"])
        self.assertIsNone(steps[3]["frame"])
        self.assertEqual(report["video"], driver.made)
        self.assertEqual(report["filmstrip"], str(self.out / "filmstrip.html"))
        written = json.loads((self.out / "run.json").read_text())
        self.assertEqual((written["video"], [s["frame"] for s in written["steps"]]),
                         (driver.made, [s["frame"] for s in steps]))
        page = (self.out / "filmstrip.html").read_text()
        self.assertIn('<img src="01-click-save.png"', page)
        self.assertIn('<video controls src="run.webm">', page)
        text = format_report(report)
        self.assertIn(f"video: {self.out / 'run.webm'} (9 frames, 1.5s, libvpx-vp9)", text)
        self.assertIn(f"filmstrip: {self.out / 'filmstrip.html'}", text)

    def test_without_video_nothing_extra(self):
        driver = RecordingDriver([self.page()])
        report = run(script_of({"click": "Save"}), self.out, jev=StubJev(), driver=driver, log=lambda _line: None)
        self.assertEqual((report["video"], report["filmstrip"], report["steps"][0]["frame"]), (None, None, None))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["run.json"])
        self.assertNotIn("recorder", [a[0] for a in driver.acted if isinstance(a, tuple)])
        self.assertNotIn("video:", format_report(report))

    def test_a_withheld_frame_and_no_screencast(self):
        class Withholding(RecordingDriver):
            def recorder(self, out_dir):
                return None  # the extension bridge

            def screenshot(self, path):
                return None

        report = run(script_of({"click": "Save"}, {"checkpoint": "keys"}), self.out, jev=StubJev(),
                     driver=Withholding([self.page()]), log=lambda _line: None, video=True)
        first, second = report["steps"]
        self.assertEqual((first["frame"], first["frame_withheld"]), (None, True))
        self.assertEqual((second["frame"], second["frame_withheld"], second["screenshot_withheld"]), (None, True, True))
        self.assertIn("   frame withheld: the page may show a secret", step_line(first))
        self.assertNotIn("frame withheld", step_line(second))  # its screenshot line says it already
        self.assertEqual(report["video"], result(reason=NO_SCREENCAST))
        self.assertIn(f"video: none — {NO_SCREENCAST}", format_report(report))
        self.assertIn("frame withheld: the page may show a secret", (self.out / "filmstrip.html").read_text())

    def test_a_dead_tab_leaves_the_frame_empty_and_the_failure_standing(self):
        class Dead(RecordingDriver):
            def screenshot(self, path):
                raise CdpError("Page.captureScreenshot: connection closed")

        report = run(script_of({"click": "Save"}), self.out, jev=StubJev(), driver=Dead([self.page()]),
                     log=lambda _line: None, video=True)
        self.assertEqual((report["steps"][0]["status"], report["steps"][0]["frame"]), ("pass", None))
        self.assertNotIn("frame_withheld", report["steps"][0])

    def test_a_run_that_dies_still_writes_its_filmstrip(self):
        class Refusing(RecordingDriver):
            def navigate(self, url):
                raise CdpError(f"navigate {url}: net::ERR_UNSAFE_PORT")

        with self.assertRaises(CdpError):
            run(script_of({"click": "Save"}, url="http://fake/"), self.out, jev=StubJev(),
                driver=Refusing([self.page()]), log=lambda _line: None, video=True)
        written = json.loads((self.out / "run.json").read_text())
        self.assertEqual((written["steps"][0]["status"], written["video"]["frames"]), ("skipped", 9))
        page = (self.out / "filmstrip.html").read_text()
        self.assertIn("net::ERR_UNSAFE_PORT", page)
        self.assertIn('<span class="skipped">skipped</span>', page)

    def test_only_our_own_tab_has_a_screencast(self):
        self.assertIsNone(Driver(ExtTab(HookCdp(), None, None)).recorder(self.out))
        self.assertIsInstance(Driver(Tab(HookCdp(), "t", "s")).recorder(self.out), Recorder)


class CliVideoTest(TempCase):
    def test_the_flag_reaches_the_run(self):
        script = self.out / "steps.yaml"
        script.write_text("steps:\n  - click: Save\n")
        seen = []

        def stop(*_args, **kwargs):
            seen.append(kwargs["video"])
            raise CdpError("stop")

        with mock.patch.object(cli, "run", stop), mock.patch.object(cli, "release_chrome"), \
                contextlib.redirect_stderr(io.StringIO()):
            cli.main(["run", str(script), "--out", str(self.out / "o"), "--stub", "--video"])
            cli.main(["run", str(script), "--out", str(self.out / "o"), "--stub"])
        self.assertEqual(seen, [True, False])

    def test_doctor_names_ffmpeg(self):
        with mock.patch.object(video_module.shutil, "which", return_value=None):
            self.assertIsNone(video_module.ffmpeg_version())
        listing = subprocess.CompletedProcess([], 0, "ffmpeg version 8.1.1 Copyright (c) 2000-2026\n", "")
        with mock.patch.object(video_module.shutil, "which", return_value="/bin/ffmpeg"), \
                mock.patch.object(video_module.subprocess, "run", return_value=listing):
            self.assertEqual(video_module.ffmpeg_version(), "8.1.1")


class LiveVideoTest(TempCase):
    """`run --stub --video` through the CLI in our own headless Chrome; skipped without Chrome."""

    @classmethod
    def setUpClass(cls):
        try:
            ensure_chrome(profile_dir=PROFILE_DIR, headless=True)
        except CdpError as exc:
            raise unittest.SkipTest(str(exc))

    def test_a_stub_run_records_frames_and_a_filmstrip(self):
        page = self.out / "page.html"
        page.write_text("<!doctype html><title>Video</title><h1>Video check</h1>"
                        "<button onclick=\"document.querySelector('p').textContent='Clicked once'\">Change</button>"
                        "<p>Waiting</p>")
        script = self.out / "steps.yaml"
        script.write_text(f"url: {page.as_uri()}\nsteps:\n  - click: Change\n  - expect: Clicked once\n"
                          "  - wait: 300\n")
        out = self.out / "run"
        flags = ["--profile-dir", PROFILE_DIR] if PROFILE_DIR else []
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["run", str(script), "--out", str(out), "--stub", "--video", "--json", *flags])
        self.assertEqual(code, 0)
        report = json.loads((out / "run.json").read_text())
        made = report["video"]
        self.assertGreater(made["frames"], 0, made)
        if made["path"]:
            self.assertTrue(Path(made["path"]).is_file())
        else:
            self.assertTrue((out / "frames" / "index.json").is_file(), made)
        self.assertTrue(all(step["frame"] for step in report["steps"]))
        self.assertTrue((out / "filmstrip.html").is_file())
        self.assertEqual([p for p in out.iterdir() if p.name.startswith(".frames-")], [])


if __name__ == "__main__":
    unittest.main()

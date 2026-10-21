"""--video: a screencast of the run as run.webm (JPEG frames when ffmpeg is missing) and a filmstrip.

Chrome sends a screencast frame only when the page paints, so a frame lasts until the next one:
the video keeps real timing (a page that waits on Jev holds still for as long as it did). A video
problem never fails the run; run.json's `video` says why there is none.
"""

import base64
import html
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .cdp import CdpError

FRAME = "Page.screencastFrame"
QUALITY = 60
# The last frame lasts until the run ended, but always long enough to be seen.
MIN_LAST_FRAME = 0.1
# A frame needs some duration of its own in the concat list, even one Chrome sent twice at once.
MIN_FRAME = 0.01
STOP_TIMEOUT = 5
# ffmpeg gets this long plus twice the video's length; realtime VP8/VP9 is far faster.
ENCODE_TIMEOUT = 60
ENCODERS = ("libvpx-vp9", "libvpx")
NO_SCREENCAST = "no screencast over the extension bridge; the filmstrip holds the step frames"


def result(path=None, reason=None, encoder=None, frames=0, frames_dir=None, duration_s=0.0):
    """run.json's `video`: the same keys whatever happened."""
    return {"path": path, "reason": reason, "encoder": encoder, "frames": frames,
            "frames_dir": frames_dir, "duration_s": duration_s}


class Recorder:
    """One tab's screencast: frames land as JPEG files in a private dir under --out until finish()."""

    def __init__(self, tab, out_dir):
        self.tab = tab
        self.out_dir = Path(out_dir)
        self.dir = Path(tempfile.mkdtemp(prefix=".frames-", dir=self.out_dir))  # 0700
        self.frames = []  # (path, Chrome's frame timestamp in epoch seconds)
        self.refused = None
        self.ended = None
        self._lock = threading.Lock()

    def start(self):
        self.tab.cdp.on_event(FRAME, self.tab.session_id, self._frame)
        try:
            self.tab.call("Page.startScreencast", format="jpeg", quality=QUALITY, maxWidth=self.tab.width,
                          maxHeight=self.tab.height, everyNthFrame=1)
        except CdpError as exc:
            self.refused = str(exc)
            self.stop()

    def _frame(self, params):
        """In the Cdp reader thread: ack first (Chrome sends the next frame only after it), then keep it."""
        self.tab.cdp.send_only("Page.screencastFrameAck", {"sessionId": params["sessionId"]},
                               session_id=self.tab.session_id)
        with self._lock:
            if self.ended is not None:
                return
            path = self.dir / f"{len(self.frames) + 1:05d}.jpg"
            path.write_bytes(base64.b64decode(params["data"]))
            self.frames.append((path, params["metadata"]["timestamp"]))

    def stop(self):
        """Best effort and idempotent; once it returns no frame is added."""
        with self._lock:
            if self.ended is not None:
                return
            self.ended = time.time()
        self.tab.cdp.off_event(FRAME, self.tab.session_id)
        if self.refused is None:
            try:
                self.tab.cdp.call("Page.stopScreencast", {}, session_id=self.tab.session_id,
                                  timeout=STOP_TIMEOUT)
            except CdpError:
                pass  # the tab is gone: so is its screencast

    def durations(self):
        """Seconds each frame was on screen: until the next frame, the last one until stop()."""
        stamps = [ts for _path, ts in self.frames]
        gaps = [max(after - before, MIN_FRAME) for before, after in zip(stamps, stamps[1:])]
        return gaps + [max(self.ended - stamps[-1], MIN_LAST_FRAME)] if stamps else []

    def finish(self):
        """run.webm when ffmpeg can make it, else the frames in frames/ with an index.json."""
        (self.out_dir / "run.webm").unlink(missing_ok=True)  # a reused --out keeps no older run's video
        durations = self.durations()
        count, total = len(self.frames), round(sum(durations), 2)
        if not count:
            shutil.rmtree(self.dir, ignore_errors=True)
            return result(reason="no frames" + (f" (screencast refused: {self.refused})" if self.refused else ""))
        reason, encoder = "ffmpeg not found", None
        if shutil.which("ffmpeg"):
            encoder = _encoder()
            reason = None if encoder else "ffmpeg has no VP9 or VP8 encoder (libvpx)"
        if encoder:
            reason = self._encode(durations, encoder, total)
            if reason is None:
                shutil.rmtree(self.dir, ignore_errors=True)
                return result(path=str(self.out_dir / "run.webm"), encoder=encoder, frames=count,
                              duration_s=total)
        frames_dir = self._keep_frames(durations)
        return result(reason=reason, frames=count, frames_dir=str(frames_dir), duration_s=total)

    def _encode(self, durations, encoder, total):
        """None when run.webm was written, else why not."""
        lines = ["ffconcat version 1.0"]
        for (path, _ts), seconds in zip(self.frames, durations):
            lines += [f"file '{path.name}'", f"duration {seconds:.3f}"]
        lines.append(f"file '{self.frames[-1][0].name}'")  # the concat demuxer ignores the last duration
        listing = self.dir / "frames.ffconcat"
        listing.write_text("\n".join(lines) + "\n")
        width, height = self.tab.width // 2 * 2, self.tab.height // 2 * 2  # yuv420p wants even sizes
        # A whole-page look sends taller frames, scaled down to fit: one size for the encoder. No fps
        # filter: webm keeps each frame's own time (to ffmpeg's 1/25 s image tick); an fps filter
        # stretched the last frame.
        scale = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                 f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
        out = self.out_dir / "run.webm"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-i", str(listing),
                   "-vf", scale, "-c:v", encoder, "-b:v", "1M", "-deadline", "realtime", "-cpu-used", "8",
                   "-an", str(out)]
        timeout = ENCODE_TIMEOUT + 2 * total
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            done = None
        except OSError as exc:
            return f"ffmpeg failed: {exc}"
        finally:
            listing.unlink(missing_ok=True)
        if done is not None and done.returncode == 0 and out.exists():
            return None
        out.unlink(missing_ok=True)
        if done is None:
            return f"ffmpeg timed out after {timeout:g}s"
        tail = " ".join(done.stderr.strip().splitlines()[-2:])[-200:]
        return f"ffmpeg failed: {tail or f'exit {done.returncode}'}"

    def _keep_frames(self, durations):
        """frames/00001.jpg… plus index.json [{file, ts, duration_s}]; an older run's frames/ goes."""
        frames_dir = self.out_dir / "frames"
        index = [{"file": path.name, "ts": ts, "duration_s": round(seconds, 3)}
                 for (path, ts), seconds in zip(self.frames, durations)]
        (self.dir / "index.json").write_text(json.dumps(index, indent=2))
        shutil.rmtree(frames_dir, ignore_errors=True)
        self.dir.rename(frames_dir)
        return frames_dir


def _encoder():
    """The best VP9/VP8 encoder this ffmpeg has, or None."""
    try:
        listed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                                timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    names = {line.split()[1] for line in listed.splitlines() if len(line.split()) > 1}
    return next((name for name in ENCODERS if name in names), None)


def ffmpeg_version():
    """`ffmpeg -version`'s version token, or None when there is no ffmpeg on PATH."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        first = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    words = first.split()
    return words[2] if len(words) > 2 and words[:2] == ["ffmpeg", "version"] else "unknown version"


# -- the filmstrip --------------------------------------------------------------

STYLE = """body{font:14px/1.4 system-ui,sans-serif;margin:24px;color:#222}
h1{font-size:18px;margin:0 0 4px}p{margin:4px 0}video{max-width:100%;margin:12px 0}
table{border-collapse:collapse;width:100%}td,th{border-top:1px solid #ddd;padding:8px;text-align:left;
vertical-align:top}img{max-width:480px;border:1px solid #ccc}.fail{color:#b00020;font-weight:600}
.skipped,.none{color:#888}.error{color:#b00020;white-space:pre-wrap}"""


def filmstrip(report, out_dir):
    """<out>/filmstrip.html: the run at a glance, one row per step with its frame. Self-contained
    (no script, font or remote asset); frames and the video load by relative path."""
    out_dir = Path(out_dir)
    totals = report["totals"]
    head = [f"<h1>{_e(report['script_path'] or 'ck-browser run')}</h1>",
            f"<p>{_e(report['url'] or '')} · started {_e(report['started_at'])} ·"
            f" {totals['passed']}/{totals['steps']} passed, {totals['failed']} failed ·"
            f" {totals['wall_ms'] / 1000:.1f} s</p>"]
    if report["error"]:
        head.append(f'<p class="error">{_e(report["error"])}</p>')
    video = report.get("video") or {}
    if video.get("path"):
        head.append(f'<video controls src="{_e(_relative(video["path"], out_dir))}"></video>')
    elif video:
        head.append(f'<p class="none">no video: {_e(video["reason"])}</p>')
    rows = [_row(record, out_dir) for record in report["steps"]]
    page = ("<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>ck-browser filmstrip</title>"
            f"<style>{STYLE}</style></head><body>\n" + "\n".join(head)
            + "\n<table><tr><th>#</th><th>step</th><th>status</th><th>ms</th><th>frame</th></tr>\n"
            + "\n".join(rows) + "\n</table></body></html>\n")
    path = out_dir / "filmstrip.html"
    path.write_text(page)
    return str(path)


def _row(record, out_dir):
    status = record["status"]
    verdict = status
    if record.get("expectation"):
        verdict += f" · verbatim={record['expectation']['verbatim']:.2f}"
    step = _e(record["step"])
    if record["error"]:
        step += f'<div class="error">{_e(record["error"])}</div>'
    if status == "skipped":
        frame = '<span class="skipped">skipped</span>'
    elif record.get("frame"):
        src = _e(_relative(record["frame"], out_dir))
        frame = f'<a href="{src}"><img src="{src}" alt="step {record["n"]}" loading="lazy"></a>'
    elif record.get("frame_withheld"):
        frame = '<span class="none">frame withheld: the page may show a secret</span>'
    else:
        frame = '<span class="none">no frame</span>'
    ms = "" if status == "skipped" else str(record["ms"])
    return (f'<tr><td>{record["n"]}</td><td>{step}</td><td class="{_e(status)}">{_e(verdict)}</td>'
            f"<td>{ms}</td><td>{frame}</td></tr>")


def _relative(path, out_dir):
    return Path(os.path.relpath(path, out_dir)).as_posix()


def _e(text):
    return html.escape(str(text))

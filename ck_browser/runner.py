"""The step loop: one Jev request per decision step, everything else is code."""

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import questions as Q
from .cdp import Cdp, StalePage, StepTimeout, Tab, ensure_chrome, ws_port
from .ext import CAPTURE_TIMEOUT, ExtCdp, ExtTab, profile_label, resolve
from .model import validate_choice, validate_noul
from .page import (Apart, Covered, act, drag, file_inputs, focused, inner_scrollers, near_misses, observe,
                   observe_full_page, scroll_inner, scroll_into_view, upload, visible_strings)
from .report import step_line
from .script import MASK, ScriptError, is_secret
from .video import NO_SCREENCAST, Recorder, filmstrip, result

# verb -> (the action kind offered as candidates, what to call them when none are visible)
ABLE = {"click": ("click", "clickable"), "fill": ("fill", "fillable"),
        "select": ("select", "selectable"), "hover": (None, "visible")}
# Abstaining this many times in a row on a page that has not changed: look at the whole page.
ABSTAIN_TRIES = 3
COST_PER_INPUT_TOKEN = 0.042 / 1e6
EXPECT_TEXT_CHARS = 8000
# Pages of one scrolling panel the below-the-fold look reads before giving up on it.
PANEL_PAGES = 8
# What can show a value capture_secret reads: fields (read-only ones too) and plain text holders.
VALUE_ROLES = {"textbox", "searchbox", "text"}
# Verbs that choose an element: a failure of theirs records which page elements came nearest.
CHOOSING = {"click", "fill", "select", "hover", "upload", "drag", "capture_secret"}
# Seconds a step may take: per-call timeouts multiply across retries into minutes on a page whose
# renderer stopped answering (an alert(), a busy loop), so the step as a whole has a deadline.
DEFAULT_STEP_TIMEOUT = 60
# A fresh budget after a failure, for its evidence (near misses, dismissing a dialog, the screenshot).
TIMEOUT_GRACE = 10


class StepFailure(RuntimeError):
    """This step did not pass; the run stops here."""


class NoTarget(StepFailure):
    """Nothing the observed page offers is what the step named."""


class Driver:
    """Everything the loop does to a browser tab. Tests substitute their own."""

    def __init__(self, tab):
        self.tab = tab

    @property
    def calls(self):
        return self.tab.cdp.calls

    def navigate(self, url):
        self.tab.navigate(url)

    def observe(self, drop_targets=False, text_holders=False):
        return observe(self.tab, drop_targets=drop_targets, text_holders=text_holders)

    def observe_full_page(self, drop_targets=False, text_holders=False):
        return observe_full_page(self.tab, drop_targets=drop_targets, text_holders=text_holders)

    def near_misses(self, text):
        return near_misses(self.tab, text)

    def visible_strings(self):
        return visible_strings(self.tab)

    def act(self, action, page, text=None):
        return act(self.tab, action, page, text=text)

    def file_inputs(self):
        return file_inputs(self.tab)

    def upload(self, node, path, page):
        return upload(self.tab, node, path, page)

    def drag(self, source, target, page):
        return drag(self.tab, source, target, page)

    def capture_secret(self, node, service):
        return self.tab.capture_secret(node, service)

    def focused(self):
        return focused(self.tab)

    def scroll_into_view(self, node):
        return scroll_into_view(self.tab, node)

    def inner_scrollers(self):
        return inner_scrollers(self.tab)

    def scroll_inner(self, node, to=None):
        return scroll_inner(self.tab, node, to=to)

    def screenshot(self, path):
        return self.tab.screenshot_png(path)

    def recorder(self, out_dir):
        """A screencast for --video, or None over the extension bridge: its frames would bypass
        the extension's per-capture check for a page showing a secret."""
        return None if isinstance(self.tab, ExtTab) else Recorder(self.tab, out_dir)

    def deadline(self, at):
        """No CDP call waits past the monotonic time `at`; None lifts the deadline."""
        if at is not None:
            self.tab.cdp.waiting_on = None  # a new budget: an older cut names nothing here
        self.tab.cdp.deadline = at

    def waiting_on(self):
        """The CDP method whose wait the deadline cut, or None."""
        return self.tab.cdp.waiting_on

    def unstick(self):
        """Dismiss a JavaScript dialog that blocks the page; what it was, or None."""
        return self.tab.dismiss_dialog()

    def drain(self):
        return self.tab.drain_events()

    def close(self):
        self.tab.close()


def run(script, out_dir, *, jev, ws_url=None, chrome_port=0, profile_dir=None,
        headless=True, launch=True, threshold=0.6, expect_threshold=0.6,
        ready_threshold=0.5, settle_tries=20, keep_tab=False, log=print, driver=None,
        target="chrome", step_timeout=DEFAULT_STEP_TIMEOUT, ext_profile=None, video=False):
    """Execute every step, writing <out_dir>/run.json and screenshots.

    Each step fails once it has taken `step_timeout` seconds (a wait step adds its own wait);
    opening the script's url and the first look share one such budget.

    `target` "chrome" drives a new tab of our own Chrome; "ext" drives the tab the human handed
    over through the extension bridge, or with none handed over, a new tab the extension opens at
    `url:` when the human allows it; either is never closed (`keep_tab` does not apply).
    `ext_profile` names the human's Chrome profile (directory, email or name); without it the
    only connected one, or the only one with a claimed tab (ext.resolve).

    `video` records the tab (run.webm, or frames/ without ffmpeg), gives every step a frame and
    writes filmstrip.html; run.json's `video` and `filmstrip` say what came of it.
    """
    for n, step in enumerate(script["steps"], 1):
        if step["verb"] == "capture_secret" and target != "ext":
            raise ScriptError(f"step {n}: capture_secret needs --target ext: only the extension"
                              " reads a secret without showing it to ck-browser")
    out_dir = Path(out_dir)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cdp, ws, opened, profile = None, ws_url, False, None
    if driver is None and target == "ext":
        entry = resolve(ext_profile)
        profile = {key: entry[key] for key in ("profile_dir", "name", "email", "instance")}
        log(f"profile: {profile_label(entry)}")
        ws = f"ext:{entry['socket']}"
        cdp = ExtCdp(entry["socket"], open_url=script.get("url"))
        opened = cdp.opened_now  # the extension opened the tab at `url:`; it is loading already
        driver = Driver(ExtTab.open(cdp, wait=opened))
        keep_tab = False
    elif driver is None:
        ws = ws_url or ensure_chrome(port=chrome_port, profile_dir=profile_dir,
                                     headless=headless, launch=launch)
        cdp = Cdp(ws)
        tab = Tab.open(cdp)
        driver = Driver(tab)
    recorder = driver.recorder(out_dir) if video else None
    if recorder is not None:
        recorder.start()
    loop = _Loop(script, out_dir, jev, driver, threshold, expect_threshold,
                 ready_threshold, settle_tries, step_timeout, log, frames=video)
    error = None
    try:
        loop.start(script.get("url"), navigate=not opened)
        loop.run()
    except Exception as exc:  # re-raised below, once run.json says what happened
        error = exc
    finally:
        # Cleanup never decides whether there is a report: run.json is written either way.
        if recorder is not None:
            recorder.stop()  # before the tab closes: its last frames are still coming
        if cdp is not None and not keep_tab:
            _close(driver.close, log)
        if cdp is not None:
            _close(cdp.close, log)
        _close(getattr(jev, "close", None), log)
    report = loop.report(ws, error, profile)
    report["video"] = report["filmstrip"] = None
    if video:
        report["video"] = _video(recorder)
        report["filmstrip"] = str(out_dir / "filmstrip.html")
    (out_dir / "run.json").write_text(json.dumps(report, indent=2))
    if video:
        filmstrip(report, out_dir)
    if error is not None:
        raise error
    return report


class _Loop:
    def __init__(self, script, out_dir, jev, driver, threshold, expect_threshold,
                 ready_threshold, settle_tries, step_timeout, log, frames=False):
        self.script = script
        self.out_dir = out_dir
        self.jev = jev
        self.driver = driver
        self.threshold = threshold
        self.expect_threshold = expect_threshold
        self.ready_threshold = ready_threshold
        self.settle_tries = settle_tries
        self.step_timeout = step_timeout
        # The monotonic time the current step runs out; Jev requests check it, CDP calls enforce it.
        self.step_deadline = None
        self.log = log
        # --video: every step that ran gets a frame for the filmstrip.
        self.frames = frames
        self.page = None
        self.history = []
        self.records = []
        self.model = getattr(jev, "model", "unknown")
        self.started = time.perf_counter()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- the loop ---------------------------------------------------------

    def start(self, url, navigate=True):
        """Open the script's url and take the first look, within one step's budget."""
        def first():
            if url and navigate:
                self.driver.navigate(url)
            self.page = self.driver.observe()

        try:
            self._within(self.step_timeout, first)
        except (ValueError, RuntimeError) as exc:
            if not self._late(exc):
                raise
            error = (f"opening {url or 'the page'} timed out after {self.step_timeout:g}s,"
                     f" waiting on {self.driver.waiting_on() or 'the page'}")
            error += self._within(TIMEOUT_GRACE, self._unstick)
            raise StepTimeout(error) from None

    def run(self):
        failed = False
        for n, step in enumerate(self.script["steps"], 1):
            if failed:
                record = self._record(n, step, status="skipped")
                self.records.append(record)
                self.log(step_line(record))
                continue
            record = self._record(n, step)
            started = time.perf_counter()
            budget = self._budget(step)
            try:
                self._within(budget, lambda: self._step(n, step, record))
            except (StepFailure, StalePage, ValueError, RuntimeError) as exc:
                record["status"] = "fail"
                record["error"] = str(exc)
                if self._late(exc):
                    url = step["arg"] if step["verb"] == "open" else (self.page or {}).get("url")
                    record["timed_out"] = True
                    record["error"] = (f"step {n} ({step['raw']}) timed out after {budget:g}s on"
                                       f" {url or 'unknown page'}, waiting on"
                                       f" {self.driver.waiting_on() or ('Jev' if record['jev'] else 'the page')}")
                    self._within(TIMEOUT_GRACE, lambda: self._after_timeout(record, step))
                else:
                    self._within(TIMEOUT_GRACE, lambda: self._after_failure(record, step, exc))
            record["ms"] = round((time.perf_counter() - started) * 1000)
            record["observations"] = self._observations()
            if self.frames:
                self._within(TIMEOUT_GRACE, lambda: self._frame(record, step))
            self.records.append(record)
            self.log(step_line(record))
            failed = record["status"] == "fail"

    def _budget(self, step):
        """Seconds the step may take: a wait step adds its wait, capture_secret the human's typing."""
        if step["verb"] == "wait":
            return self.step_timeout + step["arg"] / 1000
        if step["verb"] == "capture_secret":
            return self.step_timeout + CAPTURE_TIMEOUT
        return self.step_timeout

    def _within(self, seconds, work):
        """`work()` under a deadline `seconds` from now; the driver drops it however work ends."""
        self.step_deadline = time.monotonic() + seconds
        self.driver.deadline(self.step_deadline)
        try:
            return work()
        finally:
            self.driver.deadline(None)

    def _late(self, exc):
        """The failure is the deadline's: some paths wrap or swallow the StepTimeout itself."""
        return isinstance(exc, StepTimeout) or (self.step_deadline is not None
                                                 and time.monotonic() >= self.step_deadline)

    def _unstick(self):
        """After a timeout: dismiss a dialog that blocks the page; what it was, as an error suffix."""
        dialog = self.driver.unstick()
        return f"; {dialog}" if dialog else ""

    def _after_failure(self, record, step, exc):
        """Evidence for an ordinary failure: a page that hangs right after it costs no more than the grace."""
        if isinstance(exc, StepFailure) and step["verb"] in CHOOSING:
            record["near_misses"] = self._near_misses(step)
        self._capture(record, step, fail=True)

    def _after_timeout(self, record, step):
        """The dialog goes first: left open, it would block the failure screenshot too."""
        record["error"] += self._unstick()
        self._capture(record, step, fail=True)

    def _step(self, n, step, record):
        verb, arg = step["verb"], step["arg"]
        if verb == "open":
            self.driver.navigate(arg)
            self.page = self.driver.observe()
        elif verb == "wait":
            time.sleep(arg / 1000)
            self.page = self.driver.observe()
        elif verb == "scroll":
            delta = 560 if arg == "down" else -560
            self.driver.act({"id": f"scroll_{arg}", "kind": "scroll", "label": f"Scroll {arg}", "delta": delta},
                            self.page)
            self.page = self.driver.observe()
        elif verb == "press":
            self.driver.act({"id": "press", "kind": "press", "key": arg, "label": f"Press {arg}"}, self.page)
            self.page = self.driver.observe()
        elif verb == "type":
            # No Jev decision: the text goes wherever focus is, and a password field takes only a secret.
            focus = self.driver.focused()
            record["focused"] = _focus(focus)
            if focus is None:
                raise StepFailure("nothing has focus: click or Tab to a field first")
            if focus["password"] and not arg["secret"]:
                raise StepFailure("the focused field is a password field: use fill,"
                                  " or type: {text: ..., secret: true}")
            name = focus["label"] or focus["role"] or focus["tag"]
            before = self.page["fingerprint"] if self.page else None
            self.driver.act({"id": "type", "kind": "type", "label": f"Type into {name}"}, self.page,
                            text=arg["text"])
            self._acted(n, step, record, name, before)
        elif verb == "checkpoint":
            self._capture(record, step)
        elif verb == "expect":
            self._expect(n, step, record)
        elif verb == "expect_count":
            self._expect_count(n, step, record)
        elif verb == "upload":
            self._upload_step(n, step, record)
        elif verb == "drag":
            self._drag_step(n, step, record)
        elif verb == "capture_secret":
            self._capture_secret_step(n, step, record)
        else:
            self._target_step(n, step, record)
        if verb in ("press", "type"):
            try:
                record["focused"] = _focus(self.driver.focused())
            except (StalePage, RuntimeError):  # evidence only: an Enter that navigated keeps its pass
                pass

    # -- decision steps ---------------------------------------------------

    def _choose(self, n, step, record, arg_text, *, produce, noun, rules, value=None,
                observe=None, wide=None, holders=False):
        """One Jev pick over the page as it is now: the settle loop, an abstain option, the threshold.

        Nothing matching in the viewport is not an answer yet — the target may simply be below
        the fold, so a whole-document pass follows. That pass is not the last word either: a page
        that is still rendering abstains the same way, so a whole-page look that also finds
        nothing hands the wait back what is left of its budget. With `holders`, the plain text
        holders get a look too, as a role-less control is only text: once per page state after
        that, only those the page marks clickable; every other one only when the budget is
        spent, so a heading that repeats a late control's label does not win the race to it.
        """
        observe = observe or self.driver.observe
        wide = wide or self.driver.observe_full_page

        def build():
            """The question about the page as it is now — a retry must not offer stale elements."""
            candidates = self._wait_for_candidates(record, noun, produce, observe, budget)
            state = self._state(n, step, arg_text, [c["element"] for c in candidates], value=value)
            return state, {"target": _target_question(step, candidates, rules)}, candidates

        budget = self.settle_tries
        looked_wide_at, wide_miss = None, None
        while True:
            try:
                answers, candidates, used = self._settle(record, build, step["raw"], observe, budget)
                try:
                    return self._pick(record, answers, candidates)
                except NoTarget as missed:
                    # `ready` judges the page, not the target: a typeahead list fetched after a
                    # fill renders a moment later, so a ready page that lacks it keeps its budget.
                    missed.remaining = budget - used
                    raise
            except NoTarget as missed:
                # One whole-page look per page state: a page that has not moved since the last
                # one would only repeat its answer.
                if self.page["marker"] != looked_wide_at:
                    looked_wide_at = self.page["marker"]
                    try:
                        return self._below_the_fold(n, step, record, arg_text, produce, rules, value,
                                                    missed, observe, wide)
                    except NoTarget as wider:
                        wide_miss = wider
                    if holders:
                        try:
                            return self._among_text_holders(n, step, record, arg_text, value,
                                                            clickable_only=True)
                        except NoTarget:
                            pass
                # `remaining` is what the abstain streak did not spend; nothing left fails here.
                budget = getattr(missed, "remaining", 0)
                if budget <= 0:
                    if holders:
                        try:
                            return self._among_text_holders(n, step, record, arg_text, value)
                        except NoTarget:
                            pass
                    raise wide_miss or missed
                time.sleep(0.1)
                self.page = observe()

    def _below_the_fold(self, n, step, record, arg_text, produce, rules, value, missed,
                        observe, wide):
        """One look at the whole document, then back to the viewport with the target in it.

        The snapshot keeps only what the viewport shows, so a target further down was never
        offered. No `ready` gate here: a stretched page carries hundreds of elements and never
        reads as settled. The threshold and the abstain option decide exactly as above.
        """
        self.page = wide()
        candidates = produce()
        if not candidates:
            return self._in_panels(n, step, record, arg_text, produce, rules, value, missed, observe)
        state = self._state(n, step, arg_text, [c["element"] for c in candidates], value=value)
        answers = self._ask(record, state, {"target": _target_question(step, candidates, rules)})
        try:
            chosen = self._pick(record, answers, candidates, " on the whole page")
        except NoTarget as wider:
            return self._in_panels(n, step, record, arg_text, produce, rules, value, wider, observe)
        record["fallback"] = "full_page"
        if chosen["action"] is None:  # a hidden file input: the file needs no geometry
            self.page = observe()
            return chosen
        self.driver.scroll_into_view(chosen["action"]["node"])
        self.page = observe()
        return _relocate(chosen["action"]["node"], produce())

    def _in_panels(self, n, step, record, arg_text, produce, rules, value, missed, observe):
        """Page through the panels that scroll on their own, one Jev pick per page.

        A panel keeps its height on the stretched page, so what it clips (the rest of a picker
        grid) was never offered there either. A panel that does not hold the target is scrolled
        back, so the retries that follow see the page as it was.
        """
        for panel in self.driver.inner_scrollers():
            for _ in range(PANEL_PAGES):
                if not self.driver.scroll_inner(panel["node"]):
                    break
                time.sleep(0.1)  # a virtualised list renders its next rows after the scroll
                self.page = observe()
                candidates = produce()
                if not candidates:
                    continue
                state = self._state(n, step, arg_text, [c["element"] for c in candidates], value=value)
                answers = self._ask(record, state, {"target": _target_question(step, candidates, rules)})
                try:
                    chosen = self._pick(record, answers, candidates, " in a scrolling panel")
                except NoTarget:
                    continue
                record["fallback"] = "panel_scroll"
                return chosen
            self.driver.scroll_inner(panel["node"], to=panel["top"])
            self.page = observe()
        raise missed

    def _among_text_holders(self, n, step, record, arg_text, value, clickable_only=False):
        """One pick among plain text holders: the viewport, then the whole page.

        A custom dropdown's trigger or an option in its open list often has no role at all, so
        the semantic looks never offered it. With `clickable_only` only the holders under a
        pointer cursor are offered (a look with none asks Jev nothing): a heading that repeats
        the label of a control still rendering must not be clicked in its place. The verdict
        of the semantic looks is what the caller's error names, so a miss here puts it back.
        """
        kept = record["candidates"], record["none_p"]

        def holders():
            return [c for c in _candidates(self.page, "click") if c["element"]["role"] == "text"
                    and (not clickable_only or c["element"].get("clickable"))]

        for wide, look in enumerate((self.driver.observe, self.driver.observe_full_page)):
            self.page = look(text_holders=True)
            candidates = holders()
            if not candidates:
                continue
            state = self._state(n, step, arg_text, [c["element"] for c in candidates], value=value)
            answers = self._ask(record, state, {"target": _target_question(step, candidates, Q.TARGET_TEXT)})
            try:
                chosen = self._pick(record, answers, candidates, " among text holders")
            except NoTarget:
                continue
            record["fallback"] = "text_holder"
            if not wide:
                return chosen
            self.driver.scroll_into_view(chosen["action"]["node"])
            self.page = self.driver.observe(text_holders=True)
            return _relocate(chosen["action"]["node"], holders())
        record["candidates"], record["none_p"] = kept
        raise NoTarget("no text holder matched")

    def _target_step(self, n, step, record):
        verb = step["verb"]
        arg = step["arg"]
        arg_text = arg["field"] if verb in ("fill", "select") else arg
        value = arg["value"] if verb in ("fill", "select") else None
        before = self.page["fingerprint"]
        try:
            label = self._native_target(n, step, record, arg_text, value)
        except NoTarget:
            if verb != "select":
                raise
            label = self._custom_select(n, step, record, arg_text, value)
        self._acted(n, step, record, label, before)

    def _native_target(self, n, step, record, arg_text, value):
        """click, fill, select and hover on what the page offers for them; returns the label."""
        verb = step["verb"]
        kind, noun = ABLE[verb]
        for attempt in range(3):
            # The pick is inside the retry: choosing observes and scrolls too, and a page that
            # moved under either half of the step is the same problem.
            try:
                chosen = self._choose(n, step, record, arg_text, noun=noun, rules=Q.TARGETS[verb],
                                      produce=lambda: _candidates(self.page, kind), value=value,
                                      holders=verb in ("click", "hover"))
                action, text = chosen["action"], None
                if verb == "fill":
                    text = value
                elif verb == "select":
                    action = _select_option(chosen, value)
                elif verb == "hover":
                    action = {**action, "kind": "hover"}
                self.driver.act(action, self.page, text=text)
                break
            except StalePage as churn:
                self._again(attempt, churn)
        return chosen["element"]["label"]

    def _custom_select(self, n, step, record, field, value):
        """No native select names the field: click the dropdown, then the option the click added.

        The option is matched by text in code, among the nodes that were not on the page before
        the click — an open list's "A" is new, a table cell that also reads "A" is not.
        """
        def observe():
            return self.driver.observe(text_holders=True)

        def wide():
            return self.driver.observe_full_page(text_holders=True)

        def clickables():
            return _candidates(self.page, "click")

        self.page = observe()
        for attempt in range(3):
            try:
                trigger = self._choose(n, step, record, field, produce=clickables, noun="clickable",
                                       rules=Q.TARGET_SELECT_CUSTOM, observe=observe, wide=wide)
                known = {action.get("node") for action in self.page["actions"]}
                self.driver.act(trigger["action"], self.page)
                break
            except StalePage as churn:
                self._again(attempt, churn)
                self.page = observe()  # `_again` looks with plain eyes; the trigger may be text
        label, added = trigger["element"]["label"], []
        for look in range(self.settle_tries + 1):
            if look:
                time.sleep(0.1)  # a list animates in after the click
            self.page = observe()
            added = [c for c in clickables() if c["action"]["node"] not in known]
            option = _best_match(added, value, lambda c: (c["element"]["label"],))
            if option is None:
                continue
            try:
                self.driver.act(option["action"], self.page)
            except StalePage:
                continue  # the list was still moving under the option: look again
            record["fallback"] = "custom_select"
            record["option"] = {"label": option["element"]["label"], "role": option["element"]["role"]}
            return label
        raise StepFailure(f"option '{value}' not found after opening '{label}';"
                          f" the click added: {[c['element']['label'] for c in added][:12]}")

    def _upload_step(self, n, step, record):
        """The file goes in over CDP: a hidden input is never clicked and no chooser ever opens.

        Chrome resolves a relative path against its own working directory, so the step's path is
        made absolute here — where the script's idea of "relative to" still holds.
        """
        field, path = step["arg"]["field"], Path(step["arg"]["path"]).resolve()
        if not path.is_file():
            raise StepFailure(f"upload file not found: {path}")
        before = self.page["fingerprint"]
        for attempt in range(3):
            try:
                chosen = self._choose(n, step, record, field, produce=self._upload_candidates,
                                      noun="file picker", rules=Q.TARGET_UPLOAD)
                if chosen["file_input"] is None:
                    raise StepFailure(f"'{chosen['element']['label']}' does not open a file picker")
                self.driver.upload(chosen["file_input"], str(path), self.page)
                break
            except StalePage as churn:
                self._again(attempt, churn)
        self._acted(n, step, record, chosen["element"]["label"], before)

    def _upload_candidates(self):
        """The visible controls, plus every file input no visible control opens.

        A picker is usually a hidden input behind a button, and the button is what the step
        names — so the input it triggers is offered as that button, not a second time. A control
        two inputs both claim names neither of them: they are offered as inputs instead.
        """
        candidates = _candidates(self.page, "click")
        by_node = {c["action"]["node"]: c for c in candidates}
        for candidate in candidates:
            candidate["file_input"] = None
        inputs = self.driver.file_inputs()
        claimed = Counter(described["trigger"] for described in inputs)
        for described in inputs:
            trigger = by_node.get(described["trigger"]) if claimed[described["trigger"]] == 1 else None
            if trigger is not None:
                trigger["file_input"] = described["node"]
                continue
            candidates.append({
                "action": None, "options": None, "file_input": described["node"],
                "element": {"index": str(len(candidates) + 1), "role": "file input",
                            "label": described["label"], "value": described["accept"]},
            })
        return candidates

    def _drag_step(self, n, step, record):
        """Two picks, one gesture. The freshness guard covers the source only: from the moment
        the drag starts the page is expected to move under the destination.

        Drag observes more of the page than any other step: the thing that moves is often a
        draggable div, and where it goes is usually a plain text holder with a drop handler.
        """
        arg = step["arg"]
        before = self.page["fingerprint"]

        def observe():
            return self.driver.observe(drop_targets=True)

        def wide():
            return self.driver.observe_full_page(drop_targets=True)

        def sources():
            """What can be picked up: never a text holder, never an editable field — dragging a
            field's own selection is not the gesture, and its guard is stale against this page."""
            return [c for c in _candidates(self.page, None)
                    if c["element"]["role"] != "text" and c["action"]["kind"] != "fill"]

        def destinations():
            return _candidates(self.page, None)

        self.page = observe()
        for attempt in range(3):
            try:
                source = self._choose(n, step, record, arg["from"], produce=sources, noun="visible",
                                      rules=Q.TARGET_DRAG_FROM, observe=observe, wide=wide)
                record["drag_from"] = record["target"]
                verdict = {key: record[key] for key in ("target", "candidates", "none_p")}
                target = self._choose(n, step, record, arg["to"], produce=destinations, noun="visible",
                                      rules=Q.TARGET_DRAG_TO, observe=observe, wide=wide)
                record["drag_to"] = record["target"]
                # The step's target is what it dragged; the destination has its own key.
                record.update(verdict)
                record["drag_mode"] = self.driver.drag(source["action"], target["action"], self.page)
                break
            except Apart as apart:  # observing again changes nothing: the board is wider than the screen
                raise StepFailure(str(apart)) from None
            except StalePage as churn:
                self._again(attempt, churn)
                self.page = observe()  # `_again` looks with plain eyes; drag needs its own
        self._acted(n, step, record, source["element"]["label"], before)

    def _capture_secret_step(self, n, step, record):
        """One pick among what can show a value, then the extension reads it and the host writes it
        to the Keychain. The value never reaches ck-browser: the step records its length.

        A key on a reveal-once page is often a plain code block, so the text holders drag offers
        are candidates too; what the page shows of a secret arrives masked.
        """
        field, service = step["arg"]["field"], step["arg"]["keychain_service"]
        before = self.page["fingerprint"]

        def observe():
            return self.driver.observe(drop_targets=True)

        def wide():
            return self.driver.observe_full_page(drop_targets=True)

        def holders():
            return [c for c in _candidates(self.page, None) if c["element"]["role"] in VALUE_ROLES]

        self.page = observe()
        chosen = self._choose(n, step, record, field, produce=holders, noun="value-holding",
                              rules=Q.TARGET_CAPTURE, observe=observe, wide=wide)
        stored = self.driver.capture_secret(chosen["action"]["node"], service)
        record["secret"] = {"service": service, "length": stored["length"], "stored": stored["stored"]}
        self._acted(n, step, record, chosen["element"]["label"], before)

    def _again(self, attempt, churn):
        """A decision the page outran: observe and decide again, or say which way it failed."""
        if attempt == 2:
            raise StepFailure("target covered by another element" if isinstance(churn, Covered)
                              else "page kept changing")
        self.page = self.driver.observe()

    def _near_misses(self, step):
        """Best effort: what on the page came nearest the step's wording, and why it was not offered.
        Evidence only — it must never replace the failure it explains."""
        try:
            return self.driver.near_misses(_wording(step))
        except (RuntimeError, StalePage, OSError, ValueError):
            return []

    def _acted(self, n, step, record, label, before):
        """What every acting step ends with: the page as it is now, and whether it moved."""
        self.history.append({"step": n, "verb": step["verb"], "target": label, "page_changed": None})
        self.page = self.driver.observe()
        record["page_changed"] = self.page["fingerprint"] != before
        self.history[-1]["page_changed"] = record["page_changed"]

    def _expect(self, n, step, record):
        """The text the step names, judged by Jev on the page text alone, retried until it is there.

        The state is deliberately page-only: given the script, the step and the elements too, Jev
        reads the intent and answers yes about text the page never shows. The literal walk stays
        as evidence but decides nothing — it misses text the DOM splits or punctuates differently.
        No `ready` gate either: it guards an action, and an expectation takes none — the verdict
        itself is retried, and a compare view whose thumbnails say "Failed to load" read as
        never-ready while the expected text was right there.
        """
        text = step["arg"]
        wanted = _normalise(text)
        literal = False

        question = {
            "expectation": {
                "type": "choice",
                "criteria": {name: criterion.format(text=text)
                             for name, criterion in Q.EXPECT_CRITERIA.items()},
                "instructions": {"exact_text": text, "rules": Q.EXPECT},
            },
        }

        for attempt in range(self.settle_tries + 1):
            tries = attempt + 1
            self.page = self.driver.observe()
            strings = self.driver.visible_strings()
            literal = wanted in _normalise(strings)
            state = {"page": {**_page_state(self.page), "text": _text_window(strings, text)}}
            answers = self._ask(record, state, question)
            answer = validate_choice(answers.get("expectation") or {}, Q.EXPECT_CHOICES)
            probabilities = answer["probabilities"]
            record["expectation"] = {"literal": literal,
                                     **{name: float(probabilities[name]) for name in Q.EXPECT_CHOICES}}
            if probabilities["verbatim"] >= self.expect_threshold:
                break
            if attempt < self.settle_tries:
                time.sleep(0.1)
        self.page = self.driver.observe()
        self._capture(record, step)
        expectation = record["expectation"]
        if expectation["verbatim"] < self.expect_threshold:
            raise StepFailure(f"expectation not met: '{text}' (verbatim p={expectation['verbatim']:.2f},"
                              f" similar p={expectation['similar']:.2f},"
                              f" absent p={expectation['absent']:.2f},"
                              f" literal={str(expectation['literal']).lower()} after {tries} tries)")

    def _expect_count(self, n, step, record):
        """A whole-page assertion: observed at full document height, so cards below the fold
        count too, and with no shared ready gate — a hundred match nouls in one request drag
        `ready` below the threshold on a settled grid, so the count itself is what is retried."""
        label, wanted = next(iter(step["arg"].items()))
        for attempt in range(self.settle_tries + 1):
            record["count_tries"] = attempt + 1
            self.page = self.driver.observe_full_page()
            record["scroll_height"] = self.page.get("scroll_height")
            # A click-only candidate (a card div whose listener wraps its "Open X" button) would
            # count one thing twice: what counts is what has a role, as before Chrome's signals.
            candidates = self._wait_for_candidates(record, "countable",
                                                   lambda: _candidates(_with_roles(self.page), None),
                                                   self.driver.observe_full_page)
            elements = [c["element"] for c in candidates]
            answers = self._ask(record, self._state(n, step, label, elements), {
                f"match_{e['index']}": {
                    "type": "noul",
                    "instructions": {"step": step["raw"], "element": f"[{e['index']}] {e['label']}",
                                     "rules": Q.COUNT_MATCH},
                }
                for e in elements
            })
            probabilities = {e["index"]: validate_noul(answers.get(f"match_{e['index']}")) for e in elements}
            groups = _match_groups(elements, probabilities)
            record["groups"] = sorted(groups.values(), key=lambda group: -group["n"])
            matched = [e["label"] for e in elements if groups[(e["role"], e["label"])]["p"] >= 0.5]
            record["count"] = len(matched)
            record["matched"] = matched
            if len(matched) == wanted:
                break
            if attempt < self.settle_tries:
                time.sleep(0.1)
        # Back to the real viewport: the checkpoint screenshot and the next step's
        # freshness guards must not see the stretched page.
        self.page = self.driver.observe()
        self._capture(record, step)
        if record["count"] != wanted:
            raise StepFailure(f"expected {wanted} '{label}', found {record['count']}"
                              f" after {record['count_tries']} tries: {record['matched']}")

    # -- gates --------------------------------------------------------------

    def _wait_for_candidates(self, record, noun, produce, observe, budget=None):
        """An empty page is usually a menu still animating in, not a page without the target.

        Costs no Jev request: there is nothing to decide between until something is visible.
        An empty viewport that holds still on a page that scrolls has its target below the fold:
        after ABSTAIN_TRIES unchanged looks the failure carries what is left of `budget`, so the
        caller looks at the whole page now instead of after the whole wait.
        """
        marker, still = self.page["marker"], 0
        for waited in range(self.settle_tries + 1):
            candidates = produce()
            if candidates:
                record["waited_for_candidates"] += waited
                return candidates
            still = still + 1 if marker == self.page["marker"] else 1
            marker = self.page["marker"]
            if still == ABSTAIN_TRIES and any(a["id"] == "scroll_down" for a in self.page["actions"]):
                record["waited_for_candidates"] += waited
                missed = NoTarget(f"no {noun} elements visible after {waited + 1} tries")
                missed.remaining = (self.settle_tries if budget is None else budget) - waited
                raise missed
            if waited < self.settle_tries:
                time.sleep(0.1)
                self.page = observe()
        record["waited_for_candidates"] += self.settle_tries
        raise NoTarget(f"no {noun} elements visible after {self.settle_tries} tries")

    # -- Jev ---------------------------------------------------------------

    def _settle(self, record, build, step_raw, observe, budget):
        """Ask until the page reports ready; every try is one request.

        `build() -> (state, questions, candidates)` is called again for every try: a page that
        was not ready has moved on, so the next question describes what is on it now. The failure
        carries the tries `budget` still had, so a caller that looked wider can come back.
        """
        state, questions, candidates = build()
        marker, missing = self.page["marker"], 0
        for attempt in range(budget + 1):
            asked = {"ready": {"type": "noul",
                               "instructions": {"step": step_raw, "rules": Q.READY}},
                     **questions}
            answers = self._ask(record, state, asked)
            ready = validate_noul(answers.get("ready"))
            record["ready"].append(ready)
            if ready >= self.ready_threshold:
                return answers, candidates, attempt + 1
            # A page that is not moving will not grow the element later: waiting out the whole
            # budget costs seconds per step, while the target may simply be below the fold.
            missing = missing + 1 if marker == self.page["marker"] and \
                self._abstains(record, answers, candidates) else 0
            if missing == ABSTAIN_TRIES or attempt == budget:
                break
            time.sleep(0.1)
            marker = self.page["marker"]
            self.page = observe()
            state, questions, candidates = build()
        failure = self._never_settled(record, answers, candidates)
        failure.remaining = budget - attempt
        raise failure

    def _abstains(self, record, answers, candidates):
        """The target answer names nothing here: `none`, or no element above the threshold."""
        if not candidates or not answers.get("target"):
            return False
        answer, _by_index, ranked = self._rank(record, answers, candidates)
        return answer["choice"] == Q.NONE or ranked[0][1] < self.threshold

    def _never_settled(self, record, answers, candidates):
        """A page still loading and a page without the element look the same per request;
        the last answer tells them apart: an abstaining target means it was never there."""
        if candidates and answers.get("target"):
            answer, by_index, ranked = self._rank(record, answers, candidates)
            if answer["choice"] == Q.NONE or ranked[0][1] < self.threshold:
                return NoTarget(_no_match(record, by_index, ranked, f" after {len(record['ready'])} tries"))
        return StepFailure(f"never settled (ready p={record['ready'][-1]:.2f})")

    def _ask(self, record, state, questions):
        # Jev is HTTPS, not CDP, so the step deadline is checked here; a request already out can
        # still overrun it by its own timeout.
        if self.step_deadline is not None and time.monotonic() >= self.step_deadline:
            raise StepTimeout("no Jev request after the step deadline")
        result = self.jev.ask(state, questions)
        usage = result.get("usage") or {}
        spend = record["jev"] or {"requests": 0, "latency_ms": 0, "input_tokens": 0, "output_tokens": 0}
        spend["requests"] += 1
        spend["latency_ms"] += result.get("latency_ms") or 0
        spend["input_tokens"] += usage.get("input_tokens") or 0
        spend["output_tokens"] += usage.get("output_tokens") or 0
        record["jev"] = spend
        self.model = result.get("model") or self.model
        return result["answers"]

    def _rank(self, record, answers, candidates):
        """The answer, its candidates by index, and the top 3 real ones — abstain aside."""
        by_index = {c["element"]["index"]: c for c in candidates}
        answer = validate_choice(answers.get("target") or {}, {**by_index, Q.NONE: None})
        probabilities = answer["probabilities"]
        record["none_p"] = probabilities[Q.NONE]
        ranked = sorted(((i, p) for i, p in probabilities.items() if i != Q.NONE), key=lambda kv: -kv[1])[:3]
        record["candidates"] = [{"label": by_index[i]["element"]["label"], "p": p} for i, p in ranked]
        return answer, by_index, ranked

    def _pick(self, record, answers, candidates, suffix=""):
        answer, by_index, ranked = self._rank(record, answers, candidates)
        if answer["choice"] == Q.NONE or ranked[0][1] < self.threshold:
            raise NoTarget(_no_match(record, by_index, ranked, suffix))
        chosen = by_index[answer["choice"]]
        element = chosen["element"]
        record["target"] = {"index": element["index"], "label": element["label"], "role": element["role"],
                            "p": answer["probabilities"][answer["choice"]]}
        return chosen

    def _state(self, n, step, arg_text, elements=None, value=None):
        state = {
            "script": [s["raw"] for s in self.script["steps"]],
            "step_index": n,
            "step": {"verb": step["verb"], "arg_text": str(arg_text), "raw": step["raw"]},
            "page": _page_state(self.page),
            "elements": elements or [],
            "recent_actions": self.history[-5:],
        }
        if value is not None:
            state["step"]["value"] = MASK if is_secret(step["verb"], step["arg"]) else value
        return state

    # -- records ------------------------------------------------------------

    def _record(self, n, step, status="pass"):
        return {
            "n": n, "step": step["raw"], "verb": step["verb"], "status": status, "error": None,
            "target": None, "drag_from": None, "drag_to": None, "drag_mode": None,
            "candidates": [], "none_p": None, "near_misses": [],
            "ready": [], "waited_for_candidates": 0, "fallback": None,
            "jev": None, "page_changed": None, "focused": None,
            "screenshot": None, "failure_screenshot": None, "console_errors": [], "failed_requests": [],
            "observations": None, "ms": 0, "timed_out": False, "frame": None,
        }

    def _capture(self, record, step, fail=False):
        """Screenshot plus the events since the last capture; a failing checkpoint captures twice."""
        name = f"{record['n']:02d}-{_slug(step)}{'-FAIL' if fail else ''}.png"
        path = self.out_dir / name
        try:
            shot = self.driver.screenshot(str(path))
            events = self.driver.drain()
        except (RuntimeError, OSError):  # a dead tab must not hide the real failure
            return
        record["failure_screenshot" if fail else "screenshot"] = shot
        if shot is None:  # the extension does not photograph a page that shows a secret
            record["screenshot_withheld"] = True
        record["console_errors"] += events["console_errors"]
        record["failed_requests"] += events["failed_requests"]

    def _frame(self, record, step):
        """The filmstrip's picture of the step: its checkpoint or failure shot, else a new one named
        as a checkpoint's. Events stay for the next _capture."""
        shot = record["screenshot"] or record["failure_screenshot"]
        if shot is None and not record.get("screenshot_withheld"):
            path = self.out_dir / f"{record['n']:02d}-{_slug(step)}.png"
            try:
                shot = self.driver.screenshot(str(path))
            except (RuntimeError, OSError):  # a dead tab must not hide the real failure
                return
        record["frame"] = shot
        if shot is None:  # the extension does not photograph a page that shows a secret
            record["frame_withheld"] = True

    def _observations(self):
        page = self.page or {}
        return {
            "url": page.get("url"), "title": page.get("title"),
            "element_count": len(page.get("actions") or []),
            "text_head": (page.get("text") or "")[:200],
            "discovery": page.get("discovery"),
        }

    def report(self, ws, error=None, ext_profile=None):
        # A run that died (navigation refused, Chrome or Jev gone) still lists every step.
        for n, step in enumerate(self.script["steps"][len(self.records):], len(self.records) + 1):
            self.records.append(self._record(n, step, status="skipped"))
        totals = {
            "wall_ms": round((time.perf_counter() - self.started) * 1000),
            "steps": len(self.records),
            "passed": sum(1 for r in self.records if r["status"] == "pass"),
            "failed": sum(1 for r in self.records if r["status"] == "fail"),
            "jev_requests": 0, "jev_ms": 0, "input_tokens": 0, "output_tokens": 0,
            "cdp_calls": getattr(self.driver, "calls", 0),
        }
        for record in self.records:
            spend = record["jev"]
            if spend:
                totals["jev_requests"] += spend["requests"]
                totals["jev_ms"] += spend["latency_ms"]
                totals["input_tokens"] += spend["input_tokens"]
                totals["output_tokens"] += spend["output_tokens"]
        totals["cost_usd"] = round(totals["input_tokens"] * COST_PER_INPUT_TOKEN, 6)
        return {
            "script_path": self.script.get("path"),
            "url": self.script.get("url"),
            "started_at": self.started_at,
            "error": None if error is None else str(error),
            "steps": self.records,
            "totals": totals,
            "chrome": {"port": ws_port(ws) if ws else None, "ws": ws},
            "ext_profile": ext_profile,  # the human's Chrome profile a --target ext run drove
            "settings": {"settle_tries": self.settle_tries, "step_timeout": self.step_timeout},
            "model": self.model,
        }


def _normalise(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def _text_window(strings, text, limit=EXPECT_TEXT_CHARS):
    """The page text Jev reads for an expectation, at most `limit` characters.

    A long page is cut, and text past the cut would read as absent even though it is right
    there: when the wanted text occurs past the cut, Jev reads the head plus the part around
    that occurrence instead of the head alone.
    """
    if len(strings) <= limit:
        return strings
    words = text.split()
    found = re.search(r"\s+".join(map(re.escape, words)), strings, re.IGNORECASE) if words else None
    if not found or found.end() <= limit:
        return strings[:limit]
    head, side = limit // 2, limit // 4
    start = max(head, found.start() - side)
    return strings[:head] + "\n…\n" + strings[start:start + limit - head]


def _focus(focus):
    """What run.json keeps of the focused element: what it is and its name."""
    return focus and {key: focus[key] for key in ("tag", "role", "label")}


def _page_state(page):
    return {"url": page["url"], "title": page["title"], "text": page["text"]}


def _criterion(element):
    criterion = {"element": f"[{element['index']}] {element['label']}", "role": element["role"],
                 "current_value": element.get("value", "")}
    for key in ("checked", "expanded", "options", "clickable"):
        if key in element:
            criterion[key] = element[key]
    return criterion


def _with_roles(page):
    """The page without the candidates offered only for a click listener or a tabindex."""
    return {**page, "actions": [a for a in page["actions"] if a.get("role") != "clickable"]}


def _candidates(page, kind):
    """One candidate per DOM node: {"element": <for Jev>, "action": <to execute>, "options": [...]}."""
    chosen = {}
    for action in page["actions"]:
        if action.get("node") is None or (kind and action["kind"] != kind):
            continue
        node = action["node"]
        if action["kind"] == "select":
            candidate = chosen.get(node)
            if candidate is None:
                candidate = chosen[node] = {"action": action, "options": [],
                                            "element": _element(action, len(chosen) + 1)}
                candidate["element"]["options"] = candidate["options"]
            candidate["options"].append({
                "label": action["label"].split(" → ")[-1], "value": action["value"], "action": action,
            })
        elif node not in chosen:
            chosen[node] = {"action": action, "options": None, "element": _element(action, len(chosen) + 1)}
    return list(chosen.values())


def _target_question(step, candidates, rules):
    """The forced choice between the offered elements — plus the option that none of them is it.

    An abstain option matters: a forced choice picks the best of what is offered, even when the
    step names something that is not on the page at all.
    """
    return {
        "type": "choice",
        "criteria": {**{c["element"]["index"]: _criterion(c["element"]) for c in candidates},
                     Q.NONE: Q.NONE_CRITERION},
        "instructions": {"step": step["raw"], "rules": rules},
    }


def _relocate(node, candidates):
    """The same element on a page that has scrolled: every index moved, the node id did not."""
    for candidate in candidates:
        if candidate["action"] is not None and candidate["action"]["node"] == node:
            return candidate
    raise StalePage("Target is gone after scrolling to it. Observe again.")


def _element(action, index):
    element = {"index": str(index), "role": action["role"], "label": action["label"].split(" → ")[0],
               "value": action.get("current_value", action.get("value", ""))}
    for key in ("checked", "expanded"):
        if key in action:
            element[key] = action[key]
    if action.get("pointer"):  # a text holder under a pointer cursor: likelier a control
        element["clickable"] = True
    return element


def _no_match(record, by_index, ranked, suffix=""):
    listed = ", ".join(f'[{i}] "{by_index[i]["element"]["label"]}" p={p:.2f}' for i, p in ranked)
    return f"no matching element{suffix}: best {listed}, none p={record['none_p']:.2f}"


def _match_groups(elements, probabilities):
    """One verdict per (role, label): the group's best probability decides for all of it.

    Jev answers every match noul on its own and can disagree about elements that are
    indistinguishable on the page — six identical buttons are six or none, never three.
    """
    groups = {}
    for element in elements:
        key = (element["role"], element["label"])
        group = groups.setdefault(key, {"label": element["label"], "role": element["role"], "n": 0, "p": 0.0})
        group["n"] += 1
        group["p"] = max(group["p"], probabilities[element["index"]])
    return groups


def _select_option(candidate, value):
    """Resolve the requested option in code — the model never writes a value."""
    options = candidate["options"]
    option = _best_match(options, value, lambda o: (o["label"], o["value"]))
    if option is not None:
        return option["action"]
    raise StepFailure(f"option '{value}' not found; options: {[o['label'] for o in options]}")


def _best_match(items, value, texts):
    """The first item with a text equal to `value` (any case), else the first containing it."""
    wanted = value.lower()
    for match in (lambda text: text == wanted, lambda text: wanted in text):
        for item in items:
            if any(match(text.lower()) for text in texts(item)):
                return item
    return None


def _wording(step):
    """What a failed step was looking for, as page words: the field, never a fill value."""
    verb, arg = step["verb"], step["arg"]
    if verb == "select":
        return f"{arg['field']} {arg['value']}"
    if verb == "drag":
        return f"{arg['from']} {arg['to']}"
    return arg["field"] if isinstance(arg, dict) else str(arg)


def _slug(step):
    arg = step["arg"]
    if isinstance(arg, dict):
        arg = arg.get("field") or arg.get("from") or next(iter(arg), "")
    text = re.sub(r"[^a-z0-9]+", "-", f"{step['verb']} {arg}".lower()).strip("-")
    return text[:40].strip("-")


def _video(recorder):
    """run.json's `video`: what the screencast made, never an error that costs the run its report."""
    if recorder is None:
        return result(reason=NO_SCREENCAST)
    try:
        return recorder.finish()
    except OSError as exc:
        return result(reason=f"video: {exc}")


def _close(close, log):
    """Closing a tab, a socket or an HTTPS connection must not cost us the run report."""
    if close is None:
        return
    try:
        close()
    except (RuntimeError, OSError, ValueError) as exc:
        log(f"ck-browser: cleanup: {exc}")

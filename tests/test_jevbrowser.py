"""ck-browser: script parsing, the decision loop with a fake driver, and a dedicated Chrome run."""

import base64
import contextlib
import functools
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ck_browser import __main__ as cli
from ck_browser import cdp as cdp_module
from ck_browser import questions as Q
from ck_browser import runner as runner_module
from ck_browser import signals
from ck_browser.cdp import (Cdp, CdpError, PageCrashed, StalePage, StepTimeout, Tab, chrome_binary,
                                      ensure_chrome)
from ck_browser.model import Jev, RecordingJev, StubJev
from ck_browser.page import (DROP_SEEN_JS, FRESH_JS, PRESS_SEEN_JS, TARGET_JS, Apart, Covered, Unreached, act,
                                       drag, fingerprint, observe, upload)
from ck_browser.report import format_report
from ck_browser.runner import ABSTAIN_TRIES, Driver, StepFailure, _select_option, run
from ck_browser.script import MASK, ScriptError, load_script

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jevbrowser"
# Our own Chrome on its own profile dir, never the human's — a websocket there pops up
# "Allow remote debugging?".
PROFILE_DIR = os.environ.get("CK_BROWSER_PROFILE_DIR")
PASSWORD = "s3cr3t-kurta-pw"


# -- helpers ---------------------------------------------------------------


def action(node, kind, label, **extra):
    return {"id": f"e{node}", "node": node, "kind": kind, "role": extra.pop("role", "button"),
            "label": label, "value": extra.pop("value", ""),
            "rect": extra.pop("rect", {"x": 10, "y": 10, "w": 120, "h": 30}), **extra}


def make_page(actions, text="Products", url="http://fake/", title="Products"):
    page = {"url": url, "title": title, "text": text, "actions": actions, "marker": ["m", text],
            "page_key": ["k"], "guards": {}, "scroll": {"y": 0, "height": 800}, "w": 1280, "h": 800}
    page["fingerprint"] = fingerprint(page)
    return page


class FakeDriver:
    """Canned pages instead of Chrome; records what the loop tried to do.

    Full-page observations come from `full_pages` when given, else from the same queue; looks
    asking for text holders come from `holder_pages` (both reaches share it), else peek `pages`;
    `strings` stands in for innerText plus aria-label/placeholder/title/alt.
    """

    calls = 7

    def __init__(self, pages, drains=None, full_pages=None, strings=None, files=(), panels=(),
                 panel_pages=0, holder_pages=None):
        self.pages = list(pages)
        self.holder_pages = list(holder_pages or [])
        self.holder_looks = []
        self.misses = []
        self.near_miss_queries = []
        self.panels = list(panels)
        self.panel_pages = panel_pages
        self.full_pages = list(full_pages) if full_pages else self.pages
        self.strings = strings
        self.drains = list(drains or [])
        self.files = list(files)
        self.acted = []
        self.full_page_observations = 0
        self.drop_target_observations = 0
        self.closed = False

    def navigate(self, url):
        self.acted.append(("navigate", url))

    def observe(self, drop_targets=False, text_holders=False):
        self.drop_target_observations += drop_targets
        if text_holders:
            return self._holders("viewport")
        return self.pages[0] if len(self.pages) == 1 else self.pages.pop(0)

    def observe_full_page(self, drop_targets=False, text_holders=False):
        self.drop_target_observations += drop_targets
        if text_holders:
            return self._holders("full")
        self.full_page_observations += 1
        return self.full_pages[0] if len(self.full_pages) == 1 else self.full_pages.pop(0)

    def _holders(self, reach):
        self.holder_looks.append(reach)
        if not self.holder_pages:  # a peek: the queue the other looks walk stays as it was
            return self.pages[0]
        return self.holder_pages[0] if len(self.holder_pages) == 1 else self.holder_pages.pop(0)

    def near_misses(self, text):
        self.near_miss_queries.append(text)
        return list(self.misses)

    def visible_strings(self):
        return self.pages[0]["text"] if self.strings is None else self.strings

    def act(self, action, page, text=None):
        detail = text if text is not None else action.get("value", action.get("delta"))
        self.acted.append((action["kind"], action.get("label"), detail))
        return action

    def file_inputs(self):
        return list(self.files)

    def upload(self, node, path, page):
        self.acted.append(("upload", node, path))

    def drag(self, source, target, page):
        self.acted.append(("drag", source["label"], target["label"]))
        return "pointer"

    def capture_secret(self, node, service):
        self.acted.append(("capture_secret", node, service))
        return {"service": service, "length": 64, "stored": True}

    def focused(self):
        return None

    def scroll_into_view(self, node):
        self.acted.append(("scroll_into_view", node, None))

    def inner_scrollers(self):
        return list(self.panels)

    def scroll_inner(self, node, to=None):
        self.acted.append(("scroll_inner", node, to))
        if to is not None:
            return True
        self.panel_pages -= 1
        return self.panel_pages >= 0

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")
        return path

    def drain(self):
        return self.drains.pop(0) if self.drains else {"console_errors": [], "failed_requests": []}

    def deadline(self, at):
        pass  # canned pages never wait

    def waiting_on(self):
        return None

    def unstick(self):
        return None

    def close(self):
        self.closed = True


class ScriptedJev:
    """Fixed answers by question name, so gates can be exercised without a model."""

    model = "scripted"

    def __init__(self, **answers):
        self.answers = answers
        self.requests = 0
        self.asked = []

    def ask(self, state, questions):
        self.requests += 1
        self.asked.append((state, questions))
        answers = {}
        for name, question in questions.items():
            value = self.answers.get(name, self.answers.get("_default"))
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.9 if value is None else value}
            else:
                ids = list(question["criteria"])
                probabilities = value or {key: 1 / len(ids) for key in ids}
                choice = max(probabilities, key=probabilities.get)
                answers[name] = {"type": "choice", "choice": choice,
                                 "probabilities": probabilities, "confidence": probabilities[choice]}
        return {"answers": answers, "usage": {"input_tokens": 10, "output_tokens": 1},
                "model": self.model, "latency_ms": 3}


class ExactJev(StubJev):
    """Picks the element whose label is exactly the step's wording (any case), else abstains;
    always ready. StubJev's substring scoring picks "Cancel" for "Choose an option"."""

    model = "exact"

    def ask(self, state, questions):
        result = super().ask(state, questions)
        wanted = str((state.get("step") or {}).get("arg_text", "")).lower()
        for name, question in questions.items():
            if name == "ready":
                result["answers"][name] = {"type": "noul", "noul": 0.9}
            elif name == "target":
                criteria = question["criteria"]
                hit = next((key for key, criterion in criteria.items() if key != Q.NONE
                            and criterion["element"].split("] ", 1)[1].lower() == wanted), Q.NONE)
                probabilities = {key: float(key == hit) for key in criteria}
                result["answers"][name] = {"type": "choice", "choice": hit,
                                           "probabilities": probabilities, "confidence": 1.0}
        return result


class SettlingJev:
    """Not ready on the first request, ready after; picks the criterion that names `wanted`."""

    model = "settling"

    def __init__(self, wanted):
        self.wanted = wanted
        self.requests = 0
        self.offered = []

    def ask(self, state, questions):
        self.requests += 1
        criteria = questions["target"]["criteria"]
        self.offered.append([c["element"] for key, c in criteria.items() if key != "none"])
        choice = next((key for key, criterion in criteria.items()
                       if key != "none" and self.wanted in criterion["element"]), "none")
        probabilities = {key: 1.0 if key == choice else 0.0 for key in criteria}
        return {
            "answers": {
                "ready": {"type": "noul", "noul": 0.3 if self.requests == 1 else 0.9},
                "target": {"type": "choice", "choice": choice, "probabilities": probabilities,
                           "confidence": 1.0},
            },
            "usage": {"input_tokens": 10, "output_tokens": 1}, "model": self.model, "latency_ms": 0,
        }


class LateJev:
    """Abstains on the first target question, then picks the criterion naming `wanted`.

    What a viewport pass that missed the target looks like: only the second, whole-page
    question can be answered.
    """

    model = "late"

    def __init__(self, wanted):
        self.wanted = wanted
        self.requests = 0

    def ask(self, state, questions):
        self.requests += 1
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.9}
                continue
            criteria = question["criteria"]
            choice = "none" if self.requests == 1 else next(
                (key for key, criterion in criteria.items()
                 if key != "none" and self.wanted in criterion["element"]), "none")
            answers[name] = {"type": "choice", "choice": choice, "confidence": 1.0,
                             "probabilities": {key: 1.0 if key == choice else 0.0 for key in criteria}}
        return {"answers": answers, "usage": {"input_tokens": 5, "output_tokens": 1},
                "model": self.model, "latency_ms": 0}


class LoadingJev:
    """Not ready while nothing on the page matches; ready and certain once `wanted` is offered.

    What a page that is still rendering looks like, and what one whose target is merely below
    the fold looks like — the two are the same per request.
    """

    model = "loading"

    def __init__(self, wanted):
        self.wanted = wanted
        self.requests = 0
        self.wide_requests = 0

    def ask(self, state, questions):
        self.requests += 1
        self.wide_requests += "ready" not in questions
        criteria = questions["target"]["criteria"]
        choice = next((key for key, criterion in criteria.items()
                       if key != "none" and self.wanted in criterion["element"]), "none")
        answers = {"target": {"type": "choice", "choice": choice, "confidence": 1.0,
                              "probabilities": {key: 1.0 if key == choice else 0.0
                                                for key in criteria}}}
        if "ready" in questions:
            answers["ready"] = {"type": "noul", "noul": 0.1 if choice == "none" else 0.9}
        return {"answers": answers, "usage": {"input_tokens": 5, "output_tokens": 1},
                "model": self.model, "latency_ms": 0}


class SettledJev(LoadingJev):
    """Always calls the page ready, target or not — what a form looks like while the
    suggestion list a fill asked for is still on its way from the server."""

    def ask(self, state, questions):
        result = super().ask(state, questions)
        if "ready" in questions:
            result["answers"]["ready"]["noul"] = 0.9
        return result


def script_of(*steps, url=None):
    return {"url": url, "path": "/tmp/test.yaml", "steps": [_parse_step(s) for s in steps]}


def _parse_step(text):
    return load_script(_write_json({"steps": [text]}))["steps"][0]


def _write_json(document):
    path = Path(tempfile.mkdtemp(prefix="ck-browser-")) / "s.json"
    path.write_text(json.dumps(document))
    return path


# -- parsing ---------------------------------------------------------------


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ck-browser-script-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def load(self, text):
        path = self.dir / "steps.yaml"
        path.write_text(text)
        return load_script(path)

    def test_leading_zeros_stay_text_and_plain_numbers_stay_numbers(self):
        script = self.load(
            'steps:\n'
            '  - fill: { field: "Phone", value: 0987654321 }\n'
            '  - fill: { field: "PIN", value: 007 }\n'
            '  - wait: 500\n'
            '  - expect_count: { "product card": 0 }\n')
        self.assertEqual([s["arg"] for s in script["steps"]],
                         [{"field": "Phone", "value": "0987654321"}, {"field": "PIN", "value": "007"},
                          500, {"product card": 0}])

    def test_every_documented_form(self):
        script = self.load(
            'url: http://localhost:3000/dashboard      # top-level scalar\n'
            'steps:\n'
            '  - click: "Upload product"               # scalar\n'
            '  - fill: { field: "Product name", value: "Test kurta" }\n'
            '  - select: { field: Category, value: Kurta }\n'
            '  - upload: { field: "Browse images", path: /tmp/shot.png }\n'
            '  - hover: "Regenerate"\n'
            '  - drag: { from: "View A shot", to: "View B shot" }\n'
            '  - fill:\n'
            '      field: Password\n'
            '      value: test@123\n'
            '  - expect: "Saved"\n'
            '  - expect_count: { "product card": 3 }\n'
            '  - scroll: down\n'
            '  - press: Enter\n'
            '  - press: Shift+Tab\n'
            '  - wait: 500\n'
            '  - checkpoint: after-save\n'
            '  - open: http://localhost:3000/results\n'
        )
        self.assertEqual(script["url"], "http://localhost:3000/dashboard")
        self.assertEqual(
            [(s["verb"], s["arg"]) for s in script["steps"]],
            [
                ("click", "Upload product"),
                ("fill", {"field": "Product name", "value": "Test kurta"}),
                ("select", {"field": "Category", "value": "Kurta"}),
                ("upload", {"field": "Browse images", "path": "/tmp/shot.png"}),
                ("hover", "Regenerate"),
                ("drag", {"from": "View A shot", "to": "View B shot"}),
                ("fill", {"field": "Password", "value": "test@123"}),
                ("expect", "Saved"),
                ("expect_count", {"product card": 3}),
                ("scroll", "down"),
                ("press", "Enter"),
                ("press", "Shift+Tab"),
                ("wait", 500),
                ("checkpoint", "after-save"),
                ("open", "http://localhost:3000/results"),
            ],
        )
        self.assertEqual(script["steps"][1]["raw"], "fill: {field: Product name, value: Test kurta}")
        self.assertEqual(script["steps"][0]["raw"], "click: Upload product")
        self.assertEqual(script["steps"][3]["raw"], "upload: {field: Browse images, path: /tmp/shot.png}")
        self.assertEqual(script["steps"][5]["raw"], "drag: {from: View A shot, to: View B shot}")
        self.assertEqual(script["steps"][6]["raw"], "fill: {field: Password, value: ••••}")

    def test_hash_inside_quotes_is_kept(self):
        script = self.load('steps:\n  - expect: "Order #12 saved"  # trailing comment\n')
        self.assertEqual(script["steps"][0]["arg"], "Order #12 saved")
        self.assertIsNone(script["url"])

    def test_a_hash_inside_a_word_is_not_a_comment(self):
        """As in YAML, only `#` at the line start or after whitespace opens a comment."""
        script = self.load(
            'url: http://x/app#/board\n'
            'steps:\n'
            '  - expect: "Order #12 saved"    # trailing comment\n'
            '  - fill: { field: Note, value: p#ss }\n'
            '  # a whole-line comment\n'
        )
        self.assertEqual(script["url"], "http://x/app#/board")
        self.assertEqual([s["arg"] for s in script["steps"]],
                         ["Order #12 saved", {"field": "Note", "value": "p#ss"}])

    def test_every_secret_sounding_field_is_masked(self):
        for field in ("Password", "Passcode", "Passphrase", "Secret key", "API token", "PIN", "OTP"):
            with self.subTest(field=field):
                step = _parse_step({"fill": {"field": field, "value": "1234"}})
                self.assertEqual(step["raw"], f"fill: {{field: {field}, value: {MASK}}}")
        plain = _parse_step({"fill": {"field": "Pinterest board", "value": "Kurtas"}})
        self.assertEqual(plain["raw"], "fill: {field: Pinterest board, value: Kurtas}")

    def test_capture_secret_names_a_field_and_a_keychain_service(self):
        script = self.load('steps:\n  - capture_secret: { field: "Secret Access Key",'
                           ' keychain_service: backup-r2-secret-access-key }\n')
        [step] = script["steps"]
        self.assertEqual(step["arg"], {"field": "Secret Access Key",
                                       "keychain_service": "backup-r2-secret-access-key"})
        self.assertEqual(step["raw"],
                         "capture_secret: {field: Secret Access Key, keychain_service: backup-r2-secret-access-key}")
        with self.assertRaises(ScriptError):
            self.load(f'steps:\n  - capture_secret: {{ field: Key, keychain_service: {"s" * 129} }}\n')

    def test_json_script(self):
        path = self.dir / "steps.json"
        path.write_text(json.dumps({"url": "http://x/", "steps": [{"click": "Go"}]}))
        script = load_script(path)
        self.assertEqual(script["url"], "http://x/")
        self.assertEqual(script["steps"][0]["verb"], "click")

    def test_errors(self):
        cases = {
            'steps:\n  - fill: { field: Name }\n': "step 1: fill needs field and value",
            'steps:\n  - click: Go\n  - select: { value: Kurta }\n': "step 2: select needs field and value",
            'steps:\n  - scroll: sideways\n': "step 1: scroll is up or down, got 'sideways'",
            'steps:\n  - wait: soon\n': "step 1: wait needs milliseconds, got 'soon'",
            'steps:\n  - expect_count: { cards: many }\n': "step 1: expect_count needs a whole-number count",
            'steps:\n  - expect_count: { a: 1, b: 2 }\n': "step 1: expect_count needs one `label: count` pair",
            'steps:\n  - upload: { field: Browse }\n': "step 1: upload needs field and path",
            'steps:\n  - upload: { path: /tmp/a.png }\n': "step 1: upload needs field and path",
            'steps:\n  - drag: { from: "View A shot" }\n': "step 1: drag needs from and to",
            'steps:\n  - capture_secret: { field: Key }\n':
                "step 1: capture_secret needs field and keychain_service",
            'steps:\n  - capture_secret: { field: "", keychain_service: svc }\n':
                "step 1: capture_secret needs field and keychain_service",
            'steps:\n  - capture_secret: { field: Key, keychain_service: "my key" }\n':
                "step 1: keychain_service 'my key' is not a service name",
            'steps:\n  - capture_secret: { field: Key, keychain_service: -svc }\n':
                "step 1: keychain_service '-svc' is not a service name",
            'steps:\n  - hover: ""\n': "step 1: hover needs a value",
            'steps:\n  - swipe: Go\n': "step 1: unknown verb 'swipe'",
            'steps:\n  - click: ""\n': "step 1: click needs a value",
            'steps:\n  - "just text"\n': "expected a `- verb: value` step",
            'title: x\nsteps:\n  - click: Go\n': "expected `url:` or `steps:`",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(ScriptError) as caught:
                    self.load(text)
                self.assertIn(message, str(caught.exception))


# -- the loop with a fake driver -------------------------------------------


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="ck-browser-out-"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def run_script(self, script, pages, jev, **kwargs):
        driver = FakeDriver(pages)
        report = run(script, self.out, jev=jev, driver=driver, log=lambda _line: None, **kwargs)
        return report, driver

    def test_click_picks_the_matching_label_and_writes_run_json(self):
        page = make_page([action(1, "click", "Upload product"), action(2, "click", "Cancel")])
        report, driver = self.run_script(script_of({"click": "Upload product"}), [page], StubJev())
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["target"]["label"], "Upload product")
        self.assertEqual(driver.acted, [("click", "Upload product", "")])
        self.assertEqual(step["jev"]["requests"], 1)
        self.assertEqual(report["totals"], {**report["totals"], "passed": 1, "failed": 0, "cdp_calls": 7})
        written = json.loads((self.out / "run.json").read_text())
        self.assertEqual(written["steps"][0]["target"]["label"], "Upload product")

    def test_a_label_that_is_not_on_the_page_is_not_forced_onto_an_element(self):
        page = make_page([action(1, "click", "Upload product"), action(2, "click", "Cancel")])
        report, driver = self.run_script(script_of({"click": "Flat Lay Shoot"}), [page], StubJev())
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(step["error"], 'no matching element on the whole page:'
                                        ' best [1] "Upload product" p=0.00,'
                                        ' [2] "Cancel" p=0.00, none p=1.00')
        self.assertEqual(step["none_p"], 1.0)
        self.assertEqual(driver.acted, [])

    def test_ambiguous_target_fails_with_the_top_candidates(self):
        page = make_page([action(1, "click", "Category"), action(2, "click", "Sub-category")])
        jev = ScriptedJev(target={"1": 0.52, "2": 0.48, "none": 0.0})
        report, driver = self.run_script(script_of({"click": "Category"}, {"expect": "Saved"}), [page], jev)
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(step["error"], 'no matching element on the whole page:'
                                        ' best [1] "Category" p=0.52,'
                                        ' [2] "Sub-category" p=0.48, none p=0.00')
        self.assertEqual(step["none_p"], 0.0)
        self.assertEqual([c["label"] for c in step["candidates"]], ["Category", "Sub-category"])
        self.assertEqual(driver.acted, [])
        self.assertTrue(step["failure_screenshot"].endswith("01-click-category-FAIL.png"))
        self.assertTrue(Path(step["failure_screenshot"]).exists())
        self.assertEqual(report["steps"][1]["status"], "skipped")

    def test_no_candidates_costs_no_jev_request(self):
        jev = ScriptedJev()
        report, _ = self.run_script(script_of({"fill": {"field": "Name", "value": "x"}}),
                                    [make_page([action(1, "click", "Upload product")])], jev,
                                    settle_tries=2)
        step = report["steps"][0]
        self.assertEqual(step["error"], "no fillable elements visible after 2 tries")
        self.assertEqual(step["waited_for_candidates"], 2)
        self.assertEqual(jev.requests, 0)

    def test_a_target_that_is_still_animating_in_is_waited_for(self):
        opening = make_page([], text="")
        menu = make_page([action(1, "click", "Log out"), action(2, "click", "Settings")], text="Log out")
        script = script_of({"click": "Log out"})
        report, driver = self.run_script(script, [opening, opening, menu], StubJev())
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["waited_for_candidates"], 2)
        self.assertEqual(step["target"]["label"], "Log out")
        self.assertEqual(driver.acted, [("click", "Log out", "")])

    def test_a_page_that_never_settles_on_a_missing_element_says_so(self):
        page = make_page([action(1, "click", "Upload product")])
        jev = ScriptedJev(ready=0.3, target={"1": 0.05, "none": 0.95})
        report, driver = self.run_script(script_of({"click": "Flat Lay Shoot"}), [page], jev,
                                         settle_tries=1)
        step = report["steps"][0]
        self.assertEqual(step["error"], 'no matching element on the whole page:'
                                        ' best [1] "Upload product" p=0.05, none p=0.95')
        self.assertEqual(step["none_p"], 0.95)
        self.assertEqual([c["label"] for c in step["candidates"]], ["Upload product"])
        self.assertEqual(driver.acted, [])

    def test_never_settled_fails_after_settle_tries(self):
        """A confident target on a page that never reports ready: the wait is the problem."""
        page = make_page([action(1, "click", "Upload product")])
        jev = ScriptedJev(ready=0.1, target={"1": 1.0, "none": 0.0})
        report, driver = self.run_script(script_of({"click": "Upload product"}), [page], jev, settle_tries=2)
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(step["error"], "never settled (ready p=0.10)")
        self.assertEqual(step["ready"], [0.1, 0.1, 0.1])
        self.assertEqual(step["jev"]["requests"], 3)
        self.assertEqual(driver.acted, [])

    def test_a_not_ready_retry_rebuilds_the_question_from_the_fresh_page(self):
        """The target appears only on the second observation: the retry must offer it."""
        loading = make_page([action(1, "click", "Cancel")], text="Loading")
        menu = make_page([action(1, "click", "Cancel"), action(2, "click", "Log out")], text="Menu")
        jev = SettlingJev("Log out")
        report, driver = self.run_script(script_of({"click": "Log out"}), [loading, menu, menu], jev,
                                         settle_tries=2)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["ready"], [0.3, 0.9])
        self.assertEqual(jev.offered, [["[1] Cancel"], ["[1] Cancel", "[2] Log out"]])
        self.assertEqual(step["target"]["label"], "Log out")
        self.assertEqual(driver.acted, [("click", "Log out", "")])

    def test_select_option_resolution(self):
        options = [{"label": "Kurta", "value": "kurta", "action": {"id": "k"}},
                   {"label": "Saree", "value": "saree", "action": {"id": "s"}}]
        candidate = {"options": options}
        self.assertEqual(_select_option(candidate, "Kurta")["id"], "k")
        self.assertEqual(_select_option(candidate, "saree")["id"], "s")
        self.assertEqual(_select_option(candidate, "kur")["id"], "k")
        with self.assertRaises(StepFailure) as caught:
            _select_option(candidate, "Lehenga")
        self.assertEqual(str(caught.exception), "option 'Lehenga' not found; options: ['Kurta', 'Saree']")

    def test_select_step_executes_the_resolved_option(self):
        page = make_page([
            action(1, "select", "Category → Kurta", role="combobox", value="kurta", current_value="Choose one"),
            action(1, "select", "Category → Saree", role="combobox", value="saree", current_value="Choose one"),
        ])
        script = script_of({"select": {"field": "Category", "value": "Saree"}})
        report, driver = self.run_script(script, [page], StubJev())
        self.assertEqual(report["steps"][0]["status"], "pass")
        self.assertEqual(driver.acted, [("select", "Category → Saree", "saree")])

    def test_select_step_reports_a_missing_option(self):
        page = make_page([action(1, "select", "Category → Kurta", role="combobox", value="kurta")])
        script = script_of({"select": {"field": "Category", "value": "Lehenga"}})
        report, _ = self.run_script(script, [page], StubJev())
        self.assertEqual(report["steps"][0]["error"], "option 'Lehenga' not found; options: ['Kurta']")

    def run_with(self, script, driver, jev, **kwargs):
        return run(script, self.out, jev=jev, driver=driver, log=lambda _line: None, **kwargs)["steps"]

    def test_click_falls_back_to_a_text_holder_when_the_controls_abstain(self):
        page = make_page([action(1, "click", "Cancel")])
        holders = make_page([action(1, "click", "Cancel"), action(5, "click", "Type", role="text"),
                             action(6, "click", "Choose an option", role="text", pointer=True)])
        driver = FakeDriver([page], holder_pages=[holders])
        jev = ExactJev()
        step = self.run_with(script_of({"click": "Choose an option"}), driver, jev, settle_tries=1)[0]
        self.assertEqual(step["status"], "pass", step["error"])
        self.assertEqual(step["fallback"], "text_holder")
        self.assertEqual((step["target"]["role"], step["target"]["label"]), ("text", "Choose an option"))
        self.assertEqual(driver.acted, [("click", "Choose an option", "")])
        self.assertEqual(driver.holder_looks, ["viewport"])

    def test_a_clickable_text_holder_tells_jev_so(self):
        holders = make_page([action(5, "click", "Type", role="text"),
                             action(6, "click", "Choose an option", role="text", pointer=True)])
        jev = ExactJev()
        driver = FakeDriver([make_page([action(1, "click", "Cancel")])], holder_pages=[holders])
        with mock.patch.object(jev, "ask", wraps=jev.ask) as asked:  # the last look offers every holder
            self.run_with(script_of({"click": "Type"}), driver, jev, settle_tries=0)
        question = asked.call_args.args[1]["target"]
        criteria = question["criteria"]
        self.assertEqual(question["instructions"]["rules"], Q.TARGET_TEXT)
        self.assertTrue(criteria["2"]["clickable"])
        self.assertNotIn("clickable", criteria["1"])

    def holder_asks(self, jev, script, driver, settle_tries):
        """The step, and the text-holder questions in the order Jev was asked them."""
        with mock.patch.object(jev, "ask", wraps=jev.ask) as asked:
            step = self.run_with(script, driver, jev, settle_tries=settle_tries)[0]
        targets = [call.args[1]["target"] for call in asked.call_args_list if "target" in call.args[1]]
        return step, [t for t in targets if t["instructions"]["rules"] == Q.TARGET_TEXT]

    def test_a_heading_with_the_label_is_offered_only_once_the_budget_is_spent(self):
        """A heading that repeats a late control's label must not win the race to it."""
        page = make_page([action(1, "click", "Cancel")])
        holders = make_page([action(1, "click", "Cancel"), action(5, "click", "Save", role="text"),
                             action(6, "click", "Draft", role="text", pointer=True)])
        driver = FakeDriver([page], holder_pages=[holders])
        step, asks = self.holder_asks(ExactJev(), script_of({"click": "Save"}), driver, settle_tries=2)
        self.assertEqual(step["status"], "pass", step["error"])
        self.assertEqual(step["fallback"], "text_holder")
        self.assertEqual((step["target"]["role"], step["target"]["label"]), ("text", "Save"))
        offered = [[c["element"].split("] ", 1)[1] for c in a["criteria"].values() if isinstance(c, dict)]
                   for a in asks]
        self.assertEqual(offered, [["Draft"], ["Draft"], ["Save", "Draft"]])  # viewport, page; then all

    def test_an_early_look_without_a_clickable_holder_asks_jev_nothing(self):
        page = make_page([action(1, "click", "Cancel")])
        holders = make_page([action(1, "click", "Cancel"), action(5, "click", "Save", role="text")])
        driver = FakeDriver([page], holder_pages=[holders])
        step, asks = self.holder_asks(ExactJev(), script_of({"click": "Save"}), driver, settle_tries=2)
        self.assertEqual(step["status"], "pass", step["error"])
        self.assertEqual(step["fallback"], "text_holder")
        self.assertEqual(len(asks), 1)  # the last-resort look alone
        self.assertEqual(driver.holder_looks, ["viewport", "full", "viewport"])

    def test_a_target_matched_in_the_viewport_asks_for_no_text_holder_look(self):
        driver = FakeDriver([make_page([action(1, "click", "Upload product")])])
        step = self.run_with(script_of({"click": "Upload product"}), driver, StubJev())[0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(driver.holder_looks, [])
        self.assertEqual(driver.near_miss_queries, [])

    def test_a_label_that_is_nowhere_fails_with_the_semantic_verdict_and_its_near_misses(self):
        page = make_page([action(1, "click", "Upload product"), action(2, "click", "Cancel")])
        holders = make_page([action(7, "click", "Products", role="text")])
        driver = FakeDriver([page], holder_pages=[holders])
        driver.misses = [
            {"tag": "span", "role": None, "text": "1/2 Hour", "shadow_host": None, "visible": True,
             "hit": False, "offered": False, "why": 'covered by <select.over "1/2 Hour">'},
            {"tag": "div", "role": "option", "text": "Half hour", "shadow_host": "dns-ttl", "visible": True,
             "hit": True, "offered": True, "why": None},
        ]
        report = run(script_of({"click": "1/2 Hour"}), self.out, jev=ExactJev(), driver=driver,
                     log=lambda _line: None, settle_tries=1)
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(step["error"], 'no matching element on the whole page:'
                                        ' best [1] "Upload product" p=0.00, [2] "Cancel" p=0.00, none p=1.00')
        self.assertEqual([c["label"] for c in step["candidates"]], ["Upload product", "Cancel"])
        self.assertEqual(step["none_p"], 1.0)
        self.assertEqual(driver.holder_looks, ["viewport", "full", "viewport", "full"])
        self.assertEqual(driver.near_miss_queries, ["1/2 Hour"])
        self.assertEqual(step["near_misses"], driver.misses)
        written = json.loads((self.out / "run.json").read_text())
        self.assertEqual(written["steps"][0]["near_misses"], driver.misses)
        text = format_report(report)
        self.assertIn('   near miss: <span> "1/2 Hour" — covered by <select.over "1/2 Hour">', text)
        self.assertIn('   near miss: <div role=option> "Half hour" in dns-ttl shadow root — offered', text)

    def test_a_fill_failure_asks_for_near_misses_by_field_never_by_value(self):
        driver = FakeDriver([make_page([action(1, "click", "Upload product")])])
        script = script_of({"fill": {"field": "Name", "value": "hunter2"}})
        step = self.run_with(script, driver, ScriptedJev(), settle_tries=1)[0]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(driver.near_miss_queries, ["Name"])

    def test_near_misses_never_mask_the_failure(self):
        class DeadDriver(FakeDriver):
            def near_misses(self, text):
                raise RuntimeError("tab gone")

        driver = DeadDriver([make_page([action(1, "click", "Upload product")])])
        step = self.run_with(script_of({"click": "Flat Lay Shoot"}), driver, StubJev(), settle_tries=0)[0]
        self.assertTrue(step["error"].startswith("no matching element"), step["error"])
        self.assertEqual(step["near_misses"], [])

    def test_select_without_a_native_select_opens_the_dropdown_and_clicks_the_new_option(self):
        page = make_page([action(1, "click", "Cancel")])
        closed = make_page([action(1, "click", "Cancel"), action(25, "click", "A", role="text"),
                            action(12, "click", "Choose an option", role="text", pointer=True)])
        opened = make_page([action(1, "click", "Cancel"), action(25, "click", "A", role="text"),
                            action(12, "click", "Choose an option", role="text", pointer=True),
                            action(41, "click", "AAAA", role="text"), action(40, "click", "A", role="text")],
                           text="open")
        driver = FakeDriver([page], holder_pages=[closed, opened])
        script = script_of({"select": {"field": "Choose an option", "value": "A"}})
        step = self.run_with(script, driver, ExactJev(), settle_tries=1)[0]
        self.assertEqual(step["status"], "pass", step["error"])
        self.assertEqual(step["fallback"], "custom_select")
        self.assertEqual(step["option"], {"label": "A", "role": "text"})
        self.assertEqual(step["target"]["label"], "Choose an option")
        self.assertEqual([kind for kind, *_ in driver.acted], ["click", "click"])
        self.assertEqual(driver.acted[0][1], "Choose an option")
        self.assertEqual(driver.acted[1][1], "A")
        clicked = [a for a in opened["actions"] if a["label"] == "A"]
        self.assertEqual(clicked[1]["node"], 40)  # the second "A" is the new one

    def test_a_custom_select_whose_option_never_appears_says_what_the_click_added(self):
        page = make_page([action(1, "click", "Cancel")])
        closed = make_page([action(12, "click", "Choose an option", role="text", pointer=True)])
        opened = make_page([action(12, "click", "Choose an option", role="text", pointer=True),
                            action(40, "click", "MX", role="text"), action(41, "click", "TXT", role="text")],
                           text="open")
        driver = FakeDriver([page], holder_pages=[closed, opened])
        script = script_of({"select": {"field": "Choose an option", "value": "CNAME"}})
        step = self.run_with(script, driver, ExactJev(), settle_tries=1)[0]
        self.assertEqual(step["error"], "option 'CNAME' not found after opening 'Choose an option';"
                                        " the click added: ['MX', 'TXT']")
        self.assertEqual(driver.near_miss_queries, ["Choose an option CNAME"])

    def test_a_press_chord_reaches_the_browser_as_written(self):
        driver = FakeDriver([make_page([action(1, "click", "Upload product")])])
        with mock.patch.object(driver, "act", wraps=driver.act) as acted:
            step = self.run_with(script_of({"press": "Shift+Tab"}), driver, StubJev())[0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(acted.call_args.args[0]["key"], "Shift+Tab")

    def test_hover_moves_to_the_element_it_picked(self):
        page = make_page([action(1, "click", "Regenerate"), action(2, "click", "Delete")])
        report, driver = self.run_script(script_of({"hover": "Regenerate"}), [page], StubJev())
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["target"]["label"], "Regenerate")
        self.assertEqual(driver.acted, [("hover", "Regenerate", "")])

    def test_upload_sets_the_file_on_the_input_the_chosen_button_opens(self):
        """The picker is a hidden input: the step names the button, the file goes into the input."""
        shot = self.out / "shot.png"
        shot.write_bytes(b"\x89PNG")
        page = make_page([action(3, "click", "Browse images"), action(4, "click", "Save")])
        files = [{"node": 10, "accept": "image/*", "multiple": False, "trigger": 3,
                  "label": "Browse images"}]
        jev = ScriptedJev(target={"1": 1.0, "2": 0.0, "none": 0.0})
        driver = FakeDriver([page], files=files)
        report = run(script_of({"upload": {"field": "Browse images", "path": str(shot)}}), self.out,
                     jev=jev, driver=driver, log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["target"]["label"], "Browse images")
        self.assertEqual(driver.acted, [("upload", 10, str(shot.resolve()))])
        # the input is offered as the button that opens it, never as a second candidate
        state, _questions = jev.asked[0]
        self.assertEqual([e["label"] for e in state["elements"]], ["Browse images", "Save"])

    def test_an_input_no_visible_control_opens_is_offered_on_its_own(self):
        shot = self.out / "shot.png"
        shot.write_bytes(b"\x89PNG")
        page = make_page([action(4, "click", "Save")])
        files = [{"node": 10, "accept": "image/*", "multiple": True, "trigger": None,
                  "label": "Product images"}]
        driver = FakeDriver([page], files=files)
        report = run(script_of({"upload": {"field": "Product images", "path": str(shot)}}), self.out,
                     jev=StubJev(), driver=driver, log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["target"], {"index": "2", "label": "Product images",
                                          "role": "file input", "p": 1.0})
        self.assertEqual(driver.acted, [("upload", 10, str(shot.resolve()))])

    def test_upload_fails_before_asking_when_the_file_is_missing(self):
        page = make_page([action(3, "click", "Browse images")])
        jev = ScriptedJev()
        missing = (self.out / "nope.png").resolve()
        report, driver = self.run_script(
            script_of({"upload": {"field": "Browse images", "path": str(missing)}}), [page], jev)
        self.assertEqual(report["steps"][0]["error"], f"upload file not found: {missing}")
        self.assertEqual(jev.requests, 0)
        self.assertEqual(driver.acted, [])

    def test_upload_refuses_a_control_that_opens_no_picker(self):
        shot = self.out / "shot.png"
        shot.write_bytes(b"\x89PNG")
        page = make_page([action(3, "click", "Browse images")])
        report, driver = self.run_script(
            script_of({"upload": {"field": "Browse images", "path": str(shot)}}), [page], StubJev())
        self.assertEqual(report["steps"][0]["error"], "'Browse images' does not open a file picker")
        self.assertEqual(driver.acted, [])

    def test_drag_picks_both_ends_and_drags_between_them(self):
        page = make_page([action(1, "click", "Sort handle"), action(2, "click", "Bin")])
        jev = RecordingJev(StubJev(), self.out)
        script = script_of({"drag": {"from": "Sort handle", "to": "Bin"}})
        report, driver = self.run_script(script, [page], jev)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["drag_from"]["label"], "Sort handle")
        self.assertEqual(step["drag_to"]["label"], "Bin")
        self.assertEqual(step["target"]["label"], "Sort handle")  # the step's target is what moved
        self.assertEqual(driver.acted, [("drag", "Sort handle", "Bin")])
        # one request per end, each with its own rules
        asked = [json.loads(line) for line in (self.out / "jev-trace.jsonl").read_text().splitlines()]
        self.assertEqual([entry["questions"]["target"]["instructions"]["rules"] for entry in asked],
                         [Q.TARGET_DRAG_FROM, Q.TARGET_DRAG_TO])

    def test_drag_fails_when_the_destination_is_not_on_the_page(self):
        page = make_page([action(1, "click", "Sort handle")])
        script = script_of({"drag": {"from": "Sort handle", "to": "The archive"}})
        report, driver = self.run_script(script, [page], StubJev())
        step = report["steps"][0]
        self.assertEqual(step["error"], 'no matching element on the whole page:'
                                        ' best [1] "Sort handle" p=0.00, none p=1.00')
        self.assertEqual(driver.acted, [])

    def test_a_target_below_the_fold_is_found_on_the_whole_page_and_scrolled_to(self):
        """The viewport snapshot never offers it; the whole-page pass does, and the node id
        survives the scroll that follows."""
        viewport = make_page([action(1, "click", "Cancel")])
        whole = make_page([action(1, "click", "Cancel"), action(2, "click", "Save")])
        reached = make_page([action(2, "click", "Save")])
        driver = FakeDriver([viewport, reached, reached], full_pages=[whole])
        report = run(script_of({"click": "Save"}), self.out, jev=StubJev(), driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["fallback"], "full_page")
        self.assertEqual(driver.acted, [("scroll_into_view", 2, None), ("click", "Save", "")])
        self.assertEqual(driver.full_page_observations, 1)
        self.assertEqual(step["jev"]["requests"], 2)  # the viewport pass, then the whole-page one

    def test_a_target_in_the_viewport_costs_no_whole_page_pass(self):
        page = make_page([action(1, "click", "Save")])
        report, driver = self.run_script(script_of({"click": "Save"}), [page], StubJev())
        self.assertEqual(driver.acted, [("click", "Save", "")])
        self.assertEqual(driver.full_page_observations, 0)
        self.assertIsNone(report["steps"][0]["fallback"])

    def test_a_hidden_file_input_below_the_fold_needs_no_scrolling(self):
        shot = self.out / "shot.png"
        shot.write_bytes(b"\x89PNG")
        page = make_page([action(1, "click", "Cancel")])
        files = [{"node": 10, "accept": "", "multiple": False, "trigger": None,
                  "label": "Product images"}]
        driver = FakeDriver([page], files=files)
        report = run(script_of({"upload": {"field": "Product images", "path": str(shot)}}), self.out,
                     jev=LateJev("Product images"), driver=driver, log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["fallback"], "full_page")
        # a hidden input is nowhere on screen: nothing to scroll to
        self.assertEqual(driver.acted, [("upload", 10, str(shot.resolve()))])

    def test_an_upload_path_is_resolved_before_it_reaches_chrome(self):
        """Chrome resolves a relative path against its own directory, so the step sends an
        absolute one."""
        shot = self.out / "shot.png"
        shot.write_bytes(b"\x89PNG")
        page = make_page([action(3, "click", "Browse images")])
        files = [{"node": 10, "accept": "", "multiple": False, "trigger": 3,
                  "label": "Browse images"}]
        driver = FakeDriver([page], files=files)
        with contextlib.chdir(self.out):
            report = run(script_of({"upload": {"field": "Browse images", "path": "shot.png"}}),
                         self.out, jev=StubJev(), driver=driver, log=lambda _line: None)
        self.assertEqual(report["steps"][0]["status"], "pass")
        self.assertEqual(driver.acted, [("upload", 10, str(shot.resolve()))])

    def test_a_covered_target_says_so_instead_of_blaming_the_page(self):
        """Three tries either way, but a hit test that keeps failing is not a page that moved."""
        for failure, message in ((Covered("covered"), "target covered by another element"),
                                 (StalePage("moved"), "page kept changing")):
            with self.subTest(message=message):
                class RefusingDriver(FakeDriver):
                    def act(self, action, page, text=None):
                        raise failure

                driver = RefusingDriver([make_page([action(1, "click", "Save")])])
                report = run(script_of({"click": "Save"}), self.out, jev=StubJev(), driver=driver,
                             log=lambda _line: None)
                self.assertEqual(report["steps"][0]["error"], message)

    def test_a_target_that_vanishes_after_the_scroll_is_decided_again(self):
        """A virtualised list re-renders under the scroll: the next attempt asks about what is
        there now instead of failing the step."""
        viewport = make_page([action(1, "click", "Cancel")])
        whole = make_page([action(1, "click", "Cancel"), action(2, "click", "Save")])
        rerendered = make_page([action(1, "click", "Cancel")])
        reached = make_page([action(2, "click", "Save")])
        driver = FakeDriver([viewport, rerendered, viewport, reached, reached],
                            full_pages=[whole, whole])
        report = run(script_of({"click": "Save"}), self.out, jev=StubJev(), driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["fallback"], "full_page")
        self.assertEqual(driver.acted, [("scroll_into_view", 2, None), ("scroll_into_view", 2, None),
                                        ("click", "Save", "")])
        self.assertEqual(driver.full_page_observations, 2)

    def test_a_target_below_the_fold_is_reached_after_three_asks_not_twenty(self):
        """Twenty tries cost seconds per step and cannot help: an unchanged page will not grow
        the element later, so the whole-page look comes after three."""
        viewport = make_page([action(1, "click", "Cancel")], text="Admin")
        whole = make_page([action(1, "click", "Cancel"), action(2, "click", "Credit Type")],
                          text="Admin")
        reached = make_page([action(2, "click", "Credit Type")], text="Admin")
        jev = LoadingJev("Credit Type")
        driver = FakeDriver([viewport, viewport, viewport, reached, reached], full_pages=[whole])
        report = run(script_of({"click": "Credit Type"}), self.out, jev=jev, driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["fallback"], "full_page")
        self.assertEqual(step["ready"], [0.1, 0.1, 0.1])
        self.assertEqual(jev.requests, 4)  # three viewport asks, then the whole page
        self.assertEqual(driver.full_page_observations, 1)

    def test_an_empty_viewport_on_a_page_that_scrolls_looks_at_the_whole_page_early(self):
        """example.com in a short window: the only link sits below the fold. A still page that
        can scroll will not grow it into the viewport, so the wait stops after three looks."""
        scroll = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560}
        viewport = make_page([scroll], text="Example Domain")
        whole = make_page([action(2, "click", "Learn more")], text="Example Domain")
        reached = make_page([action(2, "click", "Learn more")], text="Example Domain")
        driver = FakeDriver([viewport] * ABSTAIN_TRIES + [reached, reached], full_pages=[whole])
        report = run(script_of({"click": "Learn more"}), self.out, jev=StubJev(), driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["fallback"], "full_page")
        self.assertLessEqual(step["waited_for_candidates"], ABSTAIN_TRIES)
        self.assertEqual(step["jev"]["requests"], 1)  # the whole-page look only
        self.assertEqual(driver.acted, [("scroll_into_view", 2, None), ("click", "Learn more", "")])

    def test_a_page_that_is_still_rendering_keeps_its_whole_wait(self):
        """The early look is not the last word: a form that renders on the sixth observation
        still gets there, with the tries the streak did not spend."""
        loading = make_page([action(1, "click", "Cancel")], text="Loading")
        form = make_page([action(1, "click", "Cancel"), action(2, "click", "Save")], text="Ready")
        jev = LoadingJev("Save")
        driver = FakeDriver([loading] * 5 + [form] * 3, full_pages=[loading])
        report = run(script_of({"click": "Save"}), self.out, jev=jev, driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["ready"], [0.1, 0.1, 0.1, 0.1, 0.1, 0.9])
        self.assertEqual((jev.requests, jev.wide_requests), (7, 1))
        self.assertEqual(driver.full_page_observations, 1)
        self.assertEqual(driver.acted, [("click", "Save", "")])

    def test_a_page_that_never_renders_fails_once_the_budget_is_gone(self):
        loading = make_page([action(1, "click", "Cancel")], text="Loading")
        jev = LoadingJev("Save")
        driver = FakeDriver([loading], full_pages=[loading])
        report = run(script_of({"click": "Save"}), self.out, jev=jev, driver=driver,
                     settle_tries=3, log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertIn("no matching element on the whole page", step["error"])
        self.assertEqual(driver.acted, [])

    def test_a_target_clipped_by_a_scrolling_panel_is_found_by_paging_the_panel(self):
        """A picker grid that scrolls on its own keeps its height on the stretched page, so the
        whole-page look cannot offer what it clips; one page of the panel down can."""
        grid = make_page([action(1, "click", "Tee 1"), action(2, "click", "Tee 2")])
        scrolled = make_page([action(3, "click", "Sample Men's Apparel 4")], text="scrolled")
        driver = FakeDriver([grid, grid, grid, scrolled, scrolled], full_pages=[grid],
                            panels=[{"node": 50, "top": 0, "area": 1}], panel_pages=1)
        report = run(script_of({"click": "Sample Men's Apparel 4"}), self.out,
                     jev=LoadingJev("Sample Men's Apparel 4"), driver=driver, log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["fallback"], "panel_scroll")
        self.assertEqual(driver.acted, [("scroll_inner", 50, None),
                                        ("click", "Sample Men's Apparel 4", "")])

    def test_a_panel_without_the_target_is_scrolled_back(self):
        grid = make_page([action(1, "click", "Tee 1")])
        driver = FakeDriver([grid], full_pages=[grid], panels=[{"node": 50, "top": 7, "area": 1}],
                            panel_pages=2)
        report = run(script_of({"click": "Save"}), self.out, jev=LoadingJev("Save"), driver=driver,
                     settle_tries=0, log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertIn("no matching element on the whole page", step["error"])
        self.assertEqual(driver.acted, [("scroll_inner", 50, None), ("scroll_inner", 50, None),
                                        ("scroll_inner", 50, None), ("scroll_inner", 50, 7)])

    def test_a_run_that_cannot_load_its_page_still_writes_run_json(self):
        class RefusedDriver(FakeDriver):
            def navigate(self, url):
                raise CdpError(f"navigate {url}: net::ERR_CONNECTION_REFUSED")

        driver = RefusedDriver([make_page([])])
        with self.assertRaises(CdpError):
            run(script_of({"expect": "Add products"}, {"click": "Save"}, url="http://localhost:3999/"),
                self.out, jev=StubJev(), driver=driver, log=lambda _line: None)
        report = json.loads((self.out / "run.json").read_text())
        self.assertEqual(report["error"], "navigate http://localhost:3999/: net::ERR_CONNECTION_REFUSED")
        self.assertEqual([s["status"] for s in report["steps"]], ["skipped", "skipped"])
        self.assertEqual(report["totals"]["passed"], 0)

    def test_a_ready_page_waits_for_a_suggestion_list_that_renders_late(self):
        """`ready` is about the page, not the target: the typeahead option a fill asked for
        arrives after the form already looks settled, and the step must still wait for it."""
        typed = make_page([action(1, "click", "Continue")], text="Client")
        listed = make_page([action(1, "click", "Continue"), action(2, "click", "Sample CRM Client")],
                           text="Client Sample CRM Client")
        jev = SettledJev("Sample CRM Client")
        driver = FakeDriver([typed] * 4 + [listed] * 3, full_pages=[typed])
        report = run(script_of({"click": "Sample CRM Client"}), self.out, jev=jev, driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(driver.acted, [("click", "Sample CRM Client", "")])
        self.assertEqual(driver.full_page_observations, 1)  # once per page state, not per retry

    def test_a_page_that_keeps_changing_still_gets_the_whole_budget(self):
        pages = [make_page([action(1, "click", "Upload product")], text=f"Loading {n}")
                 for n in range(4)]
        jev = ScriptedJev(ready=0.3, target={"1": 0.05, "none": 0.95})
        report, _ = self.run_script(script_of({"click": "Flat Lay Shoot"}), pages, jev,
                                    settle_tries=3)
        self.assertEqual(report["steps"][0]["ready"], [0.3, 0.3, 0.3, 0.3])

    def test_drag_offers_draggables_to_pick_up_and_text_holders_to_drop_on(self):
        """A drop zone is a plain div with a drop handler; the tile that moves is often one too."""
        page = make_page([action(1, "click", "Save"),
                          action(4, "click", "Tile X", role="draggable"),
                          action(5, "click", "Front", role="text")])
        jev = RecordingJev(StubJev(), self.out)
        script = script_of({"drag": {"from": "Tile X", "to": "Front"}})
        report, driver = self.run_script(script, [page], jev)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["drag_from"]["label"], "Tile X")
        self.assertEqual(step["drag_to"]["label"], "Front")
        self.assertEqual(step["drag_mode"], "pointer")
        self.assertEqual(driver.acted, [("drag", "Tile X", "Front")])
        self.assertTrue(driver.drop_target_observations)
        asked = [json.loads(line) for line in (self.out / "jev-trace.jsonl").read_text().splitlines()]
        # the text holder is a destination only; it is never offered as something to pick up
        self.assertEqual([e["label"] for e in asked[0]["state"]["elements"]], ["Save", "Tile X"])
        self.assertEqual([e["label"] for e in asked[1]["state"]["elements"]],
                         ["Save", "Tile X", "Front"])

    def test_expect_on_a_long_page_shows_jev_the_text_past_the_cut(self):
        """Jev reads at most EXPECT_TEXT_CHARS; text further down used to read as absent while
        the literal check found it right there."""
        page = make_page([action(1, "click", "Save")], text="Top")
        filler = "".join(f"Row {i}: lorem ipsum dolor sit amet.\n" for i in range(400))
        jev = ScriptedJev(expectation={"verbatim": 1.0, "similar": 0.0, "absent": 0.0})
        driver = FakeDriver([page], strings="Top heading\n" + filler + "Order  confirmed ZX-4471\n")
        report = run(script_of({"expect": "order confirmed ZX-4471"}), self.out, jev=jev,
                     driver=driver, log=lambda _line: None)
        self.assertEqual(report["steps"][0]["status"], "pass")
        text = jev.asked[0][0]["page"]["text"]
        self.assertLessEqual(len(text), 8003)
        self.assertTrue(text.startswith("Top heading"))
        self.assertIn("Order  confirmed ZX-4471", text)

    def test_expect_asks_jev_with_a_page_only_state(self):
        """Expect always asks, and the state is the page alone: given the script, the step and the
        elements, Jev agrees with the step instead of reading the page."""
        page = make_page([action(1, "fill", "", role="textbox")], text="Products")
        jev = ScriptedJev(expectation={"verbatim": 1.0, "similar": 0.0, "absent": 0.0})
        driver = FakeDriver([page], strings="Products\nEnter your email")
        report = run(script_of({"expect": "Enter your email"}), self.out, jev=jev, driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["expectation"],
                         {"literal": True, "verbatim": 1.0, "similar": 0.0, "absent": 0.0})
        self.assertEqual(step["jev"]["requests"], 1)
        state, questions = jev.asked[0]
        self.assertEqual(set(state), {"page"})
        self.assertEqual(set(state["page"]), {"url", "title", "text"})
        self.assertEqual(state["page"]["text"], "Products\nEnter your email")
        self.assertEqual(set(questions), {"expectation"})
        self.assertEqual(questions["expectation"]["instructions"]["exact_text"], "Enter your email")
        self.assertEqual(set(questions["expectation"]["criteria"]), {"verbatim", "similar", "absent"})
        self.assertTrue(step["screenshot"].endswith("01-expect-enter-your-email.png"))
        self.assertIn("01 pass  expect: Enter your email  verbatim=1.00 literal=true",
                      format_report(report))

    def test_text_split_across_elements_still_matches_innertext(self):
        page = make_page([action(1, "click", "Save")], text="Bulk shoot with\n2\nproduct\ns")
        driver = FakeDriver([page], strings="Bulk shoot with\n  2 products\n")
        report = run(script_of({"expect": "Bulk shoot with 2 products"}), self.out, jev=StubJev(),
                     driver=driver, log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertTrue(step["expectation"]["literal"])

    def test_failing_expect_keeps_its_events_and_both_screenshots(self):
        page = make_page([action(1, "click", "Save")], text="Products")
        driver = FakeDriver([page], drains=[{
            "console_errors": [{"level": "error", "text": "boom"}],
            "failed_requests": [{"method": "GET", "url": "http://fake/x", "status": 500}],
        }])
        report = run(script_of({"expect": "Saved"}), self.out, jev=StubJev(), driver=driver,
                     settle_tries=1, log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertEqual([error["text"] for error in step["console_errors"]], ["boom"])
        self.assertEqual([request["status"] for request in step["failed_requests"]], [500])
        self.assertTrue(step["screenshot"].endswith("01-expect-saved.png"))
        self.assertTrue(step["failure_screenshot"].endswith("01-expect-saved-FAIL.png"))
        text = format_report(report)
        self.assertIn(f"   screenshot {step['screenshot']}\n   screenshot {step['failure_screenshot']}", text)
        self.assertIn("   console: [error] boom", text)

    def test_expect_fails_when_neither_literal_nor_likely(self):
        page = make_page([action(1, "click", "Save")], text="Products")
        report, _ = self.run_script(script_of({"expect": "Saved"}), [page], StubJev(), settle_tries=1)
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(step["expectation"],
                         {"literal": False, "verbatim": 0.0, "similar": 0.0, "absent": 1.0})
        self.assertEqual(step["jev"]["requests"], 2)

    def test_a_verbatim_verdict_passes_even_when_the_literal_walk_misses(self):
        """The literal walk is evidence, not the verdict: it misses text the page punctuates
        differently, so Jev reading it as verbatim is enough to pass."""
        page = make_page([action(1, "click", "Save")], text="Saved — 3 products")
        jev = ScriptedJev(expectation={"verbatim": 0.97, "similar": 0.03, "absent": 0.0})
        driver = FakeDriver([page], strings="Saved — 3 products")
        report = run(script_of({"expect": "Saved: 3 products"}), self.out, jev=jev, driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["expectation"],
                         {"literal": False, "verbatim": 0.97, "similar": 0.03, "absent": 0.0})
        self.assertIn("01 pass  expect: Saved: 3 products  verbatim=0.97 literal=false",
                      format_report(report))

    def test_expect_fails_when_jev_says_absent(self):
        page = make_page([action(1, "click", "Save")], text="Products")
        jev = ScriptedJev(expectation={"verbatim": 0.05, "similar": 0.88, "absent": 0.07})
        report, _ = self.run_script(script_of({"expect": "Your work"}), [page], jev, settle_tries=1)
        step = report["steps"][0]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(step["error"], "expectation not met: 'Your work' (verbatim p=0.05,"
                                        " similar p=0.88, absent p=0.07, literal=false after 2 tries)")
        self.assertIn("01 FAIL  expect: Your work  verbatim=0.05 literal=false",
                      format_report(report))

    def test_expect_honours_the_expect_threshold(self):
        page = make_page([action(1, "click", "Save")], text="Products")
        script = script_of({"expect": "Your work"})
        verdict = {"verbatim": 0.55, "similar": 0.45, "absent": 0.0}
        report, _ = self.run_script(script, [page], ScriptedJev(expectation=verdict), settle_tries=1)
        self.assertEqual(report["steps"][0]["status"], "fail")
        report, _ = self.run_script(script, [page], ScriptedJev(expectation=verdict),
                                    expect_threshold=0.5)
        self.assertEqual(report["steps"][0]["status"], "pass")

    def test_expect_count_counts_matching_elements(self):
        page = make_page([
            action(1, "click", "Test kurta card 1", role="link"),
            action(2, "click", "Test kurta card 2", role="link"),
            action(3, "click", "Test kurta card 3", role="link"),
            action(4, "click", "Upload product"),
        ])
        report, _ = self.run_script(script_of({"expect_count": {"card": 3}}), [page], StubJev())
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["count"], 3)
        self.assertEqual(step["matched"], ["Test kurta card 1", "Test kurta card 2", "Test kurta card 3"])
        self.assertEqual(step["jev"]["requests"], 1)

    def test_expect_count_leaves_out_click_only_candidates(self):
        page = make_page([
            action(1, "click", "Test kurta card 1", role="clickable"),
            action(2, "click", "Open Test kurta card 1", role="link"),
            action(3, "click", "Open Test kurta card 2", role="link"),
        ])
        report, _ = self.run_script(script_of({"expect_count": {"card": 2}}), [page], StubJev())
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["matched"], ["Open Test kurta card 1", "Open Test kurta card 2"])

    def test_expect_count_gives_identical_elements_one_verdict(self):
        shots = [action(n, "click", "View A shot") for n in (1, 2, 3)]
        jev = ScriptedJev(match_1=0.9, match_2=0.2, match_3=0.1)
        report, _ = self.run_script(script_of({"expect_count": {"View A shot": 3}}),
                                    [make_page(shots)], jev)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["count"], 3)
        self.assertEqual(step["groups"],
                         [{"label": "View A shot", "role": "button", "n": 3, "p": 0.9}])

    def test_expect_count_counts_what_is_below_the_fold(self):
        cards = [action(n, "click", f"Test kurta card {n}", role="link") for n in (1, 2, 3)]
        visible = make_page(cards[:1])
        full = make_page(cards)
        full["scroll_height"] = 2400
        driver = FakeDriver([visible], full_pages=[full])
        script = script_of({"expect_count": {"card": 3}}, {"click": "Test kurta card 1"})
        report = run(script, self.out, jev=StubJev(), driver=driver, log=lambda _line: None)
        count, click = report["steps"]
        self.assertEqual(count["status"], "pass")
        self.assertEqual(count["count"], 3)
        self.assertEqual(count["scroll_height"], 2400)
        self.assertEqual(driver.full_page_observations, 1)
        # the step ends back on the viewport page, so the next step sees only what is visible
        self.assertEqual(count["observations"]["element_count"], 1)
        self.assertEqual(click["status"], "pass")
        self.assertEqual(driver.acted, [("click", "Test kurta card 1", "")])

    def test_expect_count_retries_until_the_grid_is_complete(self):
        cards = [action(n, "click", f"Test kurta card {n}", role="link") for n in (1, 2, 3)]
        driver = FakeDriver([make_page(cards[:2])], full_pages=[make_page(cards[:2]), make_page(cards)])
        report = run(script_of({"expect_count": {"card": 3}}), self.out, jev=StubJev(), driver=driver,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["status"], "pass")
        self.assertEqual(step["count"], 3)
        self.assertEqual(step["count_tries"], 2)
        self.assertEqual(step["jev"]["requests"], 2)
        self.assertEqual(step["ready"], [])

    def test_expect_count_gives_up_with_the_last_count(self):
        page = make_page([action(1, "click", "Test kurta card 1", role="link")])
        report, _ = self.run_script(script_of({"expect_count": {"card": 3}}), [page], StubJev(),
                                    settle_tries=1)
        step = report["steps"][0]
        self.assertEqual(step["error"],
                         "expected 3 'card', found 1 after 2 tries: ['Test kurta card 1']")
        self.assertEqual(step["count_tries"], 2)
        self.assertEqual(step["jev"]["requests"], 2)
        self.assertEqual(step["ready"], [])

    def test_cheap_steps_cost_no_jev_requests(self):
        page = make_page([action(1, "click", "Upload product")])
        jev = ScriptedJev()
        script = script_of({"open": "http://fake/x"}, {"wait": 1}, {"scroll": "down"},
                           {"press": "Enter"}, {"checkpoint": "after-save"})
        report, driver = self.run_script(script, [page], jev)
        self.assertEqual([s["status"] for s in report["steps"]], ["pass"] * 5)
        self.assertEqual([s["jev"] for s in report["steps"]], [None] * 5)
        self.assertEqual(jev.requests, 0)
        self.assertEqual(report["totals"]["jev_requests"], 0)
        self.assertIn(("navigate", "http://fake/x"), driver.acted)
        self.assertIn(("scroll", "Scroll down", 560), driver.acted)
        self.assertIn(("press", "Press Enter", None), driver.acted)
        self.assertTrue((self.out / "05-checkpoint-after-save.png").exists())

    def test_capture_secret_picks_the_value_holder_and_records_only_its_length(self):
        """A reveal-once key is often a plain code block: text holders are offered, buttons not.
        The value stays in the extension; run.json, the trace and the report hold its length."""
        page = make_page([action(1, "click", "Copy"),
                          action(2, "click", "Secret Access Key", role="text"),
                          action(3, "click", "[hidden secret, 40 chars]", role="text"),
                          action(4, "click", "Note", role="textbox", value="[hidden secret, 64 chars]")])
        jev = RecordingJev(ScriptedJev(target={"2": 0.05, "3": 0.9, "4": 0.05, "none": 0.0}), self.out)
        lines = []
        driver = FakeDriver([page])
        step = {"capture_secret": {"field": "Secret Access Key value", "keychain_service": "backup-r2-sk"}}
        report = run(script_of(step), self.out, jev=jev, driver=driver, target="ext", log=lines.append)
        record = report["steps"][0]
        self.assertEqual(record["status"], "pass")
        self.assertEqual(driver.acted, [("capture_secret", 3, "backup-r2-sk")])
        self.assertEqual(record["secret"], {"service": "backup-r2-sk", "length": 64, "stored": True})
        self.assertTrue(driver.drop_target_observations)
        [asked] = [json.loads(line) for line in (self.out / "jev-trace.jsonl").read_text().splitlines()]
        self.assertEqual([e["label"] for e in asked["state"]["elements"]],
                         ["Secret Access Key", "[hidden secret, 40 chars]", "Note"])
        self.assertEqual(asked["questions"]["target"]["instructions"]["rules"], Q.TARGET_CAPTURE)
        self.assertIn("captured → keychain backup-r2-sk (64 chars)", lines[0])

    def test_capture_secret_needs_the_ext_target_and_fails_before_any_step(self):
        driver = FakeDriver([make_page([action(1, "click", "Key", role="text")])])
        step = {"capture_secret": {"field": "Key", "keychain_service": "svc"}}
        with self.assertRaises(ScriptError) as caught:
            run(script_of({"click": "Key"}, step), self.out, jev=StubJev(), driver=driver,
                log=lambda _line: None)
        self.assertEqual(str(caught.exception), "step 2: capture_secret needs --target ext: only the"
                         " extension reads a secret without showing it to ck-browser")
        self.assertEqual(driver.acted, [])

    def test_a_withheld_screenshot_is_recorded_and_reported(self):
        class Withholding(FakeDriver):
            def screenshot(self, path):
                return None

        driver = Withholding([make_page([action(1, "click", "Save")])])
        report = run(script_of({"checkpoint": "keys"}), self.out, jev=StubJev(), driver=driver,
                     log=lambda _line: None)
        record = report["steps"][0]
        self.assertEqual((record["status"], record["screenshot"], record["screenshot_withheld"]),
                         ("pass", None, True))
        self.assertIn("01 pass  checkpoint: keys (", format_report(report))
        self.assertIn("   screenshot withheld: the page may show a secret", format_report(report))
        self.assertNotIn("checkpoints:", format_report(report))

    def test_report_formatting(self):
        page = make_page([action(1, "click", "Category"), action(2, "click", "Sub-category")])
        jev = ScriptedJev(target={"1": 0.52, "2": 0.48, "none": 0.0})
        report, _ = self.run_script(script_of({"click": "Category"}, {"expect": "Saved"}), [page], jev,
                                    settle_tries=3)
        text = format_report(report)
        lines = text.splitlines()
        # the viewport pass, the whole-page one that also found nothing, then the rest of the
        # budget on the viewport — the page never moves, so no second whole-page look
        self.assertRegex(lines[0], r"^ck-browser: 0/2 steps passed in \d+\.\d s — 4 Jev requests, ")
        self.assertIn('01 FAIL  click: Category  — no matching element on the whole page:'
                      ' best [1] "Category" p=0.52', lines[1])
        self.assertTrue(lines[2].startswith("   screenshot " + str(self.out)))
        self.assertEqual(lines[3], "02 skip  expect: Saved")
        self.assertEqual(lines[-1], "console errors: 0   failed requests: 0")

    def test_passing_report_line_shows_the_target(self):
        page = make_page([action(1, "click", "Upload product")])
        report, _ = self.run_script(script_of({"click": "Upload product"}), [page], StubJev())
        self.assertIn('01 pass  click: Upload product  → [1] button "Upload product" p=1.00',
                      format_report(report))


# -- acting on a page that moved on ----------------------------------------


class FakeTab:
    """A tab whose document has already changed: every probe answers with a new marker."""

    def __init__(self):
        self.calls = []
        self.after_input = None

    def evaluate(self, expression, await_promise=False):
        return "a different page"

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


class ActTest(unittest.TestCase):
    def test_a_press_still_dispatches_after_the_page_changed(self):
        tab = FakeTab()
        page = make_page([])
        act(tab, {"id": "press", "kind": "press", "key": "ArrowDown", "label": "Press ArrowDown"}, page)
        self.assertEqual([method for method, _ in tab.calls],
                         ["Input.dispatchKeyEvent", "Input.dispatchKeyEvent"])
        self.assertEqual({params["key"] for _, params in tab.calls}, {"ArrowDown"})

    def test_a_scroll_still_dispatches_after_the_page_changed(self):
        tab = FakeTab()
        act(tab, {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560}, make_page([]))
        self.assertEqual([method for method, _ in tab.calls], ["Input.dispatchMouseEvent"])

    def test_a_click_still_refuses_a_changed_page(self):
        tab = FakeTab()
        page = make_page([action(1, "click", "Save")])
        with self.assertRaises(StalePage):
            act(tab, page["actions"][0], page)
        self.assertEqual(tab.calls, [])


class RecordingTab:
    """A tab that answers the page probes with canned values and records every CDP call.

    `dropped` is what the drop watch saw after a pointer gesture, `pressed` what the press watch
    saw after a click.
    """

    def __init__(self, page, points=(), dropped=False, dnd=True, pressed=True):
        self.page = page
        self.points = list(points)
        self.dropped = dropped
        self.pressed = pressed
        self.dnd = dnd
        self.calls = []
        self.evaluated = []
        self.after_input = None

    def evaluate(self, expression, await_promise=False, command_line_api=False):
        self.evaluated.append(expression)
        if expression.startswith(FRESH_JS):  # the freshness guard
            return [self.page["page_key"], None]  # guards are empty, so the node guard is null
        if expression.startswith(TARGET_JS):
            return self.points.pop(0)
        if expression == DROP_SEEN_JS:
            return self.dropped is not False  # as in JS: a cache that navigated away is not `false`
        if expression.startswith(PRESS_SEEN_JS):
            return self.pressed is not False
        if "DragEvent" in expression:
            return self.dnd
        return self.page["marker"]

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"result": {"objectId": "obj-1"}}


class GestureTest(unittest.TestCase):
    """The CDP a hover, a drag and an upload actually dispatch."""

    def test_hover_moves_the_mouse_to_the_centre_and_presses_nothing(self):
        page = make_page([action(1, "click", "Regenerate")])
        tab = RecordingTab(page, points=[{"x": 40, "y": 60}])
        act(tab, {**page["actions"][0], "kind": "hover"}, page)
        self.assertEqual(tab.calls, [("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 40, "y": 60})])

    def test_a_drag_presses_moves_in_steps_and_releases(self):
        page = make_page([action(1, "click", "View A shot"), action(2, "click", "View B shot")])
        tab = RecordingTab(page, points=[{"x": 10, "y": 10}, {"x": 60, "y": 110}], dropped=True)
        mode = drag(tab, page["actions"][0], page["actions"][1], page)
        self.assertEqual(mode, "pointer")
        self.assertEqual([params["type"] for _method, params in tab.calls],
                         ["mousePressed"] + ["mouseMoved"] * 5 + ["mouseReleased"])
        self.assertEqual([(params["x"], params["y"]) for _method, params in tab.calls],
                         [(10, 10), (20, 30), (30, 50), (40, 70), (50, 90), (60, 110), (60, 110)])
        self.assertEqual({params.get("button") for _method, params in tab.calls}, {"left"})
        self.assertNotIn(True, ["DragEvent" in expression for expression in tab.evaluated])

    def test_a_drag_onto_an_end_off_to_the_side_presses_the_source_where_it_slid(self):
        """Resolving an `aside` target scrolls its panel: the source is looked up again, unscrolled,
        and pressed where it is now."""
        page = make_page([action(1, "click", "Card Four"), action(2, "click", "Card Six", aside=True)])
        tab = RecordingTab(page, points=[{"x": 760, "y": 80}, {"x": 800, "y": 80}, {"x": 160, "y": 80}],
                           dropped=True)
        drag(tab, page["actions"][0], page["actions"][1], page)
        self.assertEqual((tab.calls[0][1]["type"], tab.calls[0][1]["x"]), ("mousePressed", 160))

    def test_a_drag_whose_source_slid_out_of_view_presses_nothing(self):
        page = make_page([action(1, "click", "Card One"), action(2, "click", "Card Six", aside=True)])
        tab = RecordingTab(page, points=[{"x": 160, "y": 80}, {"x": 800, "y": 80}, None])
        with self.assertRaises(Apart):
            drag(tab, page["actions"][0], page["actions"][1], page)
        self.assertEqual(tab.calls, [])

    def test_a_drag_without_a_drop_is_replayed_as_html5_drag_events(self):
        """The page moving proves nothing — the moves alone light up whatever they pass over —
        so only a `drop` event counts as the pointer gesture having landed."""
        page = make_page([action(1, "click", "View A shot"), action(2, "click", "View B shot")])
        tab = RecordingTab(page, points=[{"x": 10, "y": 10}, {"x": 60, "y": 110}], dropped=False)
        mode = drag(tab, page["actions"][0], page["actions"][1], page)
        self.assertEqual(mode, "dnd")
        self.assertEqual(len(tab.calls), 7)  # the pointer gesture happened first, and moved nothing
        dnd = [expression for expression in tab.evaluated if "DragEvent" in expression][0]
        self.assertIn('{"from": 1, "to": 2, "start": {"x": 10, "y": 10}, "end": {"x": 60, "y": 110}}', dnd)
        for event in ("dragstart", "dragenter", "dragover", "drop", "dragend"):
            self.assertIn(event, dnd)

    def test_a_drop_that_navigated_away_counts_as_landed(self):
        """The cache goes with the document: a page that is already gone is not dragged again."""
        page = make_page([action(1, "click", "View A shot"), action(2, "click", "View B shot")])
        tab = RecordingTab(page, points=[{"x": 10, "y": 10}, {"x": 60, "y": 110}], dropped=None)
        self.assertEqual(drag(tab, page["actions"][0], page["actions"][1], page), "pointer")
        self.assertNotIn(True, ["DragEvent" in expression for expression in tab.evaluated])

    def test_a_drag_onto_a_target_that_is_gone_is_refused(self):
        page = make_page([action(1, "click", "View A shot"), action(2, "click", "View B shot")])
        tab = RecordingTab(page, points=[{"x": 10, "y": 10}, None])
        with self.assertRaises(StalePage):
            drag(tab, page["actions"][0], page["actions"][1], page)
        self.assertEqual(tab.calls, [])  # nothing is dispatched until both ends are resolved

    def test_filling_an_empty_value_clears_the_field_with_a_delete(self):
        """insertText("") types nothing, so an empty value has to delete the selection."""
        page = make_page([action(1, "fill", "Search", role="textbox", value="abc")])
        tab = RecordingTab(page, points=[{"x": 40, "y": 60}])
        act(tab, page["actions"][0], page, text="")
        methods = [(method, params.get("type"), params.get("key")) for method, params in tab.calls]
        self.assertNotIn("Input.insertText", [method for method, _params in tab.calls])
        self.assertEqual(methods[-2:], [("Input.dispatchKeyEvent", "keyDown", "Delete"),
                                        ("Input.dispatchKeyEvent", "keyUp", "Delete")])

    def test_filling_a_value_types_it(self):
        page = make_page([action(1, "fill", "Search", role="textbox")])
        tab = RecordingTab(page, points=[{"x": 40, "y": 60}])
        act(tab, page["actions"][0], page, text="abc")
        self.assertEqual(tab.calls[-1], ("Input.insertText", {"text": "abc"}))

    def test_a_click_chrome_never_delivered_fails_instead_of_passing(self):
        """A dialog over the tab (a password-breach warning) drops the input while Chrome still
        answers the dispatch: the click must not look like it happened."""
        page = make_page([action(1, "click", "Logout", role="link")])
        tab = RecordingTab(page, points=[{"x": 40, "y": 60}], pressed=False)
        with self.assertRaises(Unreached) as caught:
            act(tab, page["actions"][0], page)
        self.assertIn("the click never reached the page", str(caught.exception))
        self.assertIsNone(tab.after_input)

    def test_a_fill_chrome_never_delivered_types_nothing(self):
        page = make_page([action(1, "fill", "Search", role="textbox")])
        tab = RecordingTab(page, points=[{"x": 40, "y": 60}], pressed=False)
        with self.assertRaises(Unreached):
            act(tab, page["actions"][0], page, text="abc")
        self.assertEqual([params["type"] for _method, params in tab.calls], ["mousePressed", "mouseReleased"])

    def test_a_click_that_navigated_away_counts_as_delivered(self):
        """The press watch goes with the document: a page that is already gone was clicked."""
        page = make_page([action(1, "click", "Logout", role="link")])
        tab = RecordingTab(page, points=[{"x": 40, "y": 60}], pressed=None)
        act(tab, page["actions"][0], page)
        self.assertEqual(tab.after_input, page["actions"][0])

    def test_upload_sets_the_file_on_the_resolved_node(self):
        page = make_page([])
        tab = RecordingTab(page)
        upload(tab, 10, "/tmp/shot.png", page)
        self.assertEqual(tab.calls, [
            ("Runtime.evaluate", {"expression": "window.__ckPage.byId.get(10)"}),
            ("DOM.setFileInputFiles", {"files": ["/tmp/shot.png"], "objectId": "obj-1"}),
        ])


# -- the Jev connection ----------------------------------------------------


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


class FakeConnection:
    """Stands in for http.client.HTTPSConnection: one canned outcome per request."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = 0
        self.closed = False
        self.outcome = None

    def request(self, method, path, body=None, headers=None):
        self.requests += 1
        self.outcome = self.outcomes.pop(0)
        if isinstance(self.outcome, Exception):
            raise self.outcome

    def getresponse(self):
        return self.outcome

    def close(self):
        self.closed = True


class FakeJev(Jev):
    """Jev dialling canned connections instead of the network."""

    def __init__(self, connections):
        super().__init__("test-key")
        self.queue = list(connections)
        self.dialled = []

    def connect(self):
        if self.connection is None:
            self.connection = self.queue.pop(0)
            self.dialled.append(self.connection)
        return self.connection


class JevConnectionTest(unittest.TestCase):
    def answer(self):
        return FakeResponse(200, {"answers": {"ready": {"type": "noul", "noul": 0.9}},
                                  "usage": {"input_tokens": 3, "output_tokens": 1}})

    def test_the_second_call_reuses_the_connection(self):
        connection = FakeConnection([self.answer(), self.answer()])
        jev = FakeJev([connection])
        jev.ask({"step": {}}, {})
        jev.ask({"step": {}}, {})
        self.assertEqual(jev.dialled, [connection])
        self.assertEqual(connection.requests, 2)
        jev.close()
        self.assertTrue(connection.closed)
        self.assertIsNone(jev.connection)

    def test_a_dropped_connection_is_dialled_again(self):
        dropped = FakeConnection([http.client.RemoteDisconnected("closed")])
        fresh = FakeConnection([self.answer()])
        jev = FakeJev([dropped, fresh])
        result = jev.ask({"step": {}}, {})
        self.assertEqual(result["usage"], {"input_tokens": 3, "output_tokens": 1})
        self.assertEqual(jev.dialled, [dropped, fresh])
        self.assertTrue(dropped.closed)


# -- the websocket the reader thread lives on ------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(opcode, payload, final=True):
    """A frame as a server sends it: never masked, and short enough for a one-byte length."""
    assert len(payload) < 126
    return bytes([(0x80 if final else 0) | opcode, len(payload)]) + payload


def recv_exactly(conn, count):
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise ConnectionError("client went away")
        data += chunk
    return data


def read_client_frame(conn):
    """(opcode, payload, masked) of one client frame, unmasked with the key it carries."""
    head = recv_exactly(conn, 2)
    opcode, length, masked = head[0] & 0x0F, head[1] & 0x7F, bool(head[1] & 0x80)
    if length == 126:  # an evaluate of the snapshot script is kilobytes long
        length = int.from_bytes(recv_exactly(conn, 2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exactly(conn, 8), "big")
    mask = recv_exactly(conn, 4) if masked else b""
    payload = recv_exactly(conn, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return opcode, payload, masked


class FakeWsServer:
    """One connection: a fragmented event and a ping, then one reply per call — or garbage, or
    none at all (`silent`, a renderer that stopped answering), or a hang-up (`drop_after_call`).

    `results` answers a method with its own result; `unanswered` methods get no reply; a call on
    session `crash` gets Chrome's crash event for that session and no reply."""

    def __init__(self, reply_garbage=False, silent=False, drop_after_call=False, results=None,
                 unanswered=(), crash=None):
        self.reply_garbage = reply_garbage
        self.silent = silent
        self.drop_after_call = drop_after_call
        self.results = results or {}
        self.unanswered = set(unanswered)
        self.crash = crash
        self.received = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.url = f"ws://127.0.0.1:{self.listener.getsockname()[1]}/devtools/browser/fake"
        self.pongs = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.listener.close()

    def _handshake(self, conn):
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", request).group(1).decode()
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        conn.sendall("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())

    def _serve(self):
        conn, _ = self.listener.accept()
        with conn:
            self._handshake(conn)
            event = json.dumps({"method": "Page.loadEventFired", "params": {"n": 1}}).encode()
            half = len(event) // 2
            conn.sendall(server_frame(0x1, event[:half], final=False))
            conn.sendall(server_frame(0x0, event[half:]))
            conn.sendall(server_frame(0x9, b"ping-me"))
            while True:
                try:
                    opcode, payload, masked = read_client_frame(conn)
                except (ConnectionError, OSError):
                    return
                if opcode == 0x8:
                    return
                if opcode == 0xA:
                    self.pongs.append((payload, masked))
                elif opcode == 0x1:
                    call = json.loads(payload)
                    self.received.append(call["method"])
                    if self.drop_after_call:
                        return
                    if self.crash and call.get("sessionId") == self.crash:
                        crashed = {"method": "Inspector.targetCrashed", "params": {}, "sessionId": self.crash}
                        conn.sendall(server_frame(0x1, json.dumps(crashed).encode()))
                        continue
                    if self.silent or call["method"] in self.unanswered:
                        continue
                    result = self.results.get(call["method"], {"ok": call["method"]})
                    conn.sendall(server_frame(0x1, b"{not json" if self.reply_garbage else
                                              json.dumps({"id": call["id"], "result": result}).encode()))


class WebSocketReaderTest(unittest.TestCase):
    def serve(self, **kwargs):
        server = FakeWsServer(**kwargs)
        self.addCleanup(server.stop)
        return server

    def test_silence_fragments_and_pings_leave_the_connection_usable(self):
        """CDP goes quiet between commands; only call() may ever time out."""
        with mock.patch.object(cdp_module, "IDLE_TIMEOUT", 0.1):
            server = self.serve()
            cdp = Cdp(server.url, timeout=5)
            self.addCleanup(cdp.close)
            time.sleep(0.35)  # several idle timeouts of complete silence
            self.assertEqual(cdp.call("Runtime.evaluate"), {"ok": "Runtime.evaluate"})
        self.assertIsNone(cdp.closed)
        self.assertEqual([event["method"] for event in cdp.events], ["Page.loadEventFired"])
        self.assertEqual(server.pongs, [(b"ping-me", True)])  # RFC 6455: a client pong is masked

    def test_a_malformed_answer_fails_the_waiting_call_instead_of_hanging(self):
        server = self.serve(reply_garbage=True)
        cdp = Cdp(server.url, timeout=30)
        self.addCleanup(cdp.close)
        started = time.perf_counter()
        with self.assertRaises(CdpError) as caught:
            cdp.call("Runtime.evaluate")
        self.assertLess(time.perf_counter() - started, 5)
        self.assertIn("reader: JSONDecodeError", str(caught.exception))
        self.assertIn("reader: JSONDecodeError", cdp.closed)

    def test_a_dropped_connection_fails_the_waiting_call_and_every_later_one_at_once(self):
        server = self.serve(drop_after_call=True)
        cdp = Cdp(server.url, timeout=30)
        self.addCleanup(cdp.close)
        started = time.perf_counter()
        with self.assertRaises(CdpError) as caught:
            cdp.call("Runtime.evaluate")
        self.assertLess(time.perf_counter() - started, 2)
        self.assertNotIsInstance(caught.exception, StepTimeout)
        self.assertTrue(cdp.closed)
        started = time.perf_counter()
        with self.assertRaisesRegex(CdpError, "^Page.navigate: "):
            cdp.call("Page.navigate")
        self.assertLess(time.perf_counter() - started, 0.1)
        self.assertEqual(server.received, ["Runtime.evaluate"])

    def test_a_reader_that_died_fails_the_next_call_without_waiting(self):
        cdp = Cdp(self.serve(silent=True).url, timeout=30)
        self.addCleanup(cdp.close)
        cdp._fail("gone")
        started = time.perf_counter()
        with self.assertRaisesRegex(CdpError, "^Runtime.evaluate: gone$"):
            cdp.call("Runtime.evaluate")
        with self.assertRaisesRegex(CdpError, "^DOM.resolveNode: gone$"):
            cdp.call_many("DOM.resolveNode", [{}])
        self.assertLess(time.perf_counter() - started, 0.1)


class CrashedPageTest(unittest.TestCase):
    """A crashed renderer fails its tab's calls at once; cleanup never waits on an answer."""

    def connect(self, **kwargs):
        server = FakeWsServer(**kwargs)
        self.addCleanup(server.stop)
        cdp = Cdp(server.url, timeout=30)
        self.addCleanup(cdp.close)
        return server, cdp

    def test_a_release_nobody_answers_does_not_hold_up_register(self):
        server, cdp = self.connect(unanswered={"Runtime.releaseObjectGroup"}, results={
            "DOM.resolveNode": {"object": {"objectId": "o"}},
            "Runtime.callFunctionOn": {"result": {"type": "boolean", "value": True}}})
        started = time.perf_counter()
        signals.register(Tab(cdp, "t", "s"), [1, 2])
        self.assertLess(time.perf_counter() - started, 1)
        while "Runtime.releaseObjectGroup" not in server.received and time.perf_counter() - started < 1:
            time.sleep(0.01)  # sent, then the server reads it on its own thread
        self.assertEqual(server.received, ["DOM.resolveNode", "DOM.resolveNode", "Runtime.callFunctionOn",
                                           "Runtime.releaseObjectGroup"])

    def test_a_crash_fails_the_waiting_call_and_every_later_one_on_that_session(self):
        server, cdp = self.connect(crash="s")
        started = time.perf_counter()
        with self.assertRaisesRegex(PageCrashed, r"^Runtime.evaluate: the page crashed \(renderer gone\)$"):
            cdp.call("Runtime.evaluate", session_id="s")
        self.assertLess(time.perf_counter() - started, 1)
        started = time.perf_counter()
        with self.assertRaises(PageCrashed):
            cdp.call("Page.navigate", session_id="s")
        with self.assertRaises(PageCrashed):
            cdp.call_many("DOM.resolveNode", [{}], session_id="s")
        cdp.send_only("Runtime.releaseObjectGroup", session_id="s")
        self.assertLess(time.perf_counter() - started, 0.1)
        self.assertEqual(cdp.call("Target.closeTarget"), {"ok": "Target.closeTarget"})  # the browser lives
        self.assertEqual(server.received, ["Runtime.evaluate", "Target.closeTarget"])
        self.assertIsNone(cdp.closed)

    def test_observe_on_a_crashed_page_says_it_crashed(self):
        _server, cdp = self.connect(crash="s")
        tab = Tab(cdp, "t", "s")
        tab.after_input = {"kind": "press", "key": "Shift"}  # the settle wait meets the crash
        started = time.perf_counter()
        for _ in range(2):  # then a plain look
            with self.assertRaisesRegex(PageCrashed, "the page crashed"):
                observe(tab)
        self.assertLess(time.perf_counter() - started, 1)


# -- the step deadline ------------------------------------------------------


class FirstLookDriver(Driver):
    """The real driver with a canned first look, so a silent browser still gets the run to its steps."""

    def __init__(self, tab):
        super().__init__(tab)
        self.looked = False

    def observe(self, drop_targets=False, text_holders=False):
        if self.looked:
            return super().observe(drop_targets=drop_targets, text_holders=text_holders)
        self.looked = True
        return make_page([], url="http://x/home")


class StepDeadlineTest(unittest.TestCase):
    """A renderer that stops answering (an alert(), a busy loop) fails the step, not the hour."""

    def connect(self, **kwargs):
        server = FakeWsServer(**kwargs)
        self.addCleanup(server.stop)
        cdp = Cdp(server.url, timeout=30)
        self.addCleanup(cdp.close)
        return server, cdp

    def run_silent(self, script, driver_class=FirstLookDriver):
        _server, cdp = self.connect(silent=True)
        out = tempfile.mkdtemp(prefix="ck-browser-deadline-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        started = time.perf_counter()
        try:
            with mock.patch.object(runner_module, "TIMEOUT_GRACE", 0.2):
                return run(script, out, jev=StubJev(), driver=driver_class(Tab(cdp, "t", "s")),
                           step_timeout=0.5, log=lambda _line: None)
        finally:
            self.elapsed = time.perf_counter() - started
            self.run_json = json.loads(Path(out, "run.json").read_text())

    def test_a_navigation_nobody_answers_fails_the_step_within_its_budget(self):
        report = self.run_silent(script_of({"open": "http://x/app"}))
        step = report["steps"][0]
        self.assertLess(self.elapsed, 0.5 + 0.2 + 1)
        self.assertEqual((step["status"], step["timed_out"]), ("fail", True))
        self.assertEqual(step["error"], "step 1 (open: http://x/app) timed out after 0.5s on"
                                        " http://x/app, waiting on Page.navigate")
        self.assertEqual(report["settings"]["step_timeout"], 0.5)

    def test_a_look_nobody_answers_names_the_evaluate_not_an_unsettled_page(self):
        report = self.run_silent(script_of({"wait": 10}, {"click": "Save"}))
        step = report["steps"][0]
        self.assertLess(self.elapsed, 0.51 + 0.2 + 1)
        self.assertTrue(step["timed_out"])
        self.assertEqual(step["error"], "step 1 (wait: 10) timed out after 0.51s on http://x/home,"
                                        " waiting on Runtime.evaluate")
        self.assertEqual(report["steps"][1]["status"], "skipped")

    def test_opening_the_scripts_url_shares_the_budget(self):
        with self.assertRaises(StepTimeout) as caught:
            self.run_silent(script_of({"wait": 10}, url="http://x/app"), driver_class=Driver)
        self.assertLess(self.elapsed, 0.5 + 0.2 + 1)
        message = "opening http://x/app timed out after 0.5s, waiting on Page.navigate"
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.run_json["error"], message)

    def test_a_deadline_already_passed_sends_nothing(self):
        server, cdp = self.connect()
        cdp.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(StepTimeout, "^Page.navigate: no answer before the step deadline$"):
            cdp.call("Page.navigate")
        with self.assertRaises(StepTimeout):
            cdp.call_many("DOM.resolveNode", [{}])
        self.assertIsNone(cdp.waiting_on)  # nothing was waited on
        cdp.deadline = None
        self.assertEqual(cdp.call("Runtime.evaluate"), {"ok": "Runtime.evaluate"})
        self.assertEqual(server.received, ["Runtime.evaluate"])

    def test_the_deadline_cuts_a_wait_and_names_the_method(self):
        _server, cdp = self.connect(silent=True)
        for call in (lambda: cdp.call("Runtime.evaluate"),
                     lambda: cdp.call_many("DOM.resolveNode", [{}, {}])):
            cdp.deadline = time.monotonic() + 0.2
            started = time.perf_counter()
            with self.assertRaises(StepTimeout):
                call()
            self.assertLess(time.perf_counter() - started, 1)
        self.assertEqual(cdp.waiting_on, "DOM.resolveNode")
        self.assertIsNone(cdp.closed)  # the connection is fine; the page is not

    def test_a_calls_own_timeout_is_not_the_steps(self):
        _server, cdp = self.connect(silent=True)
        cdp.deadline = time.monotonic() + 30
        with self.assertRaisesRegex(CdpError, "^Runtime.evaluate: no answer in 0.2s$") as caught:
            cdp.call("Runtime.evaluate", timeout=0.2)
        self.assertNotIsInstance(caught.exception, StepTimeout)
        self.assertIsNone(cdp.waiting_on)

    def test_a_timed_out_step_dismisses_the_dialog_and_says_so(self):
        class Blocked(FakeDriver):
            def __init__(self, pages):
                super().__init__(pages)
                self.deadlines = []

            def deadline(self, at):
                self.deadlines.append(at)

            def act(self, action, page, text=None):
                raise StepTimeout("Input.dispatchKeyEvent: no answer before the step deadline")

            def waiting_on(self):
                return "Input.dispatchKeyEvent"

            def unstick(self):
                return 'a JavaScript alert is open: "hi" (dismissed)'

        out = tempfile.mkdtemp(prefix="ck-browser-deadline-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        driver = Blocked([make_page([])])
        report = run(script_of({"press": "Enter"}), out, jev=StubJev(), driver=driver, step_timeout=7,
                     log=lambda _line: None)
        step = report["steps"][0]
        self.assertEqual(step["error"], 'step 1 (press: Enter) timed out after 7s on http://fake/,'
                                        ' waiting on Input.dispatchKeyEvent;'
                                        ' a JavaScript alert is open: "hi" (dismissed)')
        self.assertTrue(step["failure_screenshot"])
        self.assertEqual(step["near_misses"], [])
        # first look, the step, the grace for the dialog and screenshot: each set, then lifted
        self.assertEqual(driver.deadlines[1::2], [None, None, None])
        self.assertTrue(all(at is not None for at in driver.deadlines[0::2]))

    def test_jev_is_not_asked_once_the_step_is_out_of_time(self):
        out = tempfile.mkdtemp(prefix="ck-browser-deadline-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        jev = ScriptedJev(ready=0.1, target={"1": 1.0, "none": 0.0})
        driver = FakeDriver([make_page([action(1, "click", "Upload product")])])
        started = time.perf_counter()
        report = run(script_of({"click": "Upload product"}), out, jev=jev, driver=driver,
                     step_timeout=0.25, log=lambda _line: None)
        step = report["steps"][0]
        self.assertLess(time.perf_counter() - started, 1)
        self.assertTrue(step["timed_out"])
        self.assertEqual(step["error"], "step 1 (click: Upload product) timed out after 0.25s on"
                                        " http://fake/, waiting on Jev")
        self.assertLessEqual(jev.requests, 4)


# -- navigation failures ----------------------------------------------------


class FakeCdp:
    """A Cdp that answers every call with the same canned result."""

    calls = 0

    def __init__(self, result):
        self.result = result
        self.methods = []

    def call(self, method, params=None, session_id=None, timeout=None):
        self.methods.append(method)
        return self.result


class NavigateTest(unittest.TestCase):
    def test_a_refused_navigation_raises_with_chromes_reason(self):
        cdp = FakeCdp({"frameId": "F", "errorText": "net::ERR_UNSAFE_PORT"})
        tab = Tab(cdp, "target-1", "session-1")
        with self.assertRaises(CdpError) as caught:
            tab.navigate("http://127.0.0.1:6000/app")
        self.assertEqual(str(caught.exception),
                         "navigate http://127.0.0.1:6000/app: net::ERR_UNSAFE_PORT")
        self.assertEqual(cdp.methods, ["Page.navigate"])  # no waiting for a load that never starts


class DialogCdp(FakeCdp):
    """Events as the reader stored them; every call is recorded and answered empty."""

    def __init__(self, *events):
        super().__init__({})
        self.events = [{"method": method, "params": params, "sessionId": session}
                       for method, params, session in events]
        self.sent = []

    def peek_events(self, session_id):
        return [e for e in self.events if e["sessionId"] == session_id]

    def call(self, method, params=None, session_id=None, timeout=None):
        self.sent.append((method, params, session_id))
        return {}


class DialogTest(unittest.TestCase):
    OPENING = ("Page.javascriptDialogOpening", {"type": "alert", "message": "hi"}, "s")

    def test_an_open_dialog_is_dismissed_and_described(self):
        cdp = DialogCdp(self.OPENING)
        self.assertEqual(Tab(cdp, "t", "s").dismiss_dialog(), 'a JavaScript alert is open: "hi" (dismissed)')
        self.assertEqual(cdp.sent, [("Page.handleJavaScriptDialog", {"accept": False}, "s")])
        self.assertEqual(len(cdp.events), 1)  # left for drain_events

    def test_no_open_dialog_is_none(self):
        closed = DialogCdp(self.OPENING, ("Page.javascriptDialogClosed", {"result": True}, "s"))
        elsewhere = DialogCdp(("Page.javascriptDialogOpening", {"type": "alert", "message": "hi"}, "other"))
        for cdp in (closed, elsewhere):
            self.assertIsNone(Tab(cdp, "t", "s").dismiss_dialog())
            self.assertEqual(cdp.sent, [])

    def test_a_refused_dismissal_still_describes_the_dialog(self):
        class Refusing(DialogCdp):
            def call(self, method, params=None, session_id=None, timeout=None):
                raise StepTimeout(f"{method}: no answer before the step deadline")

        self.assertEqual(Tab(Refusing(self.OPENING), "t", "s").dismiss_dialog(),
                         'a JavaScript alert is open: "hi" (still open)')


# -- the CLI ----------------------------------------------------------------


class CliTest(unittest.TestCase):
    def test_a_browser_failure_is_reported_and_exits_2(self):
        out = Path(tempfile.mkdtemp(prefix="ck-browser-cli-"))
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        script = out / "steps.yaml"
        script.write_text("steps:\n  - click: Save\n")

        def boom(*_args, **_kwargs):
            raise StalePage("Page did not settle")

        with mock.patch.object(cli, "run", boom), contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["run", str(script), "--out", str(out), "--stub"])
        self.assertEqual(code, 2)
        # last line, not the whole capture: a ResourceWarning for another
        # test's GC'd sqlite connection can land in the same stderr window
        self.assertEqual(err.getvalue().strip().splitlines()[-1],
                         "ck-browser: Page did not settle")

    def test_step_timeout_reaches_the_run_and_must_be_positive(self):
        out = Path(tempfile.mkdtemp(prefix="ck-browser-cli-"))
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        script = out / "steps.yaml"
        script.write_text("steps:\n  - click: Save\n")
        seen = []

        def stop(*_args, **kwargs):
            seen.append(kwargs["step_timeout"])
            raise StalePage("Page did not settle")

        with mock.patch.object(cli, "run", stop), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["run", str(script), "--out", str(out), "--stub", "--step-timeout", "7.5"])
            cli.main(["run", str(script), "--out", str(out), "--stub"])
        self.assertEqual(seen, [7.5, 60])
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
            cli.main(["run", str(script), "--out", str(out), "--stub", "--step-timeout", "0"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--step-timeout must be more than 0 seconds", err.getvalue())


# -- finding and launching our Chrome --------------------------------------


class ChromeLaunchTest(unittest.TestCase):
    def test_chrome_binary_honours_the_env_override(self):
        os.environ["CK_BROWSER_CHROME"] = "/opt/my-chrome"
        self.addCleanup(os.environ.pop, "CK_BROWSER_CHROME", None)
        self.assertEqual(chrome_binary(), "/opt/my-chrome")

    def test_chromes_own_default_port_is_never_touched(self):
        """9222 is the human's browser: refuse before any connection is attempted."""
        with mock.patch.object(cdp_module, "browser_ws", side_effect=AssertionError("probed 9222")):
            with self.assertRaises(CdpError) as caught:
                ensure_chrome(port=9222)
        self.assertEqual(str(caught.exception),
                         "port 9222 is Chrome's default remote-debugging port;"
                         " ck-browser only drives its own instance — pick another port")

    def test_no_launch_reports_no_verified_chrome(self):
        profile = Path(tempfile.mkdtemp(prefix="ck-browser-profile-")).resolve()
        self.addCleanup(shutil.rmtree, profile, ignore_errors=True)
        with self.assertRaises(CdpError) as caught:
            ensure_chrome(profile_dir=profile, launch=False)
        self.assertEqual(str(caught.exception),
                         f"no verified Chrome for profile {profile}: no readable DevToolsActivePort")

    def test_a_launch_lets_chrome_pick_its_port_after_dropping_a_stale_one(self):
        profile = Path(tempfile.mkdtemp(prefix="ck-browser-profile-")).resolve()
        self.addCleanup(shutil.rmtree, profile, ignore_errors=True)
        profile.chmod(0o755)
        active = profile / "DevToolsActivePort"
        active.write_text("9333\n/devtools/browser/old\n")
        stale_at_launch = []

        def popen(command, **_kwargs):
            stale_at_launch.append(active.exists())
            return mock.Mock(pid=7000)

        with mock.patch.object(cdp_module, "browser_ws",
                               side_effect=[CdpError("no verified Chrome"), "ws://127.0.0.1:50123/devtools/browser/x"]), \
                mock.patch.object(cdp_module, "chrome_binary", return_value="/bin/chrome"), \
                mock.patch.object(cdp_module.subprocess, "Popen", side_effect=popen) as launched, \
                mock.patch.object(cdp_module, "_ensure_watchdog"), \
                mock.patch.object(cdp_module, "LAUNCHED", []):
            self.assertEqual(ensure_chrome(profile_dir=profile), "ws://127.0.0.1:50123/devtools/browser/x")
        command = launched.call_args.args[0]
        self.assertIn("--remote-debugging-port=0", command)
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertEqual(stale_at_launch, [False])
        self.assertEqual(profile.stat().st_mode & 0o777, 0o700)  # cookies under a mock keychain
        self.assertEqual((profile / "chrome.log").stat().st_mode & 0o777, 0o600)


class BrowserWsTest(unittest.TestCase):
    """Only the live process that owns the profile dir, runs as us and listens is ever trusted."""

    PID = 4242

    def setUp(self):
        self.profile = Path(tempfile.mkdtemp(prefix="ck-browser-profile-")).resolve()
        self.addCleanup(shutil.rmtree, self.profile, ignore_errors=True)
        (self.profile / "DevToolsActivePort").write_text("50123\n/devtools/browser/abc\n")
        os.symlink(f"host.local-{self.PID}", self.profile / "SingletonLock")
        self.alive = True
        self.ps = f"  {os.getuid()} /bin/chrome --remote-debugging-port=0 --user-data-dir={self.profile} --no-first-run\n"
        self.lsof = f"{self.PID}\n"
        self.commands = []

    def lookup(self):
        def run(command, **_kwargs):
            self.commands.append(command)
            return mock.Mock(stdout=self.ps if command[0] == "ps" else self.lsof)

        with mock.patch.object(cdp_module, "_alive", lambda pid: self.alive and pid == self.PID), \
                mock.patch.object(cdp_module.shutil, "which", return_value="/usr/sbin/lsof"), \
                mock.patch.object(cdp_module.subprocess, "run", run):
            return cdp_module.browser_ws(self.profile)

    def assertRefused(self, why):
        with self.assertRaises(CdpError) as caught:
            self.lookup()
        self.assertEqual(str(caught.exception), f"no verified Chrome for profile {self.profile}: {why}")

    def test_a_verified_owner_gives_its_websocket(self):
        self.assertEqual(self.lookup(), "ws://127.0.0.1:50123/devtools/browser/abc")
        self.assertEqual(self.commands, [
            ["ps", "-p", str(self.PID), "-ww", "-o", "uid=,command="],
            ["/usr/sbin/lsof", "-nP", "-a", "-p", str(self.PID), "-iTCP:50123", "-sTCP:LISTEN", "-t"],
        ])

    def test_no_active_port_file(self):
        (self.profile / "DevToolsActivePort").unlink()
        self.assertRefused("no readable DevToolsActivePort")

    def test_a_dead_pid(self):
        self.alive = False
        self.assertRefused(f"pid {self.PID} is not running")

    def test_a_pid_of_another_user(self):
        self.ps = f"  {os.getuid() + 1} /bin/chrome --user-data-dir={self.profile}\n"
        self.assertRefused(f"pid {self.PID} is not running as this user")

    def test_a_pid_on_another_profile_dir(self):
        self.ps = f"  {os.getuid()} /bin/chrome --user-data-dir={self.profile}-other\n"
        self.assertRefused(f"pid {self.PID} is not a Chrome on this profile dir")

    def test_a_live_owner_not_listening_on_the_port(self):
        self.lsof = ""
        self.assertRefused(f"pid {self.PID} is not listening on port 50123")

    def test_chromes_default_port_is_never_ours(self):
        (self.profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")
        self.assertRefused("it names port 9222, Chrome's default: never ours")
        self.assertEqual(self.commands, [])


class OutPrivacyTest(unittest.TestCase):
    """--out holds full page text: the dir is this user's only, and so is the Jev trace."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ck-browser-out-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(os.umask, os.umask(0o022))

    def test_a_run_writes_a_private_dir_and_trace(self):
        out = self.root / "out"
        page = make_page([action(1, "click", "Upload product")])
        run(script_of({"click": "Upload product"}), out, jev=RecordingJev(StubJev(), out),
            driver=FakeDriver([page]), log=lambda _line: None)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / "jev-trace.jsonl").stat().st_mode & 0o777, 0o600)

    def test_the_cli_creates_a_private_out_dir(self):
        script = self.root / "steps.yaml"
        script.write_text("steps:\n  - click: Save\n")
        out = self.root / "cli-out"
        with mock.patch.object(cli, "run", side_effect=CdpError("stop")), \
                mock.patch.object(cli, "release_chrome"), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["run", str(script), "--out", str(out), "--stub"])
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)


# -- the fixture page in our own Chrome ------------------------------------


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class SlowJev(StubJev):
    """The stub at Jev's real pace: a page that polls moves on while each request is out."""

    def ask(self, state, questions):
        time.sleep(0.4)
        return super().ask(state, questions)


class FixtureTest(unittest.TestCase):
    """End to end against the dedicated headless Chrome, which is left running afterwards."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.ws_url = ensure_chrome(profile_dir=PROFILE_DIR, headless=True)
        except CdpError as exc:
            raise unittest.SkipTest(str(exc))
        handler = functools.partial(QuietHandler, directory=str(FIXTURES))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/form.html"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="ck-browser-live-"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def write_script(self, steps):
        path = self.out / "steps.yaml"
        path.write_text("steps:\n" + "".join(f"  - {line}\n" for line in steps))
        script = load_script(path)
        script["path"] = str(path)
        return script

    def happy_script(self):
        return self.write_script([
            f'open: {self.url}',
            'click: "Upload product"',
            'fill: { field: "Product name", value: "Test kurta" }',
            'select: { field: Category, value: Kurta }',
            f'fill: {{ field: Password, value: {PASSWORD} }}',
            'click: Save',
            'expect: "Saved"',
            'expect: "Image viewer"',            # only readable through an aria-label
            'expect_count: { card: 3 }',
            'fill: { field: Search, value: abc }',
            'press: Enter',
            'expect: "Searched: abc"',
        ])

    def check_pass(self, report):
        failures = [(s["n"], s["step"], s["error"]) for s in report["steps"] if s["status"] != "pass"]
        self.assertEqual(failures, [], format_report(report))

    def test_happy_path_with_the_stub(self):
        report = run(self.happy_script(), self.out, jev=StubJev(), ws_url=self.ws_url,
                     log=lambda _line: None)
        self.check_pass(report)
        self.assertEqual(report["steps"][1]["target"]["label"], "Upload product")
        self.assertEqual(report["steps"][2]["target"]["label"], "Product name")
        self.assertEqual(report["steps"][3]["target"]["label"], "Category")
        self.assertEqual(report["steps"][4]["target"]["label"], "Password")
        self.assertEqual(report["steps"][8]["count"], 3)
        self.assertTrue((self.out / "run.json").exists())
        self.assertTrue(list(self.out.glob("*.png")))
        self.assertGreater(report["totals"]["cdp_calls"], 0)

    def test_the_typed_password_never_reaches_jev_or_run_json(self):
        out = self.out
        run(self.happy_script(), out, jev=RecordingJev(StubJev(), out), ws_url=self.ws_url,
            log=lambda _line: None)
        trace = (out / "jev-trace.jsonl").read_text()
        self.assertNotIn(PASSWORD, trace)
        self.assertNotIn(PASSWORD, (out / "run.json").read_text())
        entries = [json.loads(line) for line in trace.splitlines()]
        # expect states are page-only, so only the target steps carry elements at all
        values = {element["value"] for entry in entries
                  for element in entry["state"].get("elements", [])
                  if element["label"].endswith("Password")}
        self.assertIn("••••", values)

    def test_upload_hover_and_drag_in_chrome(self):
        shot = self.out / "shot.png"
        shot.write_bytes(b"\x89PNG fixture")
        script = self.write_script([
            f'open: {self.url}',
            f'upload: {{ field: "Browse images", path: {shot} }}',
            'expect: "Picked: shot.png"',          # setFileInputFiles fires change itself
            'hover: "Regenerate"',
            'expect: "Regenerating soon"',
            'drag: { from: "View A shot", to: "Archive bin" }',
            'expect: "Passed the waypoint"',      # the moves changed the page on their way
            'expect: "Archived: View A shot"',    # and the drop still landed
        ])
        report = run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None)
        self.check_pass(report)
        self.assertEqual(report["steps"][1]["target"]["label"], "Browse images")
        self.assertEqual(report["steps"][3]["target"]["label"], "Regenerate")
        self.assertEqual(report["steps"][5]["drag_from"]["label"], "View A shot")
        self.assertEqual(report["steps"][5]["drag_to"]["label"], "Archive bin")

    def test_a_drag_between_two_plain_divs(self):
        """Neither end is interactable: a draggable div onto a div that handles the drop."""
        script = self.write_script([f'open: {self.url}',
                                    'drag: { from: "Tile X", to: "Front" }',
                                    'expect: "Placed: Tile X in Front"'])
        report = run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None)
        self.check_pass(report)
        step = report["steps"][1]
        self.assertEqual((step["drag_from"]["role"], step["drag_from"]["label"]), ("draggable", "Tile X"))
        self.assertEqual((step["drag_to"]["role"], step["drag_to"]["label"]), ("text", "Front"))
        self.assertEqual(step["drag_mode"], "dnd")  # the pointer drag started, nothing dropped

    def test_the_button_under_an_overlay_is_never_offered(self):
        """Both bars carry the same label; only the panel on top can be clicked."""
        script = self.write_script([f'open: {self.url}', 'click: "Open client list"',
                                    'expect: "Panel list opened"'])
        report = run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None)
        self.check_pass(report)
        labels = [c["label"] for c in report["steps"][1]["candidates"]]
        self.assertEqual(labels.count("Open client list"), 1)  # not the identical one underneath

    def open_fixture(self, name):
        cdp = Cdp(self.ws_url)
        self.addCleanup(cdp.close)
        tab = Tab.open(cdp)
        self.addCleanup(tab.close)
        tab.navigate(self.url.replace("form.html", name))
        return tab

    def test_a_button_a_panel_clips_sideways_is_clicked(self):
        """A wide table's last column: past the window's edge, or clipped by a narrow panel. Offered
        from the first look (one Jev request, no fallback) and scrolled into view by the click."""
        url = self.url.replace("form.html", "wide.html")
        script = self.write_script([f'open: {url}', 'click: "Edit Acme"', 'expect: "Clicked: Edit Acme"',
                                    'click: "Open Lookbook"', 'expect: "Clicked: Open Lookbook"'])
        report = run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None)
        self.check_pass(report)
        for step in (report["steps"][1], report["steps"][3]):
            self.assertEqual(step["target"]["role"], "button")
            self.assertEqual((step["fallback"], step["jev"]["requests"]), (None, 1))

    def test_rows_off_to_the_side_are_not_offered_under_a_dialog(self):
        tab = self.open_fixture("wide.html")
        tab.evaluate("document.getElementById('show').click()")
        labels = [a["label"] for a in observe(tab)["actions"]]
        self.assertIn("Close dialog", labels)
        self.assertFalse({"Edit Acme", "Edit Globex", "Open Lookbook"} & set(labels), labels)

    def test_a_page_wider_than_the_window_offers_only_rows_off_to_the_side(self):
        """No panel scrolls, the page does: a table row's last cell is offered, a closed drawer
        parked past the edge is not — it sits in no row on screen."""
        tab = self.open_fixture("widepage.html")
        offered = {a["label"]: a.get("aside") for a in observe(tab)["actions"]}
        self.assertEqual(offered.get("Edit Acme"), True)
        self.assertNotIn("Save drawer", offered)
        url = self.url.replace("form.html", "widepage.html")
        script = self.write_script([f'open: {url}', 'click: "Edit Acme"', 'expect: "Clicked: Edit Acme"'])
        self.check_pass(run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None))

    def test_a_drag_whose_ends_never_fit_on_screen_together_presses_nothing(self):
        """Scrolling the target in slides the source away: pressing its old point would drag
        whichever card moved under it."""
        url = self.url.replace("form.html", "board.html")
        script = self.write_script([f'open: {url}', 'drag: { from: "Card One", to: "Card Six" }',
                                    'expect: "Moved: Card One to Staging"'])
        report = run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None)
        step = report["steps"][1]
        self.assertEqual(step["status"], "fail")
        self.assertIn("not on screen together", step["error"])
        self.assertIn("Nothing moved", step["observations"]["text_head"] + " " + json.dumps(step))
        # Ends a sideways scroll brings on screen together: the source is pressed where it slid to.
        script = self.write_script([f'open: {url}', 'drag: { from: "Card Four", to: "Card Six" }',
                                    'expect: "Moved: Card Four to Staging"'])
        self.check_pass(run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None))

    def test_a_page_that_polls_reads_as_still(self):
        """A status line rewritten every 300 ms moves the text, not the controls."""
        tab = self.open_fixture("polling.html")
        first = observe(tab)
        time.sleep(0.7)
        second = observe(tab)
        self.assertNotEqual(first["text"], second["text"])
        self.assertEqual(first["marker"], second["marker"])
        # The looks that offer text holders too: the ticking line is one, its label left out.
        first, _ = observe(tab, text_holders=True), time.sleep(0.7)
        self.assertEqual(first["marker"], observe(tab, text_holders=True)["marker"])

    def test_a_page_that_polls_still_takes_a_fill_and_a_click(self):
        """Jev takes a few hundred ms per request; the poll inside the form ticks meanwhile."""
        url = self.url.replace("form.html", "polling.html")
        script = self.write_script([f'open: {url}', 'fill: { field: Name, value: Globex }',
                                    'click: "Save client"', 'expect: "Saved client Globex"'])
        report = run(script, self.out, jev=SlowJev(), ws_url=self.ws_url, log=lambda _line: None)
        self.check_pass(report)
        self.assertEqual([s["jev"]["requests"] for s in report["steps"][1:3]], [1, 1])

    def test_an_icon_only_control_is_named_after_what_is_beside_it(self):
        script = self.write_script([f'open: {self.url}',
                                    'click: "unlabelled button near Credit Type"',
                                    'expect: "Credit Type explained"'])
        report = run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None)
        self.check_pass(report)
        self.assertEqual(report["steps"][1]["target"]["label"], "(unlabelled button) near: Credit Type")

    def observe_picker(self):
        cdp = Cdp(self.ws_url)
        self.addCleanup(cdp.close)
        tab = Tab.open(cdp)
        self.addCleanup(tab.close)
        tab.navigate(self.url.replace("form.html", "picker.html"))
        return observe(tab, drop_targets=True)

    def test_options_without_a_role_are_offered(self):
        """react-select: the active descendant and its siblings, and ids like react-select-9-option-0."""
        page = self.observe_picker()
        options = [a["label"] for a in page["actions"] if a.get("role") == "option"]
        self.assertEqual(options, ["backups", "archive", "logs", "media"])
        script = self.write_script([f'open: {self.url.replace("form.html", "picker.html")}',
                                    'click: archive', 'expect: "Bucket: archive"',
                                    'click: logs', 'expect: "Bucket: logs"'])
        report = run(script, self.out, jev=StubJev(), ws_url=self.ws_url, log=lambda _line: None)
        self.check_pass(report)

    def test_no_cut_splits_a_token(self):
        """A 64-character key reaches the mask whole; words are still cut at 40 and 6000."""
        token = "ead1ef0ad9fc71a9c4eb9a3315887bf4b8bc23ffa16002999c4af8fc7ed55f92"
        page = self.observe_picker()
        labels = [a["label"] for a in page["actions"] if a.get("role") == "text"]
        self.assertIn(token, labels)
        self.assertIn("Applies to every bucket in this account,", labels)
        self.assertEqual(page["text"], " ".join(["lorem"] * 998) + "\n" + token)

    def test_failed_expectation_stops_the_run(self):
        script = self.write_script([f'open: {self.url}', 'expect: "Cancelled"', 'click: "Upload product"'])
        report = run(script, self.out, jev=StubJev(), ws_url=self.ws_url, settle_tries=1,
                     log=lambda _line: None)
        self.assertEqual([s["status"] for s in report["steps"]], ["pass", "fail", "skipped"])
        self.assertIn("expectation not met: 'Cancelled'", report["steps"][1]["error"])
        self.assertTrue(Path(report["steps"][1]["screenshot"]).exists())
        self.assertTrue(Path(report["steps"][1]["failure_screenshot"]).exists())
        self.assertTrue(report["steps"][1]["failure_screenshot"].endswith("-FAIL.png"))

    @unittest.skipUnless(os.environ.get("TYPESAFE_API_KEY"), "TYPESAFE_API_KEY not set")
    def test_happy_path_with_live_jev(self):
        report = run(self.happy_script(), self.out, jev=Jev(os.environ["TYPESAFE_API_KEY"]),
                     ws_url=self.ws_url, log=lambda _line: None)
        self.check_pass(report)
        # One request per decision step; a step the model finds unsettled costs another.
        self.assertGreaterEqual(report["totals"]["jev_requests"], 7)
        self.assertGreater(report["totals"]["input_tokens"], 0)


if __name__ == "__main__":
    unittest.main()

"""ck-browser dropdowns: hidden native selects, role-less triggers, shadow roots, near misses, key chords."""

import functools
import http.server
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ck_browser.cdp import Cdp, CdpError, Tab, ensure_chrome
from ck_browser.page import _key, act, near_misses, observe, visible_strings
from ck_browser.report import format_report
from ck_browser.runner import run
from tests.test_jevbrowser import ExactJev, script_of

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jevbrowser"
# Our own Chrome on its own profile dir, never the human's — a websocket there pops up
# "Allow remote debugging?".
PROFILE_DIR = os.environ.get("CK_BROWSER_PROFILE_DIR")


class KeyTab:
    """Records the CDP calls a key press makes."""

    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


class KeyTest(unittest.TestCase):
    def press(self, key):
        tab = KeyTab()
        _key(tab, key)
        self.assertEqual([method for method, _ in tab.calls], ["Input.dispatchKeyEvent"] * 2)
        return [params for _, params in tab.calls]

    def test_shift_tab_sends_the_modifier_and_no_text(self):
        down, up = self.press("Shift+Tab")
        self.assertEqual((down["key"], down["code"], down["modifiers"], up["modifiers"]), ("Tab", "Tab", 8, 8))
        self.assertNotIn("text", down)

    def test_a_control_chord_types_no_text(self):
        down, up = self.press("Control+a")
        self.assertEqual((down["key"], down["code"], down["windowsVirtualKeyCode"], down["modifiers"]),
                         ("a", "KeyA", 65, 2))
        self.assertEqual(up["modifiers"], 2)
        self.assertNotIn("text", down)

    def test_a_plain_letter_types_itself(self):
        down, up = self.press("a")
        self.assertEqual((down["key"], down["code"], down["text"]), ("a", "KeyA", "a"))
        self.assertNotIn("modifiers", down)
        self.assertEqual(up["type"], "keyUp")

    def test_shift_upper_cases_the_letter(self):
        down, _ = self.press("Shift+a")
        self.assertEqual((down["key"], down["text"], down["modifiers"]), ("A", "A", 8))

    def test_a_digit_and_a_named_key(self):
        down, _ = self.press("7")
        self.assertEqual((down["code"], down["windowsVirtualKeyCode"], down["text"]), ("Digit7", 55, "7"))
        down, _ = self.press("Delete")
        self.assertEqual((down["key"], down["code"], down["windowsVirtualKeyCode"]), ("Delete", "Delete", 46))

    def test_unknown_modifiers_and_keys_are_refused(self):
        for key in ("Hyper+x", "F13", "Shift+F13", "ab"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                _key(KeyTab(), key)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def fixed_list_only(test):
    """Runs a test as a tab whose Chrome signals are unreadable (an extension not yet reloaded):
    the fixed list and the text-holder fallback alone. With the signals on, the role-less trigger
    listens for clicks and is offered directly (tests/test_jevbrowser_signals.py)."""
    call, evaluate = Tab.call, Tab.evaluate

    def refusing(tab, method, **params):
        if method == "Accessibility.getFullAXTree":
            raise CdpError("Accessibility.getFullAXTree: 'Accessibility.getFullAXTree' wasn't found")
        return call(tab, method, **params)

    def no_listeners(tab, expression, await_promise=False, command_line_api=False):
        return evaluate(tab, expression, await_promise)

    @functools.wraps(test)
    def wrapper(self):
        with mock.patch.object(Tab, "call", refusing), mock.patch.object(Tab, "evaluate", no_listeners):
            test(self)
    return wrapper


class DropdownFixtureTest(unittest.TestCase):
    """dropdowns.html (a GoDaddy-like New Records form) in the dedicated headless Chrome."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.ws_url = ensure_chrome(profile_dir=PROFILE_DIR, headless=True)
        except CdpError as exc:
            raise unittest.SkipTest(str(exc))
        handler = functools.partial(QuietHandler, directory=str(FIXTURES))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/dropdowns.html"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def open(self):
        cdp = Cdp(self.ws_url)
        self.addCleanup(cdp.close)
        self.tab = Tab.open(cdp)
        self.addCleanup(self.tab.close)
        self.tab.navigate(self.url)
        return observe(self.tab)

    def selects(self, page, field):
        return [a for a in page["actions"] if a["kind"] == "select" and a["label"].startswith(field + " → ")]

    def pick(self, page, label):
        return next(a for a in page["actions"] if a["label"] == label)

    @fixed_list_only
    def test_hidden_selects_are_offered_through_their_box(self):
        page = self.open()
        for field, options in (("TTL", ["1 Hour", "12 Hours", "1 Day", "1 Week"]),
                               ("Priority", ["Medium", "High"]), ("Speed", ["Fast"])):
            with self.subTest(field=field):
                offered = self.selects(page, field)
                self.assertEqual([a["value"] for a in offered], options)
                self.assertTrue(all(a.get("proxied") and a["role"] == "combobox" for a in offered))
        region = self.selects(page, "Region")
        self.assertEqual([a["value"] for a in region], ["EU", "Asia"])
        self.assertFalse(any(a.get("proxied") for a in region))
        self.assertIn("Choose a protocol", [a["label"] for a in page["actions"] if a.get("role") == "button"])
        self.assertNotIn("Choose an option", [a["label"] for a in page["actions"]])
        self.assertFalse([a for a in page["actions"] if a.get("role") == "text"])

    def test_a_select_in_a_collapsed_or_hidden_panel_is_not_offered(self):
        page = self.open()
        for field in ("Shipping speed", "Return speed", "Filter age"):
            with self.subTest(field=field):
                bottom = self.tab.evaluate(
                    f"document.querySelector('select[aria-label=\"{field}\"]').getBoundingClientRect().bottom")
                self.assertLess(bottom, self.tab.evaluate("innerHeight"))  # in view: dropped for the panel
                self.assertFalse([a for a in page["actions"] if a["label"].startswith(field)])
        self.assertTrue([a for a in page["actions"] if a["label"] == "Shipping" and a["role"] == "button"])

    def test_a_proxied_select_with_nothing_before_its_box_is_named_by_its_role(self):
        page = self.open()
        size = [a for a in page["actions"] if a["kind"] == "select" and a["value"] == "Large"]
        self.assertEqual([(a["label"], a.get("proxied")) for a in size], [("combobox → Large", True)])

    def test_bare_text_in_a_shadow_root_is_read(self):
        page = self.open()
        self.assertIn("Bare shadow text", page["text"])
        self.assertIn("Bare shadow text", visible_strings(self.tab))

    @fixed_list_only
    def test_text_holders_offer_the_role_less_trigger(self):
        self.open()
        page = observe(self.tab, text_holders=True)
        trigger = [a for a in page["actions"] if a["label"] == "Choose an option"]
        self.assertEqual([(a["role"], a["kind"], a.get("pointer")) for a in trigger], [("text", "click", True)])
        self.assertFalse([a for a in page["actions"] if a.get("role") == "draggable"])
        self.assertNotIn("pointer", self.pick(page, "New Records"))

    def test_a_proxied_select_and_a_shadow_select_take_a_value(self):
        page = self.open()
        act(self.tab, self.pick(page, "TTL → 1 Hour"), page)
        self.assertIn("TTL: 1 Hour", visible_strings(self.tab))
        page = observe(self.tab)
        act(self.tab, self.pick(page, "Region → EU"), page)
        self.assertIn("Region: EU", visible_strings(self.tab))

    def test_a_shadow_root_dropdown_is_operable_and_its_text_is_read(self):
        page = self.open()
        act(self.tab, self.pick(page, "Choose a protocol"), page)
        page = observe(self.tab)
        self.assertEqual([a["label"] for a in page["actions"] if a.get("role") == "option"], ["TCP", "UDP"])
        act(self.tab, self.pick(page, "TCP"), page)
        strings = visible_strings(self.tab)
        self.assertIn("Protocol: TCP", strings)
        self.assertIn("Shadow says TCP", strings)
        self.assertIn("Shadow says TCP", observe(self.tab)["text"])

    def test_a_select_under_an_overlay_is_not_offered(self):
        self.open()
        self.tab.evaluate("document.getElementById('overlay').style.display='block'")
        page = observe(self.tab)
        self.assertFalse([a for a in page["actions"] if a["kind"] == "select"])

    @fixed_list_only
    def test_near_misses_say_why_not(self):
        self.open()
        first = near_misses(self.tab, "Type dropdown showing 'Choose an option'")[0]
        self.assertEqual((first["tag"], first["text"], first["offered"]), ("div", "Choose an option", True))
        self.assertIn("text holder", first["why"])
        ttl = near_misses(self.tab, "1/2 Hour")
        span = next(m for m in ttl if m["tag"] == "span")
        self.assertEqual((span["visible"], span["hit"], span["offered"]), (True, False, False))
        self.assertTrue(span["why"].startswith("covered by"), span["why"])
        select = next(m for m in ttl if m["tag"] == "select")
        self.assertTrue(select["offered"])
        self.assertTrue(select["why"].startswith("hidden native select (opacity 0)"), select["why"])
        save = near_misses(self.tab, "Save")[0]
        self.assertEqual((save["tag"], save["offered"], save["why"]), ("button", False, "disabled"))
        region = near_misses(self.tab, "Region")
        self.assertIn("dns-region", [m["shadow_host"] for m in region])
        self.assertTrue(all(m["shadow_host"] is None for m in near_misses(self.tab, "Cancel")))


class DropdownRunTest(unittest.TestCase):
    """dropdowns.html through the whole step loop: what a script names, the runner reaches."""

    setUpClass = classmethod(DropdownFixtureTest.setUpClass.__func__)
    tearDownClass = classmethod(DropdownFixtureTest.tearDownClass.__func__)

    def run_steps(self, *steps, settle_tries=2):
        out = tempfile.mkdtemp(prefix="ck-browser-dropdowns-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        script = script_of({"open": self.url}, *steps)
        return run(script, out, jev=ExactJev(), ws_url=self.ws_url,
                   settle_tries=settle_tries, log=lambda _line: None)

    def assertPassed(self, report):
        failed = [step for step in report["steps"] if step["status"] != "pass"]
        self.assertFalse(failed, failed and failed[0]["error"])

    def test_hidden_native_selects_take_a_value_by_their_label(self):
        for field, value, shown in (("TTL", "1 Hour", "TTL: 1 Hour"), ("Priority", "High", "Priority: High"),
                                    ("Region", "EU", "Region: EU"), ("Speed", "Fast", "Speed: Fast")):
            with self.subTest(field=field):
                report = self.run_steps({"select": {"field": field, "value": value}}, {"expect": shown})
                self.assertPassed(report)
                self.assertEqual(report["steps"][1]["target"]["label"], field)
                self.assertIsNone(report["steps"][1]["fallback"])

    @fixed_list_only
    def test_select_on_a_custom_dropdown_clicks_the_option_the_click_added(self):
        report = self.run_steps({"select": {"field": "Choose an option", "value": "A"}}, {"expect": "Type: A"})
        self.assertPassed(report)
        step = report["steps"][1]
        self.assertEqual(step["fallback"], "custom_select")
        self.assertEqual(step["option"], {"label": "A", "role": "text"})

    @fixed_list_only
    def test_a_role_less_trigger_and_option_are_clicked_by_their_text(self):
        report = self.run_steps({"click": "Choose an option"}, {"click": "CNAME"}, {"expect": "Type: CNAME"})
        self.assertPassed(report)
        self.assertEqual([step["fallback"] for step in report["steps"][1:3]], ["text_holder"] * 2)

    @fixed_list_only
    def test_a_plain_text_holder_is_clicked_once_the_budget_is_spent(self):
        report = self.run_steps({"click": "Plain row"}, {"expect": "Row clicked"})
        self.assertPassed(report)
        self.assertEqual(report["steps"][1]["fallback"], "text_holder")

    def test_a_heading_does_not_beat_a_control_that_renders_late(self):
        out = tempfile.mkdtemp(prefix="ck-browser-late-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        script = script_of({"open": self.url.replace("dropdowns.html", "late.html")}, {"click": "Save"})
        report = run(script, out, jev=ExactJev(), ws_url=self.ws_url,
                     settle_tries=40, log=lambda _line: None)
        self.assertPassed(report)
        step = report["steps"][1]
        self.assertEqual(step["target"]["role"], "button")
        self.assertIsNone(step["fallback"])

    def test_a_shadow_root_dropdown_needs_no_fallback(self):
        report = self.run_steps({"click": "Choose a protocol"}, {"click": "UDP"}, {"expect": "Shadow says UDP"})
        self.assertPassed(report)
        self.assertEqual([step["fallback"] for step in report["steps"][1:3]], [None, None])

    def test_a_covered_option_fails_with_its_near_misses(self):
        report = self.run_steps({"click": "1/2 Hour"}, settle_tries=1)
        step = report["steps"][1]
        self.assertEqual(step["status"], "fail")
        span = next(miss for miss in step["near_misses"] if miss["tag"] == "span")
        self.assertFalse(span["hit"])
        self.assertTrue(span["why"].startswith("covered by"), span["why"])
        select = next(miss for miss in step["near_misses"] if miss["tag"] == "select")
        self.assertTrue(select["why"].startswith("hidden native select (opacity 0)"), select["why"])
        self.assertIn("near miss:", format_report(report))


if __name__ == "__main__":
    unittest.main()

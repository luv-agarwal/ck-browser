"""ck-browser keyboard fallback: the `type` verb and where focus is after a press or type."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ck_browser.cdp import Cdp, StalePage, Tab
from ck_browser.page import act
from ck_browser.report import step_line
from ck_browser.runner import Driver, run
from ck_browser.script import MASK, VERBS, ScriptError, load_script
from tests import test_jevbrowser_dropdowns as dropdowns  # a module: its TestCases must not run twice
from tests.test_jevbrowser import ExactJev, FakeDriver, StubJev, make_page, script_of


class TypeScriptTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ck-browser-type-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def load(self, steps):
        path = self.dir / "steps.yaml"
        path.write_text("steps:\n" + "".join(f"  - {step}\n" for step in steps))
        return load_script(path)["steps"]

    def test_both_forms_parse(self):
        self.assertIn("type", VERBS)
        plain, quoted, secret, block = self.load([
            'type: "hello world"', 'type: "007"', 'type: { text: hunter2, secret: true }',
            'type: { text: x, secret: false }'])
        self.assertEqual(plain["arg"], {"text": "hello world", "secret": False})
        self.assertEqual(plain["raw"], "type: hello world")
        self.assertEqual(quoted["arg"]["text"], "007")
        self.assertEqual(secret["arg"], {"text": "hunter2", "secret": True})
        self.assertEqual(block["arg"], {"text": "x", "secret": False})

    def test_a_secret_is_masked_in_raw(self):
        step = self.load(['type: { text: hunter2, secret: true }'])[0]
        self.assertNotIn("hunter2", step["raw"])
        self.assertIn(MASK, step["raw"])

    def test_json_form(self):
        path = self.dir / "steps.json"
        path.write_text(json.dumps({"steps": [{"type": 42}, {"type": {"text": "pw", "secret": True}}]}))
        number, secret = load_script(path)["steps"]
        self.assertEqual(number["arg"], {"text": "42", "secret": False})
        self.assertTrue(secret["arg"]["secret"])
        self.assertNotIn("pw", secret["raw"])

    def test_errors(self):
        for step in ('type: ""', "type: { secret: true }", "type: { text: x, secret: maybe }",
                     "type: { text: x, field: Email }"):
            with self.subTest(step=step), self.assertRaises(ScriptError):
                self.load([step])


class TypeUnitTest(unittest.TestCase):
    def test_type_inserts_the_text_once_without_a_freshness_check(self):
        tab = dropdowns.KeyTab()
        tab.after_input = None
        action = {"id": "type", "kind": "type", "label": "Type into Email"}
        act(tab, action, make_page([]), text="hello")
        self.assertEqual(tab.calls, [("Input.insertText", {"text": "hello"})])
        self.assertIs(tab.after_input, action)

    def test_report_names_the_focus(self):
        record = {"n": 2, "status": "pass", "step": "press: Tab", "verb": "press", "target": None,
                  "error": None, "ms": 5, "console_errors": [], "failed_requests": [],
                  "screenshot": None, "failure_screenshot": None,
                  "focused": {"tag": "input", "role": "textbox", "label": "Email"}}
        self.assertIn('focus: textbox "Email"', step_line(record))
        record["focused"] = None
        self.assertIn("focus: none", step_line(record))

    def test_a_press_keeps_its_pass_when_focus_cannot_be_read(self):
        class Navigating(FakeDriver):
            def focused(self):
                raise StalePage("Document changed during evaluation")

        out = tempfile.mkdtemp(prefix="ck-browser-type-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        driver = Navigating([make_page([])])
        report = run(script_of({"press": "Enter"}), out, jev=StubJev(), driver=driver, log=lambda _line: None)
        self.assertEqual((report["steps"][0]["status"], report["steps"][0]["focused"]), ("pass", None))
        self.assertIn(("press", "Press Enter", None), driver.acted)


class KeyboardRunTest(unittest.TestCase):
    """keyboard.html in the dedicated headless Chrome, through the whole step loop."""

    setUpClass = classmethod(dropdowns.DropdownFixtureTest.setUpClass.__func__)
    tearDownClass = classmethod(dropdowns.DropdownFixtureTest.tearDownClass.__func__)

    def run_steps(self, *steps):
        """Run on a tab of our own, kept open so the test can read the fields afterwards."""
        out = tempfile.mkdtemp(prefix="ck-browser-keyboard-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        cdp = Cdp(self.ws_url)
        self.addCleanup(cdp.close)
        self.tab = Tab.open(cdp)
        self.addCleanup(self.tab.close)
        self.lines = []
        script = script_of({"open": self.url.replace("dropdowns.html", "keyboard.html")}, *steps)
        report = run(script, out, jev=ExactJev(), driver=Driver(self.tab), settle_tries=2,
                     log=self.lines.append)
        self.run_json = Path(out, "run.json").read_text()
        return report

    def assertPassed(self, report):
        failed = [step for step in report["steps"] if step["status"] != "pass"]
        self.assertFalse(failed, failed and failed[0]["error"])

    def field(self, expression):
        return self.tab.evaluate(expression)

    def test_click_a_field_then_type(self):
        report = self.run_steps({"click": "Email"}, {"type": "ann@example.com"})
        self.assertPassed(report)
        step = report["steps"][2]
        self.assertEqual(step["focused"], {"tag": "input", "role": "textbox", "label": "Email"})
        self.assertIs(step["page_changed"], True)  # the field's value is part of what was observed
        self.assertEqual(self.field("document.querySelector('#email').value"), "ann@example.com")
        self.assertIn('focus: textbox "Email"', self.lines[2])

    def test_tab_then_type_lands_in_the_next_field(self):
        report = self.run_steps({"press": "Tab"}, {"type": "ann"}, {"press": "Tab"}, {"press": "Tab"})
        self.assertPassed(report)
        self.assertEqual(report["steps"][1]["focused"], {"tag": "input", "role": "textbox", "label": "Email"})
        self.assertEqual(report["steps"][2]["focused"]["label"], "Email")
        self.assertEqual(self.field("document.querySelector('#email').value"), "ann")
        self.assertEqual(report["steps"][3]["focused"]["label"], "Password")
        self.assertEqual(report["steps"][4]["focused"], {"tag": "div", "role": None, "label": "Key echo"})
        self.assertIn('focus: div "Key echo"', self.lines[4])

    def test_a_key_reaches_a_focused_custom_widget(self):
        report = self.run_steps({"press": "Tab"}, {"press": "Tab"}, {"press": "Tab"}, {"press": "q"},
                                {"expect": "Keys:q"})
        self.assertPassed(report)

    def test_nothing_focused_fails_before_typing(self):
        report = self.run_steps({"type": "lost"})
        step = report["steps"][1]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(step["error"], "nothing has focus: click or Tab to a field first")
        self.assertIsNone(step["focused"])
        self.assertIn("focus: none", self.lines[1])

    def test_a_password_field_refuses_plain_text(self):
        report = self.run_steps({"press": "Tab"}, {"press": "Tab"}, {"type": "hunter2"})
        step = report["steps"][3]
        self.assertEqual(step["status"], "fail")
        self.assertEqual(step["error"], "the focused field is a password field: use fill,"
                                        " or type: {text: ..., secret: true}")
        self.assertEqual(step["focused"], {"tag": "input", "role": "textbox", "label": "Password"})
        self.assertEqual(self.field("document.querySelector('#password').value"), "")

    def test_a_secret_types_into_the_password_field_and_is_never_shown(self):
        report = self.run_steps({"press": "Tab"}, {"press": "Tab"}, {"type": {"text": "hunter2", "secret": True}})
        self.assertPassed(report)
        self.assertEqual(self.field("document.querySelector('#password').value"), "hunter2")
        self.assertNotIn("hunter2", "\n".join(self.lines))
        self.assertNotIn("hunter2", self.run_json)

    def test_focus_inside_a_shadow_root_is_named(self):
        report = self.run_steps(*[{"press": "Tab"}] * 4, {"type": "Pune"})
        self.assertPassed(report)
        self.assertEqual(report["steps"][5]["focused"],
                         {"tag": "input", "role": "textbox", "label": "Shadow city"})
        self.assertEqual(self.field("document.querySelector('shadow-field').shadowRoot"
                                    ".querySelector('#inner').value"), "Pune")

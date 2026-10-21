"""ck-browser discovery from Chrome's signals: AX tree, click listeners, closed shadow roots."""

import functools
import json
import http.server
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from ck_browser import page as page_module
from ck_browser import signals
from ck_browser.cdp import Cdp, CdpError, Tab, ensure_chrome
from ck_browser.page import fresh, near_misses, observe
from ck_browser.report import format_report
from ck_browser.runner import run
from tests.test_jevbrowser import ExactJev, script_of
from tests.test_jevbrowser_dropdowns import FIXTURES, PROFILE_DIR, QuietHandler, fixed_list_only

REFUSED = "Accessibility.getFullAXTree: 'Accessibility.getFullAXTree' wasn't found"


class ParseTest(unittest.TestCase):
    """The parsers read only the fields the extension bridge keeps."""

    def test_ax_nodes_keep_widget_role_nodes_with_a_backend_id(self):
        nodes = [
            {"ignored": False, "role": {"value": "RootWebArea"}, "backendDOMNodeId": 1},
            {"ignored": False, "role": {"value": "button"}, "name": {"value": "Go"}, "backendDOMNodeId": 2},
            {"ignored": True, "role": {"value": "button"}, "backendDOMNodeId": 3},
            {"ignored": False, "role": {"value": "generic"}, "backendDOMNodeId": 4,
             "properties": [{"name": "focusable", "value": {"value": True}}]},
            {"ignored": False, "role": {"value": "generic"}, "backendDOMNodeId": 5},
            {"ignored": False, "role": {"value": "PopUpButton"}, "backendDOMNodeId": 6},
            {"ignored": False, "role": {"value": "link"}},
        ]
        self.assertEqual(signals.ax_nodes({"nodes": nodes}), {
            "2": ["button", "Go"], "6": ["PopUpButton", ""]})

    def test_closed_roots_are_found_through_children_and_shadow_roots(self):
        tree = {"nodeName": "#document", "backendNodeId": 1, "children": [
            {"nodeName": "X-A", "backendNodeId": 2, "shadowRoots": [
                {"nodeName": "#document-fragment", "backendNodeId": 3, "shadowRootType": "closed", "children": [
                    {"nodeName": "X-B", "backendNodeId": 4, "shadowRoots": [
                        {"backendNodeId": 5, "shadowRootType": "open", "children": [
                            {"nodeName": "X-C", "backendNodeId": 6, "shadowRoots": [
                                {"backendNodeId": 7, "shadowRootType": "closed"}]}]}]}]}]}]}
        self.assertEqual(signals.closed_roots(tree), [3, 7])

    def test_a_missing_signal_is_one_line_for_the_run(self):
        bridge = "Accessibility.getFullAXTree is not allowed over the extension bridge"

        def report(*found):
            steps = [{"observations": {"discovery": d}, "console_errors": [], "failed_requests": [],
                      "screenshot": None, "failure_screenshot": None} for d in found]
            totals = {"passed": 1, "steps": len(steps), "wall_ms": 1, "jev_requests": 0, "jev_ms": 0,
                      "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}
            text = format_report({"totals": totals, "settings": {"settle_tries": 2}, "steps": steps}, steps=False)
            return [line for line in text.splitlines() if line.startswith("discovery:")]

        both = {"ax": True, "listeners": True, "why": None}
        self.assertEqual(report(both, both), [])
        self.assertEqual(report(both, {"ax": False, "listeners": True, "why": bridge},
                                {"ax": False, "listeners": True, "why": bridge}),
                         [f"discovery: no accessibility tree ({bridge})"
                          " — reload the ck-browser extension in chrome://extensions"])
        self.assertEqual(report({"ax": True, "listeners": False, "why": "no API"}),
                         ["discovery: no click listeners (no API)"])
        self.assertEqual(report({"ax": False, "listeners": False, "why": "x; y"}),
                         ["discovery: fixed list only (x; y)"])

    def test_call_many_raises_like_call_when_the_transport_is_closed(self):
        cdp = Cdp.__new__(Cdp)
        cdp.closed = "the bridge closed: the tab was released"
        with self.assertRaisesRegex(CdpError, "^DOM.resolveNode: the bridge closed"):
            cdp.call_many("DOM.resolveNode", [{"backendNodeId": 1}])

    def test_only_a_repeating_failure_turns_a_signal_off(self):
        self.assertTrue(signals.refused(CdpError(REFUSED)))
        self.assertTrue(signals.refused(CdpError("X: Accessibility.getFullAXTree is not allowed")))
        self.assertFalse(signals.refused(CdpError("DOM.resolveNode: No node with given id found")))


class SignalsTest(unittest.TestCase):
    """signals.html in headless Chrome: what Chrome's signals add, and what they must not."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.ws_url = ensure_chrome(profile_dir=PROFILE_DIR, headless=True)
        except CdpError as exc:
            raise unittest.SkipTest(str(exc))
        handler = functools.partial(QuietHandler, directory=str(FIXTURES))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/"
        cls.url = cls.base + "signals.html"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def open(self, url=None):
        cdp = Cdp(self.ws_url)
        self.addCleanup(cdp.close)
        self.tab = Tab.open(cdp)
        self.addCleanup(self.tab.close)
        self.tab.navigate(url or self.url)
        return observe(self.tab)

    def run_steps(self, *steps, url=None):
        out = tempfile.mkdtemp(prefix="ck-browser-signals-")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        return run(script_of({"open": url or self.url}, *steps), out, jev=ExactJev(), ws_url=self.ws_url,
                   settle_tries=2, log=lambda _line: None)

    def assertPassed(self, report):
        failed = [step for step in report["steps"] if step["status"] != "pass"]
        self.assertFalse(failed, failed and failed[0]["error"])

    def test_each_pattern_is_offered_in_a_plain_observe(self):
        page = self.open()
        offered = {(a["role"], a["kind"], a["label"]) for a in page["actions"] if "node" in a}
        for want in (("clickable", "click", "Pick a fruit"), ("clickable", "click", "Alpha row"),
                     ("clickable", "click", "Star rating"), ("clickable", "click", "Choose a size"),
                     ("combobox", "select", "Color → Blue"), ("button", "click", "Apply color"),
                     ("button", "click", "Zoom canvas"), ("button", "click", "D1")):
            self.assertIn(want, offered)
        self.assertIn("Closed greeting text", page["text"])
        self.assertNotIn("Fruit", [a["label"] for a in page["actions"]])  # a pointer heading, no listener

    def test_delegation_roots_and_body_are_not_offered(self):
        page = self.open()
        nodes = {a["node"] for a in page["actions"] if "node" in a}
        roots = self.tab.evaluate("[document.body,document.documentElement,document.getElementById('deleg')]"
                                  ".map(e=>window.__ckPage.idOf.get(e) ?? null)")
        self.assertFalse(nodes & {n for n in roots if n is not None})
        self.assertEqual(self.tab.evaluate("window.__ckPage.pressable.has(document.getElementById('deleg'))"),
                         True)

    def test_one_label_twice_one_inside_the_other_is_offered_once(self):
        page = self.open()
        labels = [a["label"] for a in page["actions"]]
        self.assertEqual(labels.count("Open settings"), 1)
        self.assertEqual(labels.count("Save draft"), 1)
        inner, button = self.tab.evaluate(
            "[document.getElementById('inner'), document.querySelector('button[onclick]')]"
            ".map(e=>window.__ckPage.idOf.get(e))")
        offered = {a["label"]: a["node"] for a in page["actions"] if "node" in a}
        self.assertEqual((offered["Open settings"], offered["Save draft"]), (inner, button))

    def test_discovery_reports_both_signals_and_stays_out_of_the_marker(self):
        page = self.open()
        self.assertEqual(page["discovery"], {"ax": True, "listeners": True, "why": None})
        self.assertNotIn("discovery", str(page["marker"]))
        fill = next(a for a in page["actions"] if a["kind"] == "fill")
        self.assertTrue(fresh(self.tab, page, fill))
        self.assertTrue(fresh(self.tab, page))

    def test_steady_state_costs_a_stamp_read_and_a_snapshot(self):
        self.open()
        before = self.tab.cdp.calls
        observe(self.tab)
        self.assertEqual(self.tab.cdp.calls - before, 2)

    def calls_in(self, observing=observe, **kwargs):
        """The CDP methods one observe calls (the page offered, the methods)."""
        seen, call = [], self.tab.call

        def counting(method, **params):
            seen.append(method)
            return call(method, **params)

        with mock.patch.object(self.tab, "call", counting):
            return observing(self.tab, **kwargs), seen

    def ax_reads(self):
        return self.calls_in()[1].count("Accessibility.getFullAXTree")

    def page_of(self, html):
        """signals.html's tab with `html` for a body, observed."""
        self.open()
        self.tab.evaluate(f"document.body.innerHTML={json.dumps(html)}")
        return observe(self.tab)

    def offered(self, page):
        return {(a["role"], a["label"]) for a in page["actions"] if "node" in a}

    def test_an_unchanged_page_does_not_read_the_ax_tree_again(self):
        self.open()
        self.assertEqual(self.ax_reads(), 0)
        with mock.patch.object(page_module, "AX_MAX_AGE", 0):
            self.assertEqual(self.ax_reads(), 1)

    def test_a_dom_mutation_reads_the_ax_tree_again(self):
        self.open()
        self.tab.evaluate("document.getElementById('star').setAttribute('role','button')")
        self.assertEqual(self.ax_reads(), 1)
        self.assertEqual(self.ax_reads(), 0)
        self.tab.evaluate("document.body.append(document.createElement('x-new'))")
        self.assertEqual(self.ax_reads(), 1)

    def test_an_input_reads_the_ax_tree_again(self):
        self.open()
        self.tab.after_input = {"kind": "press", "key": "Shift"}
        self.assertEqual(self.ax_reads(), 1)

    def test_a_range_input_is_offered_through_its_ax_role(self):
        page = self.page_of('<input type="range" aria-label="Volume"><div id="menu-option-a">Menu thing</div>')
        self.assertIn(("slider", "Volume"), self.offered(page))
        self.assertNotIn("Menu thing", {label for _, label in self.offered(page)})

    @fixed_list_only
    def test_the_fixed_list_alone_offers_what_it_always_did(self):
        page = self.page_of('<input type="range" aria-label="Volume"><div id="menu-option-a">Menu thing</div>'
                            '<button>Go</button>')
        self.assertEqual(self.offered(page), {("button", "Go")})

    def test_a_focusable_container_is_delegation_and_a_long_focusable_label_is_cut(self):
        page = self.page_of('<div tabindex="0" id="wrap">Toolbar wrapper' + '<button>B</button>' * 4 + '</div>'
                            '<div tabindex="0">' + 'many words here ' * 20 + '</div>')
        labels = [a["label"] for a in page["actions"] if "node" in a]
        self.assertTrue(any(label.startswith("many words") and len(label) <= 90 for label in labels), labels)
        wrap = self.tab.evaluate("window.__ckPage.idOf.get(document.getElementById('wrap')) ?? -1")
        self.assertNotIn(wrap, {a.get("node") for a in page["actions"]})
        misses = near_misses(self.tab, "Toolbar wrapper")
        self.assertIn("focusable on a container (delegation)", [m["why"] for m in misses], misses)

    def test_the_action_cap_drops_click_only_candidates_first(self):
        divs = "".join(f'<div onclick="" style="display:inline-block;width:20px;height:9px;'
                       f'font-size:6px">c{i}</div>' for i in range(300))
        page = self.page_of(divs + "<button>Real control</button>")
        self.assertGreater(page["omitted_actions"], 0)
        self.assertIn(("button", "Real control"), self.offered(page))

    def test_user_agent_shadow_roots_are_skipped_and_the_page_lives(self):
        """A date input's parts live in its user-agent root: reading that root's mode once crashed
        the renderer. Short timeouts so a crash fails in seconds, not minutes."""
        cdp = Cdp(self.ws_url, timeout=5)
        self.addCleanup(cdp.close)
        self.tab = Tab.open(cdp)
        self.addCleanup(self.tab.close)
        self.tab.navigate(self.url)
        self.tab.evaluate("""customElements.define('x-ua-vault', class extends HTMLElement {
          constructor() { super();
            this.attachShadow({mode:'closed'}).innerHTML='<button>Vault open</button>'; } });
          document.body.innerHTML='<label>Due <input type="date"></label>'
            + '<video controls width="200" height="100"></video><x-ua-vault></x-ua-vault>'""")
        page = observe(self.tab)
        self.assertEqual(self.tab.evaluate("1+1"), 2)
        offered = self.offered(page)
        self.assertIn(("button", "Vault open"), offered)
        # Chrome names the parts "Day Day", "play", ...: the date input and the video stand alone.
        self.assertFalse([(role, label) for role, label in offered if role == "spinbutton"
                          or label.split(" ")[0] in ("Day", "Month", "Year", "play", "mute")], offered)

    def test_an_undefined_dashed_tag_does_not_rescan_closed_roots(self):
        self.open()
        self.tab.evaluate("document.body.append(document.createElement('app-root'))")
        self.assertNotIn("DOM.describeNode", self.calls_in()[1])

    def test_a_tag_defined_after_the_scan_is_scanned(self):
        self.open()
        self.tab.evaluate("document.body.append(document.createElement('x-late-box'))")
        observe(self.tab)
        self.tab.evaluate("""customElements.define('x-late-box', class extends HTMLElement {
          constructor() { super(); this.attachShadow({mode:'closed'}).innerHTML=
            '<div onclick="this.textContent=\\'Late clicked\\'">Late inner</div>'; } })""")
        page, seen = self.calls_in()
        self.assertIn("DOM.describeNode", seen)
        self.assertIn(("clickable", "Late inner"), self.offered(page))

    def test_a_hidden_native_select_takes_a_value_with_the_signals_on(self):
        page = self.open(self.base + "dropdowns.html")
        ttl = [a for a in page["actions"] if a["kind"] == "select" and a["label"].startswith("TTL →")]
        self.assertTrue(ttl and all(a.get("proxied") for a in ttl), ttl)
        report = self.run_steps({"select": {"field": "TTL", "value": "1 Hour"}}, {"expect": "TTL: 1 Hour"},
                                url=self.base + "dropdowns.html")
        self.assertPassed(report)
        self.assertEqual(report["steps"][1]["target"]["label"], "TTL")
        self.assertIsNone(report["steps"][1]["fallback"])

    def test_select_on_a_role_less_custom_dropdown_with_the_signals_on(self):
        report = self.run_steps({"select": {"field": "Choose an option", "value": "A"}}, {"expect": "Type: A"},
                                url=self.base + "dropdowns.html")
        self.assertPassed(report)
        self.assertEqual(report["steps"][1]["fallback"], "custom_select")

    def test_a_role_less_listener_trigger_and_option_are_clicked_without_fallback(self):
        report = self.run_steps({"click": "Pick a fruit"}, {"click": "Apple"}, {"expect": "Fruit: Apple"})
        self.assertPassed(report)
        self.assertEqual([step.get("fallback") for step in report["steps"][1:3]], [None, None])
        self.assertEqual(report["steps"][1]["observations"]["discovery"],
                         {"ax": True, "listeners": True, "why": None})

    def test_a_closed_root_dropdown_is_operated_end_to_end(self):
        self.assertPassed(self.run_steps({"click": "Choose a size"}, {"click": "Large"}, {"expect": "Size: Large"}))

    def test_a_closed_root_select_takes_a_value(self):
        self.assertPassed(self.run_steps({"select": {"field": "Color", "value": "Blue"}}, {"expect": "Color: Blue"}))

    def test_closed_root_text_and_an_element_internals_button(self):
        self.assertPassed(self.run_steps({"expect": "Closed greeting text"}, {"click": "Zoom canvas"},
                                         {"expect": "Zoomed"}))

    def test_near_misses_say_chromes_role_and_listener(self):
        self.open()
        misses = near_misses(self.tab, "Apply plum color")
        apply = next(m for m in misses if m["text"] == "Apply color")
        self.assertEqual((apply["ax_role"], apply["listener"], apply["offered"]), ("button", True, True))
        report = self.run_steps({"click": "Pick a plum"})
        step = report["steps"][1]
        self.assertEqual(step["status"], "fail")
        pick = next(m for m in step["near_misses"] if m["text"] == "Pick a fruit")
        self.assertEqual((pick["ax_role"], pick["listener"]), (None, True))

    def test_without_the_ax_tree_the_dropdown_cases_still_pass(self):
        call = Tab.call

        def refusing(tab, method, **params):
            if method == "Accessibility.getFullAXTree":
                raise CdpError(REFUSED)
            return call(tab, method, **params)

        with mock.patch.object(Tab, "call", refusing):
            page = self.open(self.base + "dropdowns.html")
            self.assertEqual(page["discovery"], {"ax": False, "listeners": True, "why": REFUSED})
            misses = near_misses(self.tab, "Save")
            self.assertTrue(all(m["ax_role"] is None for m in misses))
            report = self.run_steps({"select": {"field": "TTL", "value": "1 Hour"}}, {"expect": "TTL: 1 Hour"},
                                    {"click": "Choose an option"}, {"click": "CNAME"}, {"expect": "Type: CNAME"},
                                    url=self.base + "dropdowns.html")
        self.assertPassed(report)
        self.assertFalse(report["steps"][1]["observations"]["discovery"]["ax"])

    def delegated(self, *steps):
        return self.run_steps(*steps, url=self.base + "delegated.html")

    def test_framework_delegated_handlers_are_offered(self):
        self.open(self.base + "delegated.html")
        self.tab.evaluate("document.getElementById('segments').hidden=false")
        page = observe(self.tab)
        offered = {(a["role"], a["kind"], a["label"]) for a in page["actions"] if "node" in a}
        for label in ("Apparel", "Pick a segment", "Svelte save", "Solid open"):
            self.assertIn(("clickable", "click", label), offered)
        self.assertNotIn("Hover only", {label for _, _, label in offered})
        nodes = {a["node"] for a in page["actions"] if "node" in a}
        roots = self.tab.evaluate("['root','portal'].map(id=>"
                                  "window.__ckPage.idOf.get(document.getElementById(id)) ?? null)")
        self.assertFalse(nodes & {n for n in roots if n is not None})

    def test_a_react_dropdown_with_pointerdown_options_is_clicked_without_fallback(self):
        report = self.delegated({"click": "Pick a segment"}, {"click": "Footwear"}, {"expect": "Segment: Footwear"})
        self.assertPassed(report)
        self.assertEqual([step.get("fallback") for step in report["steps"][1:3]], [None, None])

    def test_a_wizard_picker_with_portal_button_options(self):
        self.assertPassed(self.delegated({"click": "Select segment"}, {"click": "Women"}, {"expect": "Gender: Women"}))

    def test_svelte_and_solid_delegated_handlers_are_clicked_without_fallback(self):
        report = self.delegated({"click": "Svelte save"}, {"expect": "Svelte saved"},
                                {"click": "Solid open"}, {"expect": "Solid opened"})
        self.assertPassed(report)
        self.assertEqual([report["steps"][i].get("fallback") for i in (1, 3)], [None, None])

    def test_framework_handlers_are_read_without_get_event_listeners(self):
        evaluate = Tab.evaluate

        def no_listeners(tab, expression, await_promise=False, command_line_api=False):
            return evaluate(tab, expression, await_promise)

        with mock.patch.object(Tab, "evaluate", no_listeners):
            self.open(self.base + "delegated.html")
            self.tab.evaluate("document.getElementById('segments').hidden=false")
            page = observe(self.tab)
        self.assertFalse(page["discovery"]["listeners"])
        offered = {(a["role"], a["label"]) for a in page["actions"] if "node" in a}
        for label in ("Apparel", "Pick a segment", "Svelte save", "Solid open"):
            self.assertIn(("clickable", label), offered)


if __name__ == "__main__":
    unittest.main()

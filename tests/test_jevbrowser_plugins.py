"""ck-browser plugins (CK_BROWSER_PLUGINS): host messages the core does not know, the Jev key
fallback, and a broken plugin that is logged, never fatal."""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from ck_browser import plugins
from ck_browser.model import api_key_from_env

from tests.test_jevbrowser_ext import FakeExtension, temp_dir, wait_for

PLUGINS = {
    "ckb_test_good": (
        "HANDLERS = {'note_save': lambda message: {'saved': message['note']},\n"
        "            'note_fail': lambda message: 1 / 0}\n"
        "def api_key():\n    return 'from-plugin'\n"
    ),
    "ckb_test_typed": "RESULT_TYPE = 'note_result'\nHANDLERS = {'note_typed': lambda message: 'ok'}\n",
    "ckb_test_broken": "raise RuntimeError('cannot load')\n",
    "ckb_test_failing": "def api_key():\n    raise RuntimeError('ts-should-never-be-printed')\n",
}


class PluginCase(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir(self)
        for name, source in PLUGINS.items():
            (self.dir / f"{name}.py").write_text(source)
        sys.path.insert(0, str(self.dir))
        self.addCleanup(sys.path.remove, str(self.dir))
        for name in PLUGINS:
            self.addCleanup(sys.modules.pop, name, None)
        self.addCleanup(plugins._loaded.clear)
        plugins._loaded.clear()

    def env(self, names):
        return mock.patch.dict(os.environ, {"CK_BROWSER_PLUGINS": names})


class LoaderTest(PluginCase):
    def test_a_handler_is_found_past_a_broken_plugin_which_is_logged(self):
        lines = []
        with self.env("ckb_test_broken, ckb_test_good,,"):
            handle, reply_type = plugins.handler("note_save", lines.append)
            self.assertIsNone(plugins.handler("hello", lines.append))
        self.assertEqual(handle({"note": "x"}), {"saved": "x"})
        self.assertEqual(reply_type, plugins.RESULT_TYPE)
        self.assertEqual(lines, ["plugin ckb_test_broken: import failed: RuntimeError('cannot load')"])  # once

    def test_a_plugin_names_its_own_reply_type(self):
        with self.env("ckb_test_typed"):
            self.assertEqual(plugins.handler("note_typed")[1], "note_result")

    def test_no_plugins_means_no_handler_and_no_key(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CK_BROWSER_PLUGINS", None)
            self.assertIsNone(plugins.handler("note_save"))
            self.assertIsNone(plugins.api_key())


class ApiKeyTest(PluginCase):
    def test_env_first_then_the_first_plugin_key_past_failures_never_printing_a_value(self):
        with self.env("ckb_test_broken,ckb_test_failing,ckb_test_good"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            os.environ.pop("TYPESAFE_API_KEY", None)
            self.assertEqual(api_key_from_env(), "from-plugin")
            os.environ["TYPESAFE_API_KEY"] = "from-env"
            self.assertEqual(api_key_from_env(), "from-env")
        self.assertIn("plugin ckb_test_broken: import failed", err.getvalue())
        self.assertIn("plugin ckb_test_failing: api_key failed: RuntimeError", err.getvalue())
        self.assertNotIn("ts-should-never-be-printed", err.getvalue())

    def test_a_plugin_without_a_key_leaves_none(self):
        with self.env("ckb_test_typed"):
            os.environ.pop("TYPESAFE_API_KEY", None)
            self.assertIsNone(api_key_from_env())


class HostPluginTest(PluginCase):
    """The real host process: a plugin answers what the core does not know; a broken one is logged."""

    def host(self):
        env = {"CK_BROWSER_PLUGINS": "ckb_test_broken,ckb_test_good,ckb_test_typed",
               "PYTHONPATH": os.pathsep.join(filter(None, [str(self.dir), os.environ.get("PYTHONPATH")]))}
        return FakeExtension(self, answer=lambda call: None, env=env)

    def reply(self, ext, id):
        self.assertTrue(wait_for(lambda: any(c.get("id") == id for c in ext.calls), 15), ext.calls)
        [reply] = [c for c in ext.calls if c.get("id") == id]
        return reply

    def test_plugin_messages_are_answered_by_the_host_and_never_relayed(self):
        ext = self.host().idle()
        ext.send({"type": "note_save", "id": 1, "note": "hi"})
        self.assertEqual(self.reply(ext, 1), {"type": "plugin_result", "id": 1, "result": {"saved": "hi"}})
        ext.send({"type": "note_fail", "id": 2})
        self.assertEqual(self.reply(ext, 2), {"type": "plugin_result", "id": 2, "error": "division by zero"})
        ext.send({"type": "note_typed", "id": 3})
        self.assertEqual(self.reply(ext, 3), {"type": "note_result", "id": 3, "result": "ok"})
        self.assertIsNone(ext.proc.poll())
        log = (ext.dir / "host.log").read_text()
        self.assertIn("plugin ckb_test_broken: import failed: RuntimeError('cannot load')", log)
        self.assertIn("plugin note_fail: ZeroDivisionError", log)


if __name__ == "__main__":
    unittest.main()

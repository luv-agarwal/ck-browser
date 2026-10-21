"""Optional plugins: CK_BROWSER_PLUGINS names importable modules, comma-separated. A plugin may define

- HANDLERS: {extension message type: callable(message) -> result} for the host messages the core
  does not know; the host answers each with a frame {type: RESULT_TYPE, id, result | error};
- RESULT_TYPE: that reply's type, default "plugin_result";
- api_key(): a Jev key, or None; asked only when TYPESAFE_API_KEY is unset;
- WRAPPER_ENV: names of more env vars, each a path, that `ext install` bakes into the host wrapper
  when they are set.

A plugin that fails to import or to answer is logged and skipped: the core never fails over one.
"""

import importlib
import os
import sys

RESULT_TYPE = "plugin_result"

_loaded = {}  # CK_BROWSER_PLUGINS value -> the modules it imported


def _stderr(line):
    print(f"ck-browser: {line}", file=sys.stderr, flush=True)


def modules(log=_stderr):
    """The plugin modules CK_BROWSER_PLUGINS names, each imported once; a failed one is left out."""
    names = os.environ.get("CK_BROWSER_PLUGINS", "")
    if names not in _loaded:
        found = []
        for name in filter(None, (part.strip() for part in names.split(","))):
            try:
                found.append(importlib.import_module(name))
            except Exception as exc:  # a broken plugin costs its own feature, never the run
                log(f"plugin {name}: import failed: {exc!r}")
        _loaded[names] = found
    return _loaded[names]


def handler(kind, log=_stderr):
    """(callable, reply type) a plugin registered for extension message type `kind`, or None."""
    for module in modules(log):
        handlers = getattr(module, "HANDLERS", None)
        if isinstance(handlers, dict) and callable(handlers.get(kind)):
            return handlers[kind], getattr(module, "RESULT_TYPE", RESULT_TYPE)
    return None


def api_key(log=_stderr):
    """The first key a plugin's api_key() gives, or None."""
    for module in modules(log):
        lookup = getattr(module, "api_key", None)
        if not callable(lookup):
            continue
        try:
            key = lookup()
        except Exception as exc:
            log(f"plugin {module.__name__}: api_key failed: {type(exc).__name__}")  # never a value
            continue
        if key:
            return key
    return None


def wrapper_env(log=_stderr):
    """The env var names the plugins ask `ext install` to bake into the host wrapper."""
    return [name for module in modules(log) for name in getattr(module, "WRAPPER_ENV", ())]

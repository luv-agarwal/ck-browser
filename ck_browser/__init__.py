"""Browser step runner: plain-word YAML steps executed over CDP, one Jev call per step."""

import sys

__version__ = "0.1.0"
MIN_PYTHON = (3, 11)


def check_python(info=sys.version_info, executable=sys.executable):
    """Exit with a readable message on a Python older than MIN_PYTHON (macOS /usr/bin/python3 is 3.9).
    This file stays parseable by old interpreters so the message is what they show."""
    if tuple(info[:2]) < MIN_PYTHON:
        need, found = ".".join(map(str, MIN_PYTHON)), ".".join(map(str, info[:3]))
        raise SystemExit(
            f"ck-browser needs Python {need} or newer, but found Python {found} at {executable}. "
            f"Run it with a newer interpreter, e.g. python3.12 -m ck_browser, "
            f"or install it with `uv tool install` or `pipx`.")


check_python()

try:  # optional host-app defaults (paths, plugins), not shipped
    from . import site_defaults  # noqa: F401
except ImportError:
    pass

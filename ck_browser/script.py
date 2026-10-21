"""Step scripts: a tiny YAML subset (or JSON) parsed into validated steps."""

# ponytail: YAML subset only — swap for PyYAML if the repo ever allows deps

import json
import re
from pathlib import Path

VERBS = ("open", "click", "fill", "select", "upload", "hover", "drag", "press", "type", "expect",
         "expect_count", "scroll", "wait", "checkpoint", "capture_secret")
INTEGER = re.compile(r"-?\d+$")
# A Keychain service name for capture_secret: no space, quote or backslash, so it needs no quoting.
KEYCHAIN_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SECRET_FIELD = re.compile(r"pass(word|code|phrase)|secret|token|pin\b|otp", re.IGNORECASE)
MASK = "••••"


class ScriptError(ValueError):
    """The step script is not valid."""


def load_script(path):
    """{"url": str|None, "steps": [{"verb", "arg", "raw"}]} from a .yaml/.yml or .json script."""
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".json":
        document = json.loads(text)
        url, items = document.get("url"), document.get("steps") or []
    else:
        url, items = _parse(text)
    return {"url": url, "steps": [_step(item, n) for n, item in enumerate(items, 1)]}


def _uncomment(line):
    """Strip a trailing comment. As in YAML, `#` only starts one at the start of the line or
    after whitespace, so `expect: Order #12`, `http://x/app#/board` and `p#ss` survive."""
    quote = None
    for index, char in enumerate(line):
        if quote:
            quote = None if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    # `007`, `0123` stay text: a phone number, a PIN or a zip code typed into a field keeps
    # its leading zeros. Only a number that reads back the same becomes one.
    return int(text) if INTEGER.match(text) and str(int(text)) == text else text


def _flow(text, number):
    """{ field: "Product name", value: Test kurta } -> dict"""
    if not text.endswith("}"):
        raise ScriptError(f"line {number}: unterminated flow mapping {text!r}")
    mapping, quote, item = {}, None, ""
    for char in text[1:-1] + ",":
        if quote:
            quote = None if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == ",":
            if item.strip():
                key, separator, value = item.partition(":")
                if not separator:
                    raise ScriptError(f"line {number}: expected `key: value` in {item.strip()!r}")
                mapping[str(_scalar(key))] = _scalar(value)
            item = ""
            continue
        item += char
    return mapping


def _parse(text):
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        body = _uncomment(line).rstrip()
        if body.strip():
            lines.append((len(body) - len(body.lstrip()), body.strip(), number))
    url, items, index = None, [], 0
    while index < len(lines):
        indent, body, number = lines[index]
        index += 1
        if indent == 0 and body == "steps:":
            break
        if indent == 0 and body.startswith("url:"):
            url = str(_scalar(body[4:]))
            continue
        raise ScriptError(f"line {number}: expected `url:` or `steps:`, got {body!r}")
    while index < len(lines):
        indent, body, number = lines[index]
        index += 1
        if not body.startswith("- "):
            raise ScriptError(f"line {number}: expected a `- verb: value` step, got {body!r}")
        verb, separator, rest = body[2:].partition(":")
        if not separator:
            raise ScriptError(f"line {number}: expected a `- verb: value` step, got {body!r}")
        rest = rest.strip()
        if rest:
            items.append({verb.strip(): _flow(rest, number) if rest.startswith("{") else _scalar(rest)})
            continue
        block = {}
        while index < len(lines) and lines[index][0] > indent and not lines[index][1].startswith("- "):
            key, separator, value = lines[index][1].partition(":")
            if not separator:
                raise ScriptError(f"line {lines[index][2]}: expected `key: value`, got {lines[index][1]!r}")
            block[str(_scalar(key))] = _scalar(value)
            index += 1
        items.append({verb.strip(): block})
    return url, items


def _step(item, n):
    if not isinstance(item, dict) or len(item) != 1:
        raise ScriptError(f"step {n}: each step is one `verb: value` pair")
    verb, arg = next(iter(item.items()))
    if verb not in VERBS:
        raise ScriptError(f"step {n}: unknown verb {verb!r}; expected one of {', '.join(VERBS)}")
    if verb in ("fill", "select"):
        if not isinstance(arg, dict) or set(arg) != {"field", "value"} or not str(arg["field"]).strip():
            raise ScriptError(f"step {n}: {verb} needs field and value")
        arg = {"field": str(arg["field"]), "value": str(arg["value"])}
    elif verb == "upload":
        if not isinstance(arg, dict) or set(arg) != {"field", "path"} \
                or not all(str(value).strip() for value in arg.values()):
            raise ScriptError(f"step {n}: upload needs field and path")
        arg = {"field": str(arg["field"]), "path": str(arg["path"])}
    elif verb == "drag":
        if not isinstance(arg, dict) or set(arg) != {"from", "to"} \
                or not all(str(value).strip() for value in arg.values()):
            raise ScriptError(f"step {n}: drag needs from and to")
        arg = {"from": str(arg["from"]), "to": str(arg["to"])}
    elif verb == "capture_secret":
        if not isinstance(arg, dict) or set(arg) != {"field", "keychain_service"} \
                or not str(arg["field"]).strip():
            raise ScriptError(f"step {n}: capture_secret needs field and keychain_service")
        if not KEYCHAIN_SERVICE.fullmatch(str(arg["keychain_service"])):
            raise ScriptError(f"step {n}: keychain_service {arg['keychain_service']!r} is not a service name:"
                              " letters, digits, '.', '_' and '-', starting with a letter or digit, at most 128")
        arg = {"field": str(arg["field"]), "keychain_service": str(arg["keychain_service"])}
    elif verb == "type":
        # `type: hello` or `type: { text: hello, secret: true }`; YAML here reads `true` as text.
        if isinstance(arg, dict):
            secret = arg.get("secret", False)
            if not set(arg) <= {"text", "secret"} or "text" not in arg \
                    or not (isinstance(secret, bool) or secret in ("true", "false")):
                raise ScriptError(f"step {n}: type needs text, and secret is true or false")
            arg = {"text": str(arg["text"]), "secret": secret in (True, "true")}
        else:
            arg = {"text": str(arg), "secret": False}
        if not arg["text"]:
            raise ScriptError(f"step {n}: type needs text")
    elif verb == "expect_count":
        if not isinstance(arg, dict) or len(arg) != 1:
            raise ScriptError(f"step {n}: expect_count needs one `label: count` pair")
        label, count = next(iter(arg.items()))
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ScriptError(f"step {n}: expect_count needs a whole-number count, got {count!r}")
        arg = {str(label): count}
    elif verb == "scroll":
        if arg not in ("up", "down"):
            raise ScriptError(f"step {n}: scroll is up or down, got {arg!r}")
    elif verb == "wait":
        if not isinstance(arg, int) or isinstance(arg, bool) or arg < 0:
            raise ScriptError(f"step {n}: wait needs milliseconds, got {arg!r}")
    else:
        if isinstance(arg, dict) or not str(arg).strip():
            raise ScriptError(f"step {n}: {verb} needs a value")
        arg = str(arg)
    return {"verb": verb, "arg": arg, "raw": _render(verb, arg)}


def is_secret(verb, arg):
    """A password field or a `type` marked secret: `arg` keeps the typed text, everything shown
    or sent gets the mask."""
    if verb == "type":
        return arg["secret"]
    return verb == "fill" and bool(SECRET_FIELD.search(str(arg["field"])))


def _render(verb, arg):
    """The step as text — for logs, run.json and Jev, so never with a password in it."""
    if verb == "type":
        return f"type: {{text: {MASK}, secret: true}}" if is_secret(verb, arg) else f"type: {arg['text']}"
    if isinstance(arg, dict):
        shown = {**arg, "value": MASK} if is_secret(verb, arg) else arg
        return f"{verb}: {{{', '.join(f'{k}: {v}' for k, v in shown.items())}}}"
    return f"{verb}: {arg}"

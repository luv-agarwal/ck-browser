"""Jev decision model over one kept-alive HTTPS connection — choices and nouls, never pixels."""

import http.client
import json
import math
import os
import re
import time
from pathlib import Path

from . import plugins
from . import questions as Q

HOST = "api.typesafe.ai"
PATH = "/v1/systemone"
TIMEOUT = 25
RETRY_STATUS = {429, 503, 529}


class JevError(RuntimeError):
    """The decision model was unreachable or answered with something unusable."""


def api_key_from_env():
    """TYPESAFE_API_KEY, else the first key a CK_BROWSER_PLUGINS plugin gives; None without one."""
    return os.environ.get("TYPESAFE_API_KEY") or plugins.api_key()


def validate_choice(answer, ids):
    """Accept a choice only when it names an offered id that is the argmax of a proper distribution
    over exactly the offered ids."""
    if not _sound_choice(answer, set(ids)):
        raise JevError("Invalid Jev choice answer")
    return answer


def _unit(value):
    """A real number in [0, 1]; bools and NaN are not."""
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _sound_choice(answer, ids):
    try:
        chosen, spread, confidence = answer["choice"], answer["probabilities"], answer["confidence"]
        if chosen not in ids or set(spread) != ids:
            return False
        values = list(spread.values())
        if not all(_unit(value) for value in [*values, confidence]):
            return False
        # Rounding in the answer: the sum may be off by a little, and a tie may differ in the last digit.
        return abs(sum(values) - 1) < 0.02 and spread[chosen] + 1e-6 >= max(values)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def validate_noul(answer):
    probability = (answer or {}).get("noul")
    if type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
        raise JevError("Invalid Jev noul answer")
    return float(probability)


class Jev:
    """One HTTP call per step: typed questions in, typed answers out.

    The connection is kept alive across steps — a fresh TLS handshake costs more than
    the answer itself (~930 ms per request against ~350 ms on a warm connection).
    """

    def __init__(self, api_key):
        self.api_key = api_key
        self.model = os.environ.get("TYPESAFE_MODEL", "jev-latest")
        self.connection = None

    def ask(self, state, questions):
        body = {"model": self.model, "state": state, "questions": questions}
        started = time.perf_counter()
        result = self._post(body)
        return {
            "answers": result.get("answers") or {},
            "usage": result.get("usage") or {},
            "model": result.get("model", self.model),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    def connect(self):
        """The live connection, dialled on first use."""
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(HOST, timeout=TIMEOUT)
        return self.connection

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _post(self, body):
        data = json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        attempts = 3
        for attempt in range(1, attempts + 1):
            final = attempt == attempts
            try:
                connection = self.connect()
                connection.request("POST", PATH, body=data, headers=headers)
                response = connection.getresponse()
                status, payload = response.status, response.read()
            except (http.client.HTTPException, OSError):
                self.close()  # the server dropped the kept-alive connection: the next attempt dials anew
                if final:
                    raise JevError("Jev connection failed") from None
                continue
            if status in RETRY_STATUS and not final:
                time.sleep(0.5 * 2 ** (attempt - 1))  # rate limited or overloaded: back off, then retry
                continue
            if not 200 <= status < 300:
                raise JevError(f"Jev HTTP {status}: {payload.decode(errors='replace')[:500]}")
            return json.loads(payload)


class StubJev:
    """Offline stand-in: deterministic label matching, same answer shapes as Jev."""

    model = "stub"

    def ask(self, state, questions):
        step = state.get("step") or {}
        words = _words(step.get("arg_text") or "")
        labels = {str(e["index"]): (e.get("label") or "") for e in state.get("elements") or []}
        answers = {}
        for name, question in questions.items():
            if question["type"] == "choice":
                answers[name] = _stub_choice(question, state, words)
            else:
                answers[name] = {"type": "noul", "noul": _stub_noul(name, words, labels)}
        return {
            "answers": answers,
            "usage": {"input_tokens": len(json.dumps(state)) // 4, "output_tokens": 0},
            "model": self.model,
            "latency_ms": 0,
        }


class RecordingJev:
    """Wraps any client, appending every request and answer to <out>/jev-trace.jsonl."""

    def __init__(self, client, out_dir):
        self.client = client
        self.path = Path(out_dir) / "jev-trace.jsonl"
        self.model = getattr(client, "model", "unknown")

    def ask(self, state, questions):
        result = self.client.ask(state, questions)
        # Full page text: readable by this user only.
        with open(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as handle:
            handle.write(json.dumps({"state": state, "questions": questions, "result": result}) + "\n")
        return result

    def close(self):
        close = getattr(self.client, "close", None)
        if close:
            close()


def _words(text):
    return [w for w in re.findall(r"\w+", text.lower()) if w]


def _stub_choice(question, state, words):
    criteria = question["criteria"]
    if set(criteria) == set(Q.EXPECT_CHOICES):
        return _stub_expectation(question, state)
    ids = list(criteria)
    scores = {}
    for key, criterion in criteria.items():
        text = json.dumps(criterion).lower()
        scores[key] = 0 if key == Q.NONE else sum(len(w) for w in words if w in text)
    best = max(scores, key=lambda key: (scores[key], -ids.index(key)))
    if not scores[best]:  # nothing on the page looks like the step: abstain when offered
        best = Q.NONE if Q.NONE in criteria else ids[0]
    probabilities = {key: 1.0 if key == best else 0.0 for key in ids}
    return {"type": "choice", "choice": best, "probabilities": probabilities, "confidence": 1.0}


def _stub_expectation(question, state):
    """The expect choice: a whitespace-normalised substring walk over the page text."""
    wanted = _flat(str(question["instructions"]["exact_text"]))
    choice = "verbatim" if wanted in _flat(state["page"]["text"] or "") else "absent"
    probabilities = {name: 1.0 if name == choice else 0.0 for name in Q.EXPECT_CHOICES}
    return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": 1.0}


def _flat(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def _stub_noul(name, words, labels):
    if name == "ready":
        return 0.9
    if name.startswith("match_"):
        label = labels.get(name[len("match_"):], "").lower()
        return 1.0 if words and all(word in label for word in words) else 0.0
    return 0.0

"""Dense text rendering of a run report — facts, absolute paths, no narration."""

STATUS = {"pass": "pass", "fail": "FAIL", "skipped": "skip"}


def _screenshots(record):
    """The checkpoint shot, the failure shot, or both."""
    return [shot for shot in (record["screenshot"], record["failure_screenshot"]) if shot]


def _near_miss(miss):
    """<tag role=…> "text" [in <host> shadow root] — why it was not offered (or that it was)."""
    tag = miss["tag"] + (f" role={miss['role']}" if miss.get("role") else "")
    where = f" in {miss['shadow_host']} shadow root" if miss.get("shadow_host") else ""
    verdict = miss.get("why") or ("offered" if miss.get("offered") else "not offered")
    return f'<{tag}> "{miss["text"]}"{where} — {verdict}'


def _focus(focus):
    """focus: textbox "Email" — where the keyboard is after a press or type, or that it is nowhere."""
    if not focus:
        return "focus: none"
    return f"focus: {focus['role'] or focus['tag']}" + (f' "{focus["label"]}"' if focus["label"] else "")


def _discovery(steps):
    """One line for the run when a step read the page without one of Chrome's signals, else None."""
    for record in steps:
        found = (record.get("observations") or {}).get("discovery")
        if not found or not found.get("why"):
            continue
        what = ("fixed list only" if not found["ax"] and not found["listeners"] else
                "no accessibility tree" if not found["ax"] else
                "no click listeners" if not found["listeners"] else None)
        line = f"discovery: {what} ({found['why']})" if what else f"discovery: {found['why']}"
        if "not allowed over the extension bridge" in found["why"]:
            line += " — reload the ck-browser extension in chrome://extensions"
        return line
    return None


def _video(video):
    """video: <path> (N frames, X.Xs, encoder), or none and why (and where the frames went)."""
    if video["path"]:
        return f"video: {video['path']} ({video['frames']} frames, {video['duration_s']:.1f}s, {video['encoder']})"
    line = f"video: none — {video['reason']}"
    if video["frames_dir"]:
        line += f"; {video['frames']} frames in {video['frames_dir']}/"
    return line


def step_line(record):
    """One line per step, plus indented evidence lines when there is any."""
    line = f"{record['n']:02d} {STATUS[record['status']]}  {record['step']}"
    target = record["target"]
    if target:
        probability = target.get("p")
        shown = f' p={probability:.2f}' if probability is not None else ""
        line += f"  → [{target['index']}] {target['role']} \"{target['label']}\"{shown}"
    expectation = record.get("expectation")
    if expectation:
        line += (f"  verbatim={expectation['verbatim']:.2f}"
                 f" literal={str(expectation['literal']).lower()}")
    if record.get("count") is not None:
        line += f"  count={record['count']}"
    secret = record.get("secret")
    if secret:
        line += f"  captured → keychain {secret['service']} ({secret['length']} chars)"
    if record.get("verb") in ("press", "type") and record["status"] != "skipped":
        line += f"  {_focus(record.get('focused'))}"
    if record["error"]:
        line += f"  — {record['error']}"
    if record["status"] != "skipped":
        line += f" ({record['ms']} ms)"
    lines = [line]
    for miss in (record.get("near_misses") or [])[:4]:
        lines.append(f"   near miss: {_near_miss(miss)}")
    for shot in _screenshots(record):
        lines.append(f"   screenshot {shot}")
    if record.get("screenshot_withheld"):
        lines.append("   screenshot withheld: the page may show a secret")
    elif record.get("frame_withheld"):
        lines.append("   frame withheld: the page may show a secret")
    for error in record["console_errors"]:
        lines.append(f"   console: [{error['level']}] {error['text']}")
    for request in record["failed_requests"]:
        status = request.get("error") or request["status"]
        lines.append(f"   network: {request['method']} {request['url']} {status}")
    return "\n".join(lines)


def format_report(report, steps=True):
    """The whole report; `steps=False` drops the per-step lines a live run already printed."""
    totals = report["totals"]
    tokens = totals["input_tokens"] + totals["output_tokens"]
    lines = [
        f"ck-browser: {totals['passed']}/{totals['steps']} steps passed in {totals['wall_ms'] / 1000:.1f} s"
        f" — {totals['jev_requests']} Jev requests, {totals['jev_ms'] / 1000:.2f} s,"
        f" {tokens:,} tokens (${totals['cost_usd']:.4f})"
        f"   --settle-tries {report['settings']['settle_tries']}"
    ]
    if steps:
        lines += [step_line(record) for record in report["steps"]]
    discovery = _discovery(report["steps"])
    if discovery:
        lines.append(discovery)
    shots = [shot for record in report["steps"] for shot in _screenshots(record)]
    if shots:
        lines.append("checkpoints: " + " ".join(shots))
    if report.get("video"):  # absent from a run.json older than --video
        lines.append(_video(report["video"]))
    if report.get("filmstrip"):
        lines.append(f"filmstrip: {report['filmstrip']}")
    console = sum(len(record["console_errors"]) for record in report["steps"])
    network = sum(len(record["failed_requests"]) for record in report["steps"])
    lines.append(f"console errors: {console}   failed requests: {network}")
    return "\n".join(lines)

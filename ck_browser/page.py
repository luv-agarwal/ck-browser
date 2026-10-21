"""Observe a tab and act on it: page.js decides what is offered, this module drives CDP input.

Python never sends a selector: every element is named by the number page.js gave it, looked up in
the page-side registry `window.__ckPage`. Each targeted action re-checks its target first, so a
decision is never carried out on a page that moved on.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

from . import signals
from .cdp import CdpError, PageCrashed, StalePage, StepTimeout

# The observation script: a function expression called with its options object.
PAGE_JS = Path(__file__).with_name("page.js").read_text().strip()
# The untargeted freshness probe: the same look with no new signals, its marker only.
MARKER_JS = f"(() => ({PAGE_JS})({{}})?.marker ?? null)()"
# The targeted freshness probe: field state plus one target's guard, called with its node.
FRESH_JS = "(node => { const reg=window.__ckPage; return reg ? [reg.fieldState(), reg.guard(reg.byId.get(node))] : null; })"


class Covered(StalePage):
    """The element is where it was observed, but something else is on top of it."""


class Apart(StalePage):
    """The two ends of a drag are not on screen together: bringing one in slides the other away."""


class Unreached(RuntimeError):
    """A press was dispatched and Chrome answered, but no event of it reached the page."""


TARGETED = {"click", "fill", "select", "hover"}
DRAG_MOVES = 5
DRAG_INTERVAL = 0.03
OBSERVE_TRIES = 10
OBSERVE_PAUSE = 0.02

# key -> (code, windowsVirtualKeyCode, text on keyDown)
KEYS = {
    "Enter": ("Enter", 13, "\r"),
    "Escape": ("Escape", 27, None),
    "Tab": ("Tab", 9, None),
    "Backspace": ("Backspace", 8, None),
    "Delete": ("Delete", 46, None),
    "ArrowUp": ("ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", 40, None),
    "ArrowLeft": ("ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", 39, None),
    "Space": ("Space", 32, " "),
    "Home": ("Home", 36, None),
    "End": ("End", 35, None),
    "PageUp": ("PageUp", 33, None),
    "PageDown": ("PageDown", 34, None),
}

# CDP Input.dispatchKeyEvent `modifiers` bitmask.
MODIFIERS = {"Alt": 1, "Control": 2, "Ctrl": 2, "Meta": 4, "Cmd": 4, "Shift": 8}

# Where an action's target is now: null when it is gone, disabled or off screen, {covered: true}
# when something else is on top of it, else the point to press. Called with the action as JSON.
TARGET_JS = """(action => {
  const reg=window.__ckPage, e=reg?.byId.get(action.node);
  if (!e?.isConnected) return null;
  const up = n => n.parentElement || n.getRootNode().host || null;
  const within = (n,sel) => { for (let p=n; p; p=up(p)) if (p.matches(sel)) return p; return null; };
  const inside = (a,b) => { for (let p=b; p; p=up(p)) if (p===a) return true; return false; };
  const shadow = n => n.shadowRoot || reg.closedRoots.get(n) || null;
  const hitAt = (x,y) => {
    let hit=document.elementFromPoint(x,y);
    while (hit && shadow(hit)) {
      const deeper=shadow(hit).elementFromPoint(x,y);
      if (!deeper || deeper===hit) break;
      hit=deeper;
    }
    return hit;
  };
  if (e.matches(':disabled') || within(e,'[aria-disabled="true"],[inert]')) return null;
  const box=action.proxied ? reg.proxyBox(e) : e;
  if (!box || !box.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
  if (action.aside) box.scrollIntoView({block:'nearest',inline:'center',behavior:'instant'});
  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
  const r=box.getBoundingClientRect();
  if (r.width<=0 || r.height<=0) return null;
  const x=r.x+r.width/2;
  if (x<0 || r.y+r.height/2<0 || x>=innerWidth || r.y+r.height/2>=innerHeight) return null;
  // A panel's overlay scrollbar over the centre hits the panel itself, an ancestor of the target:
  // then aim higher on the target, at a quarter and a tenth of its height.
  const aboveBox = hit => !!hit && hit!==box && inside(hit,box);
  const aims=[0.5,0.25,0.1].map(share => r.y+r.height*share);
  const y=aims.find(at => !aboveBox(hitAt(x,at))) ?? aims[aims.length-1];
  const hit=hitAt(x,y);
  const label=hit && within(hit,'label');
  if (!(hit && inside(box,hit)) &&
      !(label && (label===within(box,'label') || label.control===box || label.control===e))) return {covered:true};
  if (action.kind==='select') {
    const usable=e.tagName==='SELECT' && [...e.options].some(o => o.value===action.value && !o.disabled &&
      !(o.parentElement.tagName==='OPTGROUP' && o.parentElement.disabled));
    if (!usable) return null;
    e.value=action.value;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return {x,y};
})"""

# After an input: at least two frames; a fill into a combobox also waits for its options to show
# (up to 200 ms), anything else gets 50 ms at most. Called with the last action as JSON.
SETTLE_JS = """(action => new Promise(resolve => {
  const reg=window.__ckPage, field=action?.kind==='fill' ? reg?.byId.get(action.node) : null;
  const combo=!!field && field.getAttribute('role')==='combobox';
  const scopes = () => {
    const root=field.getRootNode(), ids=[field.getAttribute('aria-controls'), field.getAttribute('aria-owns')]
      .join(' ').split(/\\s+/).filter(Boolean);
    const named=ids.map(id => root.getElementById?.(id)).filter(Boolean);
    return named.length ? named : [document];
  };
  const optionsShown = () => scopes().some(scope => [...scope.querySelectorAll('[role="option"]')].some(o => {
    const r=o.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
    return o.checkVisibility() && r.width>0 && r.height>0 && x>=0 && y>=0 && x<innerWidth && y<innerHeight;
  }));
  let frames=0, done=false;
  const frame = () => {
    if (done) return;
    frames++;
    if (frames>=2 && (!combo || optionsShown())) { done=true; resolve(true); }
    else requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  setTimeout(() => { done=true; resolve(false); }, combo ? 200 : 50);
}))"""

# [document, mutation count] of the registry, or null before the first look. The AX tree is read
# again only when this moved since the last read.
STAMP_JS = """(() => { const reg=window.__ckPage;
  if (!reg?.observer) return null;
  if (reg.observer.takeRecords().length) reg.mutations++;
  return [performance.timeOrigin, reg.mutations]; })()"""

# A tree older than this is read again even on a page that did not mutate (a CSS animation can
# show or hide a control without touching the DOM).
AX_MAX_AGE = 2.0

# body.innerText leaves out what shadow roots render (open, or closed and registered by the
# look's discovery), so each root's children add theirs.
STRINGS_JS = """(() => {
  const parts=[document.body?.innerText || ''], roots=[];
  const shadow = e => e.shadowRoot || window.__ckPage?.closedRoots?.get(e) || null;
  const walk = root => { for (const e of root.querySelectorAll('*')) if (shadow(e)) {
    roots.push(shadow(e)); walk(shadow(e)); } };
  walk(document);
  const up = e => e.parentElement || e.getRootNode().host || null;
  const away = e => { for (let p=e; p; p=up(p)) if (p.matches('[aria-hidden="true"],[inert]')) return true;
    return false; };
  for (const root of roots) for (const e of root.childNodes)
    if (e.nodeType===3) { if (root.host.checkVisibility()) parts.push(e.textContent); }
    else if (e.nodeType===1 && !['STYLE','SCRIPT','TEMPLATE'].includes(e.tagName) && e.checkVisibility())
      parts.push(e.innerText || '');
  const named=[document,...roots].flatMap(root=>[...root.querySelectorAll('[aria-label],[placeholder],[title],[alt]')]);
  for (const e of named) {
    if (away(e) || !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) continue;
    for (const name of ['aria-label','placeholder','title','alt']) {
      const value=e.getAttribute(name);
      if (value) parts.push(value);
    }
  }
  return parts.join('\\n');
})()"""

FRAMES_JS = """(() => new Promise(resolve => {
  setTimeout(resolve, 50);
  requestAnimationFrame(() => requestAnimationFrame(resolve));
}))()"""

FILE_INPUTS_JS = """(() => {
  const reg=window.__ckPage;
  if (!reg) return [];
  const visible = e => !!e && !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const controls = input => input.id && [...document.querySelectorAll('[aria-controls]')].find(e =>
    visible(e) && (e.getAttribute('aria-controls')||'').split(/\\s+/).includes(input.id));
  // Only an explicit relation, or a parent that holds this one input, says which control opens it.
  const opener = input => [...(input.labels||[])].find(visible) || controls(input) ||
    (input.parentElement.querySelectorAll('input[type=file]').length===1
      ? [...input.parentElement.querySelectorAll('button,label,[role="button"]')].find(visible) : null)
    || null;
  return [...document.querySelectorAll('input[type=file]')].map(input => {
    const trigger=opener(input);
    return {node:reg.identify(input), accept:input.accept, multiple:input.multiple,
      trigger:trigger ? reg.identify(trigger) : null,
      label:(trigger ? (trigger.getAttribute('aria-label')||trigger.innerText||'').trim() : '') ||
        input.getAttribute('aria-label') || input.name || 'file input'};
  });
})()"""

SCROLL_INTO_VIEW_JS = """(node => {
  const e=window.__ckPage.byId.get(node);
  if (!e?.isConnected) return false;
  e.scrollIntoView({block:'center',inline:'center'});
  return true;
})"""

# Panels that scroll on their own (a picker grid, a side list): what they clip is cut off on a
# stretched page too, since the panel keeps its height. Largest on-screen panels first.
INNER_SCROLLERS_JS = """(() => {
  const reg=window.__ckPage;
  if (!reg || !document.body) return [];
  const found=[];
  for (const e of document.body.querySelectorAll('*')) {
    if (e.scrollHeight<=e.clientHeight+2 || e===document.scrollingElement) continue;
    if (!['auto','scroll','overlay'].includes(getComputedStyle(e).overflowY)) continue;
    const r=e.getBoundingClientRect();
    if (r.width<=0 || r.height<=0 || r.bottom<=0 || r.right<=0 || r.top>=innerHeight ||
        r.left>=innerWidth || !e.checkVisibility()) continue;
    found.push({node:reg.identify(e), top:e.scrollTop, area:r.width*r.height});
  }
  return found.sort((a,b)=>b.area-a.area).slice(0,3);
})()"""

SCROLL_INNER_JS = """(spec => {
  const e=window.__ckPage?.byId.get(spec.node);
  if (!e?.isConnected) return false;
  const before=e.scrollTop;
  const top=spec.to ?? before+Math.max(1,Math.floor(e.clientHeight*0.8));
  e.scrollTo({top, behavior:'instant'});
  return e.scrollTop!==before;
})"""

DROP_WATCH_JS = """(() => {
  const reg=window.__ckPage;
  reg.dropSeen=false;
  if (!reg.dropWatching) {
    reg.dropWatching=true;
    document.addEventListener('drop', () => reg.dropSeen=true, {capture:true});
  }
  return true;
})()"""
# `undefined !== false`: a drop that navigated away took the registry with it.
DROP_SEEN_JS = "window.__ckPage?.dropSeen !== false"

# Whether a press reached the page at all: Chrome drops input to a tab it shows a dialog over
# (a password-breach warning, say) and still answers the dispatch. Any of the events counts, so
# one page listener that stops propagation does not hide the press.
PRESS_WATCH_JS = """(() => {
  const reg=window.__ckPage;
  if (!reg) return false;
  reg.pressSeen=false;
  if (!reg.pressWatching) {
    reg.pressWatching=true;
    for (const type of ['pointerdown','mousedown','pointerup','mouseup','click'])
      window.addEventListener(type, () => reg.pressSeen=true, {capture:true});
  }
  return true;
})()"""
PRESS_SEEN_JS = "window.__ckPage?.pressSeen !== false"

DND_JS = """(spec => {
  const reg=window.__ckPage, from=reg.byId.get(spec.from), to=reg.byId.get(spec.to);
  if (!from?.isConnected || !to?.isConnected) return false;
  const data=new DataTransfer();
  const fire=(node,type,point)=>node.dispatchEvent(new DragEvent(type,{bubbles:true,cancelable:true,
    composed:true,dataTransfer:data,clientX:point.x,clientY:point.y}));
  fire(from,'dragstart',spec.start);
  for (const type of ['dragenter','dragover','drop']) fire(to,type,spec.end);
  fire(from,'dragend',spec.end);
  return true;
})"""

# What has keyboard focus, followed into shadow roots (closed ones the registry holds) and
# same-origin frames. Names the element, never reads what is typed in it.
FOCUS_JS = """(() => {
  let e=document.activeElement;
  for (;;) {
    const inner=(e?.shadowRoot || window.__ckPage?.closedRoots?.get(e))?.activeElement
      || (e?.tagName==='IFRAME' ? e.contentDocument?.activeElement : null);
    if (!inner || inner===e || inner===inner.ownerDocument.body) break;
    e=inner;
  }
  if (!e || e===e.ownerDocument.body || e===e.ownerDocument.documentElement) return null;
  const tag=e.tagName.toLowerCase(), type=(e.getAttribute('type')||'text').toLowerCase();
  const field=['input','textarea','select'].includes(tag) || e.isContentEditable;
  const roles={textarea:'textbox',select:'combobox',button:'button',summary:'button',
    a:e.hasAttribute('href')?'link':null};
  const inputs={checkbox:'checkbox',radio:'radio',search:'searchbox',range:'slider',number:'spinbutton',
    button:'button',submit:'button',reset:'button',image:'button'};
  const role=e.getAttribute('role') || (tag==='input' ? inputs[type]||'textbox' : roles[tag])
    || (e.isContentEditable ? 'textbox' : null);
  const clean=t=>(t||'').replace(/\\s+/g,' ').trim();
  const label=clean(e.getAttribute('aria-label'))
    || clean([...(e.labels||[])].map(l=>l.innerText).join(' '))
    || clean(e.getAttribute('placeholder'))
    || (field ? '' : clean(e.innerText).slice(0,40));
  return {tag, role, label, password: tag==='input' && type==='password'};
})()"""

GONE = "The target is no longer on the page. Observe again."
MOVED = "The page changed after this decision. Observe again."


# -- observing ------------------------------------------------------------------------------------


def observe(tab, drop_targets=False, text_holders=False):
    """One look at the viewport: {url, title, text, actions, marker, page_key, guards, discovery, ...}.

    A pending input is settled first; a look that throws or finds no body (a navigation midway)
    is taken again, up to OBSERVE_TRIES times.
    """
    pending, tab.after_input = tab.after_input, None
    if pending is not None:
        try:
            tab.evaluate(SETTLE_JS + f"({json.dumps(pending)})", await_promise=True)
        except (StepTimeout, PageCrashed):
            raise
        except (StalePage, CdpError):
            pass  # a press that navigated: the look below waits for the new document
    options = {"drop_targets": drop_targets, "text_holders": text_holders}
    for attempt in range(OBSERVE_TRIES):
        if attempt:
            time.sleep(OBSERVE_PAUSE)
        try:
            page, why = _snapshot(tab, options, force=pending is not None)
        except StalePage:
            continue
        except (StepTimeout, PageCrashed):
            raise
        except CdpError:
            if tab.cdp.closed:
                raise
            continue
        if page is None:
            continue
        listeners = bool(page.pop("discovery_listeners", False))
        reasons = list(why.values())
        if not listeners:
            reasons.append("getEventListeners is not available in this page")
        page["discovery"] = {"ax": "ax" not in why, "listeners": listeners, "why": "; ".join(reasons) or None}
        page["fingerprint"] = fingerprint(page)
        return page
    raise StalePage(f"The page kept changing while it was read ({OBSERVE_TRIES} looks). Observe again.")


def _snapshot(tab, options, force=False):
    """One look fed Chrome's signals: the AX tree, then once more if elements or closed roots
    had to be registered first. Returns (page or None, {signal: why it is missing}).

    The AX tree costs ~800 ms on a 5,000-element page, so it is read only when the page may have
    changed: a DOM mutation (the registry's count), a pending input (`force`), or a tree older
    than AX_MAX_AGE. Steady state on an unchanged page is two CDP calls (stamp + look). A
    signal that fails is missing for this observe; one refused by the transport stays off for the
    tab, so a step never pays for it twice.
    """
    off = tab.__dict__.setdefault("discovery_off", {})
    why = {}

    def fetch():
        try:
            return signals.ax_nodes(tab.call("Accessibility.getFullAXTree"))
        except CdpError as exc:
            tab.ax_stamp = None  # no tree to reuse: the next observe reads it again
            if isinstance(exc, (StepTimeout, PageCrashed)):
                raise
            why["ax"] = f"{exc}"
            if signals.refused(exc):
                off["ax"] = why["ax"]
            return None

    def remember(stamp):  # the stamp read before a fetch: a mutation after it shows next time
        tab.ax_stamp, tab.ax_time = stamp, time.monotonic()

    def look(options):
        return tab.evaluate(PAGE_JS + f"({json.dumps(options)})", command_line_api=True)

    if "ax" in off:
        why["ax"] = off["ax"]
        options = {**options, "ax": None}
    else:
        stamp = tab.evaluate(STAMP_JS)
        stale = (force or stamp is None or stamp != getattr(tab, "ax_stamp", None)
                 or time.monotonic() - getattr(tab, "ax_time", 0) > AX_MAX_AGE)
        # A page with no registry yet has no stamp: one look without the tree sets it up first.
        if stale and stamp is not None:
            remember(stamp)
            options = {**options, "ax": fetch()}
    page = look(options)
    if page is None:
        return None, why
    if "ax" not in options and [page["marker"][0], page.get("stamp")] != stamp:
        remember([page["marker"][0], page.get("stamp")])
        options = {**options, "ax": fetch()}
        page = look(options)
        if page is None:
            return None, why
    again = False
    if page.get("unscanned") and "closed" not in off:
        try:
            signals.scan_closed_roots(tab)
            again = True
        except CdpError as exc:
            if isinstance(exc, PageCrashed):
                raise
            if signals.refused(exc):
                off["closed"] = f"closed shadow roots not read ({exc})"
    if off.get("closed"):
        why["closed"] = off["closed"]
    if page.get("ax_missing"):
        try:
            signals.register(tab, page["ax_missing"])
        except CdpError as exc:
            if not signals.refused(exc):
                raise
            why["ax"] = off["ax"] = f"{exc}"
            options["ax"] = None
        again = True
    if again:
        settled = [page["marker"][0], page.get("stamp")] == getattr(tab, "ax_stamp", None)
        page = look(options)
        # Registering mutates nothing, but the closed roots it found are observed for the first
        # time and bump the stamp. The tree read already covers them: keep it current.
        if settled and page is not None:
            tab.ax_stamp = [page["marker"][0], page.get("stamp")]
    if page is not None:
        page.pop("ax_missing", None)
        page.pop("unscanned", None)
        page.pop("stamp", None)
    return page, why


def observe_full_page(tab, max_height=10000, drop_targets=False, text_holders=False):
    """Observe with the viewport stretched to the whole document.

    The look keeps only elements whose centre is inside the viewport, so a
    whole-page assertion (expect_count) has to widen the window before it looks.
    """
    scroll_height, scroll_y = tab.evaluate("[document.documentElement.scrollHeight, scrollY]")
    tab.set_viewport(tab.width, min(int(scroll_height), max_height))
    try:
        tab.evaluate(FRAMES_JS, await_promise=True)
        page = observe(tab, drop_targets=drop_targets, text_holders=text_holders)
    finally:
        tab.set_viewport(tab.width, tab.height)
        tab.evaluate(f"window.scrollTo(0,{int(scroll_y)})")
    page["full_page"] = True
    page["scroll_height"] = scroll_height
    return page


def fresh(tab, page, action=None):
    """Whether `page` still describes the tab: for a targeted action its target and every field,
    otherwise the marker (controls, url, scroll, viewport, fields; not the text)."""
    if action is not None and action.get("kind") in TARGETED:
        node = action.get("node")
        if type(node) is not int:
            return False
        return tab.evaluate(FRESH_JS + f"({node})") == [page["page_key"], page["guards"].get(str(node))]
    return tab.evaluate(MARKER_JS, command_line_api=True) == page["marker"]


def fingerprint(page):
    """A digest of what the page shows; callers only compare two of them."""
    seen = {key: page.get(key) for key in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(seen, sort_keys=True).encode()).hexdigest()


def visible_strings(tab):
    """Everything a human can read: innerText plus visible aria-label/placeholder/title/alt.

    innerText covers the whole document and ignores how the text is split across elements,
    which the look's node-by-node text walker cannot.
    """
    return tab.evaluate(STRINGS_JS) or ""


def near_misses(tab, text):
    """Why the page elements whose words are nearest `text` were not offered, best first (<= 8).

    Each is {tag, role, text, shadow_host, visible, hit, ax_role, listener, offered, why}; needs a
    prior observe.
    """
    return tab.evaluate("window.__ckPage?.whyNot?.(" + json.dumps(text) + ") ?? []") or []


def file_inputs(tab):
    """Every <input type=file> on the page, hidden ones included, named by what opens it.

    The look drops file inputs (and everything invisible), so upload asks for them
    separately; `trigger` is the observed node of the visible control that opens the picker.
    """
    return tab.evaluate(FILE_INPUTS_JS)


def focused(tab):
    """{tag, role, label, password} of the element with keyboard focus, or None on body/nothing."""
    return tab.evaluate(FOCUS_JS)


def inner_scrollers(tab):
    """[{node, top, area}] for the on-screen panels that scroll independently of the page."""
    return tab.evaluate(INNER_SCROLLERS_JS) or []


def scroll_inner(tab, node, to=None):
    """Scroll one panel down by most of its height, or back `to` a scrollTop; True if it moved."""
    return bool(tab.evaluate(SCROLL_INNER_JS + f"({json.dumps({'node': node, 'to': to})})"))


def scroll_into_view(tab, node):
    """Bring an element the viewport does not show into it; observe again afterwards, since
    everything on the page has moved."""
    if type(node) is not int:
        raise ValueError(f"not an observed node number: {node!r}")
    if not tab.evaluate(SCROLL_INTO_VIEW_JS + f"({json.dumps(node)})"):
        raise StalePage(GONE)


# -- acting ---------------------------------------------------------------------------------------


def act(tab, action, page, text=None):
    """Carry out one action; `text` is what a fill or a type enters (never kept on the action)."""
    kind = action["kind"]
    if kind in TARGETED and not fresh(tab, page, action):
        raise StalePage(MOVED)
    if kind == "wait":
        time.sleep(0.1)
        tab.after_input = None
        return action
    if kind == "scroll":
        tab.call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
    elif kind == "press":
        _key(tab, action["key"])
    elif kind == "type":
        tab.call("Input.insertText", text=text)
    elif kind == "select":
        # page.js writes the value by script: the tab's policy sees the node first.
        tab.check_write(action["node"])
        try:
            _point(tab, action)
        except Covered:
            raise StalePage("The dropdown is under another element now. Observe again.") from None
    elif kind == "hover":
        tab.call("Input.dispatchMouseEvent", type="mouseMoved", **_point(tab, action))
    elif kind in ("click", "fill"):
        point = _point(tab, action)
        tab.evaluate(PRESS_WATCH_JS)
        for phase in ("mousePressed", "mouseReleased"):
            tab.call("Input.dispatchMouseEvent", type=phase, button="left", clickCount=1, **point)
        _reached(tab, action, point)
        if kind == "fill":
            _replace_text(tab, text)
    else:
        raise ValueError(f"unknown action kind {kind!r}")
    tab.after_input = action
    return action


def _replace_text(tab, text):
    """Select everything in the focused field, then type over it; an empty value deletes it
    (insertText of "" types nothing)."""
    keys = {"key": "a", "code": "KeyA", "modifiers": 4 if sys.platform == "darwin" else 2}
    tab.call("Input.dispatchKeyEvent", type="keyDown", commands=["selectAll"], **keys)
    tab.call("Input.dispatchKeyEvent", type="keyUp", **keys)
    if text:
        tab.call("Input.insertText", text=text)
    else:
        _key(tab, "Delete")


def _point(tab, action):
    """The point to press for an action's target, now."""
    if type(action.get("node")) is not int:
        raise ValueError(f"not an observed node number: {action.get('node')!r}")
    found = tab.evaluate(TARGET_JS + "(" + json.dumps(action) + ")")
    if found is None:
        raise StalePage(GONE)
    if found.get("covered"):
        raise Covered("Another element is on top of the target now. Observe again.")
    return {"x": found["x"], "y": found["y"]}


def drag(tab, source, target, page):
    """Drag one observed element onto another; returns which gesture moved the page.

    Real pointer input first: press, five interpolated moves, release. On an HTML5 page that
    starts a native dragstart which often never lands — the library sees a start without a drop —
    and the moves alone change the page on their way (hover states, highlights), so the verdict is
    a `drop` event watched for on the document, not the page moving. No drop means the same
    gesture is replayed as synthetic dragstart/dragenter/dragover/drop/dragend sharing one
    DataTransfer.
    """
    if not fresh(tab, page, source):
        raise StalePage(MOVED)
    start, end = _point(tab, source), _point(tab, target)
    if target.get("aside"):
        # Bringing the target in from the side slides its panel under the source: press where the
        # source is now, never on whatever moved under its old point.
        try:
            start = _point(tab, {**source, "aside": False})
        except StalePage:  # slid out of view, or under something
            raise Apart("the drag's ends are not on screen together: bringing the target into view"
                        " slid the source away") from None
    tab.evaluate(DROP_WATCH_JS)
    tab.call("Input.dispatchMouseEvent", type="mousePressed", button="left", clickCount=1, **start)
    for move in range(1, DRAG_MOVES + 1):
        tab.call("Input.dispatchMouseEvent", type="mouseMoved", button="left",
                 x=start["x"] + (end["x"] - start["x"]) * move / DRAG_MOVES,
                 y=start["y"] + (end["y"] - start["y"]) * move / DRAG_MOVES)
        time.sleep(DRAG_INTERVAL)
    tab.call("Input.dispatchMouseEvent", type="mouseReleased", button="left", clickCount=1, **end)
    tab.after_input = source
    try:
        # A frame before the verdict: a page that drops on the next tick must not be dragged twice.
        tab.evaluate(FRAMES_JS, await_promise=True)
        landed = tab.evaluate(DROP_SEEN_JS)
    except StalePage:
        return "pointer"  # the document is being replaced: the drop did something
    if landed:
        return "pointer"
    dropped = tab.evaluate(DND_JS + "(" + json.dumps(
        {"from": source["node"], "to": target["node"], "start": start, "end": end}) + ")")
    if not dropped:
        raise StalePage(GONE)
    return "dnd"


def upload(tab, node, path, page):
    """Set a file on an observed <input type=file> over CDP; the picker never opens."""
    if not fresh(tab, page):
        raise StalePage(MOVED)
    if type(node) is not int:
        raise ValueError(f"not an observed node number: {node!r}")
    tab.call("DOM.setFileInputFiles", files=[path], objectId=_object_id(tab, node))


def _reached(tab, action, point):
    """Raise Unreached when the press just dispatched never got to the page.

    Chrome acknowledges the dispatch only after the page handled it, so the watch is current.
    `undefined !== false`: a press that navigated took the registry with it, and so did happen.
    A press on a frame goes to the frame's document, which this window never hears from.
    """
    try:
        reached = tab.evaluate(
            PRESS_SEEN_JS + " || ['IFRAME','FRAME','EMBED','OBJECT']"
            f".includes(document.elementFromPoint({float(point['x'])}, {float(point['y'])})?.tagName)")
    except StalePage:  # the document is being replaced: the press did something
        return
    if not reached:
        raise Unreached(f"the {action['kind']} never reached the page: Chrome dropped the input,"
                        " as it does while a browser dialog is over the tab")


def _key(tab, key):
    """One key down and up — the press verb, and clearing a field the same way a human would.

    `key` is a KEYS name or one letter/digit, optionally after `+`-joined modifiers
    ("Shift+Tab", "Control+a"); a chord with Alt/Control/Meta types no text.
    """
    *held, name = key.split("+")
    try:
        modifiers = sum({MODIFIERS[m] for m in held})
    except KeyError:
        modifiers = None
    if name in KEYS:
        code, virtual_key, key_text = KEYS[name]
    elif len(name) == 1 and name.isascii() and name.isalnum() and modifiers is not None:
        upper = name.upper()
        code = f"Digit{name}" if name.isdigit() else f"Key{upper}"
        virtual_key, key_text = ord(upper), upper if modifiers & 8 else name
        name = key_text
    else:
        modifiers = None
    if modifiers is None:
        raise ValueError(f"unknown key {key!r}; known keys: {', '.join(sorted(KEYS))}, or one letter or "
                         f"digit, after optional modifiers ({'/'.join(MODIFIERS)}) joined with '+'")
    if modifiers & 7:
        key_text = None
    down = {"type": "keyDown", "key": name, "code": code, "windowsVirtualKeyCode": virtual_key}
    up = {"type": "keyUp", "key": name, "code": code, "windowsVirtualKeyCode": virtual_key}
    if modifiers:
        down["modifiers"] = up["modifiers"] = modifiers
    if key_text is not None:
        down["text"] = key_text
    tab.call("Input.dispatchKeyEvent", **down)
    tab.call("Input.dispatchKeyEvent", **up)


def _object_id(tab, node):
    """A live handle to an observed node, for the CDP commands that take one."""
    result = tab.call("Runtime.evaluate", expression=f"window.__ckPage.byId.get({json.dumps(node)})")
    object_id = (result.get("result") or {}).get("objectId")
    if object_id is None:
        raise StalePage(GONE)
    return object_id

"""Chrome's own signals for candidate discovery: the accessibility tree and closed shadow roots.

The snapshot decides what is offered; this module only feeds it. Every call here may fail (an
extension bridge without the method, a navigation mid-way) and discovery then falls back to what
the snapshot reads on its own. The parsing reads only the fields the extension bridge keeps.
"""

from .cdp import CdpError

# The one list of widget roles (ARIA plus Chrome's internal names): only AX nodes with one of these
# reach the snapshot, so every `ax` entry there is a widget.
WIDGET_ROLES = frozenset((
    "button", "link", "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "combobox", "textbox", "searchbox", "spinbutton", "slider",
    "treeitem", "gridcell", "togglebutton", "popupbutton", "disclosuretriangle", "comboboxselect",
    "comboboxmenubutton", "menulistoption", "listboxoption", "colorwell", "date", "datetime",
    "inputtime",
))

GROUP = "ck-browser"

# Registers resolved objects in the page cache: an element under its backend id plus the closed
# roots on its chain, a closed ShadowRoot under its host. A scan also remembers the defined custom
# tags that have no closed root, so a long list of them does not rescan; a tag defined later does.
# `.mode` is never read: on a user-agent root (a date input's parts, a video's controls) it crashes
# Chrome's renderer. A hidden root whose host cannot attach one is the user agent's, and nothing in
# it is registered: its internals are not page controls, the host is offered on its own.
REGISTER_JS = """function(scan, ids, ...objects) {
  const c=window.__ckPage;
  if (!c) return false;
  const attachable=new Set(['article','aside','blockquote','body','div','footer','h1','h2','h3','h4',
    'h5','h6','header','main','nav','p','section','span']);
  objects.forEach((o,i) => {
    const closed=[];
    for (let r=(o instanceof ShadowRoot ? o.host : o).getRootNode(); r instanceof ShadowRoot;
         r=r.host.getRootNode()) {
      if (r.host.shadowRoot===r) continue;
      if (!r.host.localName.includes('-') && !attachable.has(r.host.localName)) return;
      closed.push(r);
    }
    if (o instanceof ShadowRoot) c.closedRoots.set(o.host,o);
    else if (ids[i]!=null) c.byBackend.set(ids[i],o);
    for (const r of closed) c.closedRoots.set(r.host,r);
  });
  if (scan) {
    c.scanDone=true;
    const walk = root => { for (const e of root.querySelectorAll('*')) {
      const s=e.shadowRoot || c.closedRoots.get(e);
      if (s) walk(s);
      else if (e.localName.includes('-') && customElements.get(e.localName)) c.rootlessTags.add(e.localName);
    } };
    walk(document);
  }
  return true;
}"""


def refused(exc):
    """A failure that will repeat on this tab (method not allowed or not known), not a navigation."""
    text = str(exc)
    return "not allowed" in text or "wasn't found" in text


def ax_nodes(result):
    """{backendNodeId: [role, name]} for widget-role AX nodes.

    Focusable-only nodes stay out: the snapshot reads tabindex itself, and the closed roots they
    sit in are registered by the scan.
    """
    out = {}
    for node in result.get("nodes") or []:
        backend = node.get("backendDOMNodeId")
        if node.get("ignored") or backend is None:
            continue
        role = str((node.get("role") or {}).get("value") or "")
        if role.lower() == "rootwebarea":
            continue
        if role.lower() in WIDGET_ROLES:
            out[str(backend)] = [role, (node.get("name") or {}).get("value") or ""]
    return out


def closed_roots(node, out=None):
    """Backend ids of every closed shadow root under a described node (children and shadowRoots)."""
    out = [] if out is None else out
    for root in node.get("shadowRoots") or []:
        if root.get("shadowRootType") == "closed" and root.get("backendNodeId") is not None:
            out.append(root["backendNodeId"])
        closed_roots(root, out)
    for child in node.get("children") or []:
        closed_roots(child, out)
    return out


def register(tab, ids, scan=False, this=None):
    """Resolve backend ids (pipelined) and register them in one callFunctionOn; unresolvable ids
    are skipped."""
    objects, kept = [], []
    try:
        resolved = tab.call_many("DOM.resolveNode",
                                 [{"backendNodeId": backend, "objectGroup": GROUP} for backend in ids])
        for backend, result in zip(ids, resolved):
            if isinstance(result, CdpError):
                if refused(result):
                    raise result
                continue
            objects.append(result["object"]["objectId"])
            kept.append(backend)
        target = this or (objects[0] if objects else None)
        if target is None:
            return
        tab.call("Runtime.callFunctionOn", objectId=target, functionDeclaration=REGISTER_JS,
                 arguments=[{"value": scan}, {"value": kept if not scan else [None] * len(kept)}]
                 + [{"objectId": o} for o in objects], returnByValue=True)
    finally:  # cleanup never blocks or fails a step
        tab.send_only("Runtime.releaseObjectGroup", objectGroup=GROUP)


def scan_closed_roots(tab):
    """Find closed roots in the document and register them; marks the document scanned."""
    try:
        document = tab.call("Runtime.evaluate", expression="document",
                            objectGroup=GROUP)["result"]["objectId"]
        described = tab.call("DOM.describeNode", objectId=document, depth=-1, pierce=True)["node"]
    except CdpError:
        tab.send_only("Runtime.releaseObjectGroup", objectGroup=GROUP)
        raise
    register(tab, closed_roots(described), scan=True, this=document)

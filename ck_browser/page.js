/* One look at the page for a decision step: the elements a click, a fill or a select can reach,
   the text a reader sees, and what Python compares later to tell whether the page held still.
   Called as SCRIPT({ax, drop_targets, text_holders}); null while the document has no body. */
(options => {
  options = options || {};
  if (!document.body) return null;

  // -- the registry: kept on the window until the document unloads -------------------------------
  const reg = window.__ckPage ||= {byId: new Map(), idOf: new WeakMap(), lastId: 0};
  reg.byBackend ||= new Map(); reg.closedRoots ||= new WeakMap(); reg.rootlessTags ||= new Set();
  for (const [id, e] of reg.byBackend) if (!e.isConnected) reg.byBackend.delete(id);
  for (const [id, e] of reg.byId) if (!e.isConnected) reg.byId.delete(id);
  // One number per element for the life of the document; Python names elements only by it.
  const identify = e => {
    let id = reg.idOf.get(e);
    if (id === undefined) reg.idOf.set(e, id = ++reg.lastId);
    reg.byId.set(id, e);
    return id;
  };
  reg.identify = identify;
  // A closed root Python registered is read like an open one.
  const shadow = e => e.shadowRoot || reg.closedRoots.get(e) || null;
  // Every change under a root this script has walked moves the count; a root seen for the first
  // time counts as a change. Python reads the accessibility tree again only when it moved.
  reg.mutations ??= 0; reg.observedRoots ||= new WeakSet();
  reg.observer ||= new MutationObserver(() => reg.mutations++);
  const watch = root => {
    if (reg.observedRoots.has(root)) return;
    reg.observedRoots.add(root);
    reg.mutations++;
    reg.observer.observe(root, {subtree: true, childList: true, attributes: true});
  };
  watch(document);

  // -- the composed tree and what a reader can see ------------------------------------------------
  const shown = e => e.type === 'password' ? (e.value ? '••••' : '') : e.value;
  const parent = e => e.parentElement || e.getRootNode().host || null;
  const closest = (e, sel) => { for (let p = e; p; p = parent(p)) if (p.matches(sel)) return p; return null; };
  const contains = (a, b) => { for (let p = b; p; p = parent(p)) if (p === a) return true; return false; };
  const deepHit = (x, y) => {
    let hit = document.elementFromPoint(x, y);
    while (hit && shadow(hit)) {
      const inner = shadow(hit).elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    return hit;
  };
  const deep = (root, out = []) => {
    for (const e of root.querySelectorAll('*')) {
      out.push(e);
      if (shadow(e)) { watch(shadow(e)); deep(shadow(e), out); }
    }
    return out;
  };
  const tagc = e => e.tagName.toLowerCase() + (e.classList[0] ? '.' + e.classList[0] : '');
  const hidden = e => closest(e, '[aria-hidden="true"]') ? 'inside aria-hidden' :
    closest(e, '[inert]') ? 'inert' :
    e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) ? null :
    !e.checkVisibility() ? 'display: none' :
    !e.checkVisibility({checkVisibilityCSS: true}) ? 'visibility: hidden' : 'opacity 0';
  const visible = e => !hidden(e);
  const safe = e => !(e.tagName === 'INPUT' && ['file', 'hidden'].includes(e.type));
  const centre = r => ({x: r.x + r.width / 2, y: r.y + r.height / 2});
  const onScreen = (x, y) => x >= 0 && y >= 0 && x < innerWidth && y < innerHeight;
  const QUIET = ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'];
  const RECORDS = 'tr,li,[role="row"],[role="listitem"]';

  // A cut inside a run of token characters runs on to the run's end (at most 4096 more): the
  // extension masks whole tokens, and a fragment is too short to look like a secret.
  const cut = (text, n) => text.slice(0, n + (/[A-Za-z0-9+/=_-]$/.test(text.slice(0, n)) ?
    text.slice(n, n + 4096).match(/^[A-Za-z0-9+/=_-]*/)[0].length : 0));
  const head = text => cut((text || '').replace(/\s+/g, ' ').trim(), 40);
  const own = e => head([...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));

  const all = deep(document);
  // Role-less options (react-select's divs): the element a visible field names as its active
  // descendant, and its siblings.
  const listed = new Set();
  for (const field of all) {
    if (!field.hasAttribute('aria-activedescendant') || !visible(field)) continue;
    const active = field.getRootNode().getElementById(field.getAttribute('aria-activedescendant'));
    if (active) for (const e of (active.parentElement || active.getRootNode()).children) listed.add(e);
  }

  // -- roles and names ------------------------------------------------------------------------------
  const WIDGETS = ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemradio',
    'option', 'gridcell', 'combobox', 'textbox', 'searchbox', 'spinbutton'];
  const selector = ['a[href]', 'button', 'input', 'textarea', 'select', 'summary', '[contenteditable="true"]',
    '[id*="-option-"]', ...WIDGETS.map(r => `[role="${r}"]`),
    ...(options.drop_targets ? ['[draggable="true"]'] : [])].join(',');
  const INPUT_ROLES = {checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button', reset: 'button',
    image: 'button', search: 'searchbox', number: 'spinbutton', text: 'textbox', email: 'textbox',
    url: 'textbox', tel: 'textbox', password: 'textbox'};
  // The role the fixed tag/ARIA list gives, or null.
  const role = e => {
    const explicit = e.getAttribute('role');
    if (WIDGETS.includes(explicit)) return explicit;
    switch (e.tagName) {
      case 'BUTTON': case 'SUMMARY': return 'button';
      case 'A': if (e.hasAttribute('href')) return 'link'; break;
      case 'SELECT': return 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'INPUT': if (INPUT_ROLES[e.type]) return INPUT_ROLES[e.type]; break;
    }
    if (e.isContentEditable) return 'textbox';
    if (listed.has(e) || /-option-\d+$/.test(e.id)) return 'option';
    if (options.drop_targets && e.getAttribute('draggable') === 'true') return 'draggable';
    return null;
  };
  // Accessible-name style: labelledby, aria-label, labels, button value, alt, text, title, placeholder.
  const name = (e, seen = new Set()) => {
    if (seen.has(e)) return '';
    seen.add(e);
    const root = e.getRootNode();
    const refs = (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => root.getElementById?.(id)).filter(Boolean);
    const byRefs = refs.map(r => name(r, seen)).join(' ').trim();
    if (byRefs) return byRefs;
    const aria = (e.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const byLabels = [...(e.labels || [])].map(l => name(l, seen)).join(' ').trim();
    if (byLabels) return byLabels;
    if (e.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(e.type) && e.value) return e.value;
    const alt = (e.getAttribute('alt') || '').trim();
    if (alt) return alt;
    if (e.tagName !== 'INPUT' && e.tagName !== 'SELECT') {
      const parts = [];
      for (const n of e.childNodes) {
        if (n.nodeType === 3) parts.push(n.textContent);
        else if (n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true' && !QUIET.includes(n.tagName))
          parts.push(name(n, seen));
      }
      const text = parts.join(' ').replace(/\s+/g, ' ').trim();
      if (text) return text;
    }
    return (e.getAttribute('title') || e.getAttribute('placeholder') || '').trim();
  };
  // An icon-only control is called after what sits beside it.
  const nearby = e => head([...e.querySelectorAll('svg,img,[aria-label],[title]')]
    .map(c => c.getAttribute('aria-label') || c.getAttribute('title') || c.getAttribute('alt') ||
      c.querySelector('title')?.textContent)
    .find(Boolean) || e.previousElementSibling?.innerText || e.parentElement?.innerText || '');

  // -- Chrome's signals ---------------------------------------------------------------------------
  // The marker probe passes no `ax` and must see the same candidates, so the last map is kept.
  // getEventListeners exists only inside this evaluate and is never stored.
  const ax = 'ax' in options ? (reg.lastAx = options.ax) : reg.lastAx;
  const axOf = new Map(), axMissing = [];
  if (ax) for (const id in ax) {
    const e = reg.byBackend.get(+id);
    if (e) axOf.set(e, ax[id]); else if ('ax' in options) axMissing.push(+id);
  }
  const gel = typeof getEventListeners === 'function' ? getEventListeners : null;
  const PRESSES = ['click', 'mousedown', 'mouseup', 'pointerdown', 'pointerup'];
  // Frameworks that dispatch from a root keep the press handler on the node itself: React props
  // (`__reactEventHandlers$` before 17), Svelte 5 `__click`, Solid `$$click`. Own keys only; touch
  // handlers never see a mouse click and capture-phase props are panel plumbing.
  const REACT_PRESS = ['onClick', 'onMouseDown', 'onMouseUp', 'onPointerDown', 'onPointerUp'];
  const KEPT_PRESS = PRESSES.flatMap(t => ['__' + t, '$$' + t]);
  const framework = e => Object.keys(e).some(k => /^__react(Props|EventHandlers)\$/.test(k) ?
    REACT_PRESS.some(p => typeof e[k]?.[p] === 'function') : KEPT_PRESS.includes(k) && e[k] != null);
  const pressable = reg.pressable = new Set();
  for (const e of all) {
    const heard = gel ? gel(e) : {};
    if (PRESSES.some(t => heard[t]?.length) || framework(e)) pressable.add(e);
  }
  const tabbable = e => (e.hasAttribute('tabindex') && e.tabIndex >= 0) || e.isContentEditable;
  // Found only by a listener or a tabindex: the noise rules below apply to it alone.
  const loose = e => !role(e) && !axOf.get(e);
  const legacy = e => (e.matches(selector) || listed.has(e)) && !!role(e);
  const roleOf = e => (legacy(e) && role(e)) || axOf.get(e)?.[0].toLowerCase() || 'clickable';
  const miss = ax && gel ? "no widget role in Chrome's accessibility tree, no click listener, not focusable" :
    gel ? "no interactive role, no click listener, not focusable (Chrome's accessibility tree was not read)" :
    ax ? "no widget role in Chrome's accessibility tree, no framework press handler, not focusable (click listeners were not read)" :
    'no interactive role, no framework press handler, not focusable (neither the accessibility tree nor click listeners were read)';
  // A native select's options are chosen through the select, never one by one.
  const candidates = new Set(all.filter(e => legacy(e) ||
    ((axOf.get(e) || tabbable(e) || pressable.has(e)) && !(e.tagName !== 'SELECT' && closest(e, 'select')))));
  // A listener on html, body or a box around many controls is delegation, not a control.
  const container = e => {
    if (e === document.documentElement || e === document.body) return true;
    let inside = 0;
    for (const c of deep(e)) if (candidates.has(c) && ++inside > 3) return true;
    const r = e.getBoundingClientRect(), w = Math.min(r.right, innerWidth) - Math.max(r.left, 0),
      h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
    return w > 0 && h > 0 && w * h > innerWidth * innerHeight / 2;
  };

  // -- reach ------------------------------------------------------------------------------------
  // Why a click at the centre would not land on it.
  const blocked = e => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return 'zero size';
    const {x, y} = centre(r);
    if (!onScreen(x, y)) return 'outside the viewport';
    const hit = deepHit(x, y), label = hit && closest(hit, 'label');
    if (e === hit || contains(e, hit) || (label && (label === closest(e, 'label') || label.control === e))) return null;
    return 'covered by <' + (hit ? tagc(hit) + ' "' + (hit.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 30) + '"' :
      'nothing') + '>';
  };
  // Off to the side in a panel that scrolls sideways (a wide table's last column): the panel clips
  // it, nothing covers it. Its row must be on screen, inside that panel (or a record row when only
  // the page scrolls sideways), and a click at the row's visible edge must land in that row.
  // Memoised for this look only: `whyNot` runs later, on the page as it is then.
  let asides = new Map();
  const aside = e => {
    if (asides?.has(e)) return asides.get(e);
    const away = slides(e);
    asides?.set(e, away);
    return away;
  };
  const slides = e => {
    const r = e.getBoundingClientRect(), {x, y} = centre(r), doc = document.scrollingElement;
    if (r.width <= 0 || r.height <= 0 || y < 0 || y >= innerHeight) return false;
    let lo = 0, hi = innerWidth, panel = null;
    let slid = !onScreen(x, y) && !!doc && doc.scrollWidth > doc.clientWidth &&
      ![document.documentElement, document.body].some(n => ['hidden', 'clip'].includes(getComputedStyle(n).overflowX));
    for (let p = parent(e); p && p !== document.body && p !== document.documentElement; p = parent(p)) {
      const s = getComputedStyle(p);
      if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
      const b = p.getBoundingClientRect();
      if (s.overflowY !== 'visible' && (y < b.top || y >= b.bottom)) return false;  // cut off vertically
      if (s.overflowX === 'visible') continue;
      // It clips to its padding box: borders and a scrollbar show none of its content.
      const left = b.left + p.clientLeft, right = left + p.clientWidth;
      if ((x < left || x >= right) && ['auto', 'scroll', 'overlay'].includes(s.overflowX) &&
          p.scrollWidth > p.clientWidth) { slid = true; panel ||= p; }
      lo = Math.max(lo, left); hi = Math.min(hi, right);
    }
    if (!slid || hi - lo < 2) return false;
    const at = Math.min(Math.max(x, lo + 1), hi - 1);
    let row = parent(e);
    for (; row && row !== document.body; row = parent(row)) {
      const b = row.getBoundingClientRect();
      if (at >= b.left && at < b.right && y >= b.top && y < b.bottom) break;
    }
    if (!row || row === document.body || (panel ? !contains(panel, row) : !row.matches(RECORDS))) return false;
    return contains(row, deepHit(at, y));
  };
  const reachable = e => {
    if (blocked(e) && !aside(e)) return null;
    const r = e.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height};
  };
  const twins = new Set();
  // The one verdict: null when the element is offered, else why not.
  const dropped = e => {
    if (!safe(e)) return 'a file or hidden input';
    const away = hidden(e);
    if (away) return away;
    if (e.matches(':disabled') || closest(e, '[aria-disabled="true"]')) return 'disabled';
    if (!candidates.has(e)) return miss;
    if (loose(e) && container(e)) return (pressable.has(e) ? 'a click listener' : 'focusable') +
      ' on a container (delegation)';
    if (twins.has(e)) return (pressable.has(e) ? 'a click listener' : 'a role-less focusable') +
      ' around or inside a control with the same label';
    if (roleOf(e) === 'gridcell' && e.querySelector('button,[role="button"]')) return 'a grid cell that wraps a button';
    return reachable(e) ? null : blocked(e);
  };
  // A native select made invisible (opacity 0, clipped, covered) under a styled box: the box is
  // what a human clicks, so the select is offered through it while the box itself is reachable.
  const CONTROLS = 'select,input:not([type="hidden"]),textarea';
  const proxy = e => {
    if (e.tagName !== 'SELECT') return null;
    const why = dropped(e);
    if (!why || ['disabled', 'display: none', 'visibility: hidden', 'inert', 'outside the viewport'].includes(why))
      return null;
    const s = e.getBoundingClientRect(), {x, y} = centre(s), tiny = s.width < 8 || s.height < 8;
    for (let p = parent(e), n = 0; p && p !== document.body && n < 3; p = parent(p), n++) {
      if (deep(p).filter(c => c.matches(CONTROLS)).length > 1) return null;
      const r = p.getBoundingClientRect();
      if (r.width < 8 && r.height < 8) continue;  // a visually-hidden wrapper is not the box
      if (r.width < 8 || r.height < 8 || !visible(p) || !reachable(p)) return null;  // a collapsed or hidden panel
      // The select sits where the box is: a click there lands in the box (a full-size select),
      // or its hidden pixel lies within the box (a clipped one).
      const at = tiny ? x >= r.x - 2 && x <= r.right + 2 && y >= r.y - 2 && y <= r.bottom + 2 : contains(p, deepHit(x, y));
      return at ? p : null;
    }
    return null;
  };
  reg.proxyBox = proxy;

  // -- the offered actions ---------------------------------------------------------------------------
  const labelOf = (e, r, box) => {
    let text = name(e);
    if (loose(e)) text = cut(text.replace(/\s+/g, ' ').trim(), 80);
    if (!text && !legacy(e)) text = axOf.get(e)?.[1] || '';
    if (text) return text;
    const near = box ? head(box.previousElementSibling?.innerText) : nearby(e);
    return near ? `(unlabelled ${r}) near: ${near}` : r;
  };
  const editable = (e, r) => !e.readOnly && e.getAttribute('aria-readonly') !== 'true' &&
    (['textbox', 'searchbox', 'spinbutton'].includes(r) || (r === 'combobox' && ['INPUT', 'TEXTAREA'].includes(e.tagName)));
  const valueOf = (e, r) => {
    if (typeof e.value === 'string' && (e.value || e.type === 'password')) return shown(e);
    return e.isContentEditable || r === 'combobox' ? (e.innerText || '').trim() : '';
  };
  const STATES = [['aria-checked', 'checked'], ['aria-selected', 'selected'], ['aria-expanded', 'expanded']];
  const actions = [], offered = new Set();
  for (const e of all) {
    if (!candidates.has(e)) continue;
    const box = dropped(e) ? proxy(e) : e;
    if (!box) continue;
    const proxied = box !== e, r = proxied ? 'combobox' : roleOf(e), label = labelOf(e, r, proxied ? box : null);
    const base = {node: identify(e), role: r, label, rect: reachable(box)};
    if (proxied) base.proxied = true;
    else if (blocked(e)) base.aside = true;
    for (const [attr, key] of STATES) if (e.hasAttribute(attr)) base[key] = e.getAttribute(attr);
    if (e.tagName === 'INPUT' && (e.type === 'checkbox' || e.type === 'radio')) base.checked = String(e.checked);
    offered.add(e);
    if (e.tagName === 'SELECT') {
      const current = [...e.selectedOptions].map(o => o.label).join(', ');
      for (const o of e.options) {
        if (o.selected || o.disabled || (o.parentElement?.tagName === 'OPTGROUP' && o.parentElement.disabled)) continue;
        actions.push({...base, label: label + ' → ' + o.label, kind: 'select', value: o.value, current_value: current});
      }
      continue;
    }
    const fill = editable(e, r), value = valueOf(e, r);
    actions.push({...base, kind: fill ? 'fill' : 'click', value});
    // Opens its dropdown or picker without typing.
    if (fill) actions.push({...base, label: 'Open ' + label, kind: 'click', value});
  }

  // Two click actions with one label, one inside the other, split the choice: the role-less one
  // goes (a role-ful one wins; of two role-less ones the inner wins).
  const byLabel = new Map();
  for (const a of actions) if (a.kind === 'click') {
    const e = reg.byId.get(a.node), list = byLabel.get(a.label) || [];
    if (!list.includes(e)) list.push(e);
    byLabel.set(a.label, list);
  }
  for (const list of byLabel.values()) for (const e of list) if (loose(e) && list.some(o => o !== e &&
      (contains(e, o) || (!loose(o) && contains(o, e))))) twins.add(e);
  if (twins.size) {
    for (const e of twins) offered.delete(e);
    actions.splice(0, actions.length, ...actions.filter(a => !twins.has(reg.byId.get(a.node))));
  }

  // Plain text holders: drop zones are divs in every drag-and-drop library, and a custom dropdown's
  // trigger is one too. Offered to drag and the text fallback only, never inside another candidate.
  if (options.drop_targets || options.text_holders) {
    let extra = 0;
    for (const e of all.slice(all.indexOf(document.body) + 1)) {
      if (extra >= 150) break;
      let inside = false;
      // Text inside a row offered only for its click listener is still its own target.
      for (let p = e; p && !inside; p = parent(p)) inside = offered.has(p) && (p === e || !loose(p));
      const label = own(e);
      if (inside || !label || closest(e, 'script,style,noscript,:disabled,[aria-disabled="true"]') || !visible(e)) continue;
      const rect = reachable(e);
      if (!rect) continue;
      offered.add(e);
      extra++;
      const action = {node: identify(e), role: 'text', label, rect, kind: 'click', value: ''};
      if (getComputedStyle(e).cursor === 'pointer') action.pointer = true;
      if (blocked(e)) action.aside = true;
      actions.push(action);
    }
  }
  asides = null;

  // At most 250 node actions; the role-less ones go first.
  const MAX_ACTIONS = 250;
  const cutOff = new Set();
  for (const lessWanted of [a => a.role === 'clickable', () => true])
    for (let i = actions.length - 1; i >= 0 && actions.length - cutOff.size > MAX_ACTIONS; i--)
      if (lessWanted(actions[i])) cutOff.add(i);
  const kept = actions.filter((_, i) => !cutOff.has(i)).map((a, i) => ({id: 'e' + (i + 1), ...a}));
  const doc = document.documentElement;
  if (scrollY + innerHeight < doc.scrollHeight - 2) kept.push({id: 'scroll_down', kind: 'scroll', label: 'Scroll down', delta: 560});
  if (scrollY > 0) kept.push({id: 'scroll_up', kind: 'scroll', label: 'Scroll up', delta: -560});
  kept.push({id: 'wait', kind: 'wait', label: 'Wait for the page to update'});

  // -- freshness --------------------------------------------------------------------------------
  // The document, where it is scrolled and sized, and every field's state.
  const fieldState = () => {
    const fields = [];
    for (const e of deep(document)) if (e.matches('input,textarea,select') && safe(e))
      fields.push([identify(e), shown(e), e.checked ?? null, e.selectedIndex ?? null, e.disabled, e.readOnly ?? null]);
    return [performance.timeOrigin, location.href, scrollX, scrollY, innerWidth, innerHeight, fields];
  };
  // One target as acted on: itself and the record it sits in (a recycled row, a reused dialog).
  const RECORD = 'li,tr,[role="row"],[role="listitem"],article,dialog,[role="dialog"],[role="alertdialog"]';
  const guard = e => {
    if (!e?.isConnected || (!visible(e) && !(e.tagName === 'SELECT' && proxy(e)))) return null;
    const record = closest(e, RECORD);
    return [identify(e), e.getAttribute('role'), name(e), typeof e.value === 'string' ? shown(e) : null,
      e.checked ?? null, e.selectedIndex ?? null, e.readOnly ?? null, e.matches(':disabled'),
      e.getAttribute('aria-disabled'), e.getAttribute('aria-expanded'), e.getAttribute('aria-checked'),
      e.getAttribute('aria-selected'), e.getAttribute('href'), record ? cut(record.innerText || '', 6000) : null];
  };
  reg.fieldState = fieldState;
  reg.guard = guard;
  const pageKey = fieldState();
  const guards = {};
  for (const a of kept) if (a.node != null && !(a.node in guards)) guards[a.node] = guard(reg.byId.get(a.node));
  // Equal between two looks iff the page holds still for an untargeted decision: only the element
  // actions count, not text (a polling status line), text holders or the scroll and wait entries
  // (a page growing past the "more below" line has not moved its controls).
  const marker = [...pageKey.slice(0, 6),
    kept.filter(a => a.node != null && a.role !== 'text').map(({rect, ...rest}) => rest), pageKey[6]];

  // -- near misses ------------------------------------------------------------------------------
  // Why the elements whose words are nearest a failed step's wording were not offered.
  reg.whyNot = step => {
    const tokens = t => t.toLowerCase().match(/[\p{L}\p{N}]+/gu) || [];
    const want = new Set(tokens(step || '')), found = [];
    for (const e of deep(document.body)) {
      if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'OPTION', 'OPTGROUP'].includes(e.tagName)) continue;
      const says = (e.tagName === 'SELECT' ? [name(e), ...[...e.options].map(o => o.label)].join(' ') :
        [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim() ||
        e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('title') || '')
        .replace(/\s+/g, ' ').trim();
      const said = tokens(says), hits = said.filter(t => want.has(t)).length;
      if (hits) found.push({e, says, score: hits * hits / said.length});
    }
    return found.sort((a, b) => b.score - a.score).slice(0, 8).map(({e, says}) => {
      const why = dropped(e), box = why ? proxy(e) : null, away = hidden(e), hit = away ? null : !blocked(e);
      const holder = why === miss && !away && hit && !!own(e);
      return {tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), text: cut(says, 60),
        shadow_host: e.getRootNode().host?.tagName.toLowerCase() ?? null, visible: !away, hit,
        ax_role: axOf.get(e)?.[0] ?? null, listener: gel || pressable.has(e) ? pressable.has(e) : null,
        offered: !why || !!box || holder,
        why: !why ? null : box ? 'hidden native select (' + why + '): offered to select through its box ' + tagc(box) :
          holder ? 'a text holder: offered only to the text fallback' :
          why === miss ? away || blocked(e) || why : why};
    });
  };

  // -- page text ---------------------------------------------------------------------------------
  // Text nodes on screen, in document order through shadow roots, up to 6000 characters.
  const TEXT_MAX = 6000;
  const pageText = () => {
    const parts = [], holders = new Map(), range = document.createRange();
    let size = 0;
    const away = e => { if (!holders.has(e)) holders.set(e, !!hidden(e)); return holders.get(e); };
    const walk = node => {
      for (const n of node.childNodes) {
        if (size >= TEXT_MAX) return;
        if (n.nodeType === 3) {
          const text = n.textContent.trim(), holder = n.parentElement || n.parentNode.host;
          if (!text || !holder || away(holder)) continue;
          range.selectNodeContents(n);
          const r = range.getBoundingClientRect();
          if (r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight) {
            parts.push(text);
            size += text.length + 1;
          }
        } else if (n.nodeType === 1 && !QUIET.includes(n.tagName)) {
          if (shadow(n)) walk(shadow(n));
          walk(n);
        }
      }
    };
    walk(document.body);
    return cut(parts.join('\n'), TEXT_MAX);
  };

  const text = pageText();
  if (reg.observer.takeRecords().length) reg.mutations++;
  return {
    url: location.href, title: document.title, w: innerWidth, h: innerHeight, text,
    scroll: {y: scrollY, height: doc.scrollHeight},
    actions: kept, marker, page_key: pageKey, guards,
    omitted_actions: cutOff.size,
    discovery_listeners: !!gel,
    ax_missing: axMissing,
    stamp: reg.mutations,
    unscanned: !reg.scanDone || all.some(e => e.localName.includes('-') && !!customElements.get(e.localName) &&
      !shadow(e) && !reg.rootlessTags.has(e.localName)),
  };
})

/* Cold Storage — single-page frontend (vanilla JS, no build step). */
'use strict';

// ------------------------------------------------------------------------ //
// Constants
// ------------------------------------------------------------------------ //
const TYPES = {
  'ln2': { label: 'Liquid nitrogen', short: 'LN₂', temp: '−196 °C', cls: 't-ln2',
           layout: { racks: 6, boxes: 12, grid_rows: 9, grid_cols: 9 },
           sizes: ['Dewar, 35 L', 'Tank, 240 L', 'Tank, 500 L', 'Tank, 800 L'] },
  '-80': { label: '−80 °C freezer', short: '−80', temp: '−80 °C', cls: 't-80',
           layout: { shelves: 5, racks: 4, rows: 4, boxes: 4, grid_rows: 9, grid_cols: 9 },
           sizes: ['Upright, 25 cu ft', 'Upright, 21 cu ft', 'Upright, 17 cu ft', 'Chest, 20 cu ft', 'Undercounter, 3.5 cu ft'] },
  '-20': { label: '−20 °C freezer', short: '−20', temp: '−20 °C', cls: 't-20',
           layout: { shelves: 4, racks: 2, rows: 3, boxes: 3, grid_rows: 9, grid_cols: 9 },
           sizes: ['Upright, 20 cu ft', 'Upright, 12 cu ft', 'Undercounter, 5 cu ft', 'Chest, 15 cu ft'] },
  '+4':  { label: '+4 °C fridge', short: '+4', temp: '+4 °C', cls: 't-4',
           layout: { shelves: 4, racks: 1, rows: 1, boxes: 4, grid_rows: 9, grid_cols: 9 },
           sizes: ['Upright, 23 cu ft', 'Upright, 49 cu ft (double door)', 'Undercounter, 5 cu ft', 'Chromatography fridge'] },
};
const TYPE_ORDER = ['ln2', '-80', '-20', '+4'];
const API_VERSION = 6;   // must match API_VERSION in app.py
const LEVEL = { shelf: 'Shelf', rack: 'Rack', row: 'Row', box: 'Box', space: 'Loose space' };
const LAYOUT_KEY = { shelf: 'shelves', rack: 'racks', row: 'rows', box: 'boxes' };
// Levels per storage type. LN2 tanks have no shelves or rows: racks stand in the tank and
// hold a vertical stack of boxes.
const CHAINS = { ln2: ['rack', 'box'] };
const chainFor = type => CHAINS[type] || ['shelf', 'rack', 'row', 'box'];
/** The level below `level` for this type (`null` = the unit itself). */
function childLevel(type, level) {
  const c = chainFor(type);
  if (level == null) return c[0];
  const i = c.indexOf(level);
  return i >= 0 && i + 1 < c.length ? c[i + 1] : null;
}
const isTank = m => m.unit.type === 'ln2';
const levelPlural = l => ({ shelf: 'shelves', box: 'boxes', space: 'loose spaces' }[l] || l + 's');
const GRID_PRESETS = [[9, 9], [10, 10], [8, 12], [5, 5], [7, 7], [8, 8], [4, 4]];
const USER_COLORS = ['#e4572e', '#3b82f6', '#8b5cf6', '#059669', '#d97706', '#db2777', '#0891b2', '#65a30d', '#7c3aed', '#dc2626', '#0d9488', '#9333ea'];
const REMOVE_REASONS = ['Used up', 'Discarded', 'Thawed for culture', 'Shipped / given away', 'Expired', 'Moved to another lab'];

const state = {
  me: null, lab: 'Cold Storage', users: [], usersById: new Map(), cats: [], catsById: new Map(),
  moving: null,                 // {id, name} while an item is being moved
  highlight: '',                // owner filter in the diagram: '' | 'common' | userId
  boxSel: { box: null, cells: new Set(), on: false },
  renderSeq: 0,
};

// ------------------------------------------------------------------------ //
// Small helpers
// ------------------------------------------------------------------------ //
const $ = (s, el = document) => el.querySelector(s);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'class') el.className = v;
      else if (k === 'html') el.innerHTML = v;
      else if (k === 'style' && typeof v === 'object') {
        for (const [p, pv] of Object.entries(v)) {
          if (pv == null) continue;
          if (p.startsWith('--')) el.style.setProperty(p, pv); else el.style[p] = pv;
        }
      } else if (['value', 'checked', 'selected', 'disabled', 'indeterminate'].includes(k)) el[k] = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  appendKids(el, kids);
  return el;
}
function appendKids(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : String(k));
  }
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const todayISO = () => new Date().toLocaleDateString('en-CA');
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso.length <= 10 ? iso + 'T00:00:00' : iso);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}
function fmtWhen(utc) {
  const d = new Date(utc.replace(' ', 'T') + 'Z');
  const diff = (Date.now() - d) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  if (diff < 86400 * 6) return `${Math.floor(diff / 86400)} d ago`;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}
function ago(iso) {
  if (!iso) return '';
  const days = Math.round((Date.now() - new Date(iso + 'T00:00:00')) / 86400000);
  if (days < 0) return 'in the future';
  if (days < 45) return `${days} d ago`;
  const mo = Math.round(days / 30.4);
  return mo < 18 ? `${mo} mo ago` : `${(days / 365).toFixed(1)} yr ago`;
}
function daysSince(iso) { return iso ? (Date.now() - new Date(iso + 'T00:00:00')) / 86400000 : Infinity; }
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const initials = name => (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('');
const userColor = id => USER_COLORS[(id || 0) % USER_COLORS.length];
const fillColor = f => f <= 0 ? 'var(--empty)' : `color-mix(in srgb, var(--fill) ${Math.round(18 + f * 82)}%, var(--empty))`;

function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = err ? 'err' : ''; }, err ? 4200 : 2400);
}

async function api(method, path, body) {
  const r = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && !['/api/login', '/api/setup'].includes(path)) {
    state.me = null;
    showAuth();
    throw new Error(data.error || 'Please sign in');
  }
  if (r.status === 404 && data.error === 'Not found') {
    // The page knows an endpoint the server doesn't: the server is running older code.
    serverOutdated();
    throw new Error('The server is running an older version — restart it to use this.');
  }
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}
// Run an action, toasting errors. Returns the result or undefined on failure.
async function attempt(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r ?? true; }
  catch (e) { toast(e.message, true); return undefined; }
}

// ------------------------------------------------------------------------ //
// People / categories
// ------------------------------------------------------------------------ //
async function loadRefs() {
  const [users, cats] = await Promise.all([api('GET', '/api/users'), api('GET', '/api/categories')]);
  state.users = users; state.usersById = new Map(users.map(u => [u.id, u]));
  state.cats = cats; state.catsById = new Map(cats.map(c => [c.id, c]));
}
function ownerChip(ownerId, common, opts = {}) {
  if (common) return h('span', { class: 'chip common' + (opts.inherit ? ' inherit' : ''), title: opts.title }, h('span', { class: 'av' }, '★'), 'Common');
  if (!ownerId) return opts.none ?? null;
  const u = state.usersById.get(ownerId);
  const name = u ? u.name : 'Unknown';
  return h('span', { class: 'chip' + (opts.inherit ? ' inherit' : ''), title: opts.title || name },
    h('span', { class: 'av', style: { background: userColor(ownerId) } }, initials(name)), opts.short ? name.split(' ')[0] : name);
}
function ownerName(ownerId, common) {
  if (common) return 'Common';
  return ownerId ? (state.usersById.get(ownerId)?.name || 'Unknown') : 'Unassigned';
}
/** <select> for owner. value: '' | 'common' | id. inheritLabel shown for ''. */
function ownerSelect(ownerId, common, inheritLabel = 'Unassigned', name = 'owner') {
  const cur = common ? 'common' : (ownerId ? String(ownerId) : '');
  const sel = h('select', { name, class: 'input' },
    h('option', { value: '' }, inheritLabel),
    h('option', { value: 'common' }, '★ Common (shared by the lab)'),
    h('optgroup', { label: 'Person' },
      state.users.filter(u => u.active || String(u.id) === cur).map(u => h('option', { value: u.id }, u.name + (u.active ? '' : ' (inactive)')))));
  sel.value = cur;
  return sel;
}
const ownerPayload = v => v === 'common' ? { owner_id: null, is_common: true } : { owner_id: v ? +v : null, is_common: false };
function categorySelect(val) {
  const sel = h('select', { name: 'category_id', class: 'input' }, h('option', { value: '' }, '— None —'),
    state.cats.map(c => h('option', { value: c.id }, c.name)));
  sel.value = val ? String(val) : '';
  return sel;
}
const catColor = id => state.catsById.get(id)?.color || '#9ca3af';

// ------------------------------------------------------------------------ //
// Modal
// ------------------------------------------------------------------------ //
function openModal({ title, body, foot, wide = false, onClose }) {
  const root = $('#modal-root');
  const back = h('div', { class: 'modal-back' });
  const close = () => { back.remove(); document.removeEventListener('keydown', onKey); onClose && onClose(); };
  const onKey = e => { if (e.key === 'Escape') close(); };
  const modal = h('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true' },
    h('div', { class: 'modal-head' }, typeof title === 'string' ? h('h2', null, title) : title,
      h('button', { class: 'btn ghost sm', onclick: close, 'aria-label': 'Close' }, '✕')),
    h('div', { class: 'modal-body' }, body),
    foot ? h('div', { class: 'modal-foot' }, foot) : null);
  back.append(modal);
  back.addEventListener('mousedown', e => { if (e.target === back) close(); });
  document.addEventListener('keydown', onKey);
  root.append(back);
  setTimeout(() => modal.querySelector('input:not([type=hidden]), select, textarea')?.focus(), 30);
  return close;
}
function confirmBox(title, message, { okText = 'Confirm', danger = false } = {}) {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; resolve(v); } close(); };
    const close = openModal({
      title, body: h('p', null, message),
      foot: [h('button', { class: 'btn', onclick: () => finish(false) }, 'Cancel'),
             h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), onclick: () => finish(true) }, okText)],
      onClose: () => { if (!done) { done = true; resolve(false); } },
    });
  });
}
function field(label, input, hint, cls) {
  return h('label', { class: 'field' + (cls ? ' ' + cls : '') }, h('span', null, label, hint ? h('span', { class: 'hint' }, ' — ' + hint) : null), input);
}
const inp = (name, value, attrs = {}) => h('input', { name, value: value ?? '', class: 'input', ...attrs });
function formData(form) {
  const o = {};
  for (const el of form.querySelectorAll('[name]')) {
    if (el.type === 'checkbox') o[el.name] = el.checked;
    else o[el.name] = el.value.trim();
  }
  return o;
}

// ------------------------------------------------------------------------ //
// Router
// ------------------------------------------------------------------------ //
function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, qs] = raw.split('?');
  return { parts: path.split('/').filter(Boolean), query: Object.fromEntries(new URLSearchParams(qs || '')) };
}
async function route() {
  if (!state.me) return;
  const seq = ++state.renderSeq;
  const { parts, query } = parseHash();
  const top = parts[0] || '';
  for (const a of document.querySelectorAll('#nav a')) {
    a.classList.toggle('active', a.dataset.nav === ({ '': 'home', unit: 'home', new: 'home', search: 'search', activity: 'activity', settings: 'settings' }[top]));
  }
  const app = $('#app');
  let view;
  try {
    if (top === '') view = await viewHome();
    else if (top === 'new') view = await viewUnitForm(null);
    else if (top === 'unit' && parts[2] === 'edit') view = await viewUnitForm(+parts[1]);
    else if (top === 'unit') view = await viewUnit(+parts[1], parts[2] === 'n' ? +parts[3] : null, query);
    else if (top === 'search') view = await viewSearch(query);
    else if (top === 'activity') view = await viewActivity(query);
    else if (top === 'settings') view = await viewSettings();
    else view = h('div', { class: 'empty-note' }, 'Page not found. ', h('a', { href: '#/' }, 'Go home'));
  } catch (e) {
    if (!state.me) return;
    view = h('div', { class: 'card' }, h('p', { class: 'error' }, e.message), h('p', null, h('a', { href: '#/' }, '← Back to storage')));
  }
  if (seq !== state.renderSeq) return;   // a newer navigation won
  app.replaceChildren(view);
  renderBanner();
}
async function refresh() {
  const y = window.scrollY;
  await route();
  window.scrollTo(0, y);
}
window.addEventListener('hashchange', () => { route().then(() => window.scrollTo(0, 0)); });
document.addEventListener('click', e => {
  if (Date.now() < dnd.suppressClickUntil) { e.preventDefault(); e.stopPropagation(); return; }
  const t = e.target.closest('[data-href]');
  if (t && !e.defaultPrevented) { e.preventDefault(); location.hash = t.dataset.href; }
});

// ------------------------------------------------------------------------ //
// Drag & drop of racks / loose spaces between positions and shelves.
// Pointer events (not HTML5 DnD) so it works on SVG (LN2 view) and touch.
// Mouse: drag after moving 6px.  Touch/pen: long-press 350 ms, then drag.
// ------------------------------------------------------------------------ //
const dnd = { st: null, suppressClickUntil: 0 };
function dndClear() { for (const el of document.querySelectorAll('.drop-before, .drop-after, .drop-into, .drop-swap')) el.classList.remove('drop-before', 'drop-after', 'drop-before-v', 'drop-after-v', 'drop-into', 'drop-swap'); }
function dndStart() {
  const st = dnd.st;
  st.active = true;
  st.src.classList.add('dragging');
  document.body.classList.add('is-dragging');
  const label = st.src.querySelector('.lbl span')?.textContent || st.src.querySelector('title')?.textContent.split(' — ')[0] || 'Rack';
  st.ghost = h('div', { class: 'drag-ghost', style: { left: st.x + 'px', top: st.y + 'px' } }, label);
  document.body.append(st.ghost);
  if (navigator.vibrate && st.touch) navigator.vibrate(15);
}
function dndTarget(x, y) {
  const st = dnd.st;
  dndClear();
  st.target = null;
  const el = document.elementFromPoint(x, y);
  const zone = el && el.closest('[data-drop-parent]');
  if (!zone || !zone.closest('[data-dnd]')) return;
  const parent = +zone.dataset.dropParent;
  const tile = el.closest('[data-drag]');
  if (zone.dataset.plan) {
    // Arranged tank: racks live in fixed slots — drop on a rack to swap, on an empty slot to move there.
    const slotEl = el.closest('[data-slot]');
    if (tile && tile !== st.src && zone.contains(tile)) { tile.classList.add('drop-swap'); st.target = { parent, swap_with: +tile.dataset.drag }; }
    else if (slotEl && zone.contains(slotEl)) { slotEl.classList.add('drop-swap'); st.target = { parent, slot: +slotEl.dataset.slot }; }
    return;
  }
  if (tile && tile !== st.src && zone.contains(tile)) {
    const r = tile.getBoundingClientRect();
    const vertical = getComputedStyle(tile.parentElement).flexDirection === 'column';
    const after = vertical ? y > r.top + r.height / 2 : x > r.left + r.width / 2;
    const order = [...zone.querySelectorAll('[data-drag]')].filter(t => t !== st.src);
    const idx = order.indexOf(tile);
    const before = after ? (order[idx + 1] ? +order[idx + 1].dataset.drag : null) : +tile.dataset.drag;
    tile.classList.add((after ? 'drop-after' : 'drop-before') + (vertical ? '-v' : ''), after ? 'drop-after' : 'drop-before');
    st.target = { parent, before };
  } else if (!tile || tile === st.src) {
    zone.classList.add('drop-into');
    st.target = tile === st.src ? null : { parent, before: null };
  }
}
function dndEnd(commit) {
  const st = dnd.st;
  dnd.st = null;
  if (!st) return;
  clearTimeout(st.timer);
  if (!st.active) return;
  dnd.suppressClickUntil = Date.now() + 400;
  st.src.classList.remove('dragging');
  document.body.classList.remove('is-dragging');
  st.ghost?.remove();
  dndClear();
  if (!commit || !st.target) return;
  const t = st.target;
  if (t.swap_with != null || t.slot != null) {
    attempt(() => api('POST', `/api/nodes/${st.id}/place`, { parent_id: t.parent, swap_with: t.swap_with, slot: t.slot }))
      .then(r => { if (r) toast(t.swap_with != null ? 'Swapped' : 'Moved'); refresh(); });
    return;
  }
  // Skip no-op drops (same shelf, same slot).
  const sibs = [...st.src.closest('[data-drop-parent]').querySelectorAll('[data-drag]')].map(t => +t.dataset.drag);
  const i = sibs.indexOf(st.id);
  if (st.target.parent === st.fromParent && (st.target.before === sibs[i + 1] || (st.target.before === null && i === sibs.length - 1))) return;
  attempt(() => api('POST', `/api/nodes/${st.id}/place`, { parent_id: st.target.parent, before_id: st.target.before }))
    .then(r => { if (r) toast(r.renamed ? `Moved — renamed to “${r.renamed}” (that name was already taken there)` : 'Moved'); refresh(); });
}
document.addEventListener('pointerdown', e => {
  if (e.button !== 0 || dnd.st) return;
  const src = e.target.closest('[data-drag]');
  if (!src || !src.closest('[data-dnd]')) return;
  const zone = src.closest('[data-drop-parent]');
  if (!zone) return;
  const touch = e.pointerType !== 'mouse';
  dnd.st = { src, id: +src.dataset.drag, fromParent: +zone.dataset.dropParent, x: e.clientX, y: e.clientY, touch, active: false, target: null };
  if (touch) dnd.st.timer = setTimeout(() => { if (dnd.st && !dnd.st.active) { dndStart(); dndTarget(dnd.st.x, dnd.st.y); } }, 350);
});
document.addEventListener('pointermove', e => {
  const st = dnd.st;
  if (!st) return;
  if (!st.active) {
    const d = Math.hypot(e.clientX - st.x, e.clientY - st.y);
    if (st.touch) { if (d > 10) { clearTimeout(st.timer); dnd.st = null; } return; }   // finger moved first: it's a scroll
    if (d < 6) return;
    dndStart();
  }
  st.ghost.style.left = e.clientX + 'px';
  st.ghost.style.top = e.clientY + 'px';
  dndTarget(e.clientX, e.clientY);
});
document.addEventListener('pointerup', () => dndEnd(true));
document.addEventListener('pointercancel', () => dndEnd(false));
document.addEventListener('keydown', e => { if (e.key === 'Escape' && dnd.st) dndEnd(false); });
// While dragging on a touch screen, stop the page from scrolling.
document.addEventListener('touchmove', e => { if (dnd.st && dnd.st.active) e.preventDefault(); }, { passive: false });
// Long-press would otherwise open the browser's context menu / link preview.
document.addEventListener('contextmenu', e => { if (dnd.st) e.preventDefault(); });

function serverOutdated() {
  if ($('#outdated')) return;
  document.body.prepend(h('div', { id: 'outdated', class: 'outdated' },
    h('b', null, 'The Cold Storage server is out of date.'),
    ' This page was updated but the server process wasn\'t restarted, so newer actions (moving, converting…) will fail. ',
    'In the terminal running it press Ctrl-C, then run ', h('code', null, 'python3 app.py'), ' again and reload this page.'));
}
function renderBanner() {
  const b = $('#banner');
  if (!state.moving) { b.hidden = true; return; }
  b.hidden = false;
  b.replaceChildren(
    h('span', null, `Moving “${state.moving.name}” — open any box and click an empty position, or use “Put here as loose item”.`),
    h('span', { class: 'spacer' }),
    h('button', { class: 'btn sm', onclick: () => { state.moving = null; refresh(); } }, 'Cancel move'));
}

// ------------------------------------------------------------------------ //
// Auth screens
// ------------------------------------------------------------------------ //
async function boot() {
  const st = await fetch('/api/status').then(r => r.json());
  state.lab = st.lab_name;
  if (st.api_version !== API_VERSION) serverOutdated();
  if (st.needs_setup) return showSetup();
  try {
    const me = await api('GET', '/api/me');
    state.me = me.user; state.lab = me.lab_name;
  } catch { return; }
  await startApp();
}
async function startApp() {
  await loadRefs();
  $('#topbar').hidden = false;
  $('#lab-name').textContent = state.lab;
  document.title = `${state.lab} · Cold Storage`;
  $('#me-box').replaceChildren(
    h('a', { class: 'me-name', href: '#/settings', title: `Signed in as ${state.me.email}` }, ownerChip(state.me.id, false, { short: true })),
    h('button', { class: 'btn sm ghost', onclick: signOut }, 'Sign out'));
  route();
}
function authShell(title, form) {
  $('#topbar').hidden = true;
  $('#banner').hidden = true;
  $('#app').replaceChildren(h('div', { class: 'auth' },
    h('div', { class: 'brand' }, h('span', { class: 'brand-mark' }), state.lab || 'Cold Storage'),
    h('div', { class: 'card stack' }, title ? h('h2', null, title) : null, form)));
}
function showAuth(mode = 'signin') {
  const err = h('p', { class: 'error' });
  const tabs = h('div', { class: 'auth-tabs' },
    h('button', { type: 'button', class: mode === 'signin' ? 'on' : '', onclick: () => showAuth('signin') }, 'Sign in'),
    h('button', { type: 'button', class: mode === 'join' ? 'on' : '', onclick: () => showAuth('join') }, 'Join a lab'));
  const done = async () => {
    const me = await api('GET', '/api/me');
    state.me = me.user; state.lab = me.lab_name;
    await startApp();
  };
  let form;
  if (mode === 'join') {
    const code = inp('code', '', { required: true, autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false', placeholder: 'e.g. SHERMN', class: 'input code-input' });
    code.addEventListener('input', () => { code.value = code.value.toUpperCase(); });
    form = h('form', { class: 'stack' },
      h('p', { class: 'small muted' }, 'New here? Enter the lab code (anyone in the lab can find it under Settings › Lab) and make your account.'),
      field('Lab code', code),
      field('Your name', inp('name', '', { required: true, autocomplete: 'name' })),
      field('Email', inp('email', '', { type: 'email', required: true, autocomplete: 'username' })),
      field('Password', inp('password', '', { type: 'password', minlength: 6, required: true, autocomplete: 'new-password' }), 'at least 6 characters'),
      err, h('button', { class: 'btn primary', type: 'submit', style: { width: '100%' } }, 'Join lab'));
    form.onsubmit = async e => {
      e.preventDefault(); err.textContent = '';
      try { await api('POST', '/api/join', formData(form)); await done(); toast(`Welcome to ${state.lab}!`); }
      catch (x) { err.textContent = x.message; }
    };
  } else {
    form = h('form', { class: 'stack' },
      field('Email', inp('email', '', { type: 'email', autocomplete: 'username', required: true })),
      field('Password', inp('password', '', { type: 'password', autocomplete: 'current-password', required: true })),
      err, h('button', { class: 'btn primary', type: 'submit', style: { width: '100%' } }, 'Sign in'),
      h('p', { class: 'small muted' }, 'No account yet? ', h('a', { href: '#', onclick: e => { e.preventDefault(); showAuth('join'); } }, 'Join with your lab code'), '.'));
    form.onsubmit = async e => {
      e.preventDefault(); err.textContent = '';
      try { await api('POST', '/api/login', formData(form)); await done(); }
      catch (x) { err.textContent = x.message; }
    };
  }
  authShell(null, h('div', { class: 'stack' }, tabs, form));
}
async function signOut() {
  await api('POST', '/api/logout').catch(() => {});
  state.me = null;
  $('#me-box').replaceChildren();
  showAuth();
  toast('Signed out');
}
function showSetup() {
  const err = h('p', { class: 'error' });
  const form = h('form', { class: 'stack' },
    h('p', { class: 'muted small' }, 'Welcome! Create the first account — it becomes the lab admin, who can then add everyone else.'),
    field('Lab name', inp('lab_name', '', { placeholder: 'e.g. Sherman Lab', required: true })),
    field('Your name', inp('name', '', { required: true })),
    field('Email', inp('email', '', { type: 'email', required: true })),
    field('Password', inp('password', '', { type: 'password', minlength: 6, required: true }), 'at least 6 characters'),
    err, h('button', { class: 'btn primary', type: 'submit', style: { width: '100%' } }, 'Create lab'));
  form.onsubmit = async e => {
    e.preventDefault(); err.textContent = '';
    try {
      await api('POST', '/api/setup', formData(form));
      const me = await api('GET', '/api/me');
      state.me = me.user; state.lab = me.lab_name;
      await startApp();
    } catch (x) { err.textContent = x.message; }
  };
  authShell('Set up Cold Storage', form);
}

// ------------------------------------------------------------------------ //
// Home
// ------------------------------------------------------------------------ //
async function viewHome() {
  const [units, stats, feed] = await Promise.all([api('GET', '/api/units'), api('GET', '/api/stats'), api('GET', '/api/activity?limit=12')]);
  const groups = new Map();
  for (const u of units) {
    const k = u.location || 'No location set';
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(u);
  }
  const stat = (n, label, href) => h('a', { class: 'stat', href, style: { color: 'inherit' } }, h('b', null, n.toLocaleString()), h('span', null, label));
  return h('div', null,
    h('div', { class: 'page-head' },
      h('div', { class: 'titles' }, h('h1', null, 'Cold storage'), h('p', null, `${plural(units.length, 'unit')} across ${plural(groups.size, 'location')}`)),
      h('a', { class: 'btn primary', href: '#/new' }, '+ Add storage')),
    h('div', { class: 'stats' },
      stat(stats.stored, 'items stored', '#/search'),
      stat(stats.added_30d, 'added in the last 30 days', `#/search?status=all&added_from=${daysAgoISO(30)}`),
      stat(stats.removed_30d, 'removed in the last 30 days', `#/search?status=removed&removed_from=${daysAgoISO(30)}`),
      stat(stats.expiring, 'expired or expiring ≤ 30 days', '#/search?expiring=1')),
    units.length === 0
      ? h('div', { class: 'units' }, h('a', { class: 'unit-card add', href: '#/new' }, '+ Add your first freezer, fridge or LN₂ tank'))
      : [...groups.entries()].map(([loc, list]) => h('section', { class: 'loc-group' },
          h('h3', null, loc), h('div', { class: 'units' }, list.map(unitCard)))),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', null, 'Recent activity'), h('a', { href: '#/activity', class: 'small' }, 'Full log →')),
      feedList(feed)));
}
function daysAgoISO(n) { const d = new Date(); d.setDate(d.getDate() - n); return d.toLocaleDateString('en-CA'); }
function unitCard(u) {
  const t = TYPES[u.type];
  const boxed = u.stored - u.loose;
  const pct = u.capacity ? Math.min(100, Math.round(100 * boxed / u.capacity)) : 0;
  const defrostOld = u.type !== 'ln2' && daysSince(u.last_defrost) > 365;
  const pmOld = daysSince(u.last_pm) > 365;
  return h('a', { class: `unit-card ${t.cls}`, href: `#/unit/${u.id}` },
    h('div', { class: 'row' }, h('span', { class: 'type-badge' }, t.short), h('span', { class: 'small muted' }, t.label), h('span', { class: 'spacer' }),
      ownerChip(u.owner_id, u.is_common, { short: true })),
    h('h2', null, u.name),
    h('div', { class: 'meta' }, [u.device_name || u.manufacturer, u.model].filter(Boolean).join(' ') || u.size || ''),
    h('div', { class: 'fillbar' }, h('i', { style: { width: pct + '%' } })),
    h('div', { class: 'meta row' }, h('span', null, `${boxed.toLocaleString()} / ${u.capacity.toLocaleString()} positions · ${plural(u.box_count, 'box')}`.replace('boxs', 'boxes')),
      u.loose ? h('span', { class: 'pill' }, `${u.loose} loose`) : null),
    (u.last_defrost || u.last_pm || defrostOld || pmOld) ? h('div', { class: 'meta row', style: { marginTop: '6px' } },
      u.type !== 'ln2' ? h('span', { class: 'pill' + (defrostOld ? ' warn' : '') }, 'Defrost ' + (u.last_defrost ? ago(u.last_defrost) : 'not logged')) : null,
      h('span', { class: 'pill' + (pmOld ? ' warn' : '') }, 'PM ' + (u.last_pm ? ago(u.last_pm) : 'not logged'))) : null);
}
function feedList(feed) {
  if (!feed.length) return h('p', { class: 'empty-note' }, 'Nothing has happened yet.');
  return h('ul', { class: 'feed' }, feed.map(a => {
    const kind = a.action.split('.')[1];
    return h('li', { class: a.item_id ? 'clickable' : '', onclick: a.item_id ? () => openItem(a.item_id) : null },
      h('span', { class: 'act ' + kind }),
      h('span', { class: 'when', title: a.at + ' UTC' }, fmtWhen(a.at)),
      h('span', null, h('span', { class: 'who' }, a.user_name || 'Someone'), ' ', h('span', { class: 'what' }, lowerFirst(a.summary))));
  }));
}
const lowerFirst = s => s ? s[0].toLowerCase() + s.slice(1) : s;

// ------------------------------------------------------------------------ //
// Unit model (built from GET /api/units/:id)
// ------------------------------------------------------------------------ //
function buildModel(data) {
  const m = { unit: data.unit, nodes: new Map(), kids: new Map(), items: data.items, byBox: new Map(), loose: [], memo: new Map(), match: new Map() };
  for (const n of data.nodes) {
    m.nodes.set(n.id, n);
    const k = n.parent_id || 0;
    if (!m.kids.has(k)) m.kids.set(k, []);
    m.kids.get(k).push(n);
  }
  for (const list of m.kids.values()) {
    list.sort((a, b) => a.position - b.position || a.id - b.id);
    const per = {};   // number per level: "Rack 3" ignores loose spaces between racks
    for (const n of list) { n.index = per[n.level] = (per[n.level] ?? -1) + 1; }
  }
  for (const it of data.items) {
    if (it.pos_row == null) m.loose.push(it);
    else {
      if (!m.byBox.has(it.node_id)) m.byBox.set(it.node_id, []);
      m.byBox.get(it.node_id).push(it);
    }
  }
  return m;
}
const kidsOf = (m, id) => m.kids.get(id || 0) || [];
const nodeLabel = n => n.name || `${LEVEL[n.level]} ${n.index + 1}`;
function chainOf(m, id) {
  const out = [];
  let n = m.nodes.get(id);
  while (n) { out.unshift(n); n = m.nodes.get(n.parent_id); }
  return out;
}
/** Effective owner for a node (or null = unit). Returns {owner_id, common, from}. */
function nodeOwner(m, id) {
  for (const n of chainOf(m, id).reverse()) {
    if (n.owner_id || n.is_common) return { owner_id: n.owner_id, common: !!n.is_common, from: nodeLabel(n) };
  }
  const u = m.unit;
  if (u.owner_id || u.is_common) return { owner_id: u.owner_id, common: !!u.is_common, from: u.name };
  return { owner_id: null, common: false, from: null };
}
function itemOwner(m, it) {
  if (it.owner_id || it.is_common) return { owner_id: it.owner_id, common: !!it.is_common, from: 'item' };
  return nodeOwner(m, it.node_id);
}
function stats(m, id) {           // {used, cap} for a node subtree
  if (m.memo.has(id)) return m.memo.get(id);
  const n = m.nodes.get(id);
  let s;
  if (n.level === 'box') s = { used: (m.byBox.get(id) || []).length, cap: n.grid_rows * n.grid_cols };
  else if (n.level === 'space') s = { used: 0, cap: 0 };
  else {
    s = { used: 0, cap: 0 };
    for (const c of kidsOf(m, id)) { const cs = stats(m, c.id); s.used += cs.used; s.cap += cs.cap; }
  }
  m.memo.set(id, s);
  return s;
}
const frac = s => s.cap ? s.used / s.cap : 0;
function ownerMatch(o) {
  const f = state.highlight;
  if (!f) return true;
  if (f === 'common') return o.common;
  return !o.common && String(o.owner_id) === f;
}
function nodeMatches(m, id) {     // does anything under this node belong to the highlighted owner?
  if (!state.highlight) return true;
  if (m.match.has(id)) return m.match.get(id);
  const n = m.nodes.get(id);
  let r;
  if (n.level === 'box') r = ownerMatch(nodeOwner(m, id)) || (m.byBox.get(id) || []).some(it => ownerMatch(itemOwner(m, it)));
  else {
    const kids = kidsOf(m, id);
    r = kids.length ? kids.some(k => nodeMatches(m, k.id)) : ownerMatch(nodeOwner(m, id));
    if (!r) r = m.loose.some(it => it.node_id === id && ownerMatch(itemOwner(m, it)));
  }
  m.match.set(id, r);
  return r;
}
function posLabel(box, r, c) {
  if (box.label_style === 'number') return String(r * box.grid_cols + c + 1);
  return String.fromCharCode(65 + r) + (c + 1);
}
const nodeHref = (m, id) => id ? `#/unit/${m.unit.id}/n/${id}` : `#/unit/${m.unit.id}`;

// ------------------------------------------------------------------------ //
// Unit page
// ------------------------------------------------------------------------ //
async function viewUnit(uid, nid, query) {
  const m = buildModel(await api('GET', `/api/units/${uid}`));
  const node = nid ? m.nodes.get(nid) : null;
  if (nid && !node) throw new Error('That part of the layout no longer exists.');
  const t = TYPES[m.unit.type];
  if (!node || node.level !== 'box' || state.boxSel.box !== node.id) state.boxSel = { box: node?.level === 'box' ? node.id : null, cells: new Set(), on: false };

  const chain = node ? chainOf(m, node.id) : [];
  const crumbs = h('div', { class: 'crumbs' },
    h('a', { href: '#/' }, 'Storage'), '›',
    node ? h('a', { href: nodeHref(m, null) }, m.unit.name) : h('span', { class: 'cur' }, m.unit.name),
    chain.map((n, i) => [' › ', i === chain.length - 1 ? h('span', { class: 'cur' }, nodeLabel(n)) : h('a', { href: nodeHref(m, n.id) }, nodeLabel(n))]));

  const hl = ownerSelect(null, false, 'Highlight: everyone', 'hl');
  hl.querySelector('option[value=""]').textContent = 'Highlight: everyone';
  hl.value = state.highlight;
  hl.onchange = () => { state.highlight = hl.value; refresh(); };

  const title = node ? nodeLabel(node) : m.unit.name;
  const sub = node
    ? `${LEVEL[node.level]} in ${m.unit.name}` + (node.level === 'box' ? ` · ${node.grid_rows}×${node.grid_cols}` : '')
    : [t.label, m.unit.location].filter(Boolean).join(' · ');
  const back = node ? nodeHref(m, node.parent_id) : '#/';

  let diagram;
  if (!node) diagram = m.unit.type === 'ln2' ? dewarView(m) : cabinetView(m);
  else if (node.level === 'shelf') diagram = shelfView(m, node);
  else if (node.level === 'rack') diagram = rackView(m, node);
  else if (node.level === 'row') diagram = rowView(m, node);
  else if (node.level === 'space') diagram = spaceView(m, node);
  else diagram = boxView(m, node, query);

  const backTo = node ? (node.parent_id ? nodeLabel(m.nodes.get(node.parent_id)) : m.unit.name) : 'all storage';
  return h('div', { class: t.cls },
    h('div', { class: 'nav-row' },
      h('a', { class: 'btn back-btn', href: back, title: 'Go back one level' }, h('span', { class: 'arrow' }, '←'), `Back to ${backTo}`),
      crumbs),
    h('div', { class: 'page-head' },
      node ? locator(m, node) : null,
      h('div', { class: 'titles' }, h('div', { class: 'row' }, h('span', { class: 'type-badge' }, t.short), h('h1', null, title)), h('p', null, sub)),
      node ? null : h('a', { class: 'btn', href: `#/unit/${uid}/edit` }, 'Edit details')),
    h('div', { class: 'unit-layout' },
      h('div', { class: 'diagram', 'data-dnd': true, 'data-unit': uid }, h('div', { class: 'toolbar' }, hl, legend(m), h('span', { class: 'spacer' })), diagram),
      h('div', { class: 'side' }, node ? nodeSide(m, node) : unitSide(m))));
}
/** "You are here": a tiny map of the whole unit (and of the rack, once inside one), current spot highlighted. */
function locator(m, node) {
  const chain = chainOf(m, node.id);
  const at = lvl => chain.find(n => n.level === lvl);
  const shelf = at('shelf'), piece = chain.find(n => n.level === 'rack' || n.level === 'space');
  const shelves = kidsOf(m, null);
  const cellTitle = n => n.level === 'space' ? `${nodeLabel(n)} (${plural(looseCount(m, n.id), 'item')})` : `${nodeLabel(n)} — ${stats(m, n.id).used}/${stats(m, n.id).cap}`;
  let unitMap;
  const lplan = unitPlan(m.unit);
  const tankPieces = kidsOf(m, null).filter(p => p.level === 'rack' || p.level === 'space');
  if (isTank(m) && planFits(lplan, tankPieces)) {
    const { pos, size } = planGeom(lplan, 50, 44);
    const byPos = new Map(tankPieces.map(p => [p.position, p]));
    const cells = lplan.slots.map((sl, i) => {
      const [x, y] = pos(sl), p = byPos.get(i);
      return `<rect class="${p && piece && p.id === piece.id ? 'on' : ''}${p ? '' : ' empty'}${p && p.level === 'space' ? ' space' : ''}" ${p ? `data-href="${nodeHref(m, p.id)}"` : ''} x="${(x - size / 2).toFixed(1)}" y="${(y - size / 2).toFixed(1)}" width="${size.toFixed(1)}" height="${size.toFixed(1)}" rx="1.5"><title>${esc(p ? cellTitle(p) : 'Empty slot ' + sl.name)}</title></rect>`;
    }).join('');
    const walls = lplan.dividers ? '<line class="divider" x1="50" y1="6" x2="50" y2="94"/><line class="divider" x1="6" y1="50" x2="94" y2="50"/>' : '';
    unitMap = h('div', { class: 'loc-map', html: `<svg viewBox="0 0 100 100" class="loc-dewar"><circle class="wall" cx="50" cy="50" r="48"/>${walls}${cells}</svg>` });
  } else if (isTank(m)) {
    const pieces = tankPieces;
    const slots = [];
    pieces.forEach(p => { for (let k = 0; k < (p.level === 'space' ? (p.span || 1) : 1); k++) slots.push(p); });
    const n = slots.length, C = 50;
    const pos = i => {
      if (n === 1) return [C, C];
      const outer = n > 9 ? n - Math.min(7, Math.round(n / 3)) : n;
      const [idx, cnt, R] = i < outer ? [i, outer, n <= 4 ? 20 : n > 9 ? 32 : 27] : [i - outer, n - outer, 15];
      const a = -Math.PI / 2 + 2 * Math.PI * idx / cnt;
      return [C + R * Math.cos(a), C + R * Math.sin(a)];
    };
    const r = Math.max(3, Math.min(8, n ? 70 / n : 8));
    const dots = slots.map((p, i) => {
      const [x, y] = pos(i);
      const on = piece && p.id === piece.id;
      return `<circle class="${on ? 'on' : ''}${p.level === 'space' ? ' space' : ''}" data-href="${nodeHref(m, p.id)}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}"><title>${esc(cellTitle(p))}</title></circle>`;
    }).join('');
    unitMap = h('div', { class: 'loc-map', html: `<svg viewBox="0 0 100 100" class="loc-dewar"><circle class="wall" cx="50" cy="50" r="48"/>${dots}</svg>` });
  } else {
    // Portrait mini-freezer: shelves share its height, racks share each shelf's width.
    unitMap = h('div', { class: 'loc-map loc-cab' }, shelves.map(sh =>
      h('div', { class: 'loc-shelf' + (shelf && sh.id === shelf.id ? ' on' : ''), 'data-href': nodeHref(m, sh.id), title: nodeLabel(sh) },
        kidsOf(m, sh.id).length ? kidsOf(m, sh.id).map(p => h('i', {
          class: 'loc-cell' + (p.level === 'space' ? ' space' : '') + (piece && p.id === piece.id ? ' on' : ''),
          style: { flex: `${p.level === 'space' ? (p.span || 1) : 1} 1 0` },
          'data-href': nodeHref(m, p.id), title: cellTitle(p) })) : h('i', { class: 'loc-cell empty' }))));
  }
  const parts = [h('div', { class: 'loc-part' }, unitMap,
    h('span', null, [shelf, piece].filter(Boolean).map(nodeLabel).join(' · ') || m.unit.name))];
  return h('div', { class: 'locator', 'aria-label': 'Where you are in this unit' }, h('div', { class: 'loc-title' }, 'You are here'), h('div', { class: 'loc-parts' }, parts));
}
function legend(m) {
  const used = new Set(m.items.map(i => i.category_id));
  const cats = state.cats.filter(c => used.has(c.id));
  if (!cats.length) return null;
  return h('div', { class: 'legend' }, cats.map(c => h('span', null, h('i', { class: 'dot', style: { background: c.color } }), c.name)));
}
/** Thin fill gauge: a summary of this container only, not a picture of what's inside it. */
function fillGauge(s) {
  return h('div', { class: 'gauge' }, h('i', { style: { width: (s.cap ? Math.round(100 * frac(s)) : 0) + '%' } }));
}
function ownerBar(n) {
  return (n.owner_id || n.is_common) ? h('div', { class: 'owner-bar', style: { background: n.is_common ? 'var(--text-faint)' : userColor(n.owner_id) }, title: ownerName(n.owner_id, n.is_common) }) : null;
}
function rackTile(m, rack) {
  const s = stats(m, rack.id);
  return h('div', { class: 'rack-tile' + (nodeMatches(m, rack.id) ? '' : ' dim'), 'data-href': nodeHref(m, rack.id), 'data-drag': rack.id,
      title: `${nodeLabel(rack)} — ${s.used}/${s.cap} positions filled · drag to move` },
    h('div', { class: 'lbl' }, h('span', null, nodeLabel(rack))),
    h('div', { class: 'tile-sub' }, s.cap ? Math.round(100 * frac(s)) + '% full' : 'empty'),
    fillGauge(s), ownerBar(rack));
}
function looseCount(m, nodeId) { return m.loose.filter(i => (i.node_id || 0) === (nodeId || 0)).length; }
function spaceTile(m, sp) {
  const list = m.loose.filter(i => i.node_id === sp.id);
  const span = sp.span || 1;
  const match = !state.highlight || ownerMatch(nodeOwner(m, sp.id)) || list.some(it => ownerMatch(itemOwner(m, it)));
  // flex-grow = span: a 2-wide space always takes the room of two racks, at any window width.
  return h('div', { class: 'rack-tile space-tile' + (match ? '' : ' dim'), 'data-href': nodeHref(m, sp.id), 'data-drag': sp.id,
      style: { flex: `${span} 1 0` },
      title: `${nodeLabel(sp)} — ${plural(list.length, 'loose item')} · ${span} rack-width${span > 1 ? 's' : ''} · drag to move` },
    h('div', { class: 'lbl' }, h('span', null, nodeLabel(sp))),
    h('div', { class: 'tile-sub' }, list.length ? plural(list.length, 'item') : 'empty'),
    ownerBar(sp));
}
const shelfPiece = (m, n) => n.level === 'space' ? spaceTile(m, n) : rackTile(m, n);


function cabinetView(m) {
  const shelves = kidsOf(m, null);
  const t = TYPES[m.unit.type];
  return h('div', { class: 'cabinet' },
    h('div', { class: 'cabinet-top' }, h('span', { class: 'display' }, t.temp), h('span', { class: 'nm' }, m.unit.name),
      h('span', { class: 'spacer' }), m.unit.manufacturer ? h('span', { class: 'small muted' }, m.unit.manufacturer) : null),
    h('div', { class: 'cabinet-inner' },
      shelves.length ? shelves.map(sh => {
        const racks = kidsOf(m, sh.id);
        const lc = looseCount(m, sh.id);
        return h('div', { class: 'shelf', 'data-drop-parent': sh.id },
          h('div', { class: 'shelf-head' }, h('a', { href: nodeHref(m, sh.id) }, nodeLabel(sh)),
            ownerChip(sh.owner_id, sh.is_common, { short: true }), lc ? h('span', { class: 'pill' }, `${lc} loose`) : null),
          racks.length ? h('div', { class: 'racks' }, racks.map(r => shelfPiece(m, r)))
            : h('div', { class: 'racks empty-shelf' }, h('span', null, 'Empty shelf — ', h('a', { href: nodeHref(m, sh.id) }, 'open it'), ' to add racks or a loose space, or drop a rack here')));
      }) : h('div', { class: 'empty-note' }, 'No shelves yet — add one from the panel on the right.')));
}
// ---------- Arranged tanks (racks in fixed compartment slots, e.g. CryoExtra CE8120) ----------
function unitPlan(unit) {
  const p = unit && unit.plan;
  if (!p) return null;
  try { return typeof p === 'string' ? JSON.parse(p) : p; } catch { return null; }
}
/** The arrangement applies to a tier when every rack sits in a distinct, existing slot. */
function planFits(plan, pieces) {
  return !!plan && pieces.every(p => p.position < plan.slots.length) && new Set(pieces.map(p => p.position)).size === pieces.length;
}
/** Slot centres inside a circle of radius R around (C, C), as seen from above. */
function planGeom(plan, C, R) {
  const n = Math.max(plan.cols, plan.rows);
  const pitch = (2 * R * 0.86) / n, size = pitch * 0.9, gap = plan.dividers ? pitch * 0.06 : 0;
  const pos = sl => {
    const dx = sl.c - (plan.cols - 1) / 2, dy = sl.r - (plan.rows - 1) / 2;
    return [C + dx * pitch + Math.sign(dx) * gap, C + dy * pitch + Math.sign(dy) * gap];
  };
  return { pos, size };
}
function planSVG(m, sh, plan, pieces) {
  const { pos, size } = planGeom(plan, 160, 142);
  const bySlot = new Map(pieces.map(p => [p.position, p]));
  const walls = plan.dividers ? `<line class="divider" x1="160" y1="18" x2="160" y2="302"/><line class="divider" x1="18" y1="160" x2="302" y2="160"/>` : '';
  const cells = plan.slots.map((sl, i) => {
    const [x, y] = pos(sl), x0 = x - size / 2, y0 = y - size / 2, rx = size * 0.1;
    const p = bySlot.get(i);
    if (!p) return `<g class="slot-empty" data-slot="${i}"><title>Empty slot ${esc(sl.name)} — drop a rack here</title>
      <rect x="${x0}" y="${y0}" width="${size}" height="${size}" rx="${rx}"/><text x="${x}" y="${y}">${esc(sl.name)}</text></g>`;
    const name = nodeLabel(p), lbl = esc(name.length > 6 ? name.slice(0, 6) : name), long = name.length > 3 ? 'long' : '';
    if (p.level === 'space') {
      const cnt = looseCount(m, p.id);
      return `<g class="rk space" data-href="${nodeHref(m, p.id)}" data-drag="${p.id}"><title>${esc(name)} (slot ${esc(sl.name)}) — ${cnt} loose item(s) · drag onto another slot to swap</title>
        <rect x="${x0}" y="${y0}" width="${size}" height="${size}" rx="${rx}"/><text class="${long}" x="${x}" y="${y - 6}">${lbl}</text><text class="sub" x="${x}" y="${y + 9}">${cnt} item${cnt === 1 ? '' : 's'}</text></g>`;
    }
    const s = stats(m, p.id), own = p.owner_id || p.is_common;
    return `<g class="rk${nodeMatches(m, p.id) ? '' : ' dim'}" data-href="${nodeHref(m, p.id)}" data-drag="${p.id}">
      <title>${esc(name)}${name !== sl.name ? ` (in slot ${esc(sl.name)})` : ''} — ${s.used}/${s.cap} filled${own ? ' · ' + esc(ownerName(p.owner_id, p.is_common)) : ''} · drag onto another slot to swap</title>
      <rect x="${x0}" y="${y0}" width="${size}" height="${size}" rx="${rx}" style="fill:${fillColor(frac(s))}"/>
      ${own ? `<rect x="${x0 + 4}" y="${y0 + size - 7}" width="${size - 8}" height="3" rx="1.5" style="fill:${p.is_common ? 'var(--text-faint)' : userColor(p.owner_id)};stroke:none"/>` : ''}
      <text class="${long}" x="${x}" y="${y - 6}">${lbl}</text><text class="sub" x="${x}" y="${y + 9}">${s.cap ? Math.round(100 * frac(s)) + '%' : '—'}</text></g>`;
  }).join('');
  return walls + cells;
}

function dewarView(m) {
  const pieces = kidsOf(m, null).filter(p => p.level === 'rack' || p.level === 'space');
  if (!pieces.length) return h('div', { class: 'card empty-note' }, 'No racks yet — add racks from the panel on the right.');
  const plan = unitPlan(m.unit);
  const arranged = planFits(plan, pieces);
  let parts;
  if (arranged) parts = planSVG(m, null, plan, pieces);
  else {
    // Each rack takes one slot round the tank; a loose space takes `span` consecutive slots.
      const slots = [];
      pieces.forEach(p => { for (let k = 0; k < (p.level === 'space' ? (p.span || 1) : 1); k++) slots.push(p); });
      const n = slots.length;
      const C = 160, places = [];
      function ring(i, count, R) { const a = -Math.PI / 2 + 2 * Math.PI * i / count; return [C + R * Math.cos(a), C + R * Math.sin(a), R * 2 * Math.sin(Math.PI / Math.max(count, 2))]; }
      if (n === 1) places.push([C, C, 0]);
      else if (n <= 9) slots.forEach((_, i) => places.push(ring(i, n, n <= 4 ? 62 : 82)));
      else {
        const inner = Math.min(7, Math.round(n / 3)), outer = n - inner;
        for (let i = 0; i < outer; i++) places.push(ring(i, outer, 100));
        for (let i = 0; i < inner; i++) places.push(ring(i, inner, 46));
      }
      const size = Math.max(22, Math.min(58, ...places.map(p => p[2] ? p[2] * 0.78 : 80)));
      const bySlot = new Map();
      slots.forEach((p, i) => { if (!bySlot.has(p.id)) bySlot.set(p.id, []); bySlot.get(p.id).push(places[i]); });
      parts = pieces.map(r => {
        const pl = bySlot.get(r.id) || [];
        const [x, y] = pl[0] || [C, C];
        const own = r.owner_id || r.is_common;
        if (r.level === 'space') {
          const cnt = looseCount(m, r.id);
          const match = !state.highlight || ownerMatch(nodeOwner(m, r.id)) || m.loose.some(it => it.node_id === r.id && ownerMatch(itemOwner(m, it)));
          const cx = pl.reduce((a, p) => a + p[0], 0) / pl.length, cy = pl.reduce((a, p) => a + p[1], 0) / pl.length;
          return `<g class="rk space${match ? '' : ' dim'}" data-href="${nodeHref(m, r.id)}" data-drag="${r.id}">
            <title>${esc(nodeLabel(r))} — ${cnt} loose item(s) · ${pl.length} slot(s) · drag to move</title>
            ${pl.map(([px, py]) => `<rect x="${px - size / 2}" y="${py - size / 2}" width="${size}" height="${size}" rx="${size * 0.22}"/>`).join('')}
            <text class="long" x="${cx}" y="${cy - (size > 34 ? 5 : 0)}">${esc(r.name && !/^Loose space \d+$/.test(r.name) ? r.name.slice(0, 7) : 'Loose')}</text>
            ${size > 34 ? `<text class="sub" x="${cx}" y="${cy + 9}">${cnt} item${cnt === 1 ? '' : 's'}</text>` : ''}
          </g>`;
        }
        const s = stats(m, r.id);
        const lbl = r.name ? r.name.replace(/^Rack /, '').slice(0, 6) : String(r.index + 1);
        return `<g class="rk${nodeMatches(m, r.id) ? '' : ' dim'}" data-href="${nodeHref(m, r.id)}" data-drag="${r.id}">
          <title>${esc(nodeLabel(r))} — ${s.used}/${s.cap} filled${own ? ' · ' + esc(ownerName(r.owner_id, r.is_common)) : ''} · drag to move</title>
          <rect x="${x - size / 2}" y="${y - size / 2}" width="${size}" height="${size}" rx="${size * 0.22}" style="fill:${fillColor(frac(s))}"/>
          ${own ? `<rect x="${x - size / 2 + 4}" y="${y + size / 2 - 7}" width="${size - 8}" height="3" rx="1.5" style="fill:${r.is_common ? 'var(--text-faint)' : userColor(r.owner_id)};stroke:none"/>` : ''}
          <text class="${lbl.length > 3 ? 'long' : ''}" x="${x}" y="${y - (size > 34 ? 5 : 0)}">${esc(lbl)}</text>
          ${size > 34 ? `<text class="sub" x="${x}" y="${y + 9}">${s.cap ? Math.round(100 * frac(s)) + '%' : '—'}</text>` : ''}
        </g>`;
      }).join('');
  }
  const lc = looseCount(m, null);
  const nr = pieces.filter(p => p.level === 'rack').length, ns = pieces.length - nr;
  return h('div', { class: 'dewar-wrap' }, h('div', { class: 'dewar', 'data-drop-parent': '', 'data-plan': arranged ? '1' : null, style: { flex: '1 1 100%' } },
    h('div', { class: 'shelf-head' }, h('span', null, 'Top view'), lc ? h('span', { class: 'pill' }, `${lc} loose`) : null,
      h('span', { class: 'spacer' }), h('span', { class: 'faint' },
        arranged ? `${pieces.length}/${plan.slots.length} slots used` : plural(nr, 'rack') + (ns ? ` · ${plural(ns, 'loose space')}` : ''))),
    h('div', { html: `<svg viewBox="0 0 320 320" role="img" aria-label="Top view of ${esc(m.unit.name)}">
      <circle class="wall" cx="160" cy="160" r="156"/><circle class="inner" cx="160" cy="160" r="142"/>
      ${arranged ? '' : '<circle class="vapor" cx="160" cy="160" r="128"/>'}${parts}</svg>` })));
}
function shelfView(m, shelf) {
  const racks = kidsOf(m, shelf.id);
  return h('div', null,
    h('div', { class: 'cabinet' }, h('div', { class: 'cabinet-inner' }, h('div', { class: 'shelf', 'data-drop-parent': shelf.id },
      racks.length ? h('div', { class: 'racks big' }, racks.map(r => shelfPiece(m, r))) : h('div', { class: 'racks empty-shelf' }, 'No racks on this shelf. Add racks or a loose space from the panel, or keep loose items here.')))),
    h('p', { class: 'small faint', style: { marginTop: '8px' } }, 'Drag racks and loose spaces to reorder them (long-press on a phone).'));
}
function boxTile(m, b, big = false) {
  const s = stats(m, b.id);
  return h('div', { class: 'box-tile' + (big ? ' big' : '') + (nodeMatches(m, b.id) ? '' : ' dim'), 'data-href': nodeHref(m, b.id), 'data-drag': b.id,
      title: `${nodeLabel(b)} — ${s.used}/${s.cap} positions filled (${b.grid_rows}×${b.grid_cols})` },
    h('div', { class: 'lbl' }, h('span', null, nodeLabel(b))),
    h('div', { class: 'tile-sub' }, `${s.used}/${s.cap}`),
    fillGauge(s), ownerBar(b));
}
/** A loose space: a dashed area (wider for wider spaces) holding its loose items as cards. */
function spaceView(m, sp) {
  const list = m.loose.filter(i => i.node_id === sp.id);
  const span = sp.span || 1;
  return h('div', null,
    h('div', { class: 'space-frame', style: { maxWidth: `${Math.min(150 + 110 * span, 560)}px` } },
      list.map(it => {
        const o = itemOwner(m, it);
        return h('button', { class: 'space-item' + (ownerMatch(o) ? '' : ' dim'), style: { '--cc': catColor(it.category_id) }, onclick: () => openItem(it.id) },
          h('b', null, it.name), h('span', null, [it.quantity, it.loose_where].filter(Boolean).join(' · ') || ' '));
      }),
      state.moving
        ? h('button', { class: 'space-item add', onclick: () => moveLoose(m, sp) }, `Put “${state.moving.name}” here`)
        : h('button', { class: 'space-item add', onclick: () => openItemForm({ m, shelf: sp, loose: true }) }, '+ Add item')),
    h('p', { class: 'small faint', style: { marginTop: '8px' } }, `${span} rack-width${span > 1 ? 's' : ''} wide · ${plural(list.length, 'item')}`));
}
function rackView(m, rack) {
  if (isTank(m)) {
    const boxes = kidsOf(m, rack.id);
    if (!boxes.length) return h('div', { class: 'card empty-note' }, 'This rack has no boxes yet — add boxes from the panel.');
    return h('div', null,
      h('div', { class: 'rack-frame row-frame tank-rack' }, h('div', { class: 'rack-row', 'data-drop-parent': rack.id },
        h('div', { class: 'row-boxes col' }, boxes.map(b => boxTile(m, b, true))))),
      h('p', { class: 'small faint', style: { marginTop: '8px' } }, 'Box 1 is at the top of the rack. Drag boxes to reorder them (long-press on a phone).'));
  }
  const rowsN = kidsOf(m, rack.id);
  if (!rowsN.length) return h('div', { class: 'card empty-note' }, 'This rack has no rows yet — add rows from the panel.');
  return h('div', { class: 'rack-frame' }, rowsN.map(r => {
    const s = stats(m, r.id);
    return h('div', { class: 'rack-row' + (nodeMatches(m, r.id) ? '' : ' dim'), 'data-drop-parent': r.id },
      h('div', { class: 'row-lbl', 'data-href': nodeHref(m, r.id), title: 'Open row' }, nodeLabel(r), h('small', null, `${s.used}/${s.cap}`)),
      h('div', { class: 'row-boxes' }, kidsOf(m, r.id).length ? kidsOf(m, r.id).map(b => boxTile(m, b)) : h('span', { class: 'faint small', style: { alignSelf: 'center' } }, 'no boxes')));
  }));
}
function rowView(m, row) {
  const boxes = kidsOf(m, row.id);
  if (!boxes.length) return h('div', { class: 'card empty-note' }, 'This row has no boxes yet — add boxes from the panel.');
  return h('div', null,
    h('div', { class: 'rack-frame row-frame' }, h('div', { class: 'rack-row', 'data-drop-parent': row.id }, h('div', { class: 'row-boxes col' }, boxes.map(b => boxTile(m, b, true))))),
    h('p', { class: 'small faint', style: { marginTop: '8px' } }, 'Drag boxes to reorder them (long-press on a phone). Open the rack to move a box to another row.'));
}

// ---------- Box view ----------
function boxView(m, box, query) {
  const items = m.byBox.get(box.id) || [];
  const grid = new Map(items.map(it => [it.pos_row + ',' + it.pos_col, it]));
  const sel = state.boxSel;
  const hlId = query.hl ? +query.hl : null;
  const R = box.grid_rows, C = box.grid_cols;
  const compact = C > 12;
  const g = h('div', { class: 'box-grid' + (compact ? ' compact' : ''), style: { gridTemplateColumns: `22px repeat(${C}, minmax(0, 1fr))` } });
  g.append(h('div'));
  for (let c = 0; c < C; c++) g.append(h('div', { class: 'hdr' }, box.label_style === 'number' ? '' : c + 1));
  for (let r = 0; r < R; r++) {
    g.append(h('div', { class: 'hdr' }, box.label_style === 'number' ? '' : String.fromCharCode(65 + r)));
    for (let c = 0; c < C; c++) {
      const key = r + ',' + c, it = grid.get(key), pl = posLabel(box, r, c);
      const dim = it && !ownerMatch(itemOwner(m, it));
      const cell = h('div', {
        class: 'cell ' + (it ? 'filled' : 'empty') + (sel.cells.has(key) ? ' sel' : '') + (it && it.id === hlId ? ' hl' : '') + (dim ? ' dim' : ''),
        style: it ? { '--cc': catColor(it.category_id) } : null,
        title: it ? `${pl}: ${it.name}${it.category_id ? ' · ' + state.catsById.get(it.category_id)?.name : ''}${it.quantity ? ' · ' + it.quantity : ''}` : `${pl} — empty`,
      }, h('span', { class: 'pos' }, pl), it ? h('span', { class: 'nm' }, it.name) : null);
      cell.addEventListener('click', e => onCell(e, m, box, r, c, it));
      g.append(cell);
    }
  }
  const nSel = sel.cells.size;
  const selItems = [...sel.cells].map(k => grid.get(k)).filter(Boolean);
  const selEmpty = [...sel.cells].filter(k => !grid.has(k));
  const toolbar = h('div', { class: 'toolbar' },
    h('button', { class: 'btn sm' + (sel.on ? ' on' : ''), onclick: () => { sel.on = !sel.on; if (!sel.on) sel.cells.clear(); refresh(); } }, sel.on ? 'Done selecting' : 'Select positions'),
    nSel ? h('span', { class: 'small muted' }, `${nSel} selected`) : h('span', { class: 'small faint' }, sel.on ? 'Click positions to select them' : 'Tip: shift-click to select several'),
    selEmpty.length ? h('button', { class: 'btn sm primary', onclick: () => openItemForm({ m, box, positions: selEmpty.map(k => k.split(',').map(Number)) }) }, `+ Add item to ${plural(selEmpty.length, 'position')}`) : null,
    selItems.length ? h('button', { class: 'btn sm danger', onclick: () => removeDialog(selItems, () => { sel.cells.clear(); refresh(); }) }, `Remove ${plural(selItems.length, 'item')}`) : null,
    nSel ? h('button', { class: 'btn sm ghost', onclick: () => { sel.cells.clear(); refresh(); } }, 'Clear') : null);

  const sorted = [...items].sort((a, b) => a.pos_row - b.pos_row || a.pos_col - b.pos_col);
  return h('div', { class: 'box-wrap' + (state.moving ? ' moving' : '') },
    toolbar, g,
    h('div', { class: 'card', style: { marginTop: '16px' } },
      h('div', { class: 'card-head' }, h('h2', null, `Contents (${items.length}/${R * C})`)),
      sorted.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'list' },
        h('thead', null, h('tr', null, ['Pos', 'Item', 'Category', 'Owner', 'Qty', 'Added'].map(x => h('th', null, x)))),
        h('tbody', null, sorted.map(it => {
          const o = itemOwner(m, it);
          return h('tr', { class: 'clickable', onclick: () => openItem(it.id) },
            h('td', { class: 'mono' }, posLabel(box, it.pos_row, it.pos_col)), h('td', { class: 'nm' }, it.name),
            h('td', null, catLabel(it.category_id)), h('td', null, ownerChip(o.owner_id, o.common, { short: true, inherit: o.from !== 'item' })),
            h('td', null, it.quantity || ''), h('td', { class: 'small muted' }, fmtDate(it.date_added)));
        })))) : h('p', { class: 'empty-note' }, 'Empty box. Click a position above to add an item.')));
}
function catLabel(id) {
  const c = state.catsById.get(id);
  return c ? h('span', { class: 'row', style: { gap: '6px', flexWrap: 'nowrap' } }, h('i', { class: 'dot', style: { background: c.color } }), c.name) : h('span', { class: 'faint' }, '—');
}
async function onCell(e, m, box, r, c, it) {
  const key = r + ',' + c, sel = state.boxSel;
  if (state.moving && !it) {
    const mv = state.moving;
    const ok = await attempt(() => api('POST', `/api/items/${mv.id}/move`, { unit_id: m.unit.id, node_id: box.id, pos: [r, c] }), `Moved to ${nodeLabel(box)} ${posLabel(box, r, c)}`);
    if (ok) { state.moving = null; refresh(); }
    return;
  }
  if (sel.on || e.shiftKey || e.metaKey || e.ctrlKey) {
    sel.cells.has(key) ? sel.cells.delete(key) : sel.cells.add(key);
    refresh();
    return;
  }
  if (it) openItem(it.id);
  else openItemForm({ m, box, positions: [[r, c]], allowFill: true });
}

// ---------- Side panels ----------
function unitSide(m) {
  const u = m.unit, t = TYPES[u.type];
  const kv = [['Type', t.label], ['Location', u.location], ['Size', u.size], ['Manufacturer', u.manufacturer], ['Device name', u.device_name], ['Model', u.model],
    ['Serial no.', u.serial_number], ['Inventory no.', u.inventory_number],
    ['Last defrost', u.last_defrost ? `${fmtDate(u.last_defrost)} (${ago(u.last_defrost)})` : null],
    ['Last PM', u.last_pm ? `${fmtDate(u.last_pm)} (${ago(u.last_pm)})` : null]].filter(([, v]) => v);
  const boxed = u.stored - u.loose;
  return [
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h3', null, 'Details'), h('a', { class: 'btn sm', href: `#/unit/${u.id}/edit` }, 'Edit')),
      h('dl', { class: 'kv' }, kv.map(([k, v]) => [h('dt', null, k), h('dd', null, v)]),
        h('dt', null, 'Assigned to'), h('dd', null, ownerChip(u.owner_id, u.is_common, { none: h('span', { class: 'faint' }, 'Unassigned') })),
        h('dt', null, 'Contents'), h('dd', null, `${boxed} / ${u.capacity} positions · ${u.loose} loose`)),
      u.notes ? h('p', { class: 'small muted', style: { marginTop: '10px', whiteSpace: 'pre-wrap' } }, u.notes) : null),
    looseCard(m, null),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h3', null, 'Layout')),
      h('p', { class: 'small muted' }, isTank(m)
        ? `${plural(kidsOf(m, null).filter(x => x.level === 'rack').length, 'rack')} — click a rack in the diagram to rename it, assign it to someone, or change its boxes.`
        : `${plural(kidsOf(m, null).length, 'shelf')} — click a shelf or rack in the diagram to rename it, assign it to someone, or change what's inside.`.replace('shelfs', 'shelves')),
      h('div', { class: 'row', style: { marginTop: '10px' } },
        h('button', { class: 'btn sm', onclick: () => addChildrenDialog(m, null) }, isTank(m) ? '+ Add rack' : '+ Add shelf'),
        isTank(m) ? h('button', { class: 'btn sm', onclick: () => addSpaceDialog(m, null) }, '+ Add loose space') : null,
        h('a', { class: 'btn sm', href: `#/search?unit_id=${u.id}` }, 'List all items'),
        h('a', { class: 'btn sm', href: `#/activity?unit_id=${u.id}` }, 'History'),
        h('button', { class: 'btn sm', onclick: () => savePreset(m) }, 'Save as preset'))),
    state.me.role === 'admin' ? h('div', { class: 'card' },
      h('button', { class: 'btn danger sm', onclick: () => deleteUnit(m) }, 'Delete this storage unit…')) : null,
  ];
}
function looseCard(m, shelf) {
  const list = m.loose.filter(i => shelf ? i.node_id === shelf.id : true);
  const where = it => {
    if (!it.node_id) return it.loose_where || '';
    return [chainOf(m, it.node_id).map(nodeLabel).join(' › '), it.loose_where].filter(Boolean).join(' · ');
  };
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', null, `Loose items (${list.length})`),
      h('button', { class: 'btn sm', onclick: () => openItemForm({ m, shelf, loose: true }) }, '+ Add')),
    state.moving ? h('button', { class: 'btn sm primary', style: { marginBottom: '10px' }, onclick: () => moveLoose(m, shelf) }, `Put “${state.moving.name}” here as loose item`) : null,
    list.length ? h('ul', { class: 'feed' }, list.map(it => {
      const o = itemOwner(m, it);
      return h('li', { class: 'clickable' + (ownerMatch(o) ? '' : ' dim'), onclick: () => openItem(it.id) },
        h('i', { class: 'dot', style: { background: catColor(it.category_id), marginTop: '5px' } }),
        h('span', { style: { flex: 1, minWidth: 0 } }, h('span', { class: 'what', style: { fontWeight: 600 } }, it.name),
          it.quantity ? h('span', { class: 'muted' }, ' · ' + it.quantity) : null,
          h('div', { class: 'small faint' }, where(it))),
        ownerChip(o.owner_id, o.common, { short: true, inherit: o.from !== 'item' }));
    })) : h('p', { class: 'small faint' }, shelf ? `Nothing in this ${shelf.level === 'space' ? 'loose space' : 'shelf'} yet.` : 'Items that are not in a box or rack (bags, bottles, flasks…) go here.'));
}
async function moveLoose(m, shelf) {
  const mv = state.moving;
  const ok = await attempt(() => api('POST', `/api/items/${mv.id}/move`, { unit_id: m.unit.id, node_id: shelf ? shelf.id : null }), 'Moved');
  if (ok) { state.moving = null; refresh(); }
}
function nodeSide(m, node) {
  const inherited = node.parent_id ? nodeOwner(m, node.parent_id) : nodeOwner(m, null);
  const inhLabel = inherited.owner_id || inherited.common
    ? `Inherit — ${ownerName(inherited.owner_id, inherited.common)} (from ${inherited.from})` : 'Unassigned (inherit)';
  const form = h('form', { class: 'stack' },
    field('Name', inp('name', node.name, { placeholder: `${LEVEL[node.level]} ${node.index + 1}` }), 'optional'),
    field('Assigned to', ownerSelect(node.owner_id, node.is_common, inhLabel)),
    node.level === 'space' ? field('Width', inp('span', node.span || 1, { type: 'number', min: 1, max: 30 }), 'how many rack spaces it takes up') : null,
    node.level === 'box' ? h('div', { class: 'grid2' },
      field('Rows', inp('grid_rows', node.grid_rows, { type: 'number', min: 1, max: 26 })),
      field('Columns', inp('grid_cols', node.grid_cols, { type: 'number', min: 1, max: 26 })),
      field('Position labels', (() => { const s = h('select', { name: 'label_style', class: 'input' }, h('option', { value: 'grid' }, 'A1, A2, … (grid)'), h('option', { value: 'number' }, '1, 2, 3 … (numbered)')); s.value = node.label_style; return s; })(), null, 'span-all')) : null,
    field('Notes', h('textarea', { name: 'notes', class: 'input' }, node.notes || '')),
    h('button', { class: 'btn primary', type: 'submit' }, 'Save'));
  form.onsubmit = async e => {
    e.preventDefault();
    const d = formData(form);
    const body = { name: d.name, notes: d.notes, ...ownerPayload(d.owner) };
    if (node.level === 'box') Object.assign(body, { grid_rows: +d.grid_rows, grid_cols: +d.grid_cols, label_style: d.label_style });
    if (node.level === 'space') body.span = +d.span || 1;
    if (await attempt(() => api('PUT', `/api/nodes/${node.id}`, body), 'Saved')) refresh();
  };
  const s = stats(m, node.id);
  const child = childLevel(m.unit.type, node.level);
  return [
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h3', null, LEVEL[node.level] + ' settings'),
        node.level === 'space' ? h('span', { class: 'small muted' }, plural(looseCount(m, node.id), 'item')) : h('span', { class: 'small muted' }, `${s.used}/${s.cap} filled`)),
      form),
    node.level === 'shelf' || node.level === 'space' ? looseCard(m, node) : null,
    node.level === 'rack' || node.level === 'space' || node.level === 'box' ? placeCard(m, node) : null,
    h('div', { class: 'card' },
      h('div', { class: 'row' },
        child ? h('button', { class: 'btn sm', onclick: () => addChildrenDialog(m, node) }, `+ Add ${levelPlural(child)}`) : null,
        node.level === 'shelf' ? h('button', { class: 'btn sm', onclick: () => addSpaceDialog(m, node) }, '+ Add loose space') : null,
        node.level === 'rack' ? h('button', { class: 'btn sm', onclick: () => convertNode(m, node) }, 'Replace with loose space') : null,
        node.level === 'space' ? h('button', { class: 'btn sm', onclick: () => convertNode(m, node) }, 'Replace with rack') : null,
        node.level === 'space' ? mergeButtons(m, node) : null,
        node.level === 'box' ? h('a', { class: 'btn sm', href: `#/search?unit_id=${m.unit.id}&q=${encodeURIComponent('"' + chainOf(m, node.id).map(nodeLabel).join(' › ') + ' ›"')}` }, 'Search in box') : null,
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn sm danger', onclick: () => deleteNode(m, node) }, `Delete ${LEVEL[node.level].toLowerCase()}`))),
  ];
}
/** Where a rack / loose space sits: move left/right or to another shelf (keyboard-friendly twin of drag & drop). */
function placeCard(m, node) {
  const plan = node.level !== 'box' ? unitPlan(m.unit) : null;
  if (plan && planFits(plan, kidsOf(m, node.parent_id))) {
    const occupant = new Map(kidsOf(m, node.parent_id).map(p => [p.position, p]));
    const sel = h('select', { class: 'input' }, plan.slots.map((sl, i) => {
      const o = occupant.get(i);
      return h('option', { value: i }, `${sl.name || 'Slot ' + (i + 1)}${o ? (o.id === node.id ? ' (here)' : ` — ${nodeLabel(o)} (swap)`) : ' — empty'}`);
    }));
    sel.value = String(node.position);
    sel.onchange = async () => {
      const o = occupant.get(+sel.value);
      const body = o ? { parent_id: node.parent_id, swap_with: o.id } : { parent_id: node.parent_id, slot: +sel.value };
      if (await attempt(() => api('POST', `/api/nodes/${node.id}/place`, body), o ? 'Swapped' : 'Moved')) refresh(); else refresh();
    };
    const slotName = plan.slots[node.position]?.name;
    return h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h3', null, 'Position'), h('span', { class: 'small muted' }, `slot ${slotName || node.position + 1}`)),
      field('Slot', sel, 'picking a taken slot swaps the two'),
      h('p', { class: 'small faint', style: { marginTop: '8px' } }, 'Or drag it onto another slot in the tank diagram (long-press on a phone).'));
  }
  const sibs = kidsOf(m, node.parent_id);
  const i = sibs.findIndex(x => x.id === node.id);
  const isBox = node.level === 'box';
  const holder = node.parent_id ? m.nodes.get(node.parent_id) : null;
  // Racks/spaces can go to any shelf; a box to any row of its rack (in a tank: to any rack).
  const parents = isBox ? (isTank(m) ? kidsOf(m, null).filter(x => x.level === 'rack') : kidsOf(m, holder.parent_id))
    : (isTank(m) ? [] : kidsOf(m, null));
  const vertical = isBox && isTank(m);
  const shelves = parents;
  const shelfSel = h('select', { class: 'input' }, parents.map(sh => h('option', { value: sh.id }, nodeLabel(sh))));
  shelfSel.value = String(node.parent_id);
  shelfSel.onchange = () => placeNode(m, node, +shelfSel.value, null);
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', null, 'Position'), h('span', { class: 'small muted' }, `${i + 1} of ${sibs.length} in ${holder ? nodeLabel(holder) : m.unit.name}`)),
    h('div', { class: 'row' },
      h('button', { class: 'btn sm', disabled: i === 0, onclick: () => placeNode(m, node, node.parent_id, sibs[i - 1].id) }, vertical ? '▲ Move up' : '◀ Move left'),
      h('button', { class: 'btn sm', disabled: i === sibs.length - 1, onclick: () => placeNode(m, node, node.parent_id, sibs[i + 2] ? sibs[i + 2].id : null) }, vertical ? 'Move down ▼' : 'Move right ▶')),
    shelves.length > 1 ? field(isBox ? (isTank(m) ? 'Rack' : 'Row') : 'Shelf', shelfSel, `moves it to the ${vertical ? 'bottom' : 'end'} of that ${isBox ? (isTank(m) ? 'rack' : 'row') : 'shelf'}`) : null,
    h('p', { class: 'small faint', style: { marginTop: '8px' } }, 'Or drag it in the diagram (long-press on a phone).'));
}
async function placeNode(m, node, parentId, beforeId) {
  const r = await attempt(() => api('POST', `/api/nodes/${node.id}/place`, { parent_id: parentId, before_id: beforeId }));
  if (!r) return refresh();
  toast(r.renamed ? `Moved — renamed to “${r.renamed}” (that name was already taken there)` : 'Moved');
  refresh();
}
function addSpaceDialog(m, shelf) {
  const err = h('p', { class: 'error' });
  const form = h('form', { class: 'stack' },
    h('p', { class: 'small muted' }, `A loose space is room ${shelf ? 'on the shelf' : 'in the tank'} for things that are not in racks or boxes: bottles, bags, canes, bulky kits.`),
    h('div', { class: 'grid2' },
      field('Width', inp('span', 1, { type: 'number', min: 1, max: 30 }), 'in rack spaces'),
      field('Name', inp('name', '', { placeholder: 'optional, e.g. Media bottles' }))), err);
  const go = async () => {
    const v = formData(form);
    try { await api('POST', `/api/units/${m.unit.id}/nodes`, { parent_id: shelf ? shelf.id : null, level: 'space', span: +v.span || 1, name: v.name }); close(); toast('Loose space added'); refresh(); }
    catch (x) { err.textContent = x.message; }
  };
  form.onsubmit = e => { e.preventDefault(); go(); };
  const close = openModal({ title: `Add loose space to ${shelf ? nodeLabel(shelf) : m.unit.name}`, body: form,
    foot: [h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn primary', onclick: go }, 'Add')] });
}
/** "Merge with …" for a loose space whose direct neighbour is also a loose space. */
function mergeButtons(m, node) {
  const sibs = kidsOf(m, node.parent_id);
  if (!node.parent_id && planFits(unitPlan(m.unit), sibs)) return null;   // fixed tank slots
  const i = sibs.findIndex(x => x.id === node.id);
  return [sibs[i - 1], sibs[i + 1]].map((nb, k) => nb && nb.level === 'space'
    ? h('button', { class: 'btn sm', title: 'Combine the two spaces into one (widths add up, items move over)', onclick: () => mergeSpaces(m, node, nb) },
        k === 0 ? `◀ Merge with ${nodeLabel(nb)}` : `Merge with ${nodeLabel(nb)} ▶`) : null);
}
async function mergeSpaces(m, a, b) {
  const [left, right] = kidsOf(m, a.parent_id).findIndex(x => x.id === a.id) < kidsOf(m, a.parent_id).findIndex(x => x.id === b.id) ? [a, b] : [b, a];
  const nItems = looseCount(m, right.id);
  const ok = await confirmBox(`Merge ${nodeLabel(right)} into ${nodeLabel(left)}?`,
    `${nodeLabel(left)} becomes ${(left.span || 1) + (right.span || 1)} rack-widths wide` +
    (nItems ? ` and takes over ${plural(nItems, 'item')} from ${nodeLabel(right)}` : '') + `. ${nodeLabel(right)} is removed. To split it again later, make it narrower and add a new loose space.`,
    { okText: 'Merge' });
  if (!ok) return;
  const r = await attempt(() => api('POST', `/api/nodes/${a.id}/merge`, { with_id: b.id }), 'Merged');
  if (r) location.hash = nodeHref(m, r.id);
}
async function convertNode(m, node) {
  if (node.level === 'rack') {
    const s = stats(m, node.id);
    if (s.used) { toast(`${nodeLabel(node)} still holds ${plural(s.used, 'item')} — move or remove them first.`, true); return; }
    const ok = await confirmBox(`Replace ${nodeLabel(node)} with a loose space?`,
      `The empty rack (and its empty boxes) is removed and a loose-item space takes its place. You can widen the space afterwards, or turn it back into a rack.`,
      { okText: 'Replace with loose space' });
    if (ok && await attempt(() => api('POST', `/api/nodes/${node.id}/convert`), 'Now a loose space')) refresh();
    return;
  }
  if (looseCount(m, node.id)) { toast(`${nodeLabel(node)} still holds ${plural(looseCount(m, node.id), 'item')} — move or remove them first.`, true); return; }
  // Space → rack: ask for the rack's insides, defaulting to its neighbours' layout.
  const rackSib = kidsOf(m, node.parent_id).find(x => x.level === 'rack');
  const d = guessLayout(m, node.parent_id, 'rack');
  if (!rackSib) Object.assign(d, TYPES[m.unit.type].layout);
  const err = h('p', { class: 'error' });
  const form = h('form', { class: 'stack' },
    h('div', { class: 'grid2' }, layoutFieldsBelow(m.unit.type, 'rack', d)), gridFields(d), err);
  const go = async () => {
    const v = formData(form);
    const layout = { grid_rows: +v.grid_rows, grid_cols: +v.grid_cols, label_style: v.label_style };
    for (const k of ['rows', 'boxes']) if (v[k] !== undefined) layout[k] = +v[k];
    try { await api('POST', `/api/nodes/${node.id}/convert`, { layout }); close(); toast('Now a rack'); refresh(); }
    catch (x) { err.textContent = x.message; }
  };
  form.onsubmit = e => { e.preventDefault(); go(); };
  const close = openModal({ title: `Replace ${nodeLabel(node)} with a rack`, body: form,
    foot: [h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn primary', onclick: go }, 'Replace with rack')] });
}
function layoutFieldsBelow(type, level, defaults) {
  // inputs for the levels strictly below `level` (the level being added)
  const out = [];
  const labels = { racks: 'Racks per shelf', rows: 'Rows per rack', boxes: type === 'ln2' ? 'Boxes per rack' : 'Boxes per row' };
  let lvl = childLevel(type, level);
  while (lvl) {
    const k = LAYOUT_KEY[lvl];
    out.push(field(labels[k], inp(k, defaults[k] ?? 1, { type: 'number', min: 0, max: 40 })));
    lvl = childLevel(type, lvl);
  }
  return out;
}
function guessLayout(m, parentId, level) {
  // Copy structure from an existing sibling so "add rack" makes racks like the others.
  const sib = kidsOf(m, parentId).find(x => x.level === level);
  const d = { racks: 4, rows: 4, boxes: 4, grid_rows: 9, grid_cols: 9, ...TYPES[m.unit.type].layout };
  let n = sib;
  let lvl = level;
  while (n && childLevel(m.unit.type, lvl)) {
    const kids = kidsOf(m, n.id);
    lvl = childLevel(m.unit.type, lvl);
    d[LAYOUT_KEY[lvl]] = kids.length;
    n = kids[0];
  }
  if (n && n.level === 'box') { d.grid_rows = n.grid_rows; d.grid_cols = n.grid_cols; d.label_style = n.label_style; }
  return d;
}
function addChildrenDialog(m, parent) {
  const level = childLevel(m.unit.type, parent ? parent.level : null);
  const d = guessLayout(m, parent ? parent.id : null, level);
  const err = h('p', { class: 'error' });
  const form = h('form', { class: 'stack' },
    h('div', { class: 'grid2' },
      field(`Number of ${LEVEL[level].toLowerCase()}s to add`, inp('count', 1, { type: 'number', min: 1, max: 40 }), null, 'span-all'),
      layoutFieldsBelow(m.unit.type, level, d)),
    gridFields(d),
    err);
  const go = async () => {
    const v = formData(form);
    const layout = {};
    for (const k of ['racks', 'rows', 'boxes', 'grid_rows', 'grid_cols']) if (v[k] !== undefined) layout[k] = +v[k];
    layout.label_style = v.label_style;
    try {
      await api('POST', `/api/units/${m.unit.id}/nodes`, { parent_id: parent ? parent.id : null, count: +v.count, layout });
      close(); toast('Added'); refresh();
    } catch (x) { err.textContent = x.message; }
  };
  form.onsubmit = e => { e.preventDefault(); go(); };
  const close = openModal({
    title: `Add ${LEVEL[level].toLowerCase()}s to ${parent ? nodeLabel(parent) : m.unit.name}`, body: form,
    foot: [h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn primary', onclick: go }, 'Add')],
  });
}
function gridFields(d) {
  const preset = h('select', { class: 'input' },
    GRID_PRESETS.map(([r, c]) => h('option', { value: `${r}x${c}` }, `${r} × ${c} (${r * c})`)), h('option', { value: 'custom' }, 'Custom…'));
  const rIn = inp('grid_rows', d.grid_rows, { type: 'number', min: 1, max: 26 });
  const cIn = inp('grid_cols', d.grid_cols, { type: 'number', min: 1, max: 26 });
  const style = h('select', { name: 'label_style', class: 'input' }, h('option', { value: 'grid' }, 'A1, A2, … (grid)'), h('option', { value: 'number' }, '1, 2, 3 … (numbered)'));
  style.value = d.label_style || 'grid';
  const sync = () => { const k = `${rIn.value}x${cIn.value}`; preset.value = GRID_PRESETS.some(([r, c]) => `${r}x${c}` === k) ? k : 'custom'; };
  preset.onchange = () => { if (preset.value !== 'custom') { const [r, c] = preset.value.split('x'); rIn.value = r; cIn.value = c; rIn.dispatchEvent(new Event('input', { bubbles: true })); } };
  rIn.addEventListener('input', sync); cIn.addEventListener('input', sync);
  sync();
  return h('div', { class: 'grid2' },
    field('Box format', preset, null, 'span-all'),
    field('Box rows', rIn), field('Box columns', cIn),
    field('Position labels', style, null, 'span-all'));
}
async function deleteNode(m, node) {
  const s = stats(m, node.id);
  const ok = await confirmBox(`Delete ${nodeLabel(node)}?`,
    `This removes ${nodeLabel(node)}${node.level !== 'box' ? ' and everything inside it' : ''} from the layout. ` +
    (s.used ? `It still holds ${plural(s.used, 'item')}, so the delete will be refused until they're moved or removed.` : 'It is empty.'),
    { okText: 'Delete', danger: true });
  if (!ok) return;
  if (await attempt(() => api('DELETE', `/api/nodes/${node.id}`), 'Deleted')) location.hash = nodeHref(m, node.parent_id);
}
function savePreset(m) {
  const u = m.unit;
  const name = inp('name', [u.manufacturer && u.manufacturer.split(' ')[0], u.device_name, u.model].filter(Boolean).join(' ') || u.name, { required: true });
  const err = h('p', { class: 'error' });
  const go = async () => {
    try { await api('POST', '/api/presets', { from_unit: u.id, name: name.value }); close(); toast('Preset saved — pick it when adding a unit'); }
    catch (x) { err.textContent = x.message; }
  };
  const close = openModal({ title: 'Save as preset', body: h('div', { class: 'stack' },
      h('p', { class: 'small muted' }, `Saves ${u.name}'s type, manufacturer, device name, model, size and layout${unitPlan(u) ? ' (including its compartment arrangement)' : ''} so the next unit like it is one click away. Contents aren't copied.`),
      field('Preset name', name), err),
    foot: [h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn primary', onclick: go }, 'Save preset')] });
}
async function deleteUnit(m) {
  const typed = h('input', { class: 'input', placeholder: m.unit.name });
  const err = h('p', { class: 'error' });
  const go = async () => {
    if (typed.value.trim() !== m.unit.name) { err.textContent = 'Type the name exactly to confirm.'; return; }
    try { await api('DELETE', `/api/units/${m.unit.id}`); close(); toast('Storage unit deleted'); location.hash = '#/'; }
    catch (x) { err.textContent = x.message; }
  };
  const close = openModal({
    title: `Delete ${m.unit.name}?`,
    body: h('div', { class: 'stack' },
      h('p', null, `This deletes the unit and its whole layout. ${m.unit.stored ? `The ${plural(m.unit.stored, 'item')} still inside will be logged as removed (reason “Storage unit deleted”) so their history is kept.` : 'It is empty.'}`),
      field('Type the unit name to confirm', typed), err),
    foot: [h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn danger', onclick: go }, 'Delete permanently')],
  });
}

// ------------------------------------------------------------------------ //
// Unit create / edit form (with live layout preview)
// ------------------------------------------------------------------------ //
async function viewUnitForm(uid) {
  const editing = !!uid;
  const u = editing ? (await api('GET', `/api/units/${uid}`)).unit : { type: '-80' };
  const presets = editing ? [] : await api('GET', '/api/presets').catch(() => []);
  let type = u.type;
  let layoutTouched = false;
  let plan = editing ? unitPlan(u) : null;     // rack arrangement from a preset (LN2 only)
  const planActive = L => type === 'ln2' && !!plan && L.racks === plan.slots.length;
  const err = h('p', { class: 'error' });
  const sizeList = h('datalist', { id: 'size-list' });
  const typePick = h('div', { class: 'type-pick' });
  const drawTypes = () => {
    typePick.replaceChildren(...TYPE_ORDER.map(k => h('button', {
      type: 'button', class: `type-opt ${TYPES[k].cls}` + (type === k ? ' on' : ''),
      onclick: () => { type = k; drawTypes(); if (!editing) drawLayout(layoutTouched ? { ...TYPES[type].layout, ...readLayout(true) } : TYPES[type].layout); sizeList.replaceChildren(...TYPES[type].sizes.map(s => h('option', { value: s }))); drawPreview(); },
    }, h('b', null, TYPES[k].short), h('span', null, TYPES[k].label))));
  };
  sizeList.replaceChildren(...TYPES[type].sizes.map(s => h('option', { value: s })));
  const layoutBox = h('div');
  const preview = h('div');
  const presetNote = h('p', { class: 'small muted' });
  const presetSel = presets.length ? h('select', { class: 'input' }, h('option', { value: '' }, '— None: set it up myself —'),
    TYPE_ORDER.filter(t => presets.some(p => p.type === t)).map(t => h('optgroup', { label: TYPES[t].label },
      presets.filter(p => p.type === t).map(p => h('option', { value: p.id }, p.name))))) : null;
  const planToggle = editing && plan ? h('input', { type: 'checkbox', checked: true }) : null;
  const form = h('form', { class: 'stack' },
    presetSel ? h('div', { class: 'card stack' },
      h('h2', null, 'Start from a preset'),
      field('Common set-up', presetSel, 'fills in the details and layout below — all still editable'), presetNote) : null,
    h('div', { class: 'card stack' },
      h('h2', null, 'Storage type'), typePick,
      h('div', { class: 'grid2' },
        field('Name', inp('name', u.name, { required: true, placeholder: 'e.g. Freezer A / Tank 2' }), null, 'span-all'),
        field('Location', inp('location', u.location, { placeholder: 'Room, bench, corridor…', list: 'loc-list' })),
        field('Size', inp('size', u.size, { list: 'size-list', placeholder: 'Pick or type' })),
        field('Manufacturer', inp('manufacturer', u.manufacturer, { placeholder: 'e.g. Thermo Fisher' })),
        field('Device name', inp('device_name', u.device_name, { placeholder: 'e.g. CryoExtra' }), "manufacturer's product name"),
        field('Model', inp('model', u.model, { placeholder: 'e.g. CE8120' })),
        field('Serial number', inp('serial_number', u.serial_number)),
        field('Inventory number', inp('inventory_number', u.inventory_number)),
        field('Date of last defrost', inp('last_defrost', u.last_defrost, { type: 'date', max: todayISO() })),
        field('Date of last PM', inp('last_pm', u.last_pm, { type: 'date', max: todayISO() }), 'preventive maintenance'),
        field('Assigned to', ownerSelect(u.owner_id, u.is_common, 'Unassigned'), 'the whole unit', 'span-all'),
        field('Notes', h('textarea', { name: 'notes', class: 'input' }, u.notes || ''), null, 'span-all')),
      sizeList, h('datalist', { id: 'loc-list' })),
    editing ? h('div', { class: 'stack' },
      planToggle ? h('label', { class: 'row small' }, planToggle, `Use the tank's compartment arrangement (${plan.slots.map(x => x.name).filter(Boolean).slice(0, 1)}–${plan.slots.map(x => x.name).filter(Boolean).slice(-1)} slots). Unticking switches to a plain round layout and can't be undone here.`) : null,
      h('p', { class: 'small muted' }, 'To change shelves, racks, rows or boxes, open the unit and use the layout panel next to the diagram.')) : layoutBox,
    err,
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', type: 'submit' }, editing ? 'Save changes' : 'Create storage unit'),
      h('a', { class: 'btn', href: editing ? `#/unit/${uid}` : '#/' }, 'Cancel')));
  drawTypes();

  if (presetSel) presetSel.onchange = () => {
    const p = presets.find(x => String(x.id) === presetSel.value);
    if (!p) { plan = null; presetNote.textContent = ''; drawPreview(); return; }
    type = p.type; drawTypes();
    sizeList.replaceChildren(...TYPES[type].sizes.map(sz => h('option', { value: sz })));
    for (const k of ['manufacturer', 'device_name', 'model', 'size']) { const el = form.querySelector(`[name=${k}]`); if (el && p[k] != null) el.value = p[k]; }
    drawLayout({ ...TYPES[type].layout, ...p.layout });
    plan = p.plan;
    layoutTouched = true;
    presetNote.textContent = [p.notes, plan ? `Racks are named ${plan.slots[0].name}–${plan.slots[plan.slots.length - 1].name} and placed as in the tank.` : null,
      'Check the rack contents (rows, boxes per row, box format) match yours.'].filter(Boolean).join(' ');
    drawPreview();
  };

  // Fill location suggestions from existing units.
  api('GET', '/api/units').then(list => {
    const locs = [...new Set(list.map(x => x.location).filter(Boolean))];
    $('#loc-list', form)?.replaceChildren(...locs.map(l => h('option', { value: l })));
  }).catch(() => {});

  /** The layout card's fields follow the storage type: a tank is racks × boxes per rack,
   *  anything else shelves × racks × rows × boxes. */
  function drawLayout(d) {
    const tank = type === 'ln2';
    const num = (k, label, min, max) => field(label, inp(k, d[k] ?? 1, { type: 'number', min, max }));
    layoutBox.replaceChildren(h('div', { class: 'card stack' },
      h('h2', null, 'Layout'),
      h('p', { class: 'small muted' }, tank
        ? 'A tank holds racks, and each rack is a vertical stack of boxes. Every rack starts the same — you can add, remove, rename or re-fill racks and boxes afterwards.'
        : 'Every shelf starts the same — you can add, remove, rename or resize individual shelves, racks, rows and boxes afterwards. Set a level to 0 to leave it empty (e.g. a fridge shelf for loose items only).'),
      tank ? h('div', { class: 'grid2' }, num('racks', 'Racks', 1, 30), num('boxes', 'Boxes per rack', 0, 40))
        : h('div', { class: 'grid4' }, num('shelves', 'Shelves', 1, 30), num('racks', 'Racks / shelf', 0, 30),
            num('rows', 'Rows / rack', 0, 40), num('boxes', 'Boxes / row', 0, 40)),
      gridFields(d)));
  }
  if (!editing) {
    drawLayout(TYPES[type].layout);
    layoutBox.addEventListener('input', () => { layoutTouched = true; drawPreview(); });
    layoutBox.addEventListener('change', drawPreview);
  }
  /** Counts for this type's levels only (raw = keep whatever fields exist, for carrying values over). */
  function readLayout(raw = false) {
    const v = formData(layoutBox);
    const out = { grid_rows: +v.grid_rows || 9, grid_cols: +v.grid_cols || 9, label_style: v.label_style };
    for (const lvl of raw ? Object.keys(LAYOUT_KEY) : chainFor(type)) {
      const k = LAYOUT_KEY[lvl];
      if (v[k] !== undefined) out[k] = +v[k] || 0;
    }
    return out;
  }
  function drawPreview() {
    if (editing) return;
    const L = readLayout();
    const chain = chainFor(type);
    const nb = chain.reduce((a, l) => a * (L[LAYOUT_KEY[l]] || 0), 1);
    if (nb > 6000 || chain.some(l => (L[LAYOUT_KEY[l]] || 0) > 40)) {
      preview.replaceChildren(h('div', { class: 'card' }, h('p', { class: 'error' }, 'That layout is too large to preview.')));
      return;
    }
    // Build a fake model and reuse the real diagram renderers.
    const nodes = []; let id = 1;
    const mk = (parent, level, count) => {
      for (let i = 0; i < count; i++) {
        const n = { id: id++, parent_id: parent, level, position: i, name: null, owner_id: null, is_common: 0, grid_rows: L.grid_rows, grid_cols: L.grid_cols, label_style: L.label_style };
        nodes.push(n);
        const c = childLevel(type, level);
        if (c) mk(n.id, c, L[LAYOUT_KEY[c]]);
      }
    };
    mk(null, chain[0], L[LAYOUT_KEY[chain[0]]]);
    const usePlan = planActive(L);
    if (usePlan) for (const n of nodes) if (n.level === 'rack') n.name = plan.slots[n.position].name || null;
    const m = buildModel({ unit: { id: 0, name: formData(form).name || 'New unit', type, manufacturer: formData(form).manufacturer, plan: usePlan ? plan : null }, nodes, items: [] });
    const diagram = type === 'ln2' ? dewarView(m) : cabinetView(m);
    for (const el of diagram.querySelectorAll('[data-href]')) el.removeAttribute('data-href');
    for (const el of diagram.querySelectorAll('a')) el.removeAttribute('href');
    preview.replaceChildren(h('div', { class: `card ${TYPES[type].cls}` },
      h('div', { class: 'card-head' }, h('h3', null, 'Preview'), h('span', { class: 'small muted' },
        `${nb.toLocaleString()} boxes · ${(nb * L.grid_rows * L.grid_cols).toLocaleString()} positions`)),
      plan && type === 'ln2' && !usePlan ? h('p', { class: 'notice', style: { marginBottom: '10px' } },
        `The preset's compartment arrangement needs exactly ${plan.slots.length} racks per tier, so a plain round layout is shown.`) : null,
      diagram));
  }
  form.addEventListener('input', e => { if (e.target.name === 'name') drawPreview(); });

  form.onsubmit = async e => {
    e.preventDefault(); err.textContent = '';
    const v = formData(form);
    const body = { type, name: v.name, location: v.location, size: v.size, manufacturer: v.manufacturer, device_name: v.device_name, model: v.model,
      serial_number: v.serial_number, inventory_number: v.inventory_number, last_defrost: v.last_defrost, last_pm: v.last_pm,
      notes: v.notes, ...ownerPayload(v.owner) };
    try {
      if (editing) {
        if (planToggle && !planToggle.checked) body.plan = null;
        await api('PUT', `/api/units/${uid}`, body); toast('Saved'); location.hash = `#/unit/${uid}`;
      }
      else { body.layout = readLayout(); if (planActive(body.layout)) body.plan = plan; const r = await api('POST', '/api/units', body); toast('Storage unit created'); location.hash = `#/unit/${r.id}`; }
    } catch (x) { err.textContent = x.message; }
  };
  setTimeout(drawPreview, 0);
  return h('div', null,
    h('div', { class: 'crumbs' }, h('a', { href: '#/' }, 'Storage'), '›', editing ? [h('a', { href: `#/unit/${uid}` }, u.name), '›'] : null, h('span', { class: 'cur' }, editing ? 'Edit' : 'New')),
    h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', null, editing ? `Edit ${u.name}` : 'Add cold storage'))),
    editing ? h('div', { style: { maxWidth: '760px' } }, form) : h('div', { class: 'form-layout' }, form, h('div', { class: 'preview' }, preview)));
}

// ------------------------------------------------------------------------ //
// Items: add / edit / detail / remove
// ------------------------------------------------------------------------ //
/** opts: {m, box, positions, allowFill} for a box; {m, shelf, loose:true} for loose; {item} to edit. */
function openItemForm(opts) {
  const { m, box, item } = opts;
  const editing = !!item;
  const src = item || {};
  let inheritLabel = 'Inherit from location';
  if (m) {
    const o = box ? nodeOwner(m, box.id) : nodeOwner(m, opts.shelf ? opts.shelf.id : null);
    if (o.owner_id || o.common) inheritLabel = `Inherit — ${ownerName(o.owner_id, o.common)} (from ${o.from})`;
    else inheritLabel = 'Unassigned (inherit)';
  } else if (item) {
    inheritLabel = item.owner_from && item.owner_from !== 'item' ? `Inherit — ${ownerName(item.eff_owner_id, item.eff_common)} (from ${item.owner_from})` : 'Unassigned (inherit)';
  }
  const loose = opts.loose || (editing && item.pos_row == null);
  const err = h('p', { class: 'error' });
  let whereSel = null;
  if (opts.loose && m) {
    whereSel = h('select', { class: 'input', name: '_where' }, h('option', { value: '' }, `${m.unit.name} (general)`),
      kidsOf(m, null).map(s => s.level === 'space' ? h('option', { value: s.id }, nodeLabel(s))
        : s.level === 'shelf' ? [h('option', { value: s.id }, nodeLabel(s)),
          kidsOf(m, s.id).filter(x => x.level === 'space').map(sp => h('option', { value: sp.id }, `${nodeLabel(s)} › ${nodeLabel(sp)}`))] : null));
    whereSel.value = opts.shelf ? String(opts.shelf.id) : '';
  }
  const fillIn = opts.allowFill ? inp('_fill', 1, { type: 'number', min: 1, max: box.grid_rows * box.grid_cols }) : null;
  const fillNote = h('span', { class: 'hint' });
  const posText = () => {
    if (!box) return null;
    const ps = fillPositions();
    return ps.map(([r, c]) => posLabel(box, r, c)).join(', ');
  };
  function fillPositions() {
    if (!opts.allowFill) return opts.positions;
    const want = Math.max(1, +(fillIn.value || 1));
    const taken = new Set((m.byBox.get(box.id) || []).map(i => i.pos_row + ',' + i.pos_col));
    const [r0, c0] = opts.positions[0];
    const out = [];
    for (let k = r0 * box.grid_cols + c0; k < box.grid_rows * box.grid_cols && out.length < want; k++) {
      const r = Math.floor(k / box.grid_cols), c = k % box.grid_cols;
      if (!taken.has(r + ',' + c)) out.push([r, c]);
    }
    return out;
  }
  const posLine = h('p', { class: 'small muted' });
  const updatePos = () => {
    if (!box) return;
    const ps = fillPositions();
    posLine.textContent = `${nodeLabel(box)} · ${ps.length > 1 ? 'positions' : 'position'} ${posText()}`;
    if (fillIn) fillNote.textContent = ps.length < +(fillIn.value || 1) ? ` only ${ps.length} empty position(s) left after this one` : ' fills the next empty positions, left→right, top→bottom';
  };
  fillIn && fillIn.addEventListener('input', updatePos);
  updatePos();

  const form = h('form', { class: 'stack' },
    box && !editing ? posLine : null,
    h('div', { class: 'grid2' },
      field('Name', inp('name', src.name, { required: true, placeholder: 'e.g. HEK293T p12, pUC19-GFP, anti-CD3 (OKT3)' }), null, 'span-all'),
      field('Category', categorySelect(src.category_id)),
      field('Owner', ownerSelect(src.owner_id, src.is_common, inheritLabel)),
      fillIn ? field('Number of tubes / aliquots', fillIn, null, 'span-all') : null,
      fillIn ? h('div', { class: 'span-all small faint', style: { marginTop: '-6px' } }, fillNote) : null,
      field('Quantity', inp('quantity', src.quantity, { placeholder: 'e.g. 1 vial, 500 µL, 2×10⁶ cells' })),
      field('Concentration', inp('concentration', src.concentration, { placeholder: 'e.g. 1 mg/mL' })),
      field('Source', inp('source', src.source, { placeholder: 'Vendor, collaborator, prep…' })),
      field('Lot / batch / passage', inp('lot', src.lot)),
      field('Date added', inp('date_added', src.date_added || todayISO(), { type: 'date' })),
      field('Expiry date', inp('expiry_date', src.expiry_date, { type: 'date' })),
      loose && whereSel ? field('Where', whereSel) : null,
      loose ? field('Spot', inp('loose_where', src.loose_where, { placeholder: 'e.g. door, bottom bin, bag 3' }), 'optional', whereSel ? null : 'span-all') : null,
      editing && item.status === 'removed' ? field('Date removed', inp('date_removed', item.date_removed, { type: 'date' })) : null,
      editing && item.status === 'removed' ? field('Removal reason', inp('removal_reason', item.removal_reason, { list: 'reason-list' })) : null,
      field('Notes', h('textarea', { name: 'notes', class: 'input' }, src.notes || ''), null, 'span-all')),
    h('datalist', { id: 'reason-list' }, REMOVE_REASONS.map(r => h('option', { value: r }))),
    err);
  const save = async () => {
    if (!form.reportValidity()) return;
    const v = formData(form);
    const body = { name: v.name, category_id: v.category_id ? +v.category_id : null, ...ownerPayload(v.owner),
      quantity: v.quantity, concentration: v.concentration, source: v.source, lot: v.lot,
      date_added: v.date_added, expiry_date: v.expiry_date, notes: v.notes };
    if (loose) body.loose_where = v.loose_where;
    try {
      if (editing) {
        if (item.status === 'removed') { body.date_removed = v.date_removed; body.removal_reason = v.removal_reason; }
        await api('PUT', `/api/items/${item.id}`, body);
        toast('Saved');
      } else {
        body.unit_id = m.unit.id;
        if (box) { body.node_id = box.id; body.positions = fillPositions(); }
        else body.node_id = whereSel && whereSel.value ? +whereSel.value : null;
        if (box && !body.positions.length) throw new Error('No empty positions left in this box.');
        const r = await api('POST', '/api/items', body);
        toast(r.ids.length > 1 ? `Added ${r.ids.length} items` : 'Item added');
        state.boxSel.cells.clear();
      }
      close();
      refresh();
      if (editing && opts.reopen) openItem(item.id);
    } catch (x) { err.textContent = x.message; }
  };
  form.onsubmit = e => { e.preventDefault(); save(); };
  const close = openModal({
    title: editing ? `Edit ${item.name}` : (box ? 'Add item' : 'Add loose item'), body: form, wide: true,
    foot: [h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn primary', onclick: save }, editing ? 'Save' : 'Add')],
  });
}

async function openItem(id) {
  let it;
  try { it = await api('GET', `/api/items/${id}`); } catch (e) { toast(e.message, true); return; }
  const loc = it.location;
  const boxPart = [...loc.parts].reverse().find(p => p.kind === 'box');
  const anyPart = loc.parts[loc.parts.length - 1];
  const showHref = it.status === 'stored' && anyPart ? (boxPart ? `#/unit/${loc.parts[0].id}/n/${boxPart.id}?hl=${it.id}` : (anyPart.kind === 'unit' ? `#/unit/${anyPart.id}` : `#/unit/${loc.parts[0].id}/n/${anyPart.id}`)) : null;
  const removed = it.status === 'removed';
  const ownerEl = ownerChip(it.eff_owner_id, it.eff_common, { inherit: it.owner_from !== 'item', none: h('span', { class: 'faint' }, 'Unassigned') });
  const kv = [
    ['Category', catLabel(it.category_id)],
    ['Owner', h('span', null, ownerEl, it.owner_from && it.owner_from !== 'item' ? h('span', { class: 'small faint' }, ` via ${it.owner_from}`) : null)],
    ['Location', h('span', null, removed ? h('span', { class: 'muted' }, 'was in ') : null, removed ? it.last_location : loc.text)],
    ['Quantity', it.quantity], ['Concentration', it.concentration], ['Source', it.source], ['Lot / passage', it.lot],
    ['Expiry', it.expiry_date ? h('span', null, fmtDate(it.expiry_date), ' ', it.expiry_date < todayISO() ? h('span', { class: 'pill bad' }, 'expired') : null) : null],
    ['Added', `${fmtDate(it.date_added)}${it.added_by_name ? ' by ' + it.added_by_name : ''}`],
    removed ? ['Removed', `${fmtDate(it.date_removed)}${it.removed_by_name ? ' by ' + it.removed_by_name : ''}${it.removal_reason ? ' — ' + it.removal_reason : ''}`] : null,
  ].filter(x => x && x[1]);
  const close = openModal({
    wide: true,
    title: h('div', { style: { flex: 1, minWidth: 0 } }, h('h2', null, it.name), h('div', { class: 'row small muted', style: { marginTop: '2px' } },
      removed ? h('span', { class: 'pill bad' }, 'Removed') : h('span', { class: 'pill' }, 'In storage'), `#${it.id}`)),
    body: h('div', { class: 'stack' },
      h('dl', { class: 'kv' }, kv.map(([k, v]) => [h('dt', null, k), h('dd', null, v)])),
      it.notes ? h('div', null, h('h3', null, 'Notes'), h('p', { style: { whiteSpace: 'pre-wrap', marginTop: '4px' } }, it.notes)) : null,
      h('div', null, h('h3', null, 'History'), h('ul', { class: 'feed' }, it.history.map(a => h('li', null,
        h('span', { class: 'act ' + a.action.split('.')[1] }), h('span', { class: 'when', title: a.at + ' UTC' }, fmtWhen(a.at)),
        h('span', null, h('span', { class: 'who' }, a.user_name || 'Someone'), ' ', lowerFirst(a.summary))))))),
    foot: [
      h('div', { class: 'left' },
        showHref ? h('a', { class: 'btn', href: showHref, onclick: () => close() }, 'Show in storage') : null,
        !removed ? h('button', { class: 'btn', onclick: () => { close(); state.moving = { id: it.id, name: it.name }; renderBanner(); toast('Pick the new spot'); refresh(); } }, 'Move') : null),
      h('button', { class: 'btn', onclick: () => { close(); openItemForm({ item: it, reopen: true }); } }, 'Edit'),
      removed
        ? h('button', { class: 'btn primary', onclick: async () => { if (await attempt(() => api('POST', `/api/items/${it.id}/restore`), 'Put back')) { close(); refresh(); } } }, 'Put back')
        : h('button', { class: 'btn danger', onclick: () => { close(); removeDialog([it], refresh); } }, 'Remove…'),
    ],
  });
}
function removeDialog(items, after) {
  const err = h('p', { class: 'error' });
  const form = h('form', { class: 'stack' },
    h('p', null, items.length === 1 ? `Take “${items[0].name}” out of storage. It stays in the log and search (as removed).` : `Take ${items.length} items out of storage. They stay in the log and search (as removed).`),
    h('div', { class: 'grid2' },
      field('Date removed', inp('date', todayISO(), { type: 'date' })),
      field('Reason', inp('reason', '', { list: 'reason-list2', placeholder: 'optional' }))),
    h('datalist', { id: 'reason-list2' }, REMOVE_REASONS.map(r => h('option', { value: r }))), err);
  const go = async () => {
    const v = formData(form);
    try { const r = await api('POST', '/api/items/remove', { ids: items.map(i => i.id), date: v.date, reason: v.reason }); close(); toast(`Removed ${plural(r.removed, 'item')}`); after && after(); }
    catch (x) { err.textContent = x.message; }
  };
  form.onsubmit = e => { e.preventDefault(); go(); };
  const close = openModal({ title: items.length === 1 ? 'Remove item' : `Remove ${items.length} items`, body: form,
    foot: [h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn danger', onclick: go }, 'Remove')] });
}

// ------------------------------------------------------------------------ //
// Search
// ------------------------------------------------------------------------ //
async function viewSearch(query) {
  const units = await api('GET', '/api/units');
  const q = inp('q', query.q || '', { type: 'search', placeholder: 'Name, notes, source, lot, location… (use "quotes" for phrases)' });
  const cat = categorySelect(query.category_id);
  cat.options[0].textContent = 'All categories';
  const owner = h('select', { name: 'owner', class: 'input' }, h('option', { value: '' }, 'Anyone'), h('option', { value: 'me' }, 'Mine'),
    h('option', { value: 'common' }, '★ Common'), h('option', { value: 'none' }, 'Unassigned'),
    h('optgroup', { label: 'Person' }, state.users.map(u => h('option', { value: u.id }, u.name))));
  owner.value = query.owner || '';
  const unit = h('select', { name: 'unit_id', class: 'input' }, h('option', { value: '' }, 'All storage'),
    TYPE_ORDER.map(t => h('option', { value: 't:' + t }, `All ${TYPES[t].label}s`)),
    h('optgroup', { label: 'Unit' }, units.map(u => h('option', { value: u.id }, `${u.name}${u.location ? ' — ' + u.location : ''}`))));
  unit.value = query.unit_id || (query.unit_type ? 't:' + query.unit_type : '');
  const status = h('select', { name: 'status', class: 'input' }, h('option', { value: 'stored' }, 'In storage'), h('option', { value: 'removed' }, 'Removed'), h('option', { value: 'all' }, 'Everything'));
  status.value = query.status || 'stored';
  const extra = { added_from: query.added_from, removed_from: query.removed_from, expiring: query.expiring };
  const results = h('div');
  const params = () => {
    const p = new URLSearchParams();
    if (q.value.trim()) p.set('q', q.value.trim());
    if (cat.value) p.set('category_id', cat.value);
    if (owner.value) p.set('owner', owner.value);
    if (unit.value.startsWith('t:')) p.set('unit_type', unit.value.slice(2)); else if (unit.value) p.set('unit_id', unit.value);
    if (status.value !== 'stored') p.set('status', status.value);
    for (const [k, v] of Object.entries(extra)) if (v) p.set(k, v);
    return p;
  };
  let seq = 0;
  const run = async () => {
    const p = params();
    history.replaceState(null, '', '#/search' + (p.toString() ? '?' + p : ''));
    const mine = ++seq;
    let data;
    try { data = await api('GET', '/api/items?' + p + '&limit=1000'); } catch (e) { results.replaceChildren(h('p', { class: 'error' }, e.message)); return; }
    if (mine !== seq) return;
    let items = data.items;
    if (extra.expiring) items = items.filter(i => i.expiry_date && i.expiry_date <= daysAhead(30));
    results.replaceChildren(h('div', { class: 'card' },
      h('div', { class: 'card-head' },
        h('h2', null, `${data.total > 1000 && !extra.expiring ? 'First 1000 of ' + data.total.toLocaleString() : items.length.toLocaleString()} ${items.length === 1 ? 'item' : 'items'}`),
        Object.values(extra).some(Boolean) ? h('button', { class: 'btn sm ghost', onclick: () => { for (const k in extra) extra[k] = null; run(); } },
          `Clear date filter (${extra.expiring ? 'expiring' : extra.added_from ? 'added since ' + fmtDate(extra.added_from) : 'removed since ' + fmtDate(extra.removed_from)}) ✕`) : null,
        h('a', { class: 'btn sm', href: '/api/items.csv?' + p, download: '' }, 'Export CSV')),
      items.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'list' },
        h('thead', null, h('tr', null, ['Item', 'Category', 'Owner', 'Location', 'Qty', 'Added', status.value === 'stored' ? 'Expiry' : 'Removed'].map(x => h('th', null, x)))),
        h('tbody', null, items.map(it => h('tr', { class: 'clickable' + (it.status === 'removed' ? ' removed' : ''), onclick: () => openItem(it.id) },
          h('td', { class: 'nm' }, it.name), h('td', null, catLabel(it.category_id)),
          h('td', null, ownerChip(it.eff_owner_id, it.eff_common, { short: true }) || h('span', { class: 'faint' }, '—')),
          h('td', { class: 'small' }, it.status === 'removed' ? h('span', null, h('span', { class: 'pill bad' }, 'removed'), ' ', it.last_location || '') : it.location.text),
          h('td', { class: 'small' }, it.quantity || ''),
          h('td', { class: 'small muted', style: { whiteSpace: 'nowrap' } }, fmtDate(it.date_added)),
          h('td', { class: 'small muted', style: { whiteSpace: 'nowrap' } }, it.status === 'removed' ? fmtDate(it.date_removed) : (it.expiry_date ? h('span', { class: it.expiry_date < todayISO() ? 'pill bad' : it.expiry_date <= daysAhead(30) ? 'pill warn' : '' }, fmtDate(it.expiry_date)) : '')))))))
        : h('p', { class: 'empty-note' }, 'No items match.')));
  };
  let timer;
  q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 220); });
  for (const s of [cat, owner, unit, status]) s.addEventListener('change', run);
  run();
  setTimeout(() => q.focus(), 0);
  return h('div', null,
    h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', null, 'Search items'), h('p', null, 'Searches every freezer, fridge and tank. Click a result to see it, move it, or take it out.'))),
    h('div', { class: 'filters' }, q, cat, owner, unit, status),
    results);
}
function daysAhead(n) { const d = new Date(); d.setDate(d.getDate() + n); return d.toLocaleDateString('en-CA'); }

// ------------------------------------------------------------------------ //
// Activity log
// ------------------------------------------------------------------------ //
async function viewActivity(query) {
  const units = await api('GET', '/api/units');
  const unit = h('select', { class: 'input' }, h('option', { value: '' }, 'All storage'), units.map(u => h('option', { value: u.id }, u.name)));
  unit.value = query.unit_id || '';
  const user = h('select', { class: 'input' }, h('option', { value: '' }, 'Everyone'), state.users.map(u => h('option', { value: u.id }, u.name)));
  user.value = query.user_id || '';
  const action = h('select', { class: 'input' }, h('option', { value: '' }, 'All actions'),
    [['item.add', 'Added'], ['item.remove', 'Removed'], ['item.move', 'Moved'], ['item.edit', 'Edited'], ['item.restore', 'Put back'], ['unit', 'Storage units'], ['node', 'Layout changes']]
      .map(([v, l]) => h('option', { value: v }, l)));
  action.value = query.action || '';
  const from = inp('from', query.from, { type: 'date' });
  const to = inp('to', query.to, { type: 'date' });
  const out = h('div');
  const run = async () => {
    const p = new URLSearchParams({ limit: 500 });
    if (unit.value) p.set('unit_id', unit.value);
    if (user.value) p.set('user_id', user.value);
    if (action.value) p.set('action', action.value);
    if (from.value) p.set('from', from.value);
    if (to.value) p.set('to', to.value);
    const rowsA = await api('GET', '/api/activity?' + p);
    out.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'table-wrap' }, rowsA.length ? h('table', { class: 'list' },
      h('thead', null, h('tr', null, ['When', 'Who', 'What'].map(x => h('th', null, x)))),
      h('tbody', null, rowsA.map(a => h('tr', { class: a.item_id ? 'clickable' : '', onclick: a.item_id ? () => openItem(a.item_id) : null },
        h('td', { class: 'small muted', style: { whiteSpace: 'nowrap' }, title: a.at + ' UTC' }, new Date(a.at.replace(' ', 'T') + 'Z').toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })),
        h('td', { style: { whiteSpace: 'nowrap', fontWeight: 600 } }, a.user_name || '—'),
        h('td', null, h('span', { class: 'row', style: { flexWrap: 'nowrap', alignItems: 'flex-start' } }, h('span', { class: 'feed' }, h('span', { class: 'act ' + a.action.split('.')[1], style: { display: 'inline-block', marginTop: '6px' } })), a.summary))))))
      : h('p', { class: 'empty-note' }, 'No activity matches.'))));
  };
  for (const s of [unit, user, action, from, to]) s.addEventListener('change', run);
  run();
  return h('div', null,
    h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', null, 'Activity log'), h('p', null, 'Every addition, removal, move and layout change, with who did it and when.'))),
    h('div', { class: 'filters' }, unit, user, action, from, to),
    out);
}

// ------------------------------------------------------------------------ //
// Settings
// ------------------------------------------------------------------------ //
async function viewSettings() {
  await loadRefs();
  const presets = await api('GET', '/api/presets').catch(() => []);
  const presetCard = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, 'Storage presets')),
    h('p', { class: 'small muted', style: { marginBottom: '8px' } }, 'Ready-made set-ups for common models, offered when you add a unit. To add one, set a unit up the way you want, then use “Save as preset” on its page.'),
    presets.length ? presets.map(p => {
      const name = h('input', { class: 'input', value: p.name });
      name.addEventListener('change', () => attempt(() => api('PUT', `/api/presets/${p.id}`, { name: name.value }), 'Preset renamed'));
      const L = p.layout;
      const summary = [[p.manufacturer, p.device_name, p.model].filter(Boolean).join(' · '),
        chainFor(p.type).map(l => `${L[LAYOUT_KEY[l]] ?? 0} ${L[LAYOUT_KEY[l]] === 1 ? l : levelPlural(l)}`).join(' × ') + ` (${L.grid_rows}×${L.grid_cols} boxes)`,
        p.plan ? `compartment arrangement ${p.plan.slots[0].name}–${p.plan.slots[p.plan.slots.length - 1].name}` : null].filter(Boolean).join(' — ');
      return h('div', { class: 'preset-row' },
        h('span', { class: `type-badge ${TYPES[p.type].cls}` }, TYPES[p.type].short),
        h('div', { style: { flex: 1, minWidth: 0 } }, name, h('div', { class: 'small faint', style: { marginTop: '3px' } }, summary)),
        h('button', { class: 'btn sm ghost danger', title: 'Delete preset', onclick: async () => {
          if (!await confirmBox(`Delete preset “${p.name}”?`, 'Units already created from it are not affected.', { okText: 'Delete', danger: true })) return;
          if (await attempt(() => api('DELETE', `/api/presets/${p.id}`), 'Preset deleted')) refresh();
        } }, '✕'));
    }) : h('p', { class: 'small faint' }, 'No presets yet.'));
  const isAdmin = state.me.role === 'admin';

  // Categories
  const catList = h('div');
  const drawCats = () => catList.replaceChildren(...state.cats.map(c => {
    const name = h('input', { class: 'input', value: c.name });
    const color = h('input', { type: 'color', value: c.color });
    const save = async () => {
      if (name.value.trim() === c.name && color.value === c.color) return;
      if (await attempt(() => api('PUT', `/api/categories/${c.id}`, { name: name.value, color: color.value }), 'Category saved')) { await loadRefs(); drawCats(); }
    };
    name.addEventListener('change', save); color.addEventListener('change', save);
    return h('div', { class: 'cat-row' }, color, name, h('span', { class: 'small faint', style: { minWidth: '64px', textAlign: 'right' } }, `${c.stored} stored`),
      h('button', { class: 'btn sm ghost danger', title: 'Delete category', onclick: async () => {
        if (!await confirmBox(`Delete “${c.name}”?`, 'Only categories with no items (stored or removed) can be deleted.', { okText: 'Delete', danger: true })) return;
        if (await attempt(() => api('DELETE', `/api/categories/${c.id}`), 'Deleted')) { await loadRefs(); drawCats(); }
      } }, '✕'));
  }));
  drawCats();
  const newCatName = h('input', { class: 'input', placeholder: 'New category, e.g. sgRNA, Primers, Bacterial strains' });
  const newCatColor = h('input', { type: 'color', value: USER_COLORS[state.cats.length % USER_COLORS.length] });
  const addCat = async e => {
    e.preventDefault();
    if (!newCatName.value.trim()) return;
    if (await attempt(() => api('POST', '/api/categories', { name: newCatName.value, color: newCatColor.value }), 'Category added')) {
      newCatName.value = ''; await loadRefs(); drawCats();
    }
  };

  // Users
  const userTable = h('table', { class: 'list' },
    h('thead', null, h('tr', null, ['Name', 'Email', 'Role', ''].map(x => h('th', null, x)))),
    h('tbody', null, state.users.map(u => h('tr', { class: u.active ? '' : 'removed' },
      h('td', null, ownerChip(u.id, false)), h('td', { class: 'small' }, u.email),
      h('td', null, u.role === 'admin' ? h('span', { class: 'pill' }, 'admin') : 'member', u.active ? null : [' ', h('span', { class: 'pill bad' }, 'inactive')]),
      h('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } }, isAdmin && u.id !== state.me.id ? [
        h('button', { class: 'btn sm ghost', onclick: () => resetPw(u) }, 'Reset password'),
        h('button', { class: 'btn sm ghost', onclick: async () => { if (await attempt(() => api('PUT', `/api/users/${u.id}`, { role: u.role === 'admin' ? 'member' : 'admin' }), 'Updated')) refresh(); } }, u.role === 'admin' ? 'Make member' : 'Make admin'),
        h('button', { class: 'btn sm ghost' + (u.active ? ' danger' : ''), onclick: async () => { if (await attempt(() => api('PUT', `/api/users/${u.id}`, { active: !u.active }), 'Updated')) refresh(); } }, u.active ? 'Deactivate' : 'Activate'),
      ] : null)))));
  const addUserForm = h('form', { class: 'grid4', style: { marginTop: '14px', alignItems: 'end' } },
    field('Name', inp('name', '', { required: true })), field('Email', inp('email', '', { type: 'email', required: true })),
    field('Temporary password', inp('password', '', { required: true, minlength: 6 })),
    h('button', { class: 'btn primary', type: 'submit' }, 'Add person'));
  addUserForm.onsubmit = async e => {
    e.preventDefault();
    if (await attempt(() => api('POST', '/api/users', formData(addUserForm)), 'Person added — share their temporary password with them')) refresh();
  };
  function resetPw(u) {
    const pw = inp('password', '', { minlength: 6, required: true });
    const go = async () => { if (await attempt(() => api('PUT', `/api/users/${u.id}`, { password: pw.value }), 'Password reset')) close(); };
    const close = openModal({ title: `Reset password for ${u.name}`, body: h('div', { class: 'stack' }, field('New temporary password', pw, 'at least 6 characters'), h('p', { class: 'small muted' }, 'They will be signed out everywhere.')),
      foot: [h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), h('button', { class: 'btn primary', onclick: go }, 'Reset')] });
  }

  // Me
  const meForm = h('form', { class: 'stack' },
    field('Your name', inp('name', state.me.name, { required: true })),
    h('div', { class: 'grid2' },
      field('Current password', inp('current_password', '', { type: 'password', autocomplete: 'current-password' })),
      field('New password', inp('password', '', { type: 'password', autocomplete: 'new-password', minlength: 6 }), 'leave blank to keep')),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'submit' }, 'Save'), h('span', { class: 'spacer' }),
      h('button', { class: 'btn', type: 'button', onclick: signOut }, 'Sign out')));
  meForm.onsubmit = async e => {
    e.preventDefault();
    const v = formData(meForm);
    const body = { name: v.name };
    if (v.password) { body.password = v.password; body.current_password = v.current_password; }
    if (await attempt(() => api('PUT', `/api/users/${state.me.id}`, body), 'Saved')) { state.me.name = v.name; refresh(); }
  };

  const lab = await api('GET', '/api/lab');
  const codeBox = h('div', { class: 'join-code' }, h('span', { class: 'code' }, lab.join_code || '—'),
    h('button', { class: 'btn sm', type: 'button', onclick: () => {
      navigator.clipboard?.writeText(lab.join_code).then(() => toast('Lab code copied'), () => toast(lab.join_code));
    } }, 'Copy'));
  const labForm = h('form', { class: 'stack' },
    h('div', { class: 'grid2' },
      field('Lab name', h('input', { class: 'input', name: 'lab_name', value: state.lab })),
      field('Lab code', h('input', { class: 'input code-input', name: 'join_code', value: lab.join_code || '', autocomplete: 'off' }), '4–12 letters or digits')),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'submit' }, 'Save'),
      h('button', { class: 'btn', type: 'button', onclick: async () => {
        if (!await confirmBox('Make a new lab code?', 'The current code stops working for new sign-ups. People who already joined are not affected.', { okText: 'New code' })) return;
        if (await attempt(() => api('PUT', '/api/lab', { regenerate_code: true }), 'New lab code made')) refresh();
      } }, 'Generate a new code')));
  labForm.onsubmit = async e => {
    e.preventDefault();
    const v = formData(labForm);
    if (await attempt(() => api('PUT', '/api/lab', { lab_name: v.lab_name, join_code: v.join_code }), 'Saved')) {
      state.lab = v.lab_name; $('#lab-name').textContent = v.lab_name; refresh();
    }
  };
  const labCard = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, 'Lab')),
    h('p', { class: 'small muted', style: { marginBottom: '8px' } }, `To bring someone into ${state.lab}, give them this code. They choose “Join a lab” on the sign-in page and make their own account.`),
    codeBox, isAdmin ? h('div', { style: { marginTop: '16px' } }, labForm) : null);

  return h('div', { style: { maxWidth: '860px' } },
    h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', null, 'Settings'))),
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, 'Item categories')),
      h('p', { class: 'small muted', style: { marginBottom: '8px' } }, 'Colours are used for tubes in the box diagrams. Changes save automatically.'),
      catList,
      h('form', { class: 'cat-row', style: { marginTop: '8px' }, onsubmit: addCat }, newCatColor, newCatName, h('button', { class: 'btn', type: 'submit' }, 'Add'))),
    presetCard,
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, 'People')),
      h('p', { class: 'small muted', style: { marginBottom: '8px' } }, 'Anyone here can be assigned to a whole unit, a shelf, rack, row, box or a single item. Mark things “Common” for shared lab stock.'),
      h('div', { class: 'table-wrap' }, userTable),
      isAdmin ? [h('p', { class: 'small muted', style: { marginTop: '14px' } }, 'People can join themselves with the lab code (below), or you can add them here with a temporary password:'), addUserForm]
        : h('p', { class: 'small faint', style: { marginTop: '10px' } }, 'New people can join with the lab code below.')),
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, 'My account')), meForm),
    labCard);
}

// ------------------------------------------------------------------------ //
// Global wiring
// ------------------------------------------------------------------------ //
$('#quick-search').addEventListener('submit', e => {
  e.preventDefault();
  const v = $('#quick-search input').value.trim();
  location.hash = '#/search' + (v ? '?q=' + encodeURIComponent(v) : '');
  $('#quick-search input').value = '';
});
boot();

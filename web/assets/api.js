// Fetch helpers for the Waymark API (see docs/API.md).
// Every request has a timeout. GETs retry with backoff. When the server
// cannot be reached a banner appears at the top of the page and goes away
// on the next successful request.

const BASE = '/api';

export class ApiError extends Error {
  constructor(message, { status = 0, body = null, network = false } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    this.network = network;
  }
}

// ---------- connection state + banner ----------
let failures = 0;
let online = true;
const listeners = new Set();

export function onConnectionChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function isOnline() {
  return online;
}

function setOnline(next) {
  if (next === online) return;
  online = next;
  renderBanner();
  listeners.forEach((fn) => {
    try { fn(online); } catch (e) { console.error(e); }
  });
}

let banner = null;
function renderBanner() {
  if (!document.body) return;
  if (!banner) {
    banner = document.createElement('div');
    banner.className = 'offline-banner';
    banner.setAttribute('role', 'alert');
    banner.hidden = true;
    banner.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 2l20 20"/><path d="M8.5 16.5a5 5 0 0 1 7 0"/><path d="M5 12.9a10 10 0 0 1 5.2-2.8"/><path d="M19 12.9a10 10 0 0 0-2.3-1.7"/><path d="M12 20h.01"/></svg>' +
      '<span></span>';
    document.body.prepend(banner);
  }
  const offlineNow = typeof navigator !== 'undefined' && navigator.onLine === false;
  banner.querySelector('span').textContent = offlineNow
    ? 'This phone is offline. Waymark will reconnect when the network is back.'
    : 'Cannot reach the Waymark server. Check your connection.';
  banner.hidden = online;
}

window.addEventListener('offline', () => setOnline(false));
window.addEventListener('online', () => { renderBanner(); });

function noteSuccess() {
  failures = 0;
  setOnline(true);
}

function noteFailure() {
  failures += 1;
  // One dropped request on a phone is normal; two in a row means trouble.
  if (failures >= 2) setOnline(false);
}

// ---------- core request ----------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Accepts "/buildings", "buildings" or "/api/buildings".
function resolve(path) {
  if (path.startsWith('http') || path.startsWith('/api/')) return path;
  return BASE + (path.startsWith('/') ? path : '/' + path);
}

async function request(method, path, { json, form, timeout = 10000, retries, raw = false } = {}) {
  const url = resolve(path);
  const attempts = 1 + (retries ?? (method === 'GET' ? 2 : 0));
  let lastErr;

  for (let i = 0; i < attempts; i++) {
    if (i > 0) await sleep(400 * 2 ** (i - 1));
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const init = { method, signal: ctrl.signal, headers: {} };
      if (json !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(json);
      } else if (form) {
        init.body = form;
      }
      const res = await fetch(url, init);
      clearTimeout(timer);

      if (!res.ok) {
        let body = null;
        try { body = await res.json(); } catch { /* not JSON */ }
        const msg = (body && (body.detail || body.error || body.message)) || `Server replied ${res.status}`;
        const err = new ApiError(typeof msg === 'string' ? msg : JSON.stringify(msg), { status: res.status, body });
        // 5xx counts as the server being unwell; 4xx is a real answer.
        if (res.status >= 500) {
          noteFailure();
          lastErr = err;
          continue;
        }
        noteSuccess();
        throw err;
      }

      noteSuccess();
      if (raw) return res;
      const type = res.headers.get('content-type') || '';
      if (type.includes('application/json')) return await res.json();
      const text = await res.text();
      try { return JSON.parse(text); } catch { return text; }
    } catch (e) {
      clearTimeout(timer);
      if (e instanceof ApiError && e.status && e.status < 500) throw e;
      if (!(e instanceof ApiError)) {
        noteFailure();
        lastErr = new ApiError(
          e.name === 'AbortError' ? 'The server took too long to answer.' : 'Cannot reach the server.',
          { network: true }
        );
      } else {
        lastErr = e;
      }
    }
  }
  throw lastErr;
}

export const api = {
  get: (path, opts) => request('GET', path, opts),
  post: (path, json = {}, opts) => request('POST', path, { json, ...opts }),

  // Sends a JPEG under the field "image" plus extra form fields.
  postImage(path, blob, fields = {}, opts = {}) {
    const form = new FormData();
    form.append('image', blob, 'frame.jpg');
    for (const [k, v] of Object.entries(fields)) {
      if (v !== undefined && v !== null && !Number.isNaN(v)) form.append(k, String(v));
    }
    return request('POST', path, { form, timeout: 8000, retries: 0, ...opts });
  },

  // Fetches an image (for example the floor plan) and returns an object URL.
  async blobUrl(path, opts) {
    const res = await request('GET', path, { raw: true, ...opts });
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  },

  url: resolve,
};

// ---------- small shared helpers ----------
export const qs = (sel, root = document) => root.querySelector(sel);
export const qsa = (sel, root = document) => [...root.querySelectorAll(sel)];

export function params() {
  return new URLSearchParams(location.search);
}

// Keeps ?mock=1 when moving between pages during local testing.
export function pageUrl(path, extra = {}) {
  const p = new URLSearchParams();
  if (params().get('mock') === '1') p.set('mock', '1');
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== null) p.set(k, v);
  const s = p.toString();
  return s ? `${path}?${s}` : path;
}

export function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem('waymark:' + key) || 'null');
    if (value === null) localStorage.removeItem('waymark:' + key);
    else localStorage.setItem('waymark:' + key, JSON.stringify(value));
  } catch { /* private mode or blocked storage */ }
  return null;
}

let toastEl = null;
let toastTimer = 0;
export function toast(text, ms = 3200) {
  if (!toastEl) {
    toastEl = document.createElement('div');
    toastEl.className = 'toast';
    toastEl.setAttribute('role', 'status');
    toastEl.setAttribute('aria-live', 'polite');
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = text;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), ms);
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Building and visit objects may carry their id as id, visit_id, bid etc.
export const idOf = (o) => o && (o.id ?? o.visit_id ?? o.vid ?? o.building_id ?? o.bid ?? o.poi_id);

// Holds the screen awake while mapping or guiding. Silently does nothing if unsupported.
export async function keepAwake() {
  let lock = null;
  const acquire = async () => {
    try { lock = await navigator.wakeLock?.request('screen'); } catch { lock = null; }
  };
  await acquire();
  const onVis = () => { if (document.visibilityState === 'visible') acquire(); };
  document.addEventListener('visibilitychange', onVis);
  return () => {
    document.removeEventListener('visibilitychange', onVis);
    try { lock?.release(); } catch { /* ignore */ }
  };
}

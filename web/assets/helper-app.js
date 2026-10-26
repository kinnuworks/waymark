// Helper console (helper.html): live visits, the map, the agent's trace,
// and a box to speak to the visitor.
import { api, qs, params, escapeHtml, idOf, onConnectionChange, isOnline } from './api.js';
import { MapView, walkerColour, KIND_LABEL } from './mapdraw.js';

const VISITS_MS = 2000;
const LIVE_MS = 1000;

const H = {
  buildings: [],
  buildingId: params().get('building') || '',
  visitId: params().get('visit') || '',
  map: null,
  maps: {}, // bid -> map JSON
  plans: {}, // bid -> HTMLImageElement or null
  visits: [],
  live: null,
  liveTimer: 0,
  liveInFlight: false,
};

const mapView = new MapView(qs('#map'), { padding: 28 });

const STATE_WORDS = {
  locating: ['Finding position', 'warn'], guiding: ['Guiding', 'ok'], low_confidence: ['Low confidence', 'warn'],
  stairs_caution: ['Stairs caution', 'warn'], off_route: ['Off route', 'warn'], lost: ['Lost', 'bad'],
  helper: ['Needs a helper', 'bad'], arrived: ['Arrived', 'ok'], ended: ['Ended', 'idle'],
};
const stateWords = (s) => STATE_WORDS[s] || [s ? String(s).replace(/_/g, ' ') : 'Unknown', 'idle'];

// ---------- connection pill ----------
function setConn(ok) {
  const p = qs('#conn');
  p.textContent = ok ? 'Live' : 'Offline';
  p.dataset.tone = ok ? 'ok' : 'bad';
}
onConnectionChange(setConn);

// ---------- time formatting ----------
function toDate(t) {
  if (t == null) return null;
  if (typeof t === 'number') return new Date(t < 1e12 ? t * 1000 : t); // seconds or ms
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? null : d;
}
const clock = (t) => { const d = toDate(t); return d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : ''; };
function ago(t) {
  const d = toDate(t);
  if (!d) return '';
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 60) return `${Math.max(0, s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return d.toLocaleString([], { hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short' });
}

function syncUrl() {
  const p = new URLSearchParams(location.search);
  H.buildingId ? p.set('building', H.buildingId) : p.delete('building');
  H.visitId ? p.set('visit', H.visitId) : p.delete('visit');
  history.replaceState(null, '', `${location.pathname}?${p}`);
}

// ---------- buildings ----------
async function loadBuildings() {
  const sel = qs('#building-select');
  try {
    const raw = await api.get('/buildings');
    H.buildings = Array.isArray(raw) ? raw : raw?.buildings || [];
    if (!H.buildings.length) {
      sel.innerHTML = '<option value="">No buildings yet</option>';
      return;
    }
    sel.innerHTML = H.buildings.map((b) =>
      `<option value="${escapeHtml(idOf(b))}">${escapeHtml(b.name || idOf(b))}${b.has_map ? '' : ' (mapping)'}</option>`).join('');
    // Default to the first building with a finished map: the sample building.
    if (!H.buildingId || !H.buildings.some((b) => String(idOf(b)) === H.buildingId)) {
      H.buildingId = String(idOf(H.buildings.find((b) => b.has_map) || H.buildings[0]));
    }
    sel.value = H.buildingId;
    await showBuilding(H.buildingId);
  } catch (e) {
    sel.innerHTML = '<option value="">Could not load buildings</option>';
    qs('#building-kv').innerHTML = `<dt>Error</dt><dd>${escapeHtml(e.message)}</dd>`;
  }
}

qs('#building-select').addEventListener('change', (e) => {
  H.visitId = '';
  showBuilding(e.target.value);
});
qs('#building-view-btn').addEventListener('click', () => {
  H.visitId = '';
  showBuilding(qs('#building-select').value);
});

async function loadMap(bid, force = false) {
  if (!bid) return null;
  if (!force && H.maps[bid]) return H.maps[bid];
  try {
    H.maps[bid] = await api.get(`/buildings/${encodeURIComponent(bid)}/map`, { retries: 1 });
  } catch (e) {
    H.maps[bid] = H.maps[bid] || { error: e.message };
  }
  return H.maps[bid];
}

// The rendered floor plan, assumed to cover map.bounds exactly.
async function loadPlan(bid) {
  if (bid in H.plans) return H.plans[bid];
  H.plans[bid] = null;
  try {
    const url = await api.blobUrl(`/buildings/${encodeURIComponent(bid)}/floorplan.png`, { retries: 0 });
    const img = new Image();
    img.decoding = 'async';
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
    H.plans[bid] = img;
  } catch { /* no plan yet; the vector map still draws */ }
  return H.plans[bid];
}

async function showBuilding(bid) {
  H.buildingId = bid;
  syncUrl();
  stopLive();
  setVisitPanels(false);
  renderVisitList();
  const b = H.buildings.find((x) => String(idOf(x)) === String(bid));
  qs('#view-title').textContent = b?.name || 'Building map';
  qs('#view-sub').textContent = 'Every volunteer walk in its own colour, with the route graph and marked places.';
  const map = await loadMap(bid, true);
  H.map = map;
  mapView.setMap(map?.error ? null : map);
  mapView.setOverlay({});
  applyPlan(bid);
  renderBuildingDetails(b, map);
  renderLegend(map, null);
}

async function applyPlan(bid) {
  if (!qs('#show-plan').checked) { mapView.setBackground(null); return; }
  const img = await loadPlan(bid);
  if (H.buildingId === bid || H.live?.building_id === bid) mapView.setBackground(img);
}
qs('#show-plan').addEventListener('change', () => applyPlan(currentBuildingId()));

function currentBuildingId() {
  const v = H.visits.find((x) => String(idOf(x)) === H.visitId);
  return String(v?.building_id ?? v?.bid ?? H.live?.building_id ?? H.buildingId ?? '');
}

function renderBuildingDetails(b, map) {
  qs('#building-panel').hidden = false;
  const kv = [];
  if (map?.error) kv.push(['Map', `Not available yet (${map.error})`]);
  if (b) {
    kv.push(['Status', b.has_map ? 'Map built' : 'Mapping in progress']);
    if (b.n_walkers != null && map?.stats?.walkers == null) kv.push(['Walkers', b.n_walkers]);
  }
  const traj = map?.trajectories || {};
  const metres = Object.values(traj).reduce((sum, t) => sum + pathLength(t), 0);
  if (metres) kv.push(['Walked', `${metres.toFixed(0)} m`]);
  for (const [k, v] of Object.entries(map?.stats || {})) {
    kv.push([k.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase()), typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2)) : String(v)]);
  }
  qs('#building-kv').innerHTML = kv.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('') || '<dt>Status</dt><dd>No data</dd>';
  const pois = map?.pois || [];
  qs('#poi-list').innerHTML = pois.length
    ? pois.map((p) => `<li class="row row--between t-small" style="padding:6px 0;border-bottom:1px solid var(--line)"><span style="color:var(--ink);font-weight:600">${escapeHtml(p.label || 'Unnamed')}</span><span>${escapeHtml(KIND_LABEL[p.kind] || p.kind || '')} · ${fmtXZ(p)}</span></li>`).join('')
    : '<li class="t-small">No places marked yet.</li>';
  const n = Object.keys(traj).length;
  qs('#map').setAttribute('aria-label', `Map of ${b?.name || 'the building'}: ${n} ${n === 1 ? 'walk' : 'walks'}, ${pois.length} marked places, ${(map?.nodes || []).length} route nodes.`);
}

const fmtXZ = (p) => (Number.isFinite(p?.x) ? `${p.x.toFixed(1)}, ${p.z.toFixed(1)} m` : '–');
function pathLength(t) {
  let L = 0;
  for (let i = 1; i < (t?.length || 0); i++) {
    const a = Array.isArray(t[i - 1]) ? t[i - 1] : [t[i - 1].x, t[i - 1].z];
    const b = Array.isArray(t[i]) ? t[i] : [t[i].x, t[i].z];
    L += Math.hypot(b[0] - a[0], b[1] - a[1]);
  }
  return L;
}

function renderLegend(map, live) {
  const items = Object.keys(map?.trajectories || {}).map((wid, i) =>
    `<span style="color:${walkerColour(wid, i)}"><i></i><span style="color:var(--ink-2)">Walker ${i + 1} <span class="t-mono" style="color:var(--ink-3)">${escapeHtml(String(wid).slice(0, 10))}</span></span></span>`);
  if (live) {
    items.push('<span style="color:var(--map-me)"><i style="height:10px;width:10px;border-radius:50%"></i><span style="color:var(--ink-2)">Visitor, arrow shows heading</span></span>');
    items.push('<span style="color:var(--map-route)"><i style="height:6px"></i><span style="color:var(--ink-2)">Route still to walk</span></span>');
  }
  items.push('<span style="color:var(--accent)"><i style="height:6px"></i><span style="color:var(--ink-2)">Entrance board</span></span>');
  qs('#legend').innerHTML = items.join('');
}

// ---------- visits ----------
async function pollVisits() {
  try {
    const raw = await api.get('/visits', { retries: 0 });
    H.visits = Array.isArray(raw) ? raw : raw?.visits || [];
    qs('#visits-updated').textContent = `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
    renderVisitList();
    renderHelpBanners();
    setConn(true);
  } catch {
    qs('#visits-updated').textContent = 'Not updating';
    setConn(isOnline());
  }
}

function isActive(v) {
  if (v.active === false || v.ended) return false;
  return !['ended', 'arrived'].includes(v.state);
}

function renderVisitList() {
  const list = qs('#visit-list');
  if (!H.visits.length) {
    list.innerHTML = '<li class="t-small">No visits yet. When someone starts the guidance app, the visit shows up here.</li>';
    return;
  }
  const sorted = [...H.visits].sort((a, b) => (isActive(b) - isActive(a)) || ((toDate(b.started ?? b.created)?.getTime() || 0) - (toDate(a.started ?? a.created)?.getTime() || 0)));
  list.innerHTML = sorted.map((v) => {
    const id = String(idOf(v));
    const [word, tone] = stateWords(v.help_requested ? 'helper' : v.state);
    const dest = typeof v.destination === 'object' && v.destination ? v.destination.label : v.destination;
    const meta = [v.building_name, v.device, ago(v.started ?? v.created)].filter(Boolean).join(' · ');
    return `<li><button class="list-btn" type="button" data-visit="${escapeHtml(id)}" aria-current="${id === H.visitId}">
      <span class="list-btn__main"><span class="list-btn__title">${escapeHtml(dest ? `To ${dest}` : 'No destination yet')}</span><span class="list-btn__meta">${escapeHtml(meta || id)}</span></span>
      <span class="pill" data-tone="${tone}">${escapeHtml(word)}</span>
    </button></li>`;
  }).join('');
}

qs('#visit-list').addEventListener('click', (e) => {
  const b = e.target.closest('[data-visit]');
  if (b) selectVisit(b.dataset.visit);
});

function renderHelpBanners() {
  const needs = H.visits.filter((v) => v.help_requested && isActive(v));
  document.title = needs.length ? `(${needs.length}) Help requested · Waymark` : 'Waymark helper console';
  qs('#help-banners').innerHTML = needs.map((v) => {
    const dest = typeof v.destination === 'object' && v.destination ? v.destination.label : v.destination;
    return `<div class="help-banner" role="alert">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5h.01"/></svg>
      <span>A visitor asked for help${v.building_name ? ` in ${escapeHtml(v.building_name)}` : ''}${dest ? `, on the way to ${escapeHtml(dest)}` : ''}.</span>
      <button class="btn btn-sm" type="button" data-visit="${escapeHtml(String(idOf(v)))}">Show on map</button>
    </div>`;
  }).join('');
}
qs('#help-banners').addEventListener('click', (e) => {
  const b = e.target.closest('[data-visit]');
  if (b) selectVisit(b.dataset.visit);
});

// ---------- one visit, live ----------
function setVisitPanels(on) {
  qs('#visit-panel').hidden = !on;
  qs('#say-panel').hidden = !on;
  qs('#trace-panel').hidden = !on;
  qs('#building-panel').hidden = on;
}

async function selectVisit(vid) {
  H.visitId = vid;
  syncUrl();
  renderVisitList();
  setVisitPanels(true);
  qs('#trace').innerHTML = '<li class="t-small" style="display:block">Loading…</li>';
  qs('#view-title').textContent = 'Visit in progress';
  qs('#view-sub').textContent = 'Blue dot: the visitor. Halo: how unsure the position is. Orange line: the route left to walk.';
  const v = H.visits.find((x) => String(idOf(x)) === vid);
  const bid = String(v?.building_id ?? v?.bid ?? H.buildingId);
  if (bid && bid !== H.buildingId) {
    H.buildingId = bid;
    qs('#building-select').value = bid;
  }
  H.map = await loadMap(bid);
  mapView.setMap(H.map?.error ? null : H.map);
  applyPlan(bid);
  renderLegend(H.map, true);
  stopLive();
  pollLive();
  H.liveTimer = setInterval(pollLive, LIVE_MS);
}

function stopLive() {
  clearInterval(H.liveTimer);
  H.live = null;
}

async function pollLive() {
  if (!H.visitId || H.liveInFlight) return;
  H.liveInFlight = true;
  const vid = H.visitId;
  try {
    const live = await api.get(`/visits/${encodeURIComponent(vid)}/live`, { retries: 0 });
    if (vid !== H.visitId) return;
    H.live = live;
    renderLive(live);
  } catch (e) {
    if (e.status === 404) {
      qs('#visit-kv').innerHTML = '<dt>Status</dt><dd>This visit is no longer on the server.</dd>';
      stopLive();
    }
  } finally {
    H.liveInFlight = false;
  }
}

function renderLive(live) {
  const fix = live.fix || null;
  const dest = live.destination;
  const destLabel = typeof dest === 'object' && dest ? (dest.label || dest.poi_id) : dest;
  const route = Array.isArray(live.route_polyline) ? live.route_polyline : [];

  mapView.setOverlay({
    me: fix ? { x: fix.x, z: fix.z, yaw_deg: fix.yaw_deg, confidence: fix.confidence } : null,
    route,
    target: dest && Number.isFinite(dest.x) ? { x: dest.x, z: dest.z } : null,
    highlightWalker: '__visit__', // dim every walker trail so the visitor stands out
  });

  const [word, tone] = stateWords(live.help_requested ? 'helper' : live.state);
  const v = H.visits.find((x) => String(idOf(x)) === H.visitId) || {};
  const remaining = routeLength(route);
  const kv = [
    ['State', `<span class="pill" data-tone="${tone}">${escapeHtml(word)}</span>`],
    ['Going to', escapeHtml(destLabel || 'Not chosen yet')],
    ['Position', fix ? `${fix.x.toFixed(1)}, ${fix.z.toFixed(1)} m` : 'Unknown'],
    ['Heading', fix && Number.isFinite(fix.yaw_deg) ? `${Math.round(((fix.yaw_deg % 360) + 360) % 360)}°` : '–'],
    ['Inliers', fix?.inliers != null ? String(fix.inliers) : '–'],
    ['Route left', remaining ? `${remaining.toFixed(1)} m` : '–'],
  ];
  if (v.device) kv.push(['Phone', escapeHtml(v.device)]);
  if (live.help_requested) kv.push(['Help', '<strong style="color:var(--danger)">Requested</strong>']);
  qs('#visit-kv').innerHTML = kv.map(([k, val]) => `<dt>${k}</dt><dd>${val}</dd>`).join('');

  const conf = Number.isFinite(fix?.confidence) ? Math.max(0, Math.min(1, fix.confidence)) : null;
  qs('#conf-text').textContent = conf == null ? '–' : `${conf.toFixed(2)} ${conf >= 0.6 ? '(good)' : conf >= 0.4 ? '(shaky)' : '(poor)'}`;
  const bar = qs('#conf-bar');
  bar.style.width = conf == null ? '0' : `${conf * 100}%`;
  bar.style.background = conf == null ? '' : conf >= 0.6 ? 'var(--ok)' : conf >= 0.4 ? 'var(--warn)' : 'var(--danger)';

  qs('#map').setAttribute('aria-label', fix
    ? `Visitor at x ${fix.x.toFixed(1)}, z ${fix.z.toFixed(1)} metres, state ${word}${destLabel ? `, going to ${destLabel}` : ''}.`
    : `Visitor position unknown, state ${word}.`);

  renderTrace(live.trace_tail || []);
}

function routeLength(route) {
  return pathLength(route);
}

// Trace entries come from the server; field names are read flexibly.
function pick(o, keys) {
  for (const k of keys) if (o[k] != null && o[k] !== '') return o[k];
  return null;
}
function show(v) {
  if (v == null) return '';
  if (typeof v === 'object') return Object.entries(v).map(([k, x]) => `${k}: ${typeof x === 'number' ? +x.toFixed(3) : x}`).join(', ');
  return String(v);
}

let lastTraceKey = '';
function renderTrace(items) {
  const key = JSON.stringify(items.slice(-3));
  if (key === lastTraceKey) return; // avoid re-rendering (and re-announcing) the same list
  lastTraceKey = key;
  const el = qs('#trace');
  if (!items.length) { el.innerHTML = '<li class="t-small" style="display:block">No decisions yet.</li>'; return; }
  el.innerHTML = [...items].reverse().map((it) => {
    if (typeof it !== 'object' || it === null) return `<li><time></time><div>${escapeHtml(String(it))}</div></li>`;
    const t = pick(it, ['t', 'time', 'ts', 'timestamp', 't_ms', 'at']);
    const state = pick(it, ['state', 'new_state']);
    const saw = pick(it, ['saw', 'observation', 'observed', 'obs', 'perception', 'input', 'signals']);
    const did = pick(it, ['decided', 'decision', 'action', 'did', 'say', 'output']);
    const why = pick(it, ['why', 'reason', 'rationale', 'because']);
    const rows = [['Saw', saw], ['Did', did], ['Why', why]].filter(([, v]) => v != null && v !== '');
    return `<li><time>${escapeHtml(clock(t))}</time><div>
      ${state ? `<span class="tl-state">${escapeHtml(stateWords(state)[0])}</span>` : ''}
      <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(show(v))}</dd>`).join('')}</dl>
    </div></li>`;
  }).join('');
}

// ---------- speak to the visitor ----------
qs('#quick-says').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) { qs('#say-text').value = b.textContent; qs('#say-text').focus(); }
});
qs('#say-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = qs('#say-text').value.trim();
  const status = qs('#say-status');
  if (!text || !H.visitId) return;
  status.textContent = 'Sending…';
  try {
    await api.post(`/visits/${encodeURIComponent(H.visitId)}/say`, { text }, { retries: 1 });
    status.textContent = `Sent at ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.`;
    qs('#say-text').value = '';
  } catch (err) {
    status.textContent = `Not sent. ${err.message}`;
  }
});
// Cmd/Ctrl + Enter sends.
qs('#say-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) qs('#say-form').requestSubmit();
});

// ---------- boot ----------
await loadBuildings();
await pollVisits();
setInterval(pollVisits, VISITS_MS);
if (H.visitId) selectVisit(H.visitId);

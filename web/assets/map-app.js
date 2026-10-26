// Volunteer mapping app (map.html).
// Flow: pick or add a building -> optional calibration -> walk (frames every
// ~700 ms, never more than one in flight) -> finish. A builder can then build.
import { api, qs, qsa, params, store, toast, escapeHtml, idOf, keepAwake, pageUrl } from './api.js';
import { openCamera, stopCamera, captureJpeg, CameraError } from './camera.js';
import { startCompass, compass } from './compass.js';
import { speech } from './speech.js';
import { MapView, KIND_LABEL, walkerColour } from './mapdraw.js';

const FRAME_MS = 700;
const POLL_MS = 3000;
const KINDS = ['room', 'door', 'stairs', 'lift', 'restroom', 'exit', 'desk', 'other'];

const S = {
  building: null, // {id, name}
  walker: null, // {id, colour, name}
  stream: null,
  running: false,
  paused: false,
  inFlight: false,
  frameTimer: 0,
  pollTimer: 0,
  lastSay: '',
  lastAssignSay: '',
  pose: null,
  places: 0,
  netDown: false,
  releaseWake: null,
  calibTimer: 0,
};

// ---------- small UI helpers ----------
let currentStep = 'building';
function showStep(name) {
  currentStep = name;
  for (const id of ['building', 'calib', 'map', 'done']) qs(`#step-${id}`).hidden = id !== name;
  const order = ['building', 'calib', 'map'];
  const idx = order.indexOf(name === 'done' ? 'map' : name);
  qsa('.steps li').forEach((li, i) => {
    li.classList.toggle('done', i < idx || name === 'done');
    if (i === idx && name !== 'done') li.setAttribute('aria-current', 'step');
    else li.removeAttribute('aria-current');
  });
  updateBuilderVisibility();
  window.scrollTo({ top: 0 });
  const h = qs(`#step-${name} h1`);
  if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); }
}

function envError(msg) {
  const el = qs('#env-error');
  el.hidden = !msg;
  if (msg) el.querySelector('span').textContent = msg;
}

function setPill(el, text, tone) {
  el.textContent = text;
  el.dataset.tone = tone;
}

function say(text, opts) {
  speech.say(text, opts);
}

// Voice toggle (speech is on by default; volunteers may want it off indoors).
const voiceBtn = qs('#voice-toggle');
voiceBtn.addEventListener('click', () => {
  speech.unlock();
  const on = voiceBtn.getAttribute('aria-pressed') !== 'true';
  speech.setEnabled(on);
  voiceBtn.setAttribute('aria-pressed', String(on));
  voiceBtn.textContent = on ? 'Voice on' : 'Voice off';
  store('mapVoice', on);
});
if (store('mapVoice') === false) {
  speech.setEnabled(false);
  voiceBtn.setAttribute('aria-pressed', 'false');
  voiceBtn.textContent = 'Voice off';
}

// ---------- environment checks ----------
if (!window.isSecureContext) {
  envError('This page is not on a secure (https) connection, so the browser will block the camera. Open the https link instead.');
}

// ---------- step 1: buildings ----------
const nameInput = qs('#walker-name');
nameInput.value = store('walkerName') || '';
nameInput.addEventListener('change', () => store('walkerName', nameInput.value.trim()));

function requireName() {
  const name = nameInput.value.trim();
  if (!name) {
    nameInput.setAttribute('aria-invalid', 'true');
    nameInput.focus();
    toast('Add your first name first.');
    return null;
  }
  nameInput.removeAttribute('aria-invalid');
  store('walkerName', name);
  return name;
}

async function loadBuildings() {
  const list = qs('#building-list');
  list.setAttribute('aria-busy', 'true');
  try {
    const raw = await api.get('/buildings');
    const buildings = Array.isArray(raw) ? raw : raw?.buildings || [];
    if (!buildings.length) {
      list.innerHTML = '<li class="t-small">No buildings yet. Add the first one below.</li>';
      return;
    }
    list.innerHTML = buildings.map((b) => {
      const bits = [];
      bits.push(b.has_map ? 'Map built' : 'Mapping in progress');
      if (b.n_walkers != null) bits.push(`${b.n_walkers} ${b.n_walkers === 1 ? 'walker' : 'walkers'}`);
      if (b.n_pois != null) bits.push(`${b.n_pois} ${b.n_pois === 1 ? 'place' : 'places'}`);
      return `<li><button class="list-btn" type="button" data-id="${escapeHtml(idOf(b))}" data-name="${escapeHtml(b.name)}">
        <span class="list-btn__main"><span class="list-btn__title">${escapeHtml(b.name || 'Untitled building')}</span><span class="list-btn__meta">${escapeHtml(bits.join(' · '))}</span></span>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
      </button></li>`;
    }).join('');
  } catch (e) {
    list.innerHTML = `<li class="callout callout--bad"><span>Could not load buildings. ${escapeHtml(e.message)} Tap Refresh to try again.</span></li>`;
  } finally {
    list.setAttribute('aria-busy', 'false');
  }
}

qs('#refresh-buildings').addEventListener('click', loadBuildings);
qs('#building-list').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-id]');
  if (!btn) return;
  joinBuilding({ id: btn.dataset.id, name: btn.dataset.name });
});

qs('#new-building').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = qs('#b-name').value.trim();
  const h = parseFloat(qs('#b-height').value);
  const msg = qs('#new-building-msg');
  if (!name) { qs('#b-name').focus(); msg.textContent = 'Give the building a name.'; return; }
  if (!(h >= 0.3 && h <= 2.5)) { qs('#b-height').focus(); msg.textContent = 'Board height should be between 0.3 and 2.5 metres.'; return; }
  if (!requireName()) return;
  msg.textContent = 'Adding…';
  try {
    const res = await api.post('/buildings', { name, board_height_m: Math.round(h * 100) / 100 });
    const id = idOf(res);
    store(`builder:${id}`, true); // this device created it, so it may build the map
    msg.textContent = '';
    await joinBuilding({ id, name });
  } catch (err) {
    msg.textContent = `Could not add the building. ${err.message}`;
  }
});

async function joinBuilding(b) {
  const name = requireName();
  if (!name) return;
  try {
    const res = await api.post(`/buildings/${encodeURIComponent(b.id)}/walkers`, { name });
    S.building = b;
    S.walker = { id: res.walker_id, colour: res.colour || walkerColour(res.walker_id), name };
    store('walk', { building: b, walker: S.walker, at: Date.now() });
    showStep('calib');
  } catch (err) {
    toast(`Could not join ${b.name}. ${err.message}`, 5000);
  }
}

// Offer to continue a walk started in the last 3 hours (for example after a reload).
const saved = store('walk');
if (saved && saved.walker && Date.now() - saved.at < 3 * 3600 * 1000) {
  qs('#resume').hidden = false;
  qs('#resume-text').textContent = `You have a walk in progress in ${saved.building.name}.`;
  qs('#resume-btn').addEventListener('click', () => {
    S.building = saved.building;
    S.walker = saved.walker;
    nameInput.value = saved.walker.name || nameInput.value;
    showStep('map');
    enterMapStep();
  });
  qs('#resume-forget').addEventListener('click', () => { store('walk', null); qs('#resume').hidden = true; });
}

// ---------- camera + permissions (must start inside a tap) ----------
async function startDevices(video) {
  speech.unlock();
  startCompass(); // fire and forget; iOS shows its prompt now
  if (S.stream) {
    video.srcObject = S.stream;
    try { await video.play(); } catch { /* ignore */ }
    return true;
  }
  try {
    S.stream = await openCamera(video);
    if (!S.releaseWake) S.releaseWake = await keepAwake();
    envError('');
    return true;
  } catch (e) {
    const msg = e instanceof CameraError ? e.message : 'The camera could not start.';
    envError(msg);
    say(msg, { urgent: true });
    return false;
  }
}

// ---------- step 2: calibration ----------
let calibRunning = false;
qs('#calib-start').addEventListener('click', async () => {
  const video = qs('#calib-video');
  if (calibRunning) { stopCalib(); return; }
  if (!(await startDevices(video))) return;
  calibRunning = true;
  qs('#calib-start').textContent = 'Stop';
  say('Show the board from several angles. Hold each angle for a moment.');
  calibLoop();
});

function stopCalib() {
  calibRunning = false;
  clearTimeout(S.calibTimer);
  qs('#calib-start').textContent = 'Start calibration';
}

async function calibLoop() {
  if (!calibRunning) return;
  const video = qs('#calib-video');
  const status = qs('#calib-status');
  try {
    const blob = await captureJpeg(video, { longSide: 960, quality: 0.85 });
    if (blob) {
      const r = await api.postImage(`/buildings/${S.building.id}/walkers/${S.walker.id}/calib`, blob);
      setPill(qs('#calib-found'), r.board_found ? 'Board found' : 'No board in view', r.board_found ? 'ok' : 'warn');
      setPill(qs('#calib-count'), `${r.views ?? 0} of ${r.needed ?? '?'} views`, r.done ? 'ok' : 'idle');
      if (r.done) {
        const rms = Number.isFinite(r.rms_px) ? ` Reprojection error ${r.rms_px.toFixed(2)} px.` : '';
        status.innerHTML = `<small>Status</small>Calibration done.${escapeHtml(rms)}`;
        say('Calibration done. Starting the walk.');
        stopCalib();
        showStep('map');
        enterMapStep(true);
        return;
      }
      status.innerHTML = `<small>Status</small>${r.board_found ? 'Got it. Now tilt the phone to a new angle.' : 'Bring the whole board into view.'}`;
    }
  } catch (e) {
    status.innerHTML = `<small>Status</small>${escapeHtml(e.message)} Still trying.`;
  }
  S.calibTimer = setTimeout(calibLoop, 1000);
}

qs('#calib-skip').addEventListener('click', async () => {
  stopCalib();
  showStep('map');
  enterMapStep();
});

// ---------- step 3: mapping ----------
const mapView = new MapView(qs('#mini-map'), { padding: 16, labels: true });

function enterMapStep(autoStart = false) {
  qs('#map-building-name').textContent = S.building.name;
  qs('#map-walker-line').textContent = `You are walking as ${S.walker.name}.`;
  mapView.setWalkerColours({ [S.walker.id]: S.walker.colour });
  startPolling();
  if (autoStart && S.stream) beginWalking();
}

qs('#map-start').addEventListener('click', beginWalking);

async function beginWalking() {
  const video = qs('#map-video');
  qs('#map-video-box').hidden = false;
  if (!(await startDevices(video))) { qs('#map-video-box').hidden = true; return; }
  qs('#start-box').hidden = true;
  for (const id of ['#mark-btn', '#pause-btn', '#finish-btn']) qs(id).disabled = false;
  S.running = true;
  S.paused = false;
  say('Mapping started. Point the camera at the entrance board.');
  frameLoop();
}

async function frameLoop() {
  clearTimeout(S.frameTimer);
  if (!S.running || S.paused) return;
  const started = performance.now();
  if (!S.inFlight) {
    S.inFlight = true;
    try {
      const blob = await captureJpeg(qs('#map-video'));
      if (blob) {
        const fields = { t_ms: Date.now() };
        if (compass.heading != null) fields.heading_deg = Math.round(compass.heading * 10) / 10;
        const r = await api.postImage(`/buildings/${S.building.id}/walkers/${S.walker.id}/frame`, blob, fields);
        onFrame(r);
      }
    } catch (e) {
      onFrameError(e);
    } finally {
      S.inFlight = false;
    }
  }
  if (!S.running || S.paused || document.visibilityState === 'hidden') return;
  // Clearing first means a second chain started by visibilitychange collapses into this one.
  const wait = Math.max(0, FRAME_MS - (performance.now() - started));
  clearTimeout(S.frameTimer);
  S.frameTimer = setTimeout(frameLoop, wait);
}

const STATE_TEXT = {
  need_board: ['Looking for the board', 'warn'],
  tracking: ['Tracking', 'ok'],
  lost: ['Lost track', 'bad'],
  rejected: ['Frame skipped', 'warn'],
};

const IDLE_SAY = {
  tracking: 'Tracking. Keep walking at a steady pace.',
  need_board: 'Point the camera at the entrance board.',
};

function onFrame(r) {
  if (S.netDown) { S.netDown = false; say('Connected again.'); }
  const [label, tone] = STATE_TEXT[r.state] || [r.state || 'Unknown', 'idle'];
  setPill(qs('#map-state'), label, tone);

  const q = qs('#map-quality');
  if (r.quality) {
    q.hidden = false;
    const problems = [];
    if (r.quality.ok === false) {
      if (Number.isFinite(r.quality.brightness) && r.quality.brightness < 0.2) problems.push('Too dark');
      else if (Number.isFinite(r.quality.brightness) && r.quality.brightness > 0.9) problems.push('Too bright');
      else problems.push('Blurry');
    }
    setPill(q, problems[0] || 'Image OK', problems.length ? 'warn' : 'ok');
  }

  if (r.pose) S.pose = r.pose;
  if (r.keyframes != null) qs('#st-keyframes').textContent = Number(r.keyframes).toLocaleString();
  if (r.points != null) qs('#st-points').textContent = Number(r.points).toLocaleString();

  const sayText = (r.say || '').trim();
  // Without a new sentence, show what the current state means instead of a stale warning.
  qs('#map-say span').textContent = sayText || IDLE_SAY[r.state] || qs('#map-say span').textContent;
  if (sayText !== S.lastSay) {
    if (sayText) say(sayText, { urgent: r.state === 'lost' });
    S.lastSay = sayText;
  }
  drawOverlay();
}

function onFrameError(e) {
  if (e.status === 404) {
    // The server no longer knows this walker (for example after a restart).
    S.running = false;
    setPill(qs('#map-state'), 'Walk not found', 'bad');
    envError('The server no longer has this walk. Go back and join the building again.');
    say('The server no longer has this walk. Please join the building again.', { urgent: true });
    store('walk', null);
    return;
  }
  setPill(qs('#map-state'), e.network ? 'No connection' : 'Server error', 'bad');
  if (!S.netDown) {
    S.netDown = true;
    say('Lost the connection to the server. Hold still. I will keep trying.', { urgent: true });
  }
}

// Coordinator and live map, every 3 s.
function startPolling() {
  clearInterval(S.pollTimer);
  poll();
  S.pollTimer = setInterval(poll, POLL_MS);
}

let lastMap = null;
let lastAssign = null;
async function poll() {
  if (!S.building || document.visibilityState === 'hidden') return;
  const bid = encodeURIComponent(S.building.id);
  const [mapRes, coordRes] = await Promise.allSettled([
    api.get(`/buildings/${bid}/map`, { retries: 0 }),
    api.get(`/buildings/${bid}/coordinator`, { retries: 0 }),
  ]);
  if (mapRes.status === 'fulfilled') {
    lastMap = mapRes.value;
    mapView.setMap(lastMap);
    renderLegend();
  }
  if (coordRes.status === 'fulfilled') {
    const c = coordRes.value || {};
    if (Number.isFinite(c.coverage_pct)) qs('#st-coverage').textContent = `${Math.round(c.coverage_pct)}%`;
    lastAssign = c.assignments?.[S.walker?.id] || null;
    const box = qs('#map-assign');
    if (lastAssign?.say) {
      box.hidden = false;
      box.querySelector('span').textContent = lastAssign.say;
      // Speak the assignment when it changes, but not over a live tracking warning.
      if (lastAssign.say !== S.lastAssignSay && S.running) say(lastAssign.say);
      S.lastAssignSay = lastAssign.say;
    } else {
      box.hidden = true;
    }
  }
  drawOverlay();
}

function renderLegend() {
  const ids = Object.keys(lastMap?.trajectories || {});
  qs('#map-legend').innerHTML = ids.map((wid, i) => {
    const mine = wid === S.walker?.id;
    const c = mine ? S.walker.colour : walkerColour(wid, i);
    return `<span style="color:${escapeHtml(c)}"><i></i><span style="color:var(--ink-2)">${mine ? 'You' : 'Walker ' + (i + 1)}</span></span>`;
  }).join('') + (lastAssign?.target ? '<span style="color:var(--map-route)"><i></i><span style="color:var(--ink-2)">Your next area</span></span>' : '');
  const n = ids.length;
  qs('#mini-map').setAttribute('aria-label', `Map of ${n} ${n === 1 ? 'walk' : 'walks'} so far. Coverage ${qs('#st-coverage').textContent}.`);
}

function drawOverlay() {
  mapView.setOverlay({
    me: S.pose ? { ...S.pose, colour: S.walker?.colour } : null,
    target: lastAssign?.target ? { ...lastAssign.target, label: 'Next' } : null,
    highlightWalker: S.walker?.id,
  });
}

// Pause sending while the page is hidden or the user taps Pause.
document.addEventListener('visibilitychange', () => {
  if (!S.running) return;
  if (document.visibilityState === 'visible' && !S.paused) frameLoop();
  else clearTimeout(S.frameTimer);
});

qs('#pause-btn').addEventListener('click', () => {
  S.paused = !S.paused;
  qs('#pause-btn').textContent = S.paused ? 'Resume' : 'Pause';
  setPill(qs('#map-state'), S.paused ? 'Paused' : 'Resuming', S.paused ? 'idle' : 'warn');
  say(S.paused ? 'Paused. Tap Resume when you are back where you stopped.' : 'Resumed.');
  if (!S.paused) frameLoop();
});

// ---------- mark a place ----------
const sheet = qs('#mark-sheet');
let kind = 'room';
qs('#kind-chips').innerHTML = KINDS.map((k) =>
  `<button type="button" class="chip" role="radio" data-kind="${k}" aria-checked="${k === kind}">${KIND_LABEL[k]}</button>`).join('');
qs('#kind-chips').addEventListener('click', (e) => {
  const b = e.target.closest('[data-kind]');
  if (!b) return;
  kind = b.dataset.kind;
  qsa('#kind-chips [data-kind]').forEach((c) => c.setAttribute('aria-checked', String(c === b)));
});
// Arrow keys move between chips, like a native radio group.
qs('#kind-chips').addEventListener('keydown', (e) => {
  if (!['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp'].includes(e.key)) return;
  const chips = qsa('#kind-chips [data-kind]');
  const i = chips.indexOf(document.activeElement);
  const next = chips[(i + (e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : chips.length - 1)) % chips.length];
  next.focus(); next.click(); e.preventDefault();
});

qs('#mark-btn').addEventListener('click', () => {
  qs('#mark-label').value = '';
  if (typeof sheet.showModal === 'function') sheet.showModal(); else sheet.setAttribute('open', '');
  setTimeout(() => qs('#mark-label').focus(), 50);
});
qs('#mark-cancel').addEventListener('click', () => sheet.close());

qs('#mark-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const label = qs('#mark-label').value.trim() || KIND_LABEL[kind];
  const btn = qs('#mark-save');
  btn.disabled = true;
  try {
    const r = await api.post(`/buildings/${S.building.id}/walkers/${S.walker.id}/poi`, { label, kind });
    S.places += 1;
    qs('#st-places').textContent = S.places;
    sheet.close();
    const where = Number.isFinite(r.x) ? ` at ${r.x.toFixed(1)}, ${r.z.toFixed(1)} m` : '';
    toast(`Saved ${label}${where}.`);
    say(`Saved ${label}.`);
    poll();
  } catch (err) {
    toast(`Could not save. ${err.message}`, 5000);
    say('Could not save that place. Try again.');
  } finally {
    btn.disabled = false;
  }
});

// ---------- finish ----------
qs('#finish-btn').addEventListener('click', async () => {
  if (!confirm('Finish your walk? You will not be able to add to it afterwards.')) return;
  S.running = false;
  clearTimeout(S.frameTimer);
  try {
    await api.post(`/buildings/${S.building.id}/walkers/${S.walker.id}/finish`, {}, { retries: 2 });
  } catch (e) {
    toast(`The server did not confirm the finish. ${e.message}`, 6000);
  }
  stopCamera(S.stream);
  S.stream = null;
  S.releaseWake?.();
  S.releaseWake = null;
  store('walk', null);
  const kf = qs('#st-keyframes').textContent, pl = S.places;
  qs('#done-summary').textContent = `Your walk in ${S.building.name} has been sent: ${kf} keyframes and ${pl} ${pl === 1 ? 'place' : 'places'} marked.`;
  qs('#view-console').href = pageUrl('helper.html', { building: S.building.id });
  showStep('done');
  say('Walk finished. Thank you.');
});

qs('#new-walk').addEventListener('click', () => {
  S.building = null; S.walker = null; S.pose = null; S.places = 0; S.lastSay = ''; S.lastAssignSay = '';
  clearInterval(S.pollTimer);
  qs('#start-box').hidden = false;
  qs('#map-video-box').hidden = true;
  showStep('building');
  loadBuildings();
});

// ---------- build (builder only) ----------
function updateBuilderVisibility() {
  const bid = S.building?.id;
  const allowed = params().get('builder') === '1' || (bid && store(`builder:${bid}`));
  qs('#builder').hidden = !(bid && allowed && (currentStep === 'map' || currentStep === 'done'));
}

qs('#build-btn').addEventListener('click', async () => {
  const btn = qs('#build-btn');
  const status = qs('#build-status');
  btn.disabled = true;
  status.textContent = 'Building the map. This can take a few minutes; keep this page open.';
  try {
    const r = await api.post(`/buildings/${S.building.id}/build`, {}, { timeout: 15 * 60 * 1000 });
    status.textContent = r.ok === false ? 'The build finished with problems. See the numbers below.' : 'Map built. Visitors can now use this building.';
    const stats = r.stats || {};
    const el = qs('#build-stats');
    el.hidden = !Object.keys(stats).length;
    el.innerHTML = Object.entries(stats).map(([k, v]) =>
      `<div class="stat"><span class="stat__label">${escapeHtml(k.replace(/_/g, ' '))}</span><span class="stat__value">${escapeHtml(typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2)) : String(v))}</span></div>`).join('');
    say(r.ok === false ? 'The build finished with problems.' : 'Map built.');
    poll();
  } catch (e) {
    status.textContent = `The build failed. ${e.message}`;
  } finally {
    btn.disabled = false;
  }
});

// ---------- boot ----------
const pre = params().get('building');
loadBuildings().then(() => {
  if (pre) {
    const b = qs(`#building-list button[data-id="${CSS.escape(pre)}"]`);
    b?.focus();
  }
});

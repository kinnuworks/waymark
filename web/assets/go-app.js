// Guidance app for blind and low-vision visitors (go.html).
// Built for VoiceOver and TalkBack first: every state change is spoken (phone
// voice) or announced through an aria-live region (screen reader mode), never both.
import { api, qs, qsa, params, store, escapeHtml, idOf, keepAwake, onConnectionChange } from './api.js';
import { openCamera, stopCamera, captureJpeg, CameraError } from './camera.js';
import { startCompass, compass, onHeading, angleDiff } from './compass.js';
import { speech } from './speech.js';
import { KIND_LABEL } from './mapdraw.js';

const FRAME_MS = 1000;
const CONFIDENT = 0.6; // below this, the compass coaching stays quiet

const HAPTIC = {
  left: [80, 100, 80], // two short
  right: [80, 100, 80, 100, 80], // three short
  stop: [700], // one long
  arrived: [500, 150, 150, 150, 500], // long, short, long
};

// Plain words and an icon for each server state. Urgent states interrupt speech.
const ICON = {
  dot: '<circle cx="12" cy="12" r="9"/>',
  up: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5h.01"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  person: '<circle cx="12" cy="7" r="4"/><path d="M4 21v-1a7 7 0 0 1 16 0v1"/>',
  check: '<path d="M4 12.5l5 5L20 6.5"/>',
  off: '<path d="M2 2l20 20M8.5 16.5a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 5.2-2.8M12 20h.01"/>',
};
const STATES = {
  locating: { text: 'Finding where you are', icon: 'search' },
  guiding: { text: 'Guiding', icon: 'up' },
  low_confidence: { text: 'Not sure of your position', icon: 'warn', urgent: true },
  stairs_caution: { text: 'Stairs nearby. Stop.', icon: 'stop', urgent: true },
  off_route: { text: 'Off the route', icon: 'warn', urgent: true },
  lost: { text: 'Position lost. Stop.', icon: 'stop', urgent: true },
  helper: { text: 'A helper is coming', icon: 'person' },
  arrived: { text: 'Arrived', icon: 'check' },
  offline: { text: 'No connection. Stop and wait.', icon: 'off', urgent: true },
};

const G = {
  mode: store('voiceMode') || 'voice',
  building: null, // {id, name}
  visitId: null,
  destLabel: '',
  destReady: false,
  stream: null,
  running: false,
  inFlight: false,
  timer: 0,
  lastState: '',
  lastSay: '',
  lastHaptic: 'none',
  failures: 0,
  releaseWake: null,
  coach: null, // compass coaching between fixes
  evalArrived: null,
};

// ---------- output: speech, live regions, haptics ----------
function announce(text, { urgent = false, keep = false } = {}) {
  if (!text) return;
  if (G.mode === 'sr') {
    // Clear first so the same text is announced again when repeated.
    const el = qs(urgent ? '#live-assertive' : '#live-polite');
    el.textContent = '';
    setTimeout(() => { el.textContent = text; }, 60);
    speech.last = text;
  } else {
    speech.say(text, { urgent, keep });
  }
}

function buzz(kind) {
  const p = HAPTIC[kind];
  if (p && navigator.vibrate) {
    try { navigator.vibrate(p); } catch { /* ignore */ }
  }
}

function setMode(mode) {
  G.mode = mode;
  store('voiceMode', mode);
  speech.setEnabled(mode === 'voice');
  qsa('[data-mode]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
}
qsa('[data-mode]').forEach((b) => b.addEventListener('click', () => {
  speech.unlock();
  setMode(b.dataset.mode);
  announce(b.dataset.mode === 'voice' ? 'Directions will be spoken by the phone.' : 'Directions will go to your screen reader.');
}));
setMode(G.mode);

function showAlert(title, text, speak = true) {
  const el = qs('#alert');
  if (!title) { el.hidden = true; return; }
  qs('#alert-title').textContent = title;
  qs('#alert-text').textContent = text;
  el.hidden = false;
  if (speak) announce(`${title}. ${text}`, { urgent: true });
}

function show(screen) {
  for (const s of ['building', 'dest', 'guide', 'end']) qs(`#scr-${s}`).hidden = s !== screen;
  const h = qs(`#scr-${screen} h2`);
  if (h) { if (!h.hasAttribute('tabindex')) h.setAttribute('tabindex', '-1'); h.focus(); }
  updateEvalVisibility();
}

// ---------- environment ----------
if (!window.isSecureContext) {
  showAlert('Secure link needed', 'This page must be opened with an https address, or the camera will not work. Ask staff for the correct link.', false);
}
onConnectionChange((ok) => {
  if (!G.running) return; // the frame loop handles the guiding case itself
  if (!ok) showAlert('No connection', 'Cannot reach the Waymark server. Retrying.', false);
  else showAlert('');
});

// ---------- screen 1: buildings ----------
async function loadBuildings() {
  const list = qs('#building-list');
  list.setAttribute('aria-busy', 'true');
  try {
    const raw = await api.get('/buildings');
    const all = Array.isArray(raw) ? raw : raw?.buildings || [];
    const ready = all.filter((b) => b.has_map !== false);
    if (!ready.length) {
      list.innerHTML = '<p>No building has a finished map yet. Ask a member of staff for help.</p>';
      return;
    }
    list.innerHTML = ready.map((b) =>
      `<button class="gbtn gbtn--left" type="button" data-id="${escapeHtml(idOf(b))}" data-name="${escapeHtml(b.name)}">${escapeHtml(b.name || 'Unnamed building')}</button>`).join('');
    // ?building= picks one straight away.
    const pre = params().get('building');
    if (pre) {
      const match = ready.find((b) => String(idOf(b)) === pre);
      if (match) chooseBuilding({ id: idOf(match), name: match.name });
    }
  } catch (e) {
    list.innerHTML = `<p>Could not load the list of buildings. ${escapeHtml(e.message)}</p><button class="gbtn" type="button" id="retry-buildings">Try again</button>`;
    qs('#retry-buildings').addEventListener('click', loadBuildings);
    // Speak once, then keep retrying quietly while this screen is showing.
    if (!loadBuildings.warned) announce('Could not load the list of buildings. Check your connection. I will keep trying.', { urgent: true });
    loadBuildings.warned = true;
    setTimeout(() => { if (!qs('#scr-building').hidden) loadBuildings(); }, 5000);
  } finally {
    list.setAttribute('aria-busy', 'false');
  }
}

qs('#building-list').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-id]');
  if (b) { speech.unlock(); chooseBuilding({ id: b.dataset.id, name: b.dataset.name }); }
});

function deviceName() {
  const ua = navigator.userAgent;
  const os = /iPhone|iPad|iPod/.test(ua) ? 'iOS' : /Android/.test(ua) ? 'Android' : /Mac/.test(ua) ? 'macOS' : /Windows/.test(ua) ? 'Windows' : 'Other';
  const br = /CriOS|Chrome/.test(ua) ? 'Chrome' : /FxiOS|Firefox/.test(ua) ? 'Firefox' : /Safari/.test(ua) ? 'Safari' : 'Browser';
  return `${os} ${br}`;
}

async function chooseBuilding(b) {
  G.building = b;
  qs('#dest-building').textContent = `In ${b.name}.`;
  qs('#poi-list').innerHTML = '<p>Loading places…</p>';
  qs('#dest-chosen').hidden = true;
  qs('#ask-answer').hidden = true;
  G.destReady = false;
  qs('#start-btn').disabled = true;
  show('dest');
  try {
    const [visit, map] = await Promise.all([
      api.post(`/buildings/${encodeURIComponent(b.id)}/visits`, { device: deviceName() }, { retries: 1 }),
      api.get(`/buildings/${encodeURIComponent(b.id)}/map`),
    ]);
    G.visitId = visit.visit_id ?? idOf(visit);
    renderPois(map?.pois || []);
    updateEvalVisibility();
  } catch (e) {
    qs('#poi-list').innerHTML = `<p>Could not load places in this building. ${escapeHtml(e.message)}</p>`;
    announce('Could not load places in this building. Choose Change building to try again.', { urgent: true });
  }
}

function renderPois(pois) {
  const list = qs('#poi-list');
  list.setAttribute('aria-busy', 'false');
  // Doors are mostly waypoints; list real destinations first.
  const order = ['room', 'desk', 'restroom', 'lift', 'stairs', 'exit', 'other', 'door'];
  const sorted = [...pois].sort((a, b) => (order.indexOf(a.kind) - order.indexOf(b.kind)) || String(a.label).localeCompare(String(b.label)));
  if (!sorted.length) {
    list.innerHTML = '<p>No places have been marked in this building yet. Try typing where you want to go.</p>';
    return;
  }
  list.innerHTML = sorted.map((p) => {
    const kind = KIND_LABEL[p.kind] || '';
    const label = p.label || kind || 'Place';
    const sub = kind && kind.toLowerCase() !== String(label).toLowerCase() ? `<small>${escapeHtml(kind)}</small>` : '';
    return `<button class="gbtn gbtn--left" type="button" data-poi="${escapeHtml(idOf(p))}" data-label="${escapeHtml(label)}" aria-pressed="false"><span>${escapeHtml(label)}${sub}</span></button>`;
  }).join('');
}

qs('#poi-list').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-poi]');
  if (!b || !G.visitId) return;
  speech.unlock();
  qsa('#poi-list [data-poi]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  try {
    const r = await api.post(`/visits/${encodeURIComponent(G.visitId)}/destination`, { poi_id: b.dataset.poi }, { retries: 1 });
    G.destLabel = b.dataset.label;
    const dist = Number.isFinite(r.route_m) ? ` The route is about ${Math.round(r.route_m)} metres.` : '';
    destinationReady(`${G.destLabel} selected.${dist} Tap Start guidance when you are ready.`);
  } catch (err) {
    b.setAttribute('aria-pressed', 'false');
    const msg = err.status === 404 || err.status === 400 || err.status === 422
      ? `There is no route to ${b.dataset.label} from here. Choose another place or call a helper.`
      : `Could not set the destination. ${err.message}`;
    announce(msg, { urgent: true });
  }
});

function destinationReady(text) {
  G.destReady = true;
  const note = qs('#dest-chosen');
  note.textContent = text;
  note.hidden = false;
  qs('#start-btn').disabled = false;
  if (G.mode === 'voice') speech.say(text, { urgent: true });
  // In screen reader mode the role=status note is announced; move focus to Start.
  setTimeout(() => {
    qs('#start-btn').focus();
    qs('#start-btn').scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, G.mode === 'sr' ? 400 : 50);
}

qs('#ask-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  speech.unlock();
  const text = qs('#ask-input').value.trim();
  if (!text || !G.visitId) return;
  const out = qs('#ask-answer');
  out.hidden = false;
  out.textContent = 'Asking…';
  try {
    const r = await api.post(`/visits/${encodeURIComponent(G.visitId)}/ask`, { text }, { retries: 1 });
    out.textContent = r.say || 'No answer.';
    // The agent may set a destination from free text; let the visitor start.
    // We don't know the place's name here, so later messages say "your destination".
    G.destReady = true;
    qs('#start-btn').disabled = false;
    if (G.mode === 'voice') speech.say(r.say, { urgent: true });
  } catch (err) {
    out.textContent = `Could not ask. ${err.message}`;
    announce('Could not ask. Check your connection and try again.', { urgent: true });
  }
});

qs('#change-building').addEventListener('click', () => { show('building'); });

// ---------- start guidance (one tap unlocks speech, motion and camera) ----------
qs('#start-btn').addEventListener('click', async () => {
  if (!G.destReady) return;
  speech.unlock();
  const motion = startCompass(); // iOS permission prompt must come from this tap
  showAlert('');
  try {
    G.stream = await openCamera(qs('#video'));
  } catch (e) {
    const msg = e instanceof CameraError ? e.message : 'The camera could not start.';
    showAlert('Camera problem', msg);
    buzz('stop');
    return;
  }
  const m = await motion;
  if (m === 'denied') announce('Motion sensor access was refused, so turn hints between position checks are off. Directions still work.');
  G.releaseWake = await keepAwake();
  G.running = true;
  G.failures = 0;
  G.lastState = '';
  G.lastSay = '';
  show('guide');
  setInstruction('Hold the phone up, camera facing forward. Finding where you are.');
  announce(`Starting guidance to ${G.destLabel || 'your destination'}. Hold the phone up with the camera facing forward.`, { urgent: true });
  loop();
});

function setInstruction(text) {
  qs('#instruction-text').textContent = text;
}

// ---------- frame loop ----------
async function loop() {
  if (!G.running) return;
  const started = performance.now();
  if (!G.inFlight) {
    G.inFlight = true;
    try {
      const blob = await captureJpeg(qs('#video'));
      if (blob) {
        const fields = { t_ms: Date.now() };
        if (compass.heading != null) fields.heading_deg = Math.round(compass.heading * 10) / 10;
        const headingAtSend = compass.heading;
        const r = await api.postImage(`/visits/${encodeURIComponent(G.visitId)}/frame`, blob, fields);
        onResponse(r, headingAtSend);
      } else if (G.stream && G.stream.getVideoTracks().every((t) => t.readyState === 'ended')) {
        onCameraEnded();
      }
    } catch (e) {
      onFailure(e);
    } finally {
      G.inFlight = false;
    }
  }
  if (!G.running || document.visibilityState === 'hidden') return;
  clearTimeout(G.timer);
  G.timer = setTimeout(loop, Math.max(0, FRAME_MS - (performance.now() - started)));
}

function onResponse(r, headingAtSend) {
  if (G.failures >= 3) announce('Connected again.');
  G.failures = 0;
  showAlert('');

  const state = r.state || 'locating';
  const info = STATES[state] || { text: state.replace(/_/g, ' '), icon: 'dot' };
  setStateUi(info, state);

  const say = (r.say || '').trim();
  const stateChanged = state !== G.lastState;
  const urgent = !!info.urgent || r.haptic === 'stop';

  if (say) setInstruction(say);
  if (say && say !== G.lastSay) {
    announce(say, { urgent });
  } else if (stateChanged && !say && G.lastState) {
    // Every state change is announced, even when the server sent no sentence.
    announce(info.text, { urgent });
  }
  G.lastSay = say || G.lastSay;

  // Haptics: on a new instruction, or when the haptic itself changes.
  const h = r.haptic || 'none';
  if (h !== 'none' && (h !== G.lastHaptic || (say && say !== G.lastSpokenForHaptic))) buzz(h);
  G.lastHaptic = h;
  G.lastSpokenForHaptic = say;

  // Route facts, written out in words as well as numbers.
  if (r.route) {
    const m = r.route.remaining_m;
    qs('#fact-remaining').textContent = Number.isFinite(m) ? `${m < 10 ? m.toFixed(1) : Math.round(m)} metres` : 'Not known yet';
    const t = r.route.next_turn_deg;
    let next = r.route.next_label || '';
    if (!next && Number.isFinite(t)) next = Math.abs(t) < 20 ? 'Straight on' : t > 0 ? 'Turn right' : 'Turn left';
    qs('#fact-next').textContent = next || 'Not known yet';
  }

  if (r.helper_text) {
    qs('#helper-note').hidden = false;
    qs('#helper-text').textContent = r.helper_text;
    announce(`Message from your helper: ${r.helper_text}`, { keep: true });
  }

  updateCoach(r, state, h, headingAtSend);

  if (state === 'arrived') {
    stopGuidance(false);
    buzz('arrived');
    qs('#end-text').textContent = `Waymark thinks you have arrived at ${G.destLabel || 'your destination'}.`;
    qs('#end-yes').textContent = "Yes, I'm there";
    show('end');
  }
  G.lastState = state;
}

function setStateUi(info, state) {
  qs('#state-text').textContent = info.text;
  qs('#state-icon').innerHTML = ICON[info.icon] || ICON.dot;
  qs('#state').dataset.urgent = String(!!info.urgent);
  qs('#state').dataset.state = state;
}

function onFailure(e) {
  G.failures += 1;
  if (e.status === 404) {
    stopGuidance(true);
    showAlert('Visit ended', 'The server no longer has this visit. Choose your building again to start over.');
    show('building');
    return;
  }
  // After about three seconds without answers, tell the visitor to stop.
  if (G.failures === 3) {
    setStateUi(STATES.offline, 'offline');
    setInstruction('Stop and wait. I have lost the connection to the server.');
    announce('Stop and wait. I have lost the connection to the server. I will keep trying.', { urgent: true });
    buzz('stop');
    G.lastState = 'offline';
  } else if (G.failures > 3 && G.failures % 15 === 0) {
    announce('Still no connection. Stay where you are, or call a helper by phone.', { urgent: true });
  }
}

function onCameraEnded() {
  stopGuidance(false);
  showAlert('Camera stopped', 'The camera was turned off, perhaps by another app. Stop here, then tap Start guidance again.');
  buzz('stop');
  show('dest');
}

function stopGuidance(endCamera = true) {
  G.running = false;
  clearTimeout(G.timer);
  G.coach = null;
  if (endCamera) {
    stopCamera(G.stream);
    G.stream = null;
    G.releaseWake?.();
    G.releaseWake = null;
  }
}

// The browser pauses the camera while the screen is off or another app is open.
document.addEventListener('visibilitychange', () => {
  if (!G.running) return;
  if (document.visibilityState === 'hidden') {
    clearTimeout(G.timer);
  } else {
    announce('Guidance resumed. Hold still while I find you again.', { urgent: true });
    G.lastState = '';
    loop();
  }
});

// ---------- compass coaching between fixes ----------
// When a confident fix arrives with a turn to make now, remember the offset
// between the compass and the server's yaw. As the visitor turns, compare the
// estimated yaw (compass minus offset) with the yaw they should end up facing.
function updateCoach(r, state, haptic, headingAtSend) {
  const fix = r.fix;
  const turn = r.route?.next_turn_deg;
  const turnNow = haptic === 'left' || haptic === 'right';
  if (state !== 'guiding' || !fix || !(fix.confidence >= CONFIDENT) || headingAtSend == null || !Number.isFinite(fix.yaw_deg)) {
    G.coach = null;
    return;
  }
  // No turn instruction on this frame: an active session simply runs until it expires.
  if (!turnNow || !Number.isFinite(turn) || Math.abs(turn) < 25) return;
  // A new turn instruction starts a fresh coaching session; repeats keep the old one.
  if (G.coach && Math.abs(angleDiff(G.coach.targetYaw, fix.yaw_deg + turn)) < 20 && Date.now() - G.coach.at < 8000) return;
  G.coach = {
    offset: angleDiff(headingAtSend, fix.yaw_deg),
    fixYaw: fix.yaw_deg,
    targetYaw: fix.yaw_deg + turn,
    turn,
    at: Date.now(),
    lastKeep: 0,
    done: false,
  };
}

let lastCoachCheck = 0;
onHeading((heading) => {
  const c = G.coach;
  if (!c || c.done || !G.running) return;
  const now = Date.now();
  if (now - lastCoachCheck < 200) return;
  lastCoachCheck = now;
  if (now - c.at > 8000) { G.coach = null; return; } // too old to trust
  const estYaw = heading - c.offset;
  const turned = angleDiff(estYaw, c.fixYaw);
  const remaining = angleDiff(c.targetYaw, estYaw);
  if (Math.abs(remaining) <= 15) {
    c.done = true;
    announce('Stop. Straight ahead.', { urgent: true });
    buzz('stop');
  } else if (Math.sign(remaining) !== Math.sign(c.turn) && Math.abs(remaining) > 30) {
    if (now - c.lastKeep > 2500) { c.lastKeep = now; announce(`Too far. Turn back ${c.turn > 0 ? 'left' : 'right'} a little.`, { urgent: true }); }
  } else if (Math.sign(turned) === Math.sign(c.turn) && Math.abs(turned) >= 20 && now - c.lastKeep > 2500) {
    c.lastKeep = now;
    announce('Keep turning.');
  }
});

// ---------- guiding controls ----------
function repeatLast() {
  const text = speech.last || qs('#instruction-text').textContent;
  if (G.mode === 'voice') speech.say(text, { urgent: true });
  else announce(text, { urgent: true });
}
qs('#instruction').addEventListener('click', repeatLast);
qs('#repeat-btn').addEventListener('click', repeatLast);

// Double tap anywhere that is not a control repeats the last instruction.
// (With VoiceOver or TalkBack on, a double tap activates the focused control,
// so the large instruction button above repeats too.)
let lastTap = { t: 0, x: 0, y: 0 };
document.addEventListener('touchend', (e) => {
  if (qs('#scr-guide').hidden) return;
  if (e.target.closest('button, a, input, textarea, select, label')) return;
  const p = e.changedTouches[0];
  const now = Date.now();
  if (now - lastTap.t < 350 && Math.hypot(p.clientX - lastTap.x, p.clientY - lastTap.y) < 40) {
    e.preventDefault();
    repeatLast();
    lastTap.t = 0;
  } else {
    lastTap = { t: now, x: p.clientX, y: p.clientY };
  }
}, { passive: false });
document.addEventListener('dblclick', (e) => {
  if (qs('#scr-guide').hidden || e.target.closest('button, a, input, textarea, select')) return;
  repeatLast();
});

qs('#where-btn').addEventListener('click', async () => {
  try {
    const r = await api.post(`/visits/${encodeURIComponent(G.visitId)}/ask`, { text: 'Where am I?' }, { retries: 1 });
    if (r.say) { setInstruction(r.say); announce(r.say, { urgent: true }); }
  } catch {
    announce('I could not reach the server to check where you are. Stay where you are.', { urgent: true });
  }
});

qs('#help-btn').addEventListener('click', async () => {
  const btn = qs('#help-btn');
  btn.disabled = true;
  try {
    await api.post(`/visits/${encodeURIComponent(G.visitId)}/help`, {}, { retries: 2 });
    setStateUi(STATES.helper, 'helper');
    setInstruction('A helper has been asked to come to you. Please stay where you are.');
    announce('A helper has been asked to come to you. Please stay where you are.', { urgent: true });
    buzz('stop');
  } catch {
    announce('The help request did not go through. Please stay where you are and ask someone nearby, or try again.', { urgent: true });
  } finally {
    setTimeout(() => { btn.disabled = false; }, 3000);
  }
});

qs('#end-btn').addEventListener('click', () => {
  stopGuidance(false);
  qs('#end-text').textContent = G.destLabel ? `Your destination was ${G.destLabel}.` : '';
  show('end');
});

qs('#end-back').addEventListener('click', () => {
  if (!G.stream) { show('dest'); return; }
  G.running = true;
  show('guide');
  announce('Guidance resumed.');
  loop();
});

async function endVisit(arrived, notes = '') {
  stopGuidance(true);
  try {
    if (G.visitId) await api.post(`/visits/${encodeURIComponent(G.visitId)}/end`, { arrived, notes }, { retries: 2 });
  } catch { /* the visit still ends on this phone */ }
  qs('#end-question').hidden = true;
  qs('#end-done').hidden = false;
  show('end');
  announce('Guidance has ended. Thank you for using Waymark.');
  qs('#restart-btn').focus();
}
qs('#end-yes').addEventListener('click', () => endVisit(true));
qs('#end-no').addEventListener('click', () => endVisit(false));
qs('#restart-btn').addEventListener('click', () => {
  qs('#end-question').hidden = false;
  qs('#end-done').hidden = true;
  qs('#helper-note').hidden = true;
  G.visitId = null;
  G.destLabel = '';
  G.evalArrived = null;
  if (G.building) chooseBuilding(G.building); else show('building');
});

// ---------- evaluator panel ----------
let evalMode = params().get('eval') === '1' || store('evalMode') === true;
let titleTaps = [];
qs('#go-title').addEventListener('click', () => {
  const now = Date.now();
  titleTaps = titleTaps.filter((t) => now - t < 2500);
  titleTaps.push(now);
  if (titleTaps.length >= 5) {
    titleTaps = [];
    evalMode = !evalMode;
    store('evalMode', evalMode);
    updateEvalVisibility();
    announce(evalMode ? 'Evaluator panel shown.' : 'Evaluator panel hidden.');
  }
});

function updateEvalVisibility() {
  qs('#eval').hidden = !(evalMode && G.visitId);
}

qs('#cp-grid').innerHTML = Array.from({ length: 12 }, (_, i) =>
  `<button type="button" data-cp="${i + 1}" aria-label="Checkpoint ${i + 1}">${i + 1}</button>`).join('');
qs('#cp-grid').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-cp]');
  if (!b || !G.visitId) return;
  const id = Number(b.dataset.cp);
  const status = qs('#cp-status');
  try {
    await api.post(`/visits/${encodeURIComponent(G.visitId)}/checkpoint`, { checkpoint_id: id }, { retries: 1 });
    b.dataset.done = 'true';
    b.setAttribute('aria-label', `Checkpoint ${id}, recorded`);
    status.textContent = `Checkpoint ${id} recorded at ${new Date().toLocaleTimeString()}.`;
    if (navigator.vibrate) navigator.vibrate(40);
  } catch (err) {
    status.textContent = `Checkpoint ${id} was not recorded. ${err.message}`;
  }
});

qsa('#eval-end [data-arrived]').forEach((b) => b.addEventListener('click', () => {
  G.evalArrived = b.dataset.arrived === 'true';
  qsa('#eval-end [data-arrived]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
}));
qs('#eval-end').addEventListener('submit', (e) => {
  e.preventDefault();
  if (G.evalArrived === null) { qs('#cp-status').textContent = 'Choose Arrived or Not arrived first.'; return; }
  endVisit(G.evalArrived, qs('#eval-notes').value.trim());
});

// ---------- boot ----------
loadBuildings();

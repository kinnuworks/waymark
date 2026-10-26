// Landing page: the "Try it here" agent simulator, live results from /api/eval
// and server facts from /api/health.
import { api, escapeHtml } from './api.js';

// ---------- Try it here ----------
// Mirrors the order of checks in server/waymark/agent/guide.py (_decide and _escalate).
function decide({ conf, stairs, off, misses }) {
  const c = conf.toFixed(2);
  if (conf < 0.35) {
    if (misses <= 2) {
      return { rule: 'pan', state: 'low_confidence', tone: 'warn', say: 'Slowly pan the phone left and right.', haptic: 'none',
        why: `No confident fix (confidence ${c}). A slow pan gives the camera more to match.` };
    }
    if (misses < 6) {
      return { rule: 'stopwalk', state: 'low_confidence', tone: 'stop', say: 'Please stop walking for a moment while I find where you are.', haptic: 'stop',
        why: `${misses} frames in a row without a confident fix.` };
    }
    return { rule: 'helper', state: 'helper', tone: 'stop', say: "I can't place you reliably. Please stay where you are. I'm calling your helper.", haptic: 'stop',
      why: 'Six frames without a fix, so it hands over to a person.' };
  }
  if (off) {
    return { rule: 'off', state: 'off_route', tone: 'warn', say: "You've left the route. Finding a new one.", haptic: 'stop',
      why: 'More than the allowed distance from the route, so it re-plans. After three re-routes it prefers fewer turns.' };
  }
  if (stairs) {
    if (conf < 0.8) {
      return { rule: 'stairs', state: 'stairs_caution', tone: 'stop', say: 'Stairs ahead, about 2 metres. Stop and find the handrail with your cane.', haptic: 'stop',
        why: `Stairs 2 m ahead and confidence ${c} is below 0.8, so guidance pauses.` };
    }
    return { rule: 'stairs', state: 'guiding', tone: 'ok', say: 'Stairs ahead in about 2 metres. The handrail is the safest guide.', haptic: 'stop',
      why: `Stairs 2 m ahead with a confident fix (${c}), so it warns once and carries on.` };
  }
  const base = 'Walk straight for 6 metres, then turn left.';
  if (conf < 0.6) {
    return { rule: 'slow', state: 'guiding', tone: 'warn', say: `Slowly. ${base}`, haptic: 'straight',
      why: `On route, but confidence ${c} is below 0.6, so it asks for a slower pace.` };
  }
  return { rule: 'guide', state: 'guiding', tone: 'ok', say: base, haptic: 'straight', why: 'On route with a confident fix. Next turn in 6 m.' };
}

const STATE_LABEL = { guiding: 'Guiding', low_confidence: 'Not sure', helper: 'Calling helper', off_route: 'Re-routing', stairs_caution: 'Stop' };

const form = document.getElementById('try-form');
if (form) {
  const $ = (id) => document.getElementById(id);
  const confIn = $('t-conf-in');
  const stairsIn = $('t-stairs');
  const offIn = $('t-off');
  const missGroup = $('t-miss');
  const missBtns = [...missGroup.querySelectorAll('button')];
  const hear = $('t-hear');
  let misses = 1;
  let lastSay = '';

  function render() {
    const conf = Number(confIn.value);
    const lowConf = conf < 0.35;
    // A confident fix resets the miss streak on the server, so the control resets too.
    if (!lowConf && misses !== 1) {
      misses = 1;
      missBtns.forEach((o) => o.setAttribute('aria-pressed', String(o.dataset.v === '1')));
    }
    missBtns.forEach((b) => { b.disabled = !lowConf; });
    const d = decide({ conf, stairs: stairsIn.checked, off: offIn.checked, misses });

    $('t-conf-out').textContent = conf.toFixed(2);
    $('p-conf').textContent = conf.toFixed(2);
    const bar = $('p-bar');
    bar.dataset.tone = conf < 0.35 ? 'stop' : conf < 0.6 ? 'warn' : 'ok';
    $('p-bar-fill').style.setProperty('--v', `${Math.round(conf * 100)}%`);

    const st = $('p-state');
    st.textContent = STATE_LABEL[d.state] || d.state;
    st.dataset.tone = d.tone;
    $('p-say').textContent = d.say;
    $('p-haptic').textContent = d.haptic;
    const pulse = $('p-pulse');
    pulse.style.visibility = d.haptic === 'none' ? 'hidden' : 'visible';
    pulse.querySelector('path').setAttribute('d', d.haptic === 'stop' ? 'M7 7h10v10H7z' : 'M12 19V5M5 12l7-7 7 7');

    $('r-state').textContent = d.state;
    $('r-haptic').textContent = d.haptic;
    $('r-why').textContent = d.why;
    // Only announce the spoken line when it changes, so dragging the slider isn't noisy.
    if (d.say !== lastSay) { $('r-say').textContent = d.say; lastSay = d.say; }

    document.querySelectorAll('#r-rules li').forEach((li) => li.classList.toggle('on', li.dataset.rule === d.rule));
  }

  confIn.addEventListener('input', render);
  stairsIn.addEventListener('change', render);
  offIn.addEventListener('change', render);
  missBtns.forEach((b) => b.addEventListener('click', () => {
    misses = Number(b.dataset.v);
    missBtns.forEach((o) => o.setAttribute('aria-pressed', String(o === b)));
    render();
  }));
  form.addEventListener('submit', (e) => e.preventDefault());

  if ('speechSynthesis' in window) {
    hear.hidden = false;
    hear.addEventListener('click', () => {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(lastSay);
      u.lang = 'en-GB';
      u.rate = 0.95;
      speechSynthesis.speak(u);
    });
  }
  render();
}

// ---------- results ----------
const resultsEl = document.getElementById('results-body');

function isNumeric(v) {
  return typeof v === 'number' || (typeof v === 'string' && /^[-+]?[\d.,]+\s*(%|ms|m|px|s)?$/.test(v.trim()));
}

function fmt(v) {
  if (v === null || v === undefined || v === '') return 'n/a';
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return v.toLocaleString();
    return Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(Math.abs(v) >= 10 ? 2 : 3).replace(/0+$/, '').replace(/\.$/, '');
  }
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

// Renders {sections:[{title, note, columns, rows}]} without knowing the content.
function renderResults(data) {
  const sections = Array.isArray(data?.sections) ? data.sections.filter((s) => s && (s.rows?.length || s.title)) : [];
  if (!sections.length) {
    resultsEl.innerHTML = '<p class="results-note">No evaluation tables on this server yet. They appear here as soon as a run finishes.</p>';
    return;
  }
  resultsEl.innerHTML = '<div class="result-cards">' + sections.map((s, i) => {
    let cols = Array.isArray(s.columns) ? s.columns : [];
    const rows = Array.isArray(s.rows) ? s.rows : [];
    // Rows may be arrays or objects keyed by column name.
    if (!cols.length && rows.length && !Array.isArray(rows[0]) && typeof rows[0] === 'object') cols = Object.keys(rows[0]);
    const cells = rows.map((r) => (Array.isArray(r) ? r : cols.map((c) => r?.[c])));
    const numCol = cols.map((_, ci) => cells.length > 0 && cells.every((r) => r[ci] == null || isNumeric(r[ci])));
    const head = cols.map((c, ci) => `<th scope="col"${numCol[ci] && ci > 0 ? ' class="num"' : ''}>${escapeHtml(c)}</th>`).join('');
    const body = cells.map((r) => `<tr>${r.map((v, ci) => (ci === 0
      ? `<th scope="row">${escapeHtml(fmt(v))}</th>`
      : `<td${numCol[ci] ? ' class="num"' : ''}>${escapeHtml(fmt(v))}</td>`)).join('')}</tr>`).join('');
    const tableId = `result-${i}`;
    return `<article class="card result-card">
      <header><h4 class="t-title" id="${tableId}">${escapeHtml(s.title || 'Results')}</h4>${s.note ? `<p class="t-small">${escapeHtml(s.note)}</p>` : ''}</header>
      ${cells.length ? `<div class="table-wrap" tabindex="0" role="region" aria-labelledby="${tableId}"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>` : '<p class="t-small" style="padding:0 20px 18px">No rows yet.</p>'}
    </article>`;
  }).join('') + '</div>';
}

if (resultsEl) {
  api.get('/eval', { retries: 1 })
    .then(renderResults)
    .catch(() => {
      resultsEl.innerHTML = '<p class="results-note">The results server did not answer just now. Reload the page to try again.</p>';
    })
    .finally(() => resultsEl.setAttribute('aria-busy', 'false'));
}

// ---------- server facts ----------
const NICE = { aliked: 'ALIKED', lightglue: 'LightGlue', kleidicv: 'KleidiCV' };
const nice = (k) => NICE[String(k).toLowerCase()] || k;
const healthEl = document.getElementById('health');
if (healthEl) {
  api.get('/health', { retries: 1 })
    .then((h) => {
      const chips = [];
      if (h.opencv) chips.push(`OpenCV ${h.opencv}`);
      if (h.arch) chips.push(nice(h.arch));
      if (h.instance) chips.push(h.instance);
      for (const [k, v] of Object.entries(h.build || {})) if (v) chips.push(typeof v === 'boolean' ? nice(k) : `${nice(k)} ${v}`);
      const feats = Object.entries(h.features || {}).filter(([, v]) => v).map(([, v]) => nice(v));
      if (feats.length) chips.push(feats.join(' + '));
      healthEl.innerHTML = chips.map((c) => `<span class="chip chip--soft">${escapeHtml(c)}</span>`).join('') ||
        '<span class="chip chip--soft">Server online</span>';
    })
    .catch(() => {
      healthEl.innerHTML = '<span class="chip chip--soft">Server offline</span>';
    });
}

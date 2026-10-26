// Landing page: live results from /api/eval and server facts from /api/health.
import { api, escapeHtml } from './api.js';

const goUrl = document.getElementById('go-url');
if (goUrl) goUrl.textContent = `${location.origin}/go.html`;

// ---------- results ----------
const resultsEl = document.getElementById('results-body');

function isNumeric(v) {
  return typeof v === 'number' || (typeof v === 'string' && /^[-+]?[\d.,]+\s*(%|ms|m|px|s)?$/.test(v.trim()));
}

function fmt(v) {
  if (v === null || v === undefined || v === '') return '–';
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
    resultsEl.innerHTML = '<div class="empty"><strong>Results are being measured.</strong>The evaluation runs in real buildings. Tables will appear here when the first run finishes.</div>';
    return;
  }
  resultsEl.innerHTML = sections.map((s, i) => {
    let cols = Array.isArray(s.columns) ? s.columns : [];
    const rows = Array.isArray(s.rows) ? s.rows : [];
    // Rows may be arrays or objects keyed by column name.
    if (!cols.length && rows.length && !Array.isArray(rows[0]) && typeof rows[0] === 'object') cols = Object.keys(rows[0]);
    const cells = rows.map((r) => (Array.isArray(r) ? r : cols.map((c) => r?.[c])));
    const numCol = cols.map((_, ci) => cells.length > 0 && cells.every((r) => r[ci] == null || isNumeric(r[ci])));
    const head = cols.map((c, ci) => `<th scope="col"${numCol[ci] && ci > 0 ? ' class="num"' : ''}>${escapeHtml(c)}</th>`).join('');
    const body = cells.map((r) => `<tr>${r.map((v, ci) => (ci === 0
      ? `<th scope="row" style="text-transform:none;letter-spacing:0;font-size:14.5px;background:none;color:var(--ink);font-weight:600">${escapeHtml(fmt(v))}</th>`
      : `<td${numCol[ci] ? ' class="num"' : ''}>${escapeHtml(fmt(v))}</td>`)).join('')}</tr>`).join('');
    const tableId = `result-${i}`;
    return `<article class="card result-card">
      <header><h3 class="t-title" id="${tableId}">${escapeHtml(s.title || 'Results')}</h3>${s.note ? `<p class="t-small">${escapeHtml(s.note)}</p>` : ''}</header>
      ${cells.length ? `<div class="table-wrap" tabindex="0" role="region" aria-labelledby="${tableId}"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>` : '<p class="t-small" style="padding:0 22px 20px">No rows yet.</p>'}
    </article>`;
  }).join('');
}

api.get('/eval', { retries: 1 })
  .then(renderResults)
  .catch(() => {
    resultsEl.innerHTML = '<div class="empty"><strong>Results are being measured.</strong>The results server did not answer just now. Reload the page to try again.</div>';
  })
  .finally(() => resultsEl.setAttribute('aria-busy', 'false'));

// ---------- server facts ----------
const NICE = { aliked: 'ALIKED', lightglue: 'LightGlue', kleidicv: 'KleidiCV' };
const nice = (k) => NICE[String(k).toLowerCase()] || k;
const healthEl = document.getElementById('health');
api.get('/health', { retries: 1 })
  .then((h) => {
    const chips = [];
    if (h.opencv) chips.push(`OpenCV ${h.opencv}`);
    if (h.arch) chips.push(nice(h.arch));
    if (h.instance) chips.push(h.instance);
    for (const [k, v] of Object.entries(h.build || {})) if (v) chips.push(typeof v === 'boolean' ? nice(k) : `${nice(k)} ${v}`);
    const feats = Object.entries(h.features || {}).filter(([, v]) => v).map(([k]) => nice(k));
    if (feats.length) chips.push(feats.join(' + '));
    healthEl.innerHTML = chips.map((c) => `<span class="chip chip--soft">${escapeHtml(c)}</span>`).join('') ||
      '<span class="chip chip--soft">Server online</span>';
  })
  .catch(() => {
    healthEl.innerHTML = '<span class="chip chip--soft">Server offline</span>';
  });

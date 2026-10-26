// Canvas renderer for the map JSON returned by GET /api/buildings/{bid}/map.
// World frame (metres): x to the right along the entrance board, z away from
// the board into the building. Drawn top-down with +x right and +z up, so the
// entrance board sits at the bottom and the building opens upwards.
// Yaw: 0 deg faces +z, positive turns towards +x (clockwise, like a compass).

// Okabe-Ito palette: distinguishable with the common colour vision deficiencies.
// Orange is left until last because the route line is orange.
export const WALKER_COLOURS = ['#0072B2', '#009E73', '#CC79A7', '#56B4E9', '#8C6D31', '#6A3D9A', '#D55E00', '#E69F00'];

const KIND_GLYPH = { room: 'R', door: 'D', stairs: 'St', lift: 'L', restroom: 'WC', exit: 'Ex', desk: 'i', other: '•' };
export const KIND_LABEL = { room: 'Room', door: 'Door', stairs: 'Stairs', lift: 'Lift', restroom: 'Restroom', exit: 'Exit', desk: 'Desk', other: 'Other' };

export function walkerColour(wid, index, known = {}) {
  if (known[wid]) return known[wid];
  if (typeof index === 'number') return WALKER_COLOURS[index % WALKER_COLOURS.length];
  let h = 0;
  for (const c of String(wid)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return WALKER_COLOURS[h % WALKER_COLOURS.length];
}

// Accepts the bounds shapes we might reasonably get from the server.
export function normaliseBounds(b) {
  if (!b) return null;
  let r = null;
  if (Array.isArray(b) && b.length >= 4) r = { minX: b[0], minZ: b[1], maxX: b[2], maxZ: b[3] };
  else if (b.min_x !== undefined) r = { minX: b.min_x, maxX: b.max_x, minZ: b.min_z, maxZ: b.max_z };
  else if (b.xmin !== undefined) r = { minX: b.xmin, maxX: b.xmax, minZ: b.zmin, maxZ: b.zmax };
  else if (Array.isArray(b.x) && Array.isArray(b.z)) r = { minX: b.x[0], maxX: b.x[1], minZ: b.z[0], maxZ: b.z[1] };
  else if (Array.isArray(b.min) && Array.isArray(b.max)) r = { minX: b.min[0], minZ: b.min[1], maxX: b.max[0], maxZ: b.max[1] };
  if (!r || ![r.minX, r.maxX, r.minZ, r.maxZ].every(Number.isFinite)) return null;
  if (r.maxX - r.minX < 0.01 || r.maxZ - r.minZ < 0.01) return null;
  return r;
}

const pt = (p) => (Array.isArray(p) ? { x: p[0], z: p[1] } : p);

function extentOf(map, overlay) {
  const e = { minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity };
  const add = (x, z) => {
    if (!Number.isFinite(x) || !Number.isFinite(z)) return;
    e.minX = Math.min(e.minX, x); e.maxX = Math.max(e.maxX, x);
    e.minZ = Math.min(e.minZ, z); e.maxZ = Math.max(e.maxZ, z);
  };
  const b = normaliseBounds(map?.bounds);
  if (b) { add(b.minX, b.minZ); add(b.maxX, b.maxZ); }
  (map?.walls || []).forEach((w) => { add(w[0], w[1]); add(w[2], w[3]); });
  (map?.nodes || []).forEach((n) => add(n.x, n.z));
  (map?.pois || []).forEach((p) => add(p.x, p.z));
  Object.values(map?.trajectories || {}).forEach((t) => t.forEach((p) => { const q = pt(p); add(q.x, q.z); }));
  if (overlay?.me) add(overlay.me.x, overlay.me.z);
  if (overlay?.target) add(overlay.target.x, overlay.target.z);
  (overlay?.route || []).forEach((p) => { const q = pt(p); add(q.x, q.z); });
  Object.values(overlay?.poses || {}).forEach((p) => p && add(p.x, p.z));
  add(0, 0); // always keep the entrance board in view
  if (!Number.isFinite(e.minX)) return { minX: -5, maxX: 5, minZ: -1, maxZ: 9 };
  // Never zoom in tighter than 8 m across, and leave a margin.
  const pad = 1;
  const cx = (e.minX + e.maxX) / 2, cz = (e.minZ + e.maxZ) / 2;
  const hw = Math.max(4, (e.maxX - e.minX) / 2 + pad), hz = Math.max(4, (e.maxZ - e.minZ) / 2 + pad);
  return { minX: cx - hw, maxX: cx + hw, minZ: cz - hz, maxZ: cz + hz };
}

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

export class MapView {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.opts = { padding: 20, nodes: true, labels: true, grid: true, ...opts };
    this.map = null;
    this.overlay = {};
    this.background = null; // HTMLImageElement stretched over map.bounds
    this.walkerColours = {};
    this._raf = 0;
    if ('ResizeObserver' in window) {
      this._ro = new ResizeObserver(() => this.draw());
      this._ro.observe(canvas);
    } else {
      window.addEventListener('resize', () => this.draw());
    }
    window.matchMedia?.('(prefers-color-scheme: dark)').addEventListener?.('change', () => this.draw());
  }

  setMap(map) { this.map = map; this.draw(); }
  setOverlay(overlay) { this.overlay = overlay || {}; this.draw(); }
  setBackground(img) { this.background = img; this.draw(); }
  setWalkerColours(c) { this.walkerColours = c || {}; this.draw(); }

  draw() {
    cancelAnimationFrame(this._raf);
    this._raf = requestAnimationFrame(() => this._draw());
  }

  _size() {
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
    }
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w, h };
  }

  _fit(w, h) {
    const v = extentOf(this.map, this.overlay);
    const p = this.opts.padding;
    const s = Math.min((w - 2 * p) / (v.maxX - v.minX), (h - 2 * p) / (v.maxZ - v.minZ));
    const ox = (w - s * (v.maxX - v.minX)) / 2;
    const oy = (h - s * (v.maxZ - v.minZ)) / 2;
    this.view = v;
    this.scale = s;
    this.toScreen = (x, z) => [ox + (x - v.minX) * s, oy + (v.maxZ - z) * s];
  }

  _draw() {
    const { w, h } = this._size();
    const ctx = this.ctx;
    const col = {
      bg: cssVar('--map-bg', '#FBFBFC'),
      wall: cssVar('--map-wall', '#2C2C30'),
      edge: cssVar('--map-edge', 'rgba(0,0,0,.22)'),
      node: cssVar('--map-node', '#86868B'),
      route: cssVar('--map-route', '#C77700'),
      me: cssVar('--map-me', '#0A66D8'),
      ink: cssVar('--ink', '#1D1D1F'),
      ink3: cssVar('--ink-3', '#6E6E73'),
      line: cssVar('--line', 'rgba(0,0,0,.08)'),
      surface: cssVar('--surface', '#FFFFFF'),
      accent: cssVar('--accent', '#F7A028'),
    };
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = col.bg;
    ctx.fillRect(0, 0, w, h);
    this._fit(w, h);
    const S = this.toScreen;
    const map = this.map || {};
    const ov = this.overlay || {};

    // Rendered floor plan underneath, stretched to map.bounds.
    const b = normaliseBounds(map.bounds);
    if (this.background && b && this.background.naturalWidth) {
      const [x0, y0] = S(b.minX, b.maxZ);
      const [x1, y1] = S(b.maxX, b.minZ);
      ctx.globalAlpha = 0.9;
      ctx.drawImage(this.background, x0, y0, x1 - x0, y1 - y0);
      ctx.globalAlpha = 1;
    }

    // Metre grid: 1 m when zoomed in, 5 m when zoomed out.
    if (this.opts.grid) {
      const step = this.scale >= 14 ? 1 : 5;
      ctx.strokeStyle = col.line;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let x = Math.ceil(this.view.minX / step) * step; x <= this.view.maxX; x += step) {
        const [sx] = S(x, 0); ctx.moveTo(Math.round(sx) + 0.5, 0); ctx.lineTo(Math.round(sx) + 0.5, h);
      }
      for (let z = Math.ceil(this.view.minZ / step) * step; z <= this.view.maxZ; z += step) {
        const [, sy] = S(0, z); ctx.moveTo(0, Math.round(sy) + 0.5); ctx.lineTo(w, Math.round(sy) + 0.5);
      }
      ctx.stroke();
    }

    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    // Route graph
    if (this.opts.nodes && map.nodes?.length) {
      const byId = new Map(map.nodes.map((n) => [String(n.id), n]));
      ctx.strokeStyle = col.edge;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      (map.edges || []).forEach(([a, c]) => {
        const na = byId.get(String(a)), nb = byId.get(String(c));
        if (!na || !nb) return;
        ctx.moveTo(...S(na.x, na.z)); ctx.lineTo(...S(nb.x, nb.z));
      });
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = col.node;
      map.nodes.forEach((n) => { const [x, y] = S(n.x, n.z); ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.fill(); });
    }

    // Walls
    if (map.walls?.length) {
      ctx.strokeStyle = col.wall;
      ctx.lineWidth = Math.max(2, Math.min(4, this.scale * 0.15));
      ctx.beginPath();
      map.walls.forEach((wl) => { ctx.moveTo(...S(wl[0], wl[1])); ctx.lineTo(...S(wl[2], wl[3])); });
      ctx.stroke();
    }

    // Walker trajectories, one colour each
    const traj = map.trajectories || {};
    Object.keys(traj).forEach((wid, i) => {
      const pts = traj[wid];
      if (!pts || pts.length < 2) return;
      const c = walkerColour(wid, i, this.walkerColours);
      ctx.strokeStyle = c;
      ctx.globalAlpha = ov.highlightWalker && ov.highlightWalker !== wid ? 0.35 : 0.9;
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      pts.forEach((p, k) => { const q = pt(p); const [x, y] = S(q.x, q.z); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
      ctx.globalAlpha = 1;
    });

    // Entrance board at the origin
    {
      const [x0, y0] = S(-0.3, 0);
      const [x1] = S(0.3, 0);
      ctx.strokeStyle = col.accent;
      ctx.lineWidth = 6;
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(Math.max(x1, x0 + 10), y0); ctx.stroke();
      if (this.opts.labels) this._label('Entrance board', x0, y0 + 16, col, 'left');
    }

    // Planned route
    const route = ov.route || [];
    if (route.length > 1) {
      ctx.strokeStyle = col.route;
      ctx.lineWidth = 6;
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      route.forEach((p, k) => { const q = pt(p); const [x, y] = S(q.x, q.z); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Target (coordinator assignment or destination)
    if (ov.target && Number.isFinite(ov.target.x)) {
      const [x, y] = S(ov.target.x, ov.target.z);
      ctx.strokeStyle = col.route;
      ctx.lineWidth = 2.5;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.arc(x, y, 13, 0, Math.PI * 2); ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fillStyle = col.route; ctx.fill();
      if (ov.target.label && this.opts.labels) this._label(ov.target.label, x + 16, y + 4, col, 'left');
    }

    // Points of interest
    (map.pois || []).forEach((p) => {
      if (!Number.isFinite(p.x) || !Number.isFinite(p.z)) return;
      const [x, y] = S(p.x, p.z);
      const g = KIND_GLYPH[p.kind] || KIND_GLYPH.other;
      ctx.beginPath(); ctx.arc(x, y, 11, 0, Math.PI * 2);
      ctx.fillStyle = col.surface; ctx.fill();
      ctx.lineWidth = 2; ctx.strokeStyle = col.ink; ctx.stroke();
      ctx.fillStyle = col.ink;
      ctx.font = `700 ${g.length > 1 ? 9 : 11}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(g, x, y + 0.5);
      if (this.opts.labels && p.label) this._label(p.label, x + 15, y, col, 'left');
    });

    // Other walkers' latest poses
    Object.entries(ov.poses || {}).forEach(([wid, p], i) => {
      if (!p) return;
      this._pose(p, walkerColour(wid, i, this.walkerColours), col, 0.9);
    });

    // This device / the visitor
    if (ov.me && Number.isFinite(ov.me.x)) this._pose(ov.me, ov.me.colour || col.me, col, ov.me.confidence);

    this._scaleBar(w, h, col);
  }

  _pose(p, colour, col, confidence) {
    const ctx = this.ctx;
    const [x, y] = this.toScreen(p.x, p.z);
    if (typeof confidence === 'number') {
      // Wider halo = less sure. Roughly 0.5 m at full confidence, 3 m at none.
      const r = Math.max(10, (0.5 + (1 - Math.max(0, Math.min(1, confidence))) * 2.5) * this.scale);
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = colour; ctx.globalAlpha = 0.14; ctx.fill(); ctx.globalAlpha = 1;
    }
    if (Number.isFinite(p.yaw_deg)) {
      const a = (p.yaw_deg * Math.PI) / 180;
      const dx = Math.sin(a), dy = -Math.cos(a);
      const len = 24;
      ctx.beginPath();
      ctx.moveTo(x + dx * len, y + dy * len);
      ctx.lineTo(x + dy * 7 + dx * 6, y - dx * 7 + dy * 6);
      ctx.lineTo(x - dy * 7 + dx * 6, y + dx * 7 + dy * 6);
      ctx.closePath();
      ctx.fillStyle = colour; ctx.fill();
    }
    ctx.beginPath(); ctx.arc(x, y, 7.5, 0, Math.PI * 2);
    ctx.fillStyle = colour; ctx.fill();
    ctx.lineWidth = 3; ctx.strokeStyle = col.surface; ctx.stroke();
  }

  _label(text, x, y, col, align) {
    const ctx = this.ctx;
    ctx.font = '600 12px Inter, system-ui, sans-serif';
    ctx.textAlign = align; ctx.textBaseline = 'middle';
    ctx.lineWidth = 4; ctx.strokeStyle = col.bg; ctx.strokeText(text, x, y);
    ctx.fillStyle = col.ink; ctx.fillText(text, x, y);
  }

  _scaleBar(w, h, col) {
    const ctx = this.ctx;
    const target = 90 / this.scale;
    const nice = [0.5, 1, 2, 5, 10, 20, 50, 100].find((n) => n >= target) || 100;
    const len = nice * this.scale;
    // Bottom-right, so it never sits on the entrance board label (bottom-left of most maps).
    const x = w - 14 - len, y = h - 14;
    ctx.strokeStyle = col.ink; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, y - 5); ctx.lineTo(x, y); ctx.lineTo(x + len, y); ctx.lineTo(x + len, y - 5); ctx.stroke();
    this._label(`${nice} m`, x + len / 2, y - 12, col, 'center');
  }
}

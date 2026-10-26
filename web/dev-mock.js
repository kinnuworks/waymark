/* Development mock for the Waymark API.
   Does nothing unless the page URL has ?mock=1.
   With ?mock=1 it intercepts fetch() calls to /api/* and answers with
   plausible fake data, and replaces the camera with a generated video,
   so every page can be exercised without the backend.
   Extra switches: &down=1 makes the server unreachable, &evalempty=1 returns no results. */
(function () {
  'use strict';
  var params = new URLSearchParams(location.search);
  if (params.get('mock') !== '1') return;
  var DOWN = params.get('down') === '1';
  var EVAL_EMPTY = params.get('evalempty') === '1';

  window.WAYMARK_MOCK = true;
  var realFetch = window.fetch.bind(window);
  var t0 = Date.now();
  var COLOURS = ['#0072B2', '#009E73', '#CC79A7', '#56B4E9', '#8C6D31'];

  // ---------- sample building ----------
  function sampleMap() {
    return {
      bounds: { min_x: -8, max_x: 24, min_z: -1, max_z: 20 },
      walls: [
        [-8, 0, -1.2, 0], [1.2, 0, 24, 0], [24, 0, 24, 20], [24, 20, -8, 20], [-8, 20, -8, 0],
        [-8, 8.8, -1.2, 8.8], [1.2, 8.8, 15, 8.8], [16.8, 8.8, 24, 8.8],
        [-8, 11.2, -5, 11.2], [-3, 11.2, 5, 11.2], [7, 11.2, 11, 11.2], [13, 11.2, 24, 11.2],
        [-1.2, 0, -1.2, 8.8], [1.2, 0, 1.2, 2], [1.2, 4, 1.2, 8.8],
        [15, 0, 15, 8.8], [16.8, 0, 16.8, 3], [16.8, 5, 16.8, 8.8],
        [-1.2, 11.2, -1.2, 20], [16, 11.2, 16, 20],
      ],
      nodes: [
        { id: 'n0', x: 0, z: 1 }, { id: 'n1', x: 0, z: 5 }, { id: 'n2', x: 0, z: 10 },
        { id: 'n3', x: 6, z: 10 }, { id: 'n4', x: 12, z: 10 }, { id: 'n5', x: 18, z: 10 },
        { id: 'n6', x: -4, z: 10 }, { id: 'n7', x: 12, z: 15 }, { id: 'n8', x: 16, z: 4 },
        { id: 'n9', x: 16, z: 10 }, { id: 'n10', x: 6, z: 12 }, { id: 'n11', x: -4, z: 13 },
      ],
      edges: [['n0', 'n1'], ['n1', 'n2'], ['n2', 'n3'], ['n3', 'n4'], ['n4', 'n9'], ['n9', 'n5'], ['n2', 'n6'],
        ['n4', 'n7'], ['n9', 'n8'], ['n3', 'n10'], ['n6', 'n11']],
      pois: [
        { id: 'p1', label: 'Front desk', kind: 'desk', x: 2.5, z: 3 },
        { id: 'p2', label: 'Reading room', kind: 'room', x: 12, z: 15.5 },
        { id: 'p3', label: 'Restrooms', kind: 'restroom', x: -4, z: 13.5 },
        { id: 'p4', label: 'Lift', kind: 'lift', x: 6, z: 12.5 },
        { id: 'p5', label: 'Main stairs', kind: 'stairs', x: 20, z: 10 },
        { id: 'p6', label: 'Main entrance', kind: 'exit', x: 0, z: 0.4 },
        { id: 'p7', label: 'Study room 2', kind: 'room', x: 19, z: 4 },
        { id: 'p8', label: 'Reading room door', kind: 'door', x: 12, z: 11.2 },
      ],
      trajectories: {
        w_ana: wiggle([[0, 0.8], [0, 10], [-6, 10], [-6, 17], [-3, 18], [-4, 12], [0, 10], [0, 1]]),
        w_ben: wiggle([[0.4, 1], [0.4, 10], [12, 10], [12, 18], [10, 18], [12, 10], [22, 10], [22, 3], [17.5, 3.6], [16, 10]]),
      },
      stats: { walkers: 2, keyframes: 412, points: 38120, reprojection_rms_px: 0.71, loop_closures: 3, nodes: 12, edges: 11, pois: 8, build_seconds: 94 },
    };
  }

  function wiggle(path) {
    var out = [];
    for (var i = 0; i < path.length - 1; i++) {
      var a = path[i], b = path[i + 1];
      var n = Math.max(2, Math.round(Math.hypot(b[0] - a[0], b[1] - a[1]) / 0.6));
      for (var k = 0; k < n; k++) {
        var f = k / n;
        out.push([+(a[0] + (b[0] - a[0]) * f + Math.sin((i * 7 + k) * 0.9) * 0.12).toFixed(2),
          +(a[1] + (b[1] - a[1]) * f + Math.cos((i * 5 + k) * 0.7) * 0.12).toFixed(2)]);
      }
    }
    out.push(path[path.length - 1]);
    return out;
  }

  var state = {
    buildings: [
      { id: 'b_sample', name: 'Hartley Library, ground floor', created: '2026-10-20T10:12:00Z', status: 'mapped', n_walkers: 2, n_pois: 8, has_map: true },
      { id: 'b_clinic', name: 'Riverside Clinic, level 1', created: '2026-10-26T15:40:00Z', status: 'mapping', n_walkers: 1, n_pois: 2, has_map: false },
    ],
    maps: { b_sample: sampleMap() },
    walkers: {}, // wid -> {bid, name, colour, frames, calib, path}
    visits: {}, // vid -> {bid, device, dest, frames, helperText, help, started, checkpoints, ended}
    nextId: 1,
  };
  state.maps.b_clinic = {
    bounds: null, walls: [], nodes: [], edges: [],
    pois: [{ id: 'c1', label: 'Reception', kind: 'desk', x: 1.5, z: 2.5 }, { id: 'c2', label: 'Waiting area', kind: 'room', x: -3, z: 6 }],
    trajectories: { w_cara: wiggle([[0, 0.8], [0, 6], [-4, 6], [-4, 9]]) }, stats: {},
  };

  // A visit already in progress, so the helper console has something to show.
  state.visits.v_demo = { id: 'v_demo', bid: 'b_sample', device: 'iPhone 15, Safari', dest: 'p2', frames: 0, help: false, started: Date.now() - 95000, checkpoints: [], timeBased: true };

  // ---------- route simulation for visits ----------
  var ROUTES = {
    p2: [[0, 1], [0, 10], [12, 10], [12, 15.5]],
    p3: [[0, 1], [0, 10], [-4, 10], [-4, 13.5]],
    p4: [[0, 1], [0, 10], [6, 10], [6, 12.5]],
    p1: [[0, 1], [0, 3], [2.5, 3]],
    p5: [[0, 1], [0, 10], [20, 10]],
    p6: [[0, 1], [0, 0.4]],
    p7: [[0, 1], [0, 10], [16, 10], [16, 4], [19, 4]],
    p8: [[0, 1], [0, 10], [12, 10], [12, 11.2]],
  };
  function routeFor(dest) { return ROUTES[dest] || ROUTES.p2; }
  function routeLen(r) { var L = 0; for (var i = 1; i < r.length; i++) L += Math.hypot(r[i][0] - r[i - 1][0], r[i][1] - r[i - 1][1]); return L; }
  function yawOf(a, b) { return Math.atan2(b[0] - a[0], b[1] - a[1]) * 180 / Math.PI; }
  function wrap(d) { d = ((d % 360) + 360) % 360; return d > 180 ? d - 360 : d; }
  function poiLabel(bid, id) { var m = state.maps[bid]; var p = m && m.pois.find(function (q) { return q.id === id; }); return p ? p.label : 'your destination'; }

  // Position s metres along the route plus guidance for that point.
  function guidance(v, s) {
    var r = routeFor(v.dest), L = routeLen(r), acc = 0;
    s = Math.max(0, Math.min(L, s));
    for (var i = 1; i < r.length; i++) {
      var a = r[i - 1], b = r[i], seg = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (acc + seg >= s || i === r.length - 1) {
        var f = seg ? (s - acc) / seg : 1;
        var pos = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
        var yaw = yawOf(a, b);
        var toCorner = seg - (s - acc);
        var turn = i < r.length - 1 ? wrap(yawOf(b, r[i + 1]) - yaw) : 0;
        return { pos: pos, yaw: yaw, remaining: L - s, toCorner: toCorner, turn: turn, last: i === r.length - 1, routeLeft: [pos].concat(r.slice(i)) };
      }
      acc += seg;
    }
  }

  var STATE_SAY = {
    low: "I'm not sure where you are. Hold the phone up and pan it slowly left and right.",
    stairs: 'Stairs ahead on your right. Stop here while I check your position.',
  };

  function visitStep(v, s, frameNo) {
    var g = guidance(v, s);
    var dest = poiLabel(v.bid, v.dest);
    var out = { state: 'guiding', fix: null, say: '', haptic: 'none', route: null, helper_text: null };
    var conf = 0.82 + 0.1 * Math.sin(frameNo);
    if (frameNo < 3) {
      out.state = 'locating';
      out.say = frameNo === 0 ? 'Finding where you are. Hold the phone at chest height, camera facing forward.' : '';
      return out;
    }
    out.fix = { x: +g.pos[0].toFixed(2), z: +g.pos[1].toFixed(2), yaw_deg: +g.yaw.toFixed(1), confidence: +conf.toFixed(2), inliers: Math.round(120 + 60 * conf) };
    out.route = { remaining_m: +g.remaining.toFixed(1), next_turn_deg: Math.round(g.turn), next_label: g.last ? dest : (g.turn > 0 ? 'right' : 'left') + ' turn' };
    if (frameNo === 9 || frameNo === 10) {
      out.state = 'low_confidence';
      out.fix.confidence = 0.31;
      out.say = STATE_SAY.low;
      out.haptic = 'stop';
      return out;
    }
    if (g.remaining < 0.8) {
      out.state = 'arrived';
      out.haptic = 'arrived';
      out.say = 'You have arrived at ' + dest + '.';
      return out;
    }
    if (!g.last && g.toCorner < 1.6 && Math.abs(g.turn) > 20) {
      out.say = 'Turn ' + (g.turn > 0 ? 'right' : 'left') + ' now.';
      out.haptic = g.turn > 0 ? 'right' : 'left';
    } else if (!g.last && g.toCorner < 7) {
      out.say = 'In ' + Math.round(g.toCorner) + ' metres, turn ' + (g.turn > 0 ? 'right' : 'left') + '.';
    } else if (g.last && g.remaining < 6) {
      out.say = dest + ' is ' + Math.round(g.remaining) + ' metres ahead.';
    } else {
      out.say = 'Walk straight ahead for ' + Math.round(g.toCorner) + ' metres.';
      out.haptic = frameNo % 6 === 0 ? 'straight' : 'none';
    }
    return out;
  }

  function liveFor(v) {
    var frameNo = v.timeBased ? Math.floor((Date.now() - v.started) / 1000) % 60 : v.frames;
    var s = Math.max(0, (frameNo - 3) * 0.8);
    var step = visitStep(v, s, frameNo);
    var g = guidance(v, s);
    return { step: step, g: g, frameNo: frameNo };
  }

  function traceFor(v, frameNo) {
    var items = [];
    var start = Math.max(0, frameNo - 9);
    for (var f = start; f <= frameNo; f++) {
      var s = Math.max(0, (f - 3) * 0.8);
      var st = visitStep(v, s, f);
      var conf = st.fix ? st.fix.confidence : null;
      var item = {
        t: new Date(v.started + f * 1000).toISOString(),
        state: st.state,
        saw: st.fix ? (st.fix.inliers + ' inliers matched against 14 nearby keyframes, confidence ' + conf.toFixed(2)) : 'No board or keyframe match yet',
        decided: st.say || 'Stay quiet',
        why: '',
      };
      if (st.state === 'low_confidence') item.why = 'Confidence fell below 0.5, so a position guess could send the visitor the wrong way.';
      else if (st.state === 'locating') item.why = 'No fix yet. The visitor should hold still until one arrives.';
      else if (st.haptic === 'left' || st.haptic === 'right') item.why = 'Visitor is within 1.6 m of the corner.';
      else if (st.state === 'arrived') item.why = 'Within 0.8 m of the destination.';
      else item.why = st.say ? 'Next instruction changed.' : 'Nothing new to say.';
      items.push(item);
    }
    if (v.help) items.push({ t: new Date().toISOString(), state: 'helper', saw: 'Visitor pressed Call a helper', decided: 'Notify the helper console and tell the visitor to stay put', why: 'A person asked for help.' });
    return items;
  }

  // ---------- generated images ----------
  function canvasBlob(w, h, paint) {
    var c = document.createElement('canvas');
    c.width = w; c.height = h;
    paint(c.getContext('2d'), w, h);
    return new Promise(function (res) { c.toBlob(res, 'image/png'); });
  }

  function floorplanBlob(bid) {
    var m = state.maps[bid];
    var b = m && m.bounds;
    if (!b) return canvasBlob(10, 10, function (ctx) { ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 10, 10); });
    var ppm = 30, W = (b.max_x - b.min_x) * ppm, H = (b.max_z - b.min_z) * ppm;
    return canvasBlob(W, H, function (ctx) {
      ctx.fillStyle = '#F4F1EA'; ctx.fillRect(0, 0, W, H);
      function S(x, z) { return [(x - b.min_x) * ppm, (b.max_z - z) * ppm]; }
      // Room fills, as a rendered plan would have.
      ctx.fillStyle = '#E8E2D6';
      [[-8, 11.2, -1.2, 20], [16, 11.2, 24, 20], [-8, 0, -1.2, 8.8], [15, 0, 24, 8.8]].forEach(function (r) {
        var p = S(r[0], r[3]), q = S(r[2], r[1]); ctx.fillRect(p[0], p[1], q[0] - p[0], q[1] - p[1]);
      });
      ctx.strokeStyle = '#3A3630'; ctx.lineWidth = 5; ctx.lineCap = 'round';
      m.walls.forEach(function (wl) { var p = S(wl[0], wl[1]), q = S(wl[2], wl[3]); ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke(); });
    });
  }

  function boardBlob() {
    return canvasBlob(620, 877, function (ctx, w, h) {
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h);
      var sq = 70, ox = (w - sq * 7) / 2, oy = 120;
      for (var r = 0; r < 9; r++) for (var c = 0; c < 7; c++) {
        if ((r + c) % 2 === 0) { ctx.fillStyle = '#000'; ctx.fillRect(ox + c * sq, oy + r * sq, sq, sq); }
      }
      ctx.fillStyle = '#000'; ctx.font = '600 22px sans-serif'; ctx.fillText('Mock ChArUco board', 40, 70);
    });
  }

  // ---------- helpers ----------
  function json(data, status) {
    return new Response(JSON.stringify(data), { status: status || 200, headers: { 'Content-Type': 'application/json' } });
  }
  function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function newId(prefix) { return prefix + '_' + (state.nextId++).toString(36) + Math.random().toString(36).slice(2, 6); }
  function readJson(init) { try { return init && typeof init.body === 'string' ? JSON.parse(init.body) : {}; } catch (e) { return {}; } }

  function walkerPose(w) {
    // Walk a slow loop around the east corridor.
    var loop = [[0.6, 1], [0.6, 10], [14, 10], [14, 18], [22, 18], [22, 10], [0.6, 10]];
    var L = routeLen(loop), s = (w.frames * 0.45) % L, acc = 0;
    for (var i = 1; i < loop.length; i++) {
      var a = loop[i - 1], b = loop[i], seg = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (acc + seg >= s) { var f = (s - acc) / seg; return { x: +(a[0] + (b[0] - a[0]) * f).toFixed(2), z: +(a[1] + (b[1] - a[1]) * f).toFixed(2), yaw_deg: +yawOf(a, b).toFixed(1) }; }
      acc += seg;
    }
    return { x: 0, z: 1, yaw_deg: 0 };
  }

  // ---------- router ----------
  function route(method, path, init) {
    var m;
    if (path === '/api/health') {
      return json({ ok: true, opencv: '5.0.0', build: { kleidicv: true, cool: true }, instance: 'c8g.2xlarge', arch: 'aarch64', features: { aliked: true, lightglue: true } });
    }
    if (path === '/api/eval') {
      if (EVAL_EMPTY) return json({ sections: [] });
      return json({ sections: [
        { title: 'Mock: localisation error at checkpoints', note: 'Mock data for layout testing only. Not a measurement.', columns: ['Building', 'Checkpoints', 'Median error (m)', 'p90 error (m)', 'Fix rate'],
          rows: [['Sample library', 12, 0.41, 0.93, '96%'], ['Sample clinic', 12, 0.55, 1.20, '91%']] },
        { title: 'Mock: per-frame latency on the server', note: 'Mock data for layout testing only.', columns: ['Stage', 'Median (ms)', 'p95 (ms)'],
          rows: [['ALIKED features', 61, 88], ['LightGlue matching', 74, 120], ['solvePnP + MAGSAC', 6, 11], ['Total round trip', 238, 410]] },
      ] });
    }
    if (path === '/api/board.png') return boardBlob().then(function (b) { return new Response(b, { headers: { 'Content-Type': 'image/png' } }); });

    if (path === '/api/buildings' && method === 'GET') return json(state.buildings);
    if (path === '/api/buildings' && method === 'POST') {
      var body = readJson(init), id = newId('b');
      state.buildings.unshift({ id: id, name: body.name || 'Untitled building', created: new Date().toISOString(), status: 'mapping', n_walkers: 0, n_pois: 0, has_map: false, board_height_m: body.board_height_m });
      state.maps[id] = { bounds: null, walls: [], nodes: [], edges: [], pois: [], trajectories: {}, stats: {} };
      return json({ id: id });
    }

    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/walkers$/)) && method === 'POST') {
      var wid = newId('w');
      var bld = state.buildings.find(function (b) { return b.id === m[1]; });
      if (!bld) return json({ detail: 'Building not found' }, 404);
      bld.n_walkers++;
      state.walkers[wid] = { bid: m[1], name: readJson(init).name, colour: COLOURS[(bld.n_walkers - 1) % COLOURS.length], frames: 0, calibViews: 0 };
      state.maps[m[1]].trajectories[wid] = [];
      return json({ walker_id: wid, colour: state.walkers[wid].colour });
    }
    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/walkers\/([^/]+)\/calib$/))) {
      var wc = state.walkers[m[2]] || (state.walkers[m[2]] = { bid: m[1], frames: 0, calibViews: 0 });
      var found = Math.random() > 0.25;
      if (found) wc.calibViews++;
      var done = wc.calibViews >= 8;
      return json({ board_found: found, views: Math.min(8, wc.calibViews), needed: 8, done: done, rms_px: done ? 0.38 : null });
    }
    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/walkers\/([^/]+)\/frame$/))) {
      var w = state.walkers[m[2]] || (state.walkers[m[2]] = { bid: m[1], frames: 0 });
      var n = w.frames++;
      if (n < 3) return json({ state: 'need_board', pose: null, quality: { sharpness: 140, brightness: 0.52, ok: true }, keyframes: 0, points: 0, say: 'Point the camera at the entrance board.', code: 'board_not_seen' });
      if (n % 17 === 0) return json({ state: 'rejected', pose: null, quality: { sharpness: 22, brightness: 0.48, ok: false }, keyframes: Math.floor(n / 3), points: n * 92, say: 'Too blurry. Slow down a little.', code: 'blur' });
      if (n % 41 === 40) return json({ state: 'lost', pose: null, quality: { sharpness: 120, brightness: 0.3, ok: true }, keyframes: Math.floor(n / 3), points: n * 92, say: 'Lost track. Walk back a few steps to somewhere you have already been.', code: 'lost' });
      var pose = walkerPose(w);
      var tr = state.maps[m[1]].trajectories[m[2]] || (state.maps[m[1]].trajectories[m[2]] = []);
      tr.push([pose.x, pose.z]);
      return json({ state: 'tracking', pose: pose, quality: { sharpness: 160, brightness: 0.55, ok: true }, keyframes: Math.floor(n / 3), points: n * 92, say: n === 3 ? 'Board found. Start walking slowly.' : '', code: 'ok' });
    }
    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/walkers\/([^/]+)\/poi$/))) {
      var b2 = readJson(init), wp = state.walkers[m[2]] || { frames: 0 }, p = walkerPose(wp);
      var poi = { id: newId('p'), label: b2.label, kind: b2.kind, x: p.x, z: p.z };
      state.maps[m[1]].pois.push(poi);
      return json({ poi_id: poi.id, x: p.x, z: p.z });
    }
    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/walkers\/([^/]+)\/finish$/))) return json({ ok: true });
    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/coordinator$/))) {
      var asg = {}, total = 0;
      Object.keys(state.walkers).forEach(function (k, i) {
        var wk = state.walkers[k];
        if (wk.bid !== m[1]) return;
        total += wk.frames;
        asg[k] = i % 2 === 0
          ? { target: { x: 20, z: 16 }, say: 'Head to the north-east corner. Nobody has mapped it yet.' }
          : { target: { x: -6, z: 4 }, say: 'Cover the room to the left of the entrance.' };
      });
      return json({ coverage_pct: Math.min(97, 18 + total * 0.6), frontiers: [{ x: 20, z: 16 }, { x: -6, z: 4 }], assignments: asg });
    }
    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/build$/))) {
      var bb = state.buildings.find(function (b) { return b.id === m[1]; });
      if (bb) { bb.has_map = true; bb.status = 'mapped'; }
      return delay(1200).then(function () { return json({ ok: true, stats: { keyframes: 388, points: 35210, walkers: 2, reprojection_rms_px: 0.74, loop_closures: 2, nodes: 12, edges: 11, pois: 8, seconds: 71 } }); });
    }
    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/map$/))) {
      var mp = state.maps[m[1]];
      return mp ? json(mp) : json({ detail: 'No map yet' }, 404);
    }
    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/floorplan\.png$/))) {
      return floorplanBlob(m[1]).then(function (b) { return new Response(b, { headers: { 'Content-Type': 'image/png' } }); });
    }

    if ((m = path.match(/^\/api\/buildings\/([^/]+)\/visits$/)) && method === 'POST') {
      var vid = newId('v');
      state.visits[vid] = { id: vid, bid: m[1], device: readJson(init).device, dest: null, frames: 0, help: false, started: Date.now(), checkpoints: [] };
      return json({ visit_id: vid });
    }
    if (path === '/api/visits' && method === 'GET') {
      return json(Object.keys(state.visits).map(function (k) {
        var v = state.visits[k], l = liveFor(v), bname = (state.buildings.find(function (b) { return b.id === v.bid; }) || {}).name;
        return { id: k, building_id: v.bid, building_name: bname, device: v.device, started: new Date(v.started).toISOString(), state: v.ended ? 'ended' : (v.help ? 'helper' : l.step.state), destination: v.dest ? poiLabel(v.bid, v.dest) : null, help_requested: v.help, active: !v.ended };
      }));
    }
    if ((m = path.match(/^\/api\/visits\/([^/]+)\/(\w+)$/))) {
      var v = state.visits[m[1]];
      if (!v) return json({ detail: 'Visit not found' }, 404);
      var op = m[2], body3 = readJson(init);
      if (op === 'destination') {
        v.dest = body3.poi_id; v.frames = 0;
        return json({ ok: true, route_m: +routeLen(routeFor(v.dest)).toFixed(1) });
      }
      if (op === 'frame') {
        var frameNo = v.frames++;
        if (!v.dest) return json({ state: 'locating', fix: null, say: 'Choose where you want to go first.', haptic: 'none', route: null, helper_text: null });
        var step = v.help ? { state: 'helper', fix: visitStep(v, Math.max(0, (frameNo - 3) * 0.8), frameNo).fix, say: 'A helper is on the way. Please stay where you are.', haptic: 'stop', route: null, helper_text: null }
          : visitStep(v, Math.max(0, (frameNo - 3) * 0.8), frameNo);
        if (v.helperText) { step.helper_text = v.helperText; v.helperText = null; }
        return delay(150 + Math.random() * 250).then(function () { return json(step); });
      }
      if (op === 'ask') {
        var q = String(body3.text || '').toLowerCase();
        var l2 = liveFor(v);
        if (/where am i/.test(q)) return json({ say: v.dest ? 'You are in the main corridor, ' + Math.round(l2.g.remaining) + ' metres from ' + poiLabel(v.bid, v.dest) + '.' : 'You are just inside the main entrance. The front desk is a few steps ahead on your right.' });
        var maps = state.maps[v.bid], hit = maps && maps.pois.find(function (p) { return q.indexOf(p.label.toLowerCase()) >= 0 || q.indexOf(p.kind) >= 0 || (p.kind === 'restroom' && /toilet|bathroom/.test(q)); });
        if (hit) { v.dest = hit.id; v.frames = 0; return json({ say: 'Okay. Taking you to ' + hit.label + '. It is about ' + Math.round(routeLen(routeFor(hit.id))) + ' metres away.' }); }
        return json({ say: "I didn't find that place in this building. Try the list of places, or call a helper." });
      }
      if (op === 'help') { v.help = true; return json({ ok: true }); }
      if (op === 'checkpoint') { v.checkpoints.push(body3.checkpoint_id); return json({ ok: true }); }
      if (op === 'end') { v.ended = true; v.endResult = body3; return json({ ok: true }); }
      if (op === 'say') { v.helperText = body3.text; return json({ ok: true }); }
      if (op === 'live') {
        var l = liveFor(v);
        return json({
          state: v.ended ? 'ended' : (v.help ? 'helper' : l.step.state),
          fix: l.step.fix,
          destination: v.dest ? Object.assign({ poi_id: v.dest }, (state.maps[v.bid].pois.find(function (p) { return p.id === v.dest; }) || {})) : null,
          route_polyline: v.dest ? l.g.routeLeft : [],
          trace_tail: traceFor(v, l.frameNo),
          help_requested: v.help,
          building_id: v.bid,
        });
      }
    }
    if ((m = path.match(/^\/api\/trace\/([^/]+)$/))) {
      var tv = state.visits[m[1]];
      return json(tv ? traceFor(tv, liveFor(tv).frameNo) : []);
    }
    return json({ detail: 'Mock has no route for ' + method + ' ' + path }, 404);
  }

  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : input.url;
    var u = new URL(url, location.href);
    if (u.origin !== location.origin || u.pathname.indexOf('/api/') !== 0) return realFetch(input, init);
    var method = ((init && init.method) || 'GET').toUpperCase();
    return delay(60 + Math.random() * 140).then(function () {
      if (DOWN) throw new TypeError('Failed to fetch (mock server down)');
      return route(method, u.pathname, init);
    });
  };

  // ---------- fake camera ----------
  function fakeStream() {
    var c = document.createElement('canvas');
    c.width = 1280; c.height = 960;
    var ctx = c.getContext('2d'), f = 0;
    (function paint() {
      f++;
      var g = ctx.createLinearGradient(0, 0, 1280, 960);
      g.addColorStop(0, '#3b3f46'); g.addColorStop(1, '#8a8174');
      ctx.fillStyle = g; ctx.fillRect(0, 0, 1280, 960);
      ctx.fillStyle = 'rgba(255,255,255,.18)';
      for (var i = 0; i < 9; i++) ctx.fillRect(((i * 170 - f * 4) % 1500 + 1500) % 1500 - 120, 220, 70, 520);
      ctx.fillStyle = '#fff'; ctx.font = '600 44px sans-serif';
      ctx.fillText('Mock camera', 60, 90);
      setTimeout(paint, 100);
    })();
    return c.captureStream ? c.captureStream(10) : null;
  }
  if (!navigator.mediaDevices) { try { navigator.mediaDevices = {}; } catch (e) { /* read-only */ } }
  if (navigator.mediaDevices) {
    navigator.mediaDevices.getUserMedia = function () {
      var s = fakeStream();
      return s ? Promise.resolve(s) : Promise.reject(Object.assign(new Error('No captureStream'), { name: 'NotFoundError' }));
    };
  }

  // ---------- keep ?mock=1 on internal links, and show a badge ----------
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('a[href]').forEach(function (a) {
      var href = a.getAttribute('href');
      if (!/^[\w-]+\.html/.test(href)) return;
      var u = new URL(href, location.href);
      u.searchParams.set('mock', '1');
      a.setAttribute('href', u.pathname.split('/').pop() + u.search + u.hash);
    });
    var badge = document.createElement('div');
    badge.className = 'mock-badge';
    badge.setAttribute('aria-hidden', 'true');
    badge.textContent = DOWN ? 'mock: server down' : 'mock data';
    document.body.appendChild(badge);
  });
})();

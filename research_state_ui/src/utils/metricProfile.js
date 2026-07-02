import { goodDirection, curveValues } from './metrics.js';

/**
 * metricProfile — profile a project MLflow ledger and decide what to render.
 *
 * Project-agnostic by construction: every metric/param key gets a statistical
 * fingerprint (coverage, variance, series depth, numeric vs categorical,
 * direction, baseline_/delta_ pairing), a role falls out of deterministic
 * rules over the fingerprint, and the page composes renderers from the roles.
 * Pure data → data; no React, unit-testable with node.
 */

// Tolerant numeric parser for MLflow param strings ("262144", "2**18", "1e-4").
export function parseNumeric(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v !== 'string') return null;
  const pow = v.match(/^\s*(\d+(?:\.\d+)?)\s*\*\*\s*(\d+(?:\.\d+)?)\s*$/);
  if (pow) return Math.pow(Number(pow[1]), Number(pow[2]));
  const n = Number(v.trim());
  return Number.isFinite(n) && v.trim() !== '' ? n : null;
}

// Overview payload → flat chronological run list, each tagged with its parent
// app-experiment identity (the unit the user navigates by).
export function flattenLedger(payload) {
  const runs = [];
  for (const exp of Array.isArray(payload?.experiments) ? payload.experiments : []) {
    for (const me of (exp.metrics && Array.isArray(exp.metrics.experiments)) ? exp.metrics.experiments : []) {
      for (const r of Array.isArray(me.runs) ? me.runs : []) {
        runs.push({
          expId: exp.experiment_id,
          expName: exp.name,
          expStatus: exp.status,
          runId: r.run_id,
          runName: r.run_name || r.run_id,
          runStatus: r.status,
          start: r.start_time ?? 0,
          end: r.end_time ?? null,
          metrics: r.metrics || {},
          params: r.params || {},
          history: r.history || {},
        });
      }
    }
  }
  runs.sort((a, b) => (a.start || 0) - (b.start || 0));
  return runs;
}

// Spearman rank correlation (average ranks on ties); null when degenerate.
export function spearman(xs, ys) {
  const n = xs.length;
  if (n < 3) return null;
  const rank = (arr) => {
    const order = arr.map((v, i) => [v, i]).sort((a, b) => a[0] - b[0]);
    const ranks = new Array(n);
    for (let i = 0; i < n; ) {
      let j = i;
      while (j + 1 < n && order[j + 1][0] === order[i][0]) j++;
      const r = (i + j) / 2 + 1;
      for (let k = i; k <= j; k++) ranks[order[k][1]] = r;
      i = j + 1;
    }
    return ranks;
  };
  const rx = rank(xs); const ry = rank(ys);
  const mean = (a) => a.reduce((s, v) => s + v, 0) / n;
  const mx = mean(rx); const my = mean(ry);
  let num = 0; let dx = 0; let dy = 0;
  for (let i = 0; i < n; i++) {
    num += (rx[i] - mx) * (ry[i] - my);
    dx += (rx[i] - mx) ** 2;
    dy += (ry[i] - my) ** 2;
  }
  return dx && dy ? num / Math.sqrt(dx * dy) : null;
}

const distinctCount = (vals) => new Set(vals.map(v => (typeof v === 'number' ? v.toPrecision(6) : String(v)))).size;

// baseline_X / base_X pairs with X; delta_X / X_delta / relative_*_percent
// derive from some primary metric.
const anchorTarget = (key) => (key.match(/^base(?:line)?_(.+)$/) || [])[1] || null;
const deltaTarget = (key) => {
  const m = key.match(/^delta_(.+)$|^(.+)_delta$/);
  return m ? (m[1] || m[2]) : (/^relative_.*_percent$/.test(key) ? key : null);
};
const matchesTarget = (keys, self, target) =>
  target != null && [...keys].some(k => k !== self && (k === target || k.endsWith(`_${target}`)));

// Identity and provenance params (ids, uuids, commits, hashes) locate the
// run, they don't describe it — never rendered anywhere.
const isProvenanceParam = (key) => /commit|hash|_sha$|(^|_)uu?id$|(^|_)id$/i.test(key);

// Agents may declare the focus explicitly (primary_metric{,_direction}) —
// workflow metadata to consume, not a constant to exhibit.
const isContractParam = (key) => /^primary_metric(_direction)?$/i.test(key);

// The run's own recorded anchor for a metric (logged as metric or param).
export function anchorValueOf(run, key) {
  for (const a of [`baseline_${key}`, `base_${key}`]) {
    const m = run.metrics[a];
    if (m && Number.isFinite(m.last)) return m.last;
    const p = parseNumeric(run.params?.[a]);
    if (p != null) return p;
  }
  return null;
}

// One run's metrics sorted into what a reader needs: headline results
// (directional or anchored, with the anchor folded into a delta), quiet
// telemetry, and ok/bad diagnostics. Anchors, deriveds (any delta_*), config
// echoes, and unit duplicates disappear — the ledger's rules at run scale.
export function classifyRunMetrics(run) {
  const keys = new Set(Object.keys(run.metrics || {}));
  const paramsLower = new Set(Object.keys(run.params || {}).map(k => k.toLowerCase()));
  const headline = []; const telemetry = []; const diagnostics = [];
  for (const key of keys) {
    const m = run.metrics[key];
    if (!m || !Number.isFinite(m.last)) continue;
    if (matchesTarget(keys, key, anchorTarget(key)) || keys.has(anchorTarget(key))) continue;
    if (deltaTarget(key)) continue; // derived by name — some primary carries it
    if (paramsLower.has(key.toLowerCase())) continue;
    if (/_exit(?:_code)?$|_code$/.test(key)) { diagnostics.push({ key, v: m.last }); continue; }
    const direction = goodDirection(key);
    const anchor = anchorValueOf(run, key);
    if (direction !== 0 || anchor != null) headline.push({ key, v: m.last, direction, anchor });
    else telemetry.push({ key, v: m.last });
  }
  // The same measure in two units is one fact — keep the larger unit.
  const lower = new Set(telemetry.map(t => t.key.toLowerCase()));
  const dropped = telemetry.filter(({ key }) => {
    const m = key.toLowerCase().match(/^(.*)_m(i?)b$/);
    return !(m && (lower.has(`${m[1]}_g${m[2]}b`)));
  });
  const byKey = (a, b) => a.key.localeCompare(b.key);
  headline.sort((a, b) => ((b.anchor != null) - (a.anchor != null)) || byKey(a, b));
  dropped.sort(byKey);
  return { headline, telemetry: dropped, diagnostics };
}

/**
 * The whole read model for the page:
 *   { runs, focus, summary, strips, curves, knobs, diagnostics, invariants, sparse }
 * focus.directionAssumed flags a guessed lower-is-better so the UI can say so.
 */
export function planLedger(payload) {
  const runs = flattenLedger(payload);
  const n = runs.length;

  // ── metric fingerprints ──
  const keys = new Set(runs.flatMap(r => Object.keys(r.metrics)));
  const paramKeysLower = new Set(runs.flatMap(r => Object.keys(r.params)).map(k => k.toLowerCase()));
  const fps = [];
  for (const key of keys) {
    // Config echoed into metrics (same key logged as a param) — the param is
    // authoritative; the metric copy would double-report every knob.
    if (paramKeysLower.has(key.toLowerCase()) && !anchorTarget(key)) continue;
    const values = [];
    let seriesDepth = 0;
    runs.forEach((r, i) => {
      const m = r.metrics[key];
      if (m && Number.isFinite(m.last)) values.push({ i, v: m.last });
      seriesDepth = Math.max(seriesDepth, curveValues(r.history[key]).length);
    });
    if (!values.length) continue;
    const nums = values.map(p => p.v);
    const distinct = distinctCount(nums);
    const min = Math.min(...nums); const max = Math.max(...nums);
    const allBinary = nums.every(v => v === 0 || v === 1);
    const fp = {
      key, values, min, max, distinct, seriesDepth,
      coverage: values.length / n,
      direction: goodDirection(key),
      hasAnchor: keys.has(`baseline_${key}`) || keys.has(`base_${key}`)
        || runs.some(r => anchorValueOf(r, key) != null),
    };
    fp.role =
      matchesTarget(keys, key, anchorTarget(key)) || keys.has(anchorTarget(key)) ? 'anchor'
      : deltaTarget(key) != null ? 'derived'
      : (/_exit(?:_code)?$|_code$/.test(key) || (allBinary && values.length > 1)) ? 'diagnostic'
      // "Shared across runs" is a full-coverage claim — a constant logged by
      // a subset is neither shared nor comparable, so it simply doesn't show.
      : (values.length === n && n > 1 && distinct === 1) ? 'invariant'
      : (n >= 3 && values.length < Math.max(2, Math.ceil(n / 2))) ? 'sparse'
      : 'compare';
    fps.push(fp);
  }

  // ── the declared contract, when agents logged one (last run wins) ──
  const contract = {};
  for (const r of runs) {
    const pm = r.params.primary_metric;
    if (pm) contract.key = String(pm);
    const d = String(r.params.primary_metric_direction || '').toLowerCase();
    if (d) contract.direction = /min|down|lower/.test(d) ? -1 : /max|up|higher/.test(d) ? 1 : undefined;
  }

  // ── focus metric: declared contract, else anchored + directional + covered ──
  const spread = (fp) => (fp.max - fp.min) / (Math.abs((fp.max + fp.min) / 2) || 1);
  const candidates = fps.filter(fp => fp.role === 'compare' && fp.distinct > 1);
  candidates.sort((a, b) =>
    (b.hasAnchor - a.hasAnchor)
    || (Math.abs(b.direction) - Math.abs(a.direction))
    || (b.coverage - a.coverage)
    || (spread(b) - spread(a)));
  const focusFp = candidates.find(fp => fp.key === contract.key) || candidates[0] || null;
  const declaredDir = focusFp && focusFp.key === contract.key ? contract.direction : undefined;
  const focus = focusFp && {
    key: focusFp.key,
    direction: declaredDir ?? (focusFp.direction || -1),
    directionAssumed: declaredDir == null && focusFp.direction === 0,
    anchorKey: focusFp.hasAnchor ? `baseline_${focusFp.key}` : null,
  };

  // ── summary (pulse): best, project baseline, staleness ──
  let summary = null;
  if (focus) {
    const dir = focus.direction;
    let best = null;
    for (const { i, v } of focusFp.values) {
      if (!best || (dir < 0 ? v < best.v : v > best.v)) best = { i, v };
    }
    const projectBaseline = runs.map(r => anchorValueOf(r, focus.key)).find(v => v != null) ?? null;
    summary = {
      best: { run: runs[best.i], value: best.v, i: best.i },
      projectBaseline,
      sinceBest: focusFp.values.filter(p => p.i > best.i).length,
      runCount: n,
      expCount: new Set(runs.map(r => r.expId)).size,
      liveCount: runs.filter(r => /running/i.test(r.runStatus || '')).length,
    };
  }

  // ── strips: every comparable varying metric, focus first ──
  const strips = fps.filter(fp => fp.role === 'compare' && fp.distinct > 1);
  strips.sort((a, b) =>
    ((b === focusFp) - (a === focusFp))
    || (Math.abs(b.direction) - Math.abs(a.direction))
    || (b.coverage - a.coverage));
  const curves = fps.filter(fp => fp.seriesDepth >= 3 && fp.role !== 'anchor' && fp.role !== 'derived');

  // ── params: constants are config; varied ones are knobs ranked by pull ──
  const paramKeys = new Set(runs.flatMap(r => Object.keys(r.params)));
  const knobs = [];
  const config = [];
  for (const key of paramKeys) {
    if (anchorTarget(key) || isProvenanceParam(key) || isContractParam(key)) continue;
    const present = [];
    runs.forEach((r, i) => { if (r.params[key] != null) present.push({ i, raw: String(r.params[key]) }); });
    if (!present.length) continue;
    if (distinctCount(present.map(p => p.raw)) === 1) {
      // Same full-coverage bar as metric invariants: "shared" means every run.
      if (present.length === n && n > 1) config.push({ key, value: present[0].raw });
      continue;
    }
    const nums = present.map(p => parseNumeric(p.raw));
    const numeric = nums.every(v => v != null);
    const points = present
      .map((p, j) => ({ i: p.i, x: numeric ? nums[j] : null, cat: p.raw, y: focusFp?.values.find(v => v.i === p.i)?.v }))
      .filter(p => p.y != null);
    const assoc = numeric && points.length >= 3 ? spearman(points.map(p => p.x), points.map(p => p.y)) : null;
    knobs.push({ key, numeric, points, assoc });
  }
  const pull = (k) => (k.assoc == null ? -1 : Math.abs(k.assoc));
  knobs.sort((a, b) => pull(b) - pull(a));

  return {
    runs, focus, summary, strips, curves, knobs,
    diagnostics: fps.filter(fp => fp.role === 'diagnostic'),
    invariants: [
      ...fps.filter(fp => fp.role === 'invariant').map(fp => ({ key: fp.key, value: fp.values[0].v })),
      ...config,
    ],
    sparse: fps.filter(fp => fp.role === 'sparse'),
  };
}

// Leaderboard order: best first along the focus direction.
export function rankRuns(plan) {
  if (!plan.focus) return [];
  const fp = plan.strips.find(s => s.key === plan.focus.key);
  if (!fp) return [];
  const dir = plan.focus.direction;
  return fp.values.slice().sort((a, b) => (dir < 0 ? a.v - b.v : b.v - a.v));
}

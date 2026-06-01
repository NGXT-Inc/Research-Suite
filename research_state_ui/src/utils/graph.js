/**
 * Logic DAG construction + layered layout.
 *
 *   Layer 0 (Claim):       one node per claim, top of the chart
 *   Layer 1 (Approach):    one node per experiment, in its parent claim's lane
 *   Layer 2 (Attempt):     one node per attempt; intermediate attempts hang
 *                          beneath their parent experiment, the final attempt
 *                          carries the outcome glyph
 *   Layer 3 (Outcome):     one bucket per (claim × outcome). Multiple
 *                          experiments funnel into the same colored sink.
 *
 * The shape is pure: pass in `claims` and `experiments` from /home, get back
 * `{ nodes, edges, layers, winningPath }` with no React dependencies.
 */
import { classifyExperiment, outcomeLabel } from './evidence';

export function buildLogicDag(claims, experiments) {
  const nodes = [];
  const edges = [];
  const layers = [[], [], [], []];
  const winningPath = new Set();

  const expsByClaim = new Map();
  for (const e of experiments || []) {
    for (const tc of e.tested_claims || []) {
      if (!expsByClaim.has(tc.id)) expsByClaim.set(tc.id, []);
      expsByClaim.get(tc.id).push(e);
    }
  }

  // Layer 0: claims
  for (const c of claims || []) {
    const node = {
      id: `claim:${c.id}`,
      kind: 'claim',
      label: c.statement,
      status: c.status,
      confidence: c.confidence,
      scope: c.scope,
      ref: c,
      layer: 0,
    };
    layers[0].push(node);
    nodes.push(node);
  }

  // Layer 1: approaches (per claim — an experiment testing N claims appears
  // N times so its lineage is fully visible under each claim it belongs to).
  // Within a claim, sort experiments by creation time so the lane reads as
  // research history left-to-right.
  for (const c of claims || []) {
    const exps = (expsByClaim.get(c.id) || []).slice()
      .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
    for (const e of exps) {
      const id = `exp:${e.id}@${c.id}`;
      const outcome = classifyExperiment(e);
      const node = {
        id,
        kind: 'experiment',
        label: e.intent || e.id,
        status: e.status,
        attempt_index: e.attempt_index,
        outcome,
        ref: e,
        claimId: c.id,
        layer: 1,
      };
      layers[1].push(node);
      nodes.push(node);
      edges.push({
        source: `claim:${c.id}`,
        target: id,
        type: 'tests',
        outcome,
      });
    }
  }

  // Layer 2: attempt chains — one mini-node per attempt under each experiment.
  for (const expNode of layers[1].slice()) {
    const e = expNode.ref;
    for (let a = 1; a <= (e.attempt_index || 1); a++) {
      const isFinal = a === (e.attempt_index || 1);
      const id = `att:${e.id}@${expNode.claimId}#${a}`;
      const node = {
        id,
        kind: 'attempt',
        label: `attempt ${a}`,
        attempt: a,
        isFinal,
        outcome: isFinal ? expNode.outcome : 'dead_end',
        ref: e,
        claimId: expNode.claimId,
        parentExpNodeId: expNode.id,
        layer: 2,
      };
      layers[2].push(node);
      nodes.push(node);
      if (a === 1) {
        edges.push({ source: expNode.id, target: id, type: 'attempts' });
      } else {
        const prev = `att:${e.id}@${expNode.claimId}#${a - 1}`;
        edges.push({ source: prev, target: id, type: 'attempts' });
      }
    }
  }

  // Layer 3: per-claim outcome buckets. Track both an experiment count
  // (how many distinct approaches landed here) and an attempt count
  // (how much effort funneled in) so the bucket can convey weight.
  const outcomeBuckets = new Map();
  for (const expNode of layers[1]) {
    const outcome = expNode.outcome;
    const key = `${expNode.claimId}|${outcome}`;
    if (!outcomeBuckets.has(key)) {
      const node = {
        id: `out:${expNode.claimId}|${outcome}`,
        kind: 'outcome',
        label: outcomeLabel(outcome),
        outcome,
        claimId: expNode.claimId,
        layer: 3,
        experimentCount: 0,
        attemptCount: 0,
      };
      outcomeBuckets.set(key, node);
      layers[3].push(node);
      nodes.push(node);
    }
    const bucket = outcomeBuckets.get(key);
    bucket.experimentCount += 1;
    bucket.attemptCount += expNode.ref.attempt_index || 1;
    const finalAttemptId = `att:${expNode.ref.id}@${expNode.claimId}#${expNode.ref.attempt_index || 1}`;
    edges.push({
      source: finalAttemptId,
      target: bucket.id,
      type: 'flows_to',
      outcome,
    });
  }

  // Winning lineage: claim → supporting experiment → final attempt → supports bucket.
  for (const expNode of layers[1]) {
    if (expNode.outcome !== 'supports') continue;
    winningPath.add(`claim:${expNode.claimId}`);
    winningPath.add(expNode.id);
    const finalAttId = `att:${expNode.ref.id}@${expNode.claimId}#${expNode.ref.attempt_index || 1}`;
    winningPath.add(finalAttId);
    winningPath.add(`out:${expNode.claimId}|supports`);
  }

  return { nodes, edges, layers, winningPath };
}

/**
 * Layer the DAG into a viewBox of (width × height) using time as the only
 * horizontal organizer. There are no per-claim lanes — every node sits on a
 * single global time axis. This lets each experiment take its full width
 * without being squeezed into a claim's slice.
 *
 *   - Layer 0 (Claim):    x = claim.created_at on the global time axis
 *   - Layer 1 (Approach): x = experiment.created_at on the global time axis
 *   - Layer 2 (Attempt):  x = parent experiment's x; y = stacked by attempt
 *   - Layer 3 (Outcome):  x = centroid of contributing experiments
 *
 * Same-date collisions get nudged apart by a 1-D resolver. Edges connecting
 * claims to their experiments will curve across the chart — that's the whole
 * point: the visual answers "which experiment tested which claim, and when?"
 *
 * `focusClaimId` (optional) restricts to one claim's sub-tree and gives it
 * the entire width. `timeScale` (optional, from projectTimeScale) caps the
 * X range to a known project window; otherwise it's derived from visible
 * nodes' timestamps.
 */
export function layoutLayeredDag(dag, width, height, focusClaimId = null, timeScale = null) {
  const padX = 60;
  const padTop = 90;   // room for time-axis labels above claims
  const padBot = 60;   // room for time-axis labels below outcomes
  const innerW = width - padX * 2;
  const innerH = height - padTop - padBot;
  const claimY = padTop;
  const expY = padTop + innerH * 0.30;
  const attemptTopY = padTop + innerH * 0.48;
  const attemptMaxY = padTop + innerH * 0.86;
  const outcomeY = padTop + innerH * 0.96;

  const allClaims = dag.layers[0];
  const visibleClaims = focusClaimId
    ? allClaims.filter(c => c.ref.id === focusClaimId)
    : allClaims;
  const visibleSet = new Set(visibleClaims.map(c => c.ref.id));
  for (const n of dag.nodes) {
    n.hidden = focusClaimId ? !visibleSet.has(n.claimId || n.ref?.id) : false;
  }

  // Derive the time axis from whatever's visible.
  const tsCandidates = [];
  for (const c of visibleClaims) {
    const t = Date.parse(c.ref.created_at);
    if (Number.isFinite(t)) tsCandidates.push(t);
  }
  for (const e of dag.layers[1].filter(n => !n.hidden)) {
    const t = Date.parse(e.ref.created_at);
    if (Number.isFinite(t)) tsCandidates.push(t);
  }
  const fallbackNow = Date.now();
  let tStart = timeScale?.start ?? (tsCandidates.length ? Math.min(...tsCandidates) : fallbackNow);
  let tEnd = timeScale?.end ?? (tsCandidates.length ? Math.max(...tsCandidates) : fallbackNow);
  if (tEnd <= tStart) tEnd = tStart + 24 * 60 * 60 * 1000;
  // Add small left/right time padding so first/last nodes don't kiss the edge.
  const span = tEnd - tStart;
  tStart -= span * 0.04;
  tEnd += span * 0.04;

  const xAt = (ms) => {
    const norm = Math.max(0, Math.min(1, (ms - tStart) / (tEnd - tStart)));
    return padX + norm * innerW;
  };

  // Expose the time scale on the dag for the renderer (axis ticks, gridlines).
  dag.timeAxis = { tStart, tEnd, xAt, padX, padTop, padBot, innerW, innerH, width, height };

  // Layer 0: claims at the top. Clamp to leave a half-width on either side
  // so the rect edges don't clip into the viewBox padding.
  const CLAIM_HALF = 115;
  for (const c of visibleClaims) {
    const t = Date.parse(c.ref.created_at);
    c.x = Number.isFinite(t) ? xAt(t) : padX + innerW / 2;
    c.y = claimY;
  }
  resolveCollisions(visibleClaims, 240, padX + CLAIM_HALF, padX + innerW - CLAIM_HALF);

  // Layer 1: experiments — one row, positioned by their own created_at.
  const EXP_HALF = 75;
  const exps = dag.layers[1].filter(n => !n.hidden);
  exps.sort((a, b) =>
    (a.ref.created_at || '').localeCompare(b.ref.created_at || ''),
  );
  for (const e of exps) {
    const t = Date.parse(e.ref.created_at);
    e.x = Number.isFinite(t) ? xAt(t) : padX + innerW / 2;
    e.y = expY;
  }
  resolveCollisions(exps, 170, padX + EXP_HALF, padX + innerW - EXP_HALF);

  // Layer 2: attempt chains hang under their parent experiment.
  const attempts = dag.layers[2].filter(n => !n.hidden);
  const expById = new Map(exps.map(e => [e.id, e]));
  for (const a of attempts) {
    const parent = expById.get(a.parentExpNodeId);
    if (!parent) {
      a.x = padX;
      a.y = attemptTopY;
      continue;
    }
    const total = parent.ref.attempt_index || 1;
    const stepBand = Math.max(0, attemptMaxY - attemptTopY);
    const step = total > 1 ? Math.min(26, stepBand / (total - 1)) : 0;
    a.x = parent.x;
    a.y = attemptTopY + (a.attempt - 1) * step;
  }

  // Layer 3: outcome buckets — placed at the centroid of contributing
  // experiments. Multiple buckets for the same claim get nudged apart.
  const outcomes = dag.layers[3].filter(n => !n.hidden);
  const expsByClaim = new Map();
  for (const e of exps) {
    if (!expsByClaim.has(e.claimId)) expsByClaim.set(e.claimId, []);
    expsByClaim.get(e.claimId).push(e);
  }
  for (const o of outcomes) {
    const claimExps = (expsByClaim.get(o.claimId) || []).filter(e => e.outcome === o.outcome);
    if (claimExps.length === 0) {
      o.x = padX + innerW / 2;
    } else {
      const sum = claimExps.reduce((s, e) => s + e.x, 0);
      o.x = sum / claimExps.length;
    }
    o.y = outcomeY;
  }
  const OUTCOME_HALF = 65;
  resolveCollisions(outcomes, 140, padX + OUTCOME_HALF, padX + innerW - OUTCOME_HALF);

  return dag;
}

/**
 * 1-D collision resolver. Sorts by x, then pushes overlapping pairs apart
 * iteratively. Clamps to [left, right] after each pass.
 */
function resolveCollisions(nodes, minGap, left, right) {
  if (!nodes || nodes.length < 2) return;
  // Don't mutate the caller's iteration order.
  const ordered = nodes.slice().sort((a, b) => a.x - b.x);
  let moved = true;
  let iter = 0;
  while (moved && iter < 40) {
    moved = false;
    iter++;
    for (let i = 0; i < ordered.length - 1; i++) {
      const a = ordered[i];
      const b = ordered[i + 1];
      const gap = b.x - a.x;
      if (gap < minGap) {
        const push = (minGap - gap) / 2;
        a.x -= push;
        b.x += push;
        moved = true;
      }
    }
    if (ordered[0].x < left) {
      const shift = left - ordered[0].x;
      for (const n of ordered) n.x += shift;
    }
    if (ordered[ordered.length - 1].x > right) {
      const shift = ordered[ordered.length - 1].x - right;
      for (const n of ordered) n.x -= shift;
    }
  }
}

/**
 * Compute small summary chips per claim — the at-a-glance score shown
 * alongside the claim node ("3 tested · 2 ✓ 1 ·").
 */
export function summarizeClaim(claim, dag) {
  const exps = dag.layers[1].filter(n => n.claimId === claim.ref.id);
  const tally = { supports: 0, refutes: 0, qualifies: 0, inflight: 0, abandoned: 0 };
  let attempts = 0;
  for (const e of exps) {
    tally[e.outcome] = (tally[e.outcome] || 0) + 1;
    attempts += e.attempt_index || 1;
  }
  return {
    tested: exps.length,
    attempts,
    tally,
  };
}

// ----- Chronology helpers ----------------------------------------------------

/**
 * Per-attempt start timestamps derived from durable events:
 *
 *   attempt 1 starts at experiment.created_at
 *   attempt k (k>=2) starts at the kth experiment.returned_to_planned event
 *
 * Plus an "end_or_now" tail = the timestamp of the latest event we found
 * for this experiment, or `now` for an experiment with no events past
 * creation. Useful for "this revision took N days" math.
 */
export function attemptTimings(experiment, events, now = Date.now()) {
  const expEvents = (events || [])
    .filter(e => e.target_id === experiment.id)
    .slice()
    .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
  const starts = [];
  const created = experiment.created_at;
  if (created) starts.push(Date.parse(created));
  for (const ev of expEvents) {
    if (ev.type === 'experiment.returned_to_planned') {
      const t = Date.parse(ev.created_at);
      if (Number.isFinite(t)) starts.push(t);
    }
  }
  const lastEv = expEvents[expEvents.length - 1];
  const endOrNow = lastEv ? Date.parse(lastEv.created_at) : now;
  return { starts, endOrNow };
}

/**
 * Derive the project's time scale: min/max across all experiment + claim
 * creation timestamps. Returned as { start, end } in ms epoch, with `end`
 * clamped to "now" if everything's in the past.
 */
export function projectTimeScale(claims, experiments, events, now = Date.now()) {
  let start = Infinity, end = -Infinity;
  const consider = ts => {
    const t = Date.parse(ts);
    if (Number.isFinite(t)) {
      if (t < start) start = t;
      if (t > end) end = t;
    }
  };
  for (const c of claims || []) consider(c.created_at);
  for (const e of experiments || []) consider(e.created_at);
  for (const ev of events || []) consider(ev.created_at);
  if (!Number.isFinite(start)) return null;
  if (end < now) end = now;
  return { start, end };
}

/**
 * mapLayout — pure layout math for the Experiment Map.
 *
 * Row packing, per-day ticks, and the clamped "now" line follow the
 * design-handoff prototype. The time axis does not (user respec, 7/18):
 * it is a NARRATIVE scale, not a calendar — order is exact (left = older),
 * but each consecutive gap maps to a sublinear spread, so a same-day burst
 * still fans out while an idle week barely stretches. Dates stay truthful
 * at the cards; the header ticks ride the elastic scale.
 * Pure data → data; no React, no fetches, unit-testable with node.
 */

export const CARD_W = 284;
export const CARD_H = 122;
export const ROW_H = 200;

// Per-step spread: a gap of h hours advances the axis by SPREAD_MIN px
// (simultaneous starts) up to SPREAD_MAX px, saturating at ~30 days. On this
// curve 5 hours ≈ 170px and 5 days ≈ 250px — "kind of equidistant".
export const SPREAD_MIN = 120;
export const SPREAD_MAX = 300;
const SPREAD_SAT_H = 720;
const spreadFrac = (h) => Math.min(1, Math.log1p(Math.max(0, h)) / Math.log1p(SPREAD_SAT_H));
const spreadFor = (h) => SPREAD_MIN + (SPREAD_MAX - SPREAD_MIN) * spreadFrac(h);

/**
 * Narrative time scale over experiment start times. Sorted unique times
 * become anchors; consecutive anchors sit spreadFor(gap) px apart. Returns
 * { anchors, xFor }; xFor interpolates linearly between anchors, clamps flat
 * before the first, and past the last ramps continuously to at most
 * SPREAD_MAX px — the "now" line can never run far away from the newest card.
 */
export function buildTimeScale(startTimesMs) {
  const times = [...new Set(startTimesMs)].filter(Number.isFinite).sort((a, b) => a - b);
  if (times.length === 0) return { anchors: [], xFor: () => 0 };
  const anchors = [{ t: times[0], x: 0 }];
  for (let i = 1; i < times.length; i++) {
    const prev = anchors[anchors.length - 1];
    anchors.push({ t: times[i], x: prev.x + spreadFor((times[i] - times[i - 1]) / 3600000) });
  }
  const xFor = (t) => {
    // Both tails ramp continuously to at most SPREAD_MAX px, so the world
    // stays anchored on the experiments no matter how stale the clock is.
    if (t <= anchors[0].t) return anchors[0].x - SPREAD_MAX * spreadFrac((anchors[0].t - t) / 3600000);
    for (let i = 1; i < anchors.length; i++) {
      if (t <= anchors[i].t) {
        const a = anchors[i - 1];
        const b = anchors[i];
        return a.x + ((t - a.t) / (b.t - a.t)) * (b.x - a.x);
      }
    }
    const la = anchors[anchors.length - 1];
    return la.x + SPREAD_MAX * spreadFrac((t - la.t) / 3600000);
  };
  return { anchors, xFor };
}

/**
 * Greedy row packing: sort by x, take the first row whose last occupant's
 * right edge sits more than 28px left of the new x; y = row × ROW_H.
 * items = [{ id, x }] → { id: { x, y } }.
 */
export function packRows(items) {
  const pos = {};
  const rows = [];
  const all = items.slice().sort((a, b) => a.x - b.x);
  for (const { id, x } of all) {
    let row = rows.findIndex((end) => x > end + 28);
    if (row === -1) { row = rows.length; rows.push(0); }
    rows[row] = x + CARD_W;
    pos[id] = { x, y: row * ROW_H };
  }
  return pos;
}

const p2 = (n) => String(n).padStart(2, '0');
// Local-time day key, matching the local-time labels the cards render.
const dayKey = (ms) => {
  const d = new Date(ms);
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
};

const DAY_MS = 86400000;
// Margin days stop where the elastic tails flatten below this per-day step —
// the calendar fades out instead of piling labels onto a saturated scale.
const MARGIN_STEP_MIN = 30;

/**
 * One axis tick per distinct start day, at that day's earliest event x − 24,
 * plus margin days riding the elastic tails: a few days before the first
 * experiment, and the days after the last one up to (never past) now. Wide
 * label ("Thu, Jul 2") when there's > 110px of room since the previous
 * label's end, tight ("Jul 2") otherwise. World coordinates — screen-space
 * culling belongs to the view.
 */
export function dayTicks(experiments, xFor, nowMs) {
  const byDay = {};
  for (const e of experiments) {
    if (!Number.isFinite(e.startMs)) continue;
    const day = dayKey(e.startMs);
    if (!(day in byDay) || e.startMs < byDay[day]) byDay[day] = e.startMs;
  }
  const eventDays = Object.values(byDay).sort((a, b) => a - b);
  if (eventDays.length === 0) return [];

  // Walk a tail one day at a time until the elastic step collapses.
  const tail = (fromMs, dir, keep) => {
    const out = [];
    let prevX = xFor(fromMs);
    for (let k = 1; k <= 14; k++) {
      const t = fromMs + dir * k * DAY_MS;
      if (!keep(t)) break;
      const x = xFor(t);
      if (Math.abs(x - prevX) < MARGIN_STEP_MIN) break;
      out.push(t);
      prevX = x;
    }
    return out;
  };
  const nowDay = Number.isFinite(nowMs) ? dayKey(nowMs) : null;
  const before = tail(eventDays[0], -1, () => true).reverse();
  const after = tail(eventDays[eventDays.length - 1], 1, (t) => (
    nowDay == null || (t < nowMs && dayKey(t) !== nowDay)
  ));

  const eventSet = new Set(eventDays);
  let lastEnd = -Infinity;
  return [...before, ...eventDays, ...after].map((t) => {
    const x = Math.round(xFor(t)) - 24;
    const wide = x - lastEnd > 110;
    const label = new Date(`${dayKey(t)}T12:00`).toLocaleDateString(
      'en-US',
      wide ? { weekday: 'short', month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric' },
    );
    lastEnd = x + label.length * 7.5;
    // m: margin day (no experiment) — the view culls these with lower priority.
    return { x, label, m: !eventSet.has(t) };
  });
}

// The now line: nothing may sit to its right, so clamp past the rightmost
// card's right edge + 48px even when wall-clock time maps further left.
export function nowX(xFor, positions, nowMs) {
  const xs = Object.values(positions).map((p) => p.x);
  const rightMost = (xs.length ? Math.max(...xs) : 0) + CARD_W;
  return Math.round(Math.max(xFor(nowMs), rightMost + 48));
}

/**
 * Composed layout for the map. cards = [{ id, startMs }] →
 * { pos, ticks, nowX, bounds, xFor }; bounds covers the card rects.
 * (xFor is included beyond the base contract so the view can refresh the
 * cheap now-clamp without re-packing.)
 */
export function computeLayout(cards, nowMs) {
  const scale = buildTimeScale(cards.map((c) => c.startMs));
  const pos = packRows(cards.map((c) => ({ id: c.id, x: Math.round(scale.xFor(c.startMs)) })));
  const ticks = dayTicks(cards, scale.xFor, nowMs);
  const nx = nowX(scale.xFor, pos, nowMs);
  let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
  for (const p of Object.values(pos)) {
    if (p.x < minX) minX = p.x;
    if (p.x + CARD_W > maxX) maxX = p.x + CARD_W;
    if (p.y < minY) minY = p.y;
    if (p.y + CARD_H > maxY) maxY = p.y + CARD_H;
  }
  if (!Number.isFinite(minX)) { minX = 0; maxX = 0; minY = 0; maxY = 0; }
  return { pos, ticks, nowX: nx, bounds: { minX, maxX, minY, maxY }, xFor: scale.xFor };
}

import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { parseIntent } from '../utils/intent';

/**
 * LiveJobsTimeline (exported as ProjectDashboard for back-compat) — Gantt-
 * style "what's going on now" visual.
 *
 *   ┌─ N running ─────────────────────── 1d │ 7d │ 30d ─┐
 *   │  Mon   Tue   Wed   Thu   Fri   Sat   Sun ⏐now    │
 *   │  ─────────────────────────────────────────────── │
 *   │  job_…  → exp_…  python run.py        ▓▓▓▓▓▓⏐44m │
 *   │  job_…  → exp_…  python run.py        ▓▓▓▓▓▓▓⏐7h │
 *   │  …                                                │
 *   └───────────────────────────────────────────────────┘
 *
 * Scope: shows only currently in-flight Ray jobs (status ∈ {submitting,
 * queued, running}). Each lane carries one job; the bar runs from start
 * time to the live "now" edge. Older jobs whose start time is before the
 * selected window get a clipped left edge with a small `«` indicator so
 * they read as "this is older than the window suggests".
 *
 * Live-vs-done isn't a concern here because every lane is live by
 * definition. What needs to read in a glance is:
 *   - How long has each thing been running? → bar width relative to window
 *   - Which experiment owns it? → meta row under the bar
 *   - Is anything stuck/queued? → bar style (solid vs diagonal stripes)
 *
 * Layout: fixed-height scrollable body so the panel doesn't grow without
 * bound. Most-recently-submitted lanes pinned to the top.
 */

const WINDOWS = [
  { id: '1d',  label: '1d',  ms: 24 * 60 * 60 * 1000 },
  { id: '7d',  label: '7d',  ms: 7  * 24 * 60 * 60 * 1000 },
  { id: '30d', label: '30d', ms: 30 * 24 * 60 * 60 * 1000 },
];
const DEFAULT_WINDOW_ID = '7d';
const WINDOW_STORAGE_KEY = 'rsui:liveTimeline:window';
const TICK_MS = 1000;
const BODY_MAX_HEIGHT_PX = 320;

const ACTIVE_STATUSES = new Set(['running', 'queued', 'submitting']);
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);

/**
 * Filter buckets exposed to the user. Each bucket maps to a set of underlying
 * job statuses. Default = all on (show everything); users can isolate slices
 * when catching up. Choice persists in localStorage.
 */
const FILTER_BUCKETS = [
  { id: 'active',    label: 'Active',    statuses: new Set(['running', 'queued', 'submitting']) },
  { id: 'succeeded', label: 'Succeeded', statuses: new Set(['succeeded']) },
  { id: 'failed',    label: 'Failed',    statuses: new Set(['failed']) },
  { id: 'cancelled', label: 'Cancelled', statuses: new Set(['cancelled']) },
];
const FILTER_STORAGE_KEY = 'rsui:liveTimeline:filters';

// Every bar gets this much width unconditionally — enough to fit the in-bar
// duration label even when the actual elapsed time would render as a sliver
// (e.g. a 60-second job on a 30-day axis). Additional width is added strictly
// in proportion to elapsed time, so longer jobs still visibly read as longer.
const BAR_BASE_WIDTH_PX = 50;

function parseTs(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

function fmtDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

/**
 * Coarse "X ago" formatter for catch-up context — calmer than fmtDuration's
 * minute-precision. "just now" for <60s so a freshly-finished job has a soft
 * readable label rather than ticking by the second.
 */
function fmtAgo(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const s = Math.floor(ms / 1000);
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d === 1) return 'yesterday';
  return `${d}d ago`;
}

function dayStart(ms) {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/**
 * Tick anchors picked for natural readability per window:
 *   1d  → every 4 hours (HH:00)
 *   7d  → each midnight (Mon, Tue, …)
 *   30d → every 7 days, weeks ago (M/D)
 *
 * Ticks within 4% of the right edge are dropped so they don't collide with
 * the always-on `now` label.
 */
function buildAxisTicks(start, end, windowId) {
  const ticks = [];
  if (windowId === '1d') {
    const stepMs = 4 * 60 * 60 * 1000;
    const anchor = new Date(end);
    anchor.setMinutes(0, 0, 0);
    anchor.setHours(Math.floor(anchor.getHours() / 4) * 4);
    let t = anchor.getTime();
    while (t > start) {
      ticks.unshift({ ms: t, label: new Date(t).toLocaleTimeString([], { hour: '2-digit', hour12: false }) + ':00' });
      t -= stepMs;
    }
  } else if (windowId === '7d') {
    let t = dayStart(end);
    while (t > start) {
      ticks.unshift({ ms: t, label: new Date(t).toLocaleDateString([], { weekday: 'short' }) });
      t -= 24 * 60 * 60 * 1000;
    }
  } else {
    let t = dayStart(end);
    while (t > start) {
      const d = new Date(t);
      ticks.unshift({ ms: t, label: `${d.getMonth() + 1}/${d.getDate()}` });
      t -= 7 * 24 * 60 * 60 * 1000;
    }
  }
  const minGap = (end - start) * 0.04;
  return ticks.filter((tk) => end - tk.ms > minGap);
}

function loadStored(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}
function saveStored(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}
function loadStoredSet(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return new Set(fallback);
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : fallback);
  } catch { return new Set(fallback); }
}
function saveStoredSet(key, set) {
  try { localStorage.setItem(key, JSON.stringify([...set])); } catch {}
}

function clampPct(p) { return Math.max(0, Math.min(100, p)); }

export default function ProjectDashboard({ jobs = [], experiments = [] }) {
  const [windowId, setWindowId] = useState(() => loadStored(WINDOW_STORAGE_KEY, DEFAULT_WINDOW_ID));
  const [activeFilters, setActiveFilters] = useState(() =>
    loadStoredSet(FILTER_STORAGE_KEY, FILTER_BUCKETS.map((b) => b.id)),
  );
  const [now, setNow] = useState(() => Date.now());
  const bodyRef = useRef(null);
  const [scrollbarWidth, setScrollbarWidth] = useState(0);

  // 1s tick so running bars visibly grow against the `now` edge between
  // /home polls (every 3s). Terminal bars are fixed in position so this
  // also keeps the relative "X ago" anchor right.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);

  // Measure the body's scrollbar width (offsetWidth - clientWidth). The `now`
  // line lives OUTSIDE the scrolling body so it doesn't scroll with the lane
  // list, but the bars sit at the body's content-area right edge — which is
  // `scrollbarWidth` px short of the body's outer right when a scrollbar is
  // present. The line offsets by that exact amount to stay aligned.
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return undefined;
    const measure = () => setScrollbarWidth(el.offsetWidth - el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const windowDef = WINDOWS.find((w) => w.id === windowId) || WINDOWS[1];
  const windowEnd = now;
  const windowStart = now - windowDef.ms;
  const windowSpan = windowDef.ms;

  // Experiment id → experiment, used to resolve the headline title for every
  // job (active jobs already carry a compact `experiment` summary; terminal
  // jobs from /jobs don't, so we fall back to this lookup).
  const experimentsById = useMemo(() => {
    const m = new Map();
    for (const exp of experiments) if (exp?.id) m.set(exp.id, exp);
    return m;
  }, [experiments]);

  // One lane per job that overlaps the window. Anchor = the most recent
  // timestamp on the job (finished_at for terminal, started/submitted for
  // active). Sort by anchor desc so the most-recent activity sits on top.
  // We compute lanes BEFORE the filter so the chip counts stay honest — they
  // always reflect the window, not the current visibility selection.
  const lanesAll = useMemo(() => {
    return jobs
      .map((job) => {
        const status = String(job?.status || '').toLowerCase();
        const submittedMs = parseTs(job.submitted_at);
        const startedMs = parseTs(job.started_at);
        const finishedMs = parseTs(job.finished_at);
        const startMs = submittedMs || startedMs || finishedMs || null;
        const isActive = ACTIVE_STATUSES.has(status);
        const endMs = isActive ? now : (finishedMs || startedMs || submittedMs || now);
        const anchorMs = finishedMs || startedMs || submittedMs || 0;
        return { job, status, isActive, startMs, endMs, anchorMs };
      })
      .filter((l) => l.startMs !== null)
      .filter((l) => l.endMs > windowStart && l.startMs < windowEnd)
      .sort((a, b) => b.anchorMs - a.anchorMs);
  }, [jobs, now, windowStart, windowEnd]);

  // Bucket counts drive the chip labels — they reflect what's actually in the
  // window, so a user can tell at a glance "5 done, 2 failed today" without
  // toggling anything.
  const bucketCounts = useMemo(() => {
    const counts = {};
    for (const bucket of FILTER_BUCKETS) counts[bucket.id] = 0;
    for (const lane of lanesAll) {
      for (const bucket of FILTER_BUCKETS) {
        if (bucket.statuses.has(lane.status)) counts[bucket.id] += 1;
      }
    }
    return counts;
  }, [lanesAll]);

  const lanes = useMemo(() => {
    return lanesAll.filter((lane) => {
      for (const bucket of FILTER_BUCKETS) {
        if (bucket.statuses.has(lane.status)) return activeFilters.has(bucket.id);
      }
      return true; // status we don't recognise — show by default
    });
  }, [lanesAll, activeFilters]);

  const ticks = useMemo(
    () => buildAxisTicks(windowStart, windowEnd, windowId),
    [windowStart, windowEnd, windowId],
  );

  const handleWindowChange = (id) => {
    setWindowId(id);
    saveStored(WINDOW_STORAGE_KEY, id);
  };

  const toggleFilter = (bucketId) => {
    setActiveFilters((prev) => {
      const next = new Set(prev);
      if (next.has(bucketId)) next.delete(bucketId);
      else next.add(bucketId);
      // Never let the user filter to zero — that produces an empty timeline
      // that looks broken. Re-enable the chip they just clicked off if so.
      if (next.size === 0) next.add(bucketId);
      saveStoredSet(FILTER_STORAGE_KEY, next);
      return next;
    });
  };

  return (
    <div className="live-timeline">
      <div className="live-timeline-header">
        <h2 className="live-timeline-title">Process Timeline</h2>
        <div className="live-timeline-windows" role="tablist" aria-label="Time window">
          {WINDOWS.map((w) => (
            <button
              key={w.id}
              type="button"
              role="tab"
              aria-selected={w.id === windowId}
              className={`live-timeline-window-btn${w.id === windowId ? ' live-timeline-window-btn--active' : ''}`}
              onClick={() => handleWindowChange(w.id)}
            >{w.label}</button>
          ))}
        </div>
      </div>

      {/* Filter / status row — chips show counts in the window, click to toggle.
          When all chips are on you get the unfiltered view; turning chips off
          isolates failures / actives for triage. */}
      <div className="live-timeline-filters" role="group" aria-label="Filter by status">
        {FILTER_BUCKETS.map((b) => {
          const isOn = activeFilters.has(b.id);
          const count = bucketCounts[b.id] || 0;
          return (
            <button
              key={b.id}
              type="button"
              aria-pressed={isOn}
              className={`live-timeline-chip live-timeline-chip--${b.id}${isOn ? ' live-timeline-chip--on' : ''}`}
              onClick={() => toggleFilter(b.id)}
              title={`${isOn ? 'Hide' : 'Show'} ${b.label.toLowerCase()} (${count} in window)`}
            >
              <span className="live-timeline-chip-dot" aria-hidden="true" />
              <span className="live-timeline-chip-label">{b.label}</span>
              <span className="live-timeline-chip-count tabular">{count}</span>
            </button>
          );
        })}
      </div>

      {lanes.length === 0 ? (
        <div className="live-timeline-empty">
          <div className="live-timeline-empty-dot" aria-hidden="true" />
          <span>
            {lanesAll.length === 0
              ? 'No processes in this window.'
              : 'No processes match the current filters.'}
          </span>
        </div>
      ) : (
        // The frame holds axis + scrollable body + now-line overlay. By
        // exposing the body's measured scrollbar width as a CSS custom
        // property, the axis-wrap and the now-line both reserve exactly
        // that much right-side space — keeping axis ticks, bars, and the
        // now-line aligned whether or not the body actually has a scrollbar.
        <div
          className="live-timeline-frame"
          style={{ '--scrollbar-w': `${scrollbarWidth}px` }}
        >
          <div className="live-timeline-axis-wrap">
            <div className="live-timeline-axis">
              {ticks.map((tk) => {
                const left = ((tk.ms - windowStart) / windowSpan) * 100;
                return (
                  <div key={tk.ms} className="live-timeline-tick" style={{ left: `${left}%` }}>
                    <span className="live-timeline-tick-label">{tk.label}</span>
                  </div>
                );
              })}
              <div className="live-timeline-tick live-timeline-tick--now" style={{ left: '100%' }}>
                <span className="live-timeline-tick-label">now</span>
              </div>
            </div>
          </div>

          <div
            className="live-timeline-body"
            ref={bodyRef}
            style={{ maxHeight: BODY_MAX_HEIGHT_PX }}
          >
            {lanes.map(({ job, status, isActive, startMs, endMs }) => (
              <Lane
                key={job.id}
                job={job}
                status={status}
                isActive={isActive}
                startMs={startMs}
                endMs={endMs}
                experiment={experimentsById.get(job.experiment_id)}
                windowEnd={windowEnd}
                windowSpan={windowSpan}
                now={now}
                ticks={ticks}
              />
            ))}
          </div>

          {/* `now` overlay lives in the frame (outside the scrolling body)
              so it never scrolls with lanes. The right offset = body's
              right padding + measured scrollbar width, so the line sits
              exactly where the bars terminate. */}
          <div className="live-timeline-grid-now" aria-hidden="true" />
        </div>
      )}
    </div>
  );
}

/**
 * Last resort if intent is empty: pull the script basename out of the command
 * so the lane has *some* semantic label besides a hex id.
 */
function extractScript(cmd) {
  if (!cmd) return '';
  const tokens = cmd.split(/\s+/);
  for (const tok of tokens) {
    if (/\.(py|sh|js|ts)$/.test(tok)) {
      const slashIdx = tok.lastIndexOf('/');
      return slashIdx >= 0 ? tok.slice(slashIdx + 1) : tok;
    }
  }
  return tokens[1] || '';
}

function Lane({ job, status, isActive, startMs, endMs, experiment, windowEnd, windowSpan, now, ticks = [] }) {
  const windowStart = windowEnd - windowSpan;
  const durationMs = Math.max(0, endMs - startMs);
  const rawWidthPct = (durationMs / windowSpan) * 100;
  // Right-anchor each bar to its `endMs` position. For active jobs endMs ==
  // now, so the bar reaches the right edge; for terminal jobs the bar's
  // right edge sits where the job actually finished, leaving space to the
  // right of it on the timeline. Width = base + proportional duration.
  const rightPct = clampPct(((windowEnd - endMs) / windowSpan) * 100);
  const widthCss = `min(${100 - rightPct}%, calc(${BAR_BASE_WIDTH_PX}px + ${rawWidthPct}%))`;
  const startsBeforeWindow = startMs < windowStart;
  const elapsed = fmtDuration(durationMs);

  // Headline = "what this job is doing", picked from the richest available
  // semantic source. The compact `experiment` field on the active /home rows
  // and the looked-up experiment from /home.experiments are both checked so
  // active + terminal jobs render identically.
  const intentSource = experiment?.intent || job.experiment?.intent || '';
  const expTitle = parseIntent(intentSource).title;
  const scriptName = extractScript(job.command);
  const headline = expTitle || scriptName || job.experiment_id || job.id;
  const attempt = experiment?.attempt_index ?? job.experiment?.attempt_index ?? job.attempt_index;

  const barCls = [
    'live-timeline-bar',
    `live-timeline-bar--${status}`,
    startsBeforeWindow ? 'live-timeline-bar--clipped' : '',
  ].filter(Boolean).join(' ');

  // Tooltip preserves the full forensic detail for when someone actually
  // needs to dig in — without putting it in the row layout. For failed jobs
  // the error message is the most valuable thing to surface on hover.
  const tooltipText = [
    `${status}${isActive ? '' : ` · ran ${elapsed}`}`,
    job.id,
    job.experiment_id ? `experiment ${job.experiment_id}` : null,
    job.command || null,
    job.error ? `error: ${String(job.error).slice(0, 240)}` : null,
  ].filter(Boolean).join('\n');

  return (
    <div className="live-timeline-lane">
      <div className="live-timeline-track">
        {/* Per-track grid lines so temporal reference is visible alongside
            the bar without crossing into the description row below. */}
        {ticks.map((tk) => {
          const left = ((tk.ms - windowStart) / windowSpan) * 100;
          return (
            <div
              key={tk.ms}
              className="live-timeline-track-grid-line"
              style={{ left: `${left}%` }}
              aria-hidden="true"
            />
          );
        })}
        <Link
          to={`/jobs#${job.id}`}
          className={barCls}
          style={{ right: `${rightPct}%`, width: widthCss }}
          title={tooltipText}
        >
          <span className="live-timeline-bar-duration tabular">
            {elapsed}
            {status === 'succeeded' && <span className="live-timeline-bar-glyph live-timeline-bar-glyph--ok" aria-label="succeeded">✓</span>}
            {status === 'failed'    && <span className="live-timeline-bar-glyph live-timeline-bar-glyph--fail" aria-label="failed">✗</span>}
            {status === 'cancelled' && <span className="live-timeline-bar-glyph live-timeline-bar-glyph--cancel" aria-label="cancelled">⊘</span>}
          </span>
        </Link>
      </div>
      <div className="live-timeline-desc">
        <span className="live-timeline-desc-title" title={expTitle ? `${expTitle}\n${job.id}` : tooltipText}>
          {headline}
        </span>
      </div>
    </div>
  );
}

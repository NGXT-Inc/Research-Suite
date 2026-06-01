import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import ObjId from './ObjId';

/**
 * ProcessTimeline — horizontal Gantt-style strip showing every Ray job in the
 * project as a positioned, status-colored segment.
 *
 * Why this shape:
 *   - At-a-glance concurrency: overlapping segments visualise "what's running
 *     together right now" without forcing the reader to read text.
 *   - Toned-down completed segments stay visible so the user can spot the
 *     immediate past (just-failed runs, recently-finished long jobs) without
 *     them dominating the active slice.
 *   - Filter chips drive the visible set. Default is `running` only; flip
 *     others on to widen the view. Preference persists per browser.
 *
 * Time axis:
 *   The right edge is "now" (auto-ticking). The left edge is the wider of
 *   `minWindowMs` (default 30 min) and the earliest visible job start, capped
 *   at `maxWindowMs` (default 4h) so a stray week-old run can't crush the
 *   scale.
 *
 * Live-tick:
 *   Running segments extend to `now` and a 1s interval moves the clock so the
 *   bars visibly grow between /home polls (which fire every 3s).
 */

const ALL_STATUSES = ['running', 'queued', 'submitting', 'succeeded', 'failed', 'cancelled'];
const ACTIVE_STATUSES = new Set(['running', 'queued', 'submitting']);
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);
const DEFAULT_VISIBLE = new Set(['running']);
const FILTER_STORAGE_KEY = 'rsui:processTimeline:filters';

const MIN_WINDOW_MS = 30 * 60 * 1000;   // 30 min
const MAX_WINDOW_MS = 4 * 60 * 60 * 1000; // 4 h
const TICK_MS = 1000;
const LANE_HEIGHT = 22;
const LANE_GAP = 4;
const MIN_SEGMENT_PX = 6;

function loadFilters() {
  try {
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    if (!raw) return new Set(DEFAULT_VISIBLE);
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr) || arr.length === 0) return new Set(DEFAULT_VISIBLE);
    return new Set(arr.filter((s) => ALL_STATUSES.includes(s)));
  } catch { return new Set(DEFAULT_VISIBLE); }
}

function saveFilters(set) {
  try { localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify([...set])); } catch {}
}

function parseTs(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

function fmtClock(ms) {
  const d = new Date(ms);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

export default function ProcessTimeline({ jobs }) {
  const [visible, setVisible] = useState(loadFilters);
  const [now, setNow] = useState(() => Date.now());
  const containerRef = useRef(null);
  const [containerWidth, setContainerWidth] = useState(0);

  // 1s tick so the right edge + active segment widths move smoothly.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);

  // Track container width so segment pixel widths recompute on resize.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return undefined;
    const update = () => setContainerWidth(el.clientWidth);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const toggleStatus = (status) => {
    setVisible((prev) => {
      const next = new Set(prev);
      if (next.has(status)) next.delete(status);
      else next.add(status);
      saveFilters(next);
      return next;
    });
  };

  // Counts by status — drives the chip labels and the right-aligned summary.
  const counts = useMemo(() => {
    const c = Object.fromEntries(ALL_STATUSES.map((s) => [s, 0]));
    for (const j of jobs) {
      const s = String(j?.status || '').toLowerCase();
      if (s in c) c[s] += 1;
    }
    return c;
  }, [jobs]);

  // Hydrate each job with a normalized segment {startMs, endMs, status}.
  // submitted_at is the earliest signal; started_at preferred when present so
  // queue time is visible as a leading dashed slice (rendered separately).
  const hydrated = useMemo(() => {
    return (jobs || [])
      .map((job) => {
        const status = String(job?.status || '').toLowerCase();
        const submittedMs = parseTs(job.submitted_at);
        const startedMs = parseTs(job.started_at);
        const finishedMs = parseTs(job.finished_at);
        return {
          job,
          status,
          submittedMs,
          startedMs,
          finishedMs,
          // canonical anchor for sorting / window-fit
          anchorMs: submittedMs || startedMs || finishedMs || null,
        };
      })
      .filter((row) => row.anchorMs !== null);
  }, [jobs]);

  const filtered = useMemo(
    () => hydrated.filter((row) => visible.has(row.status)),
    [hydrated, visible],
  );

  // Compute the [windowStart, windowEnd] range. End is `now`. Start fits the
  // earliest visible segment plus the min window, clamped to maxWindow.
  const windowEnd = now;
  const windowStart = useMemo(() => {
    const fromJobs = filtered.reduce(
      (acc, row) => Math.min(acc, row.anchorMs),
      windowEnd,
    );
    const padded = windowEnd - Math.max(MIN_WINDOW_MS, windowEnd - fromJobs);
    return Math.max(padded, windowEnd - MAX_WINDOW_MS);
  }, [filtered, windowEnd]);
  const windowSpan = Math.max(1, windowEnd - windowStart);

  // Sort: most-recently-anchored on top.
  const lanes = useMemo(
    () => [...filtered].sort((a, b) => b.anchorMs - a.anchorMs),
    [filtered],
  );

  const axisTicks = useMemo(() => buildTicks(windowStart, windowEnd), [windowStart, windowEnd]);

  return (
    <div className="process-timeline" ref={containerRef}>
      <div className="process-timeline-header">
        <div className="process-filter-row">
          {ALL_STATUSES.map((status) => {
            const isOn = visible.has(status);
            const n = counts[status];
            return (
              <button
                key={status}
                type="button"
                className={`process-filter-chip process-filter-chip--${status}`}
                aria-pressed={isOn}
                onClick={() => toggleStatus(status)}
                title={`Toggle ${status} (${n})`}
              >
                <span className="process-filter-chip-dot" />
                <span className="process-filter-chip-label">{status}</span>
                <span className="process-filter-chip-count tabular">{n}</span>
              </button>
            );
          })}
        </div>
        <div className="process-timeline-summary faint">
          {filtered.length} of {hydrated.length} shown
        </div>
      </div>

      {hydrated.length === 0 ? (
        <div className="empty" style={{ marginTop: 8 }}>No processes recorded yet.</div>
      ) : filtered.length === 0 ? (
        <div className="empty" style={{ marginTop: 8 }}>
          No processes match the current filters. Toggle a chip above.
        </div>
      ) : (
        <>
          <div className="process-timeline-axis" aria-hidden="true">
            {axisTicks.map((t) => {
              const left = ((t - windowStart) / windowSpan) * 100;
              return (
                <div key={t} className="process-timeline-tick" style={{ left: `${left}%` }}>
                  <span className="process-timeline-tick-label">{fmtClock(t)}</span>
                </div>
              );
            })}
            <div className="process-timeline-tick process-timeline-tick--now" style={{ left: '100%' }}>
              <span className="process-timeline-tick-label">now</span>
            </div>
          </div>

          <div
            className="process-timeline-body"
            style={{ height: lanes.length * (LANE_HEIGHT + LANE_GAP) }}
          >
            {/* Vertical guide lines for each tick, behind the lanes */}
            <div className="process-timeline-grid" aria-hidden="true">
              {axisTicks.map((t) => {
                const left = ((t - windowStart) / windowSpan) * 100;
                return <div key={t} className="process-timeline-grid-line" style={{ left: `${left}%` }} />;
              })}
            </div>

            {lanes.map((row, i) => (
              <Lane
                key={row.job.id}
                row={row}
                top={i * (LANE_HEIGHT + LANE_GAP)}
                windowStart={windowStart}
                windowSpan={windowSpan}
                now={now}
                containerWidth={containerWidth}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * One job lane = a horizontal stripe with the experiment chip on the left and
 * one (or two, for queued→running) positioned segments. Hover surfaces the
 * command + duration; click deep-links to /jobs#<id>.
 */
function Lane({ row, top, windowStart, windowSpan, now, containerWidth }) {
  const { job, status, submittedMs, startedMs, finishedMs } = row;
  const isActive = ACTIVE_STATUSES.has(status);

  // Render order:
  //   1. queue slice  (submitted → started) — dashed, "queued" tint
  //   2. run slice    (started   → finished|now)
  // For jobs that never moved past submitting/queued, only slice 1 exists and
  // its end is "now" (or finished_at if cancelled).
  const segments = [];
  if (startedMs && submittedMs && startedMs > submittedMs) {
    segments.push({
      kind: 'queue',
      startMs: submittedMs,
      endMs: startedMs,
    });
  }
  if (startedMs) {
    segments.push({
      kind: 'run',
      startMs: startedMs,
      endMs: finishedMs || (isActive ? now : startedMs),
    });
  } else {
    // No start yet — the only span we have is queue/submitting time.
    segments.push({
      kind: 'queue',
      startMs: submittedMs || finishedMs || now,
      endMs: finishedMs || (isActive ? now : submittedMs || now),
    });
  }

  const runSeg = segments.find((s) => s.kind === 'run');
  const totalMs = runSeg
    ? runSeg.endMs - runSeg.startMs
    : Math.max(0, (finishedMs || now) - (submittedMs || now));

  return (
    <div className="process-lane" style={{ top }}>
      <div className="process-lane-label">
        {job.experiment_id ? (
          <Link to={`/experiments/${job.experiment_id}`} title="Open experiment">
            <ObjId id={job.experiment_id} />
          </Link>
        ) : (
          <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>—</span>
        )}
      </div>
      <div className="process-lane-track">
        {segments.map((seg, i) => {
          const left = clampPct(((seg.startMs - windowStart) / windowSpan) * 100);
          const widthPct = ((seg.endMs - seg.startMs) / windowSpan) * 100;
          const widthPx = (widthPct / 100) * (containerWidth || 1);
          const effectiveWidth = Math.max(widthPct, (MIN_SEGMENT_PX / Math.max(containerWidth, 1)) * 100);
          const cls = [
            'process-segment',
            `process-segment--${seg.kind}`,
            `process-segment--${status}`,
            TERMINAL_STATUSES.has(status) ? 'process-segment--terminal' : '',
            seg.kind === 'run' && isActive ? 'process-segment--live' : '',
          ].filter(Boolean).join(' ');
          return (
            <Link
              key={i}
              to={`/jobs#${job.id}`}
              className={cls}
              style={{ left: `${left}%`, width: `${effectiveWidth}%` }}
              title={tooltipFor(job, seg, totalMs, widthPx)}
            >
              {widthPx > 60 && (
                <span className="process-segment-label">
                  {seg.kind === 'run' ? fmtDuration(seg.endMs - seg.startMs) : 'queued'}
                </span>
              )}
            </Link>
          );
        })}
      </div>
    </div>
  );
}

function tooltipFor(job, seg, totalMs, widthPx) {
  const cmd = job.command || '';
  const elapsed = seg.kind === 'run' ? fmtDuration(seg.endMs - seg.startMs) : fmtDuration(seg.endMs - seg.startMs);
  const head = seg.kind === 'queue' ? `queued for ${elapsed}` : `${job.status} · ${elapsed}`;
  const exp = job.experiment_id ? ` · ${job.experiment_id}` : '';
  return `${head}${exp}\n${cmd}`;
}

function clampPct(p) { return Math.max(0, Math.min(100, p)); }

function buildTicks(start, end) {
  const span = end - start;
  // Aim for ~5 tick labels across the strip.
  const candidates = [60_000, 5 * 60_000, 10 * 60_000, 15 * 60_000, 30 * 60_000, 60 * 60_000, 2 * 60 * 60_000];
  const target = span / 5;
  let step = candidates[0];
  for (const c of candidates) if (c <= target * 1.2) step = c;
  const first = Math.ceil(start / step) * step;
  const ticks = [];
  // Suppress any tick within ~half a step of the right edge — the always-on
  // "now" marker collides with it visually.
  const minGapFromEnd = step * 0.4;
  for (let t = first; t < end - minGapFromEnd; t += step) ticks.push(t);
  return ticks;
}

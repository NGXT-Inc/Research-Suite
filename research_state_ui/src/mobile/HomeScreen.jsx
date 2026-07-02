import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';
import {
  useProjectStore,
  useProjectHref,
  selectProject,
  selectStats,
  selectActiveExperiments,
  selectReviewRequests,
  selectSandboxes,
  selectExperiments,
} from '../store/useProjectStore';
import { expName } from '../utils/experiment';
import { fmtDuration } from '../utils/format';
import { goodDirection, curveValues } from '../utils/metrics';

const REVIEW_STATES = new Set(['design_review', 'experiment_review']);
const SOON_MS = 30 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
const METRICS_POLL_MS = 12000;

// "8×H100" / "8x H100" → 8; bare "H100" → 1; no gpu → 0.
function gpuCountOf(sandbox) {
  const gpu = sandbox?.gpu || '';
  if (!gpu) return 0;
  const m = gpu.match(/(\d+)\s*[x×]/i);
  return m ? Number(m[1]) : 1;
}

// Preferred live-metric key: a loss-like curve first, else the first curve.
function pickMetricKey(history) {
  const keys = Object.keys(history || {});
  return keys.find(k => /loss/i.test(k)) || keys[0] || null;
}

function fmtStanding(d) {
  const day = d.toLocaleDateString([], { weekday: 'short' });
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    .toLowerCase().replace(/\s/g, '');
  return `${day} · ${time}`;
}

// GPU compute over the trailing 24h: Σ(overlap-with-window × gpu-count) per
// sandbox. Approximate by design — released sandboxes without an end stamp
// fall back to their expiry.
function gpuHours24(sandboxes, now) {
  const windowStart = now - DAY_MS;
  let ms = 0;
  for (const s of sandboxes) {
    const gpus = gpuCountOf(s);
    if (!gpus || !s.requested_at) continue;
    const start = Date.parse(s.requested_at);
    if (!Number.isFinite(start)) continue;
    const rawEnd = s.status === 'running'
      ? now
      : Date.parse(s.released_at || s.expires_at || s.updated_at || '') || start;
    const overlap = Math.min(rawEnd, now) - Math.max(start, windowStart);
    if (overlap > 0) ms += overlap * gpus;
  }
  const h = ms / 3600000;
  if (h <= 0) return '0';
  if (h < 10) return String(Math.round(h * 10) / 10);
  return String(Math.round(h));
}

// Live GPU-util/VRAM sampler — polls only while a running sandbox exists.
function useLiveGpu(projectId, sandbox) {
  const [gpu, setGpu] = useState(null); // {util, vram}
  const sandboxUid = sandbox?.sandbox_uid || null;
  const experimentId = sandbox?.experiment_id
    || (Array.isArray(sandbox?.active_experiment_ids) ? sandbox.active_experiment_ids[0] : null);
  useEffect(() => {
    if (!projectId || !sandbox || sandbox.status !== 'running') { setGpu(null); return undefined; }
    let cancelled = false;
    const sample = async () => {
      try {
        const res = await api.getSandboxMetrics(projectId, experimentId, { sandboxUid });
        if (cancelled) return;
        const gpus = Array.isArray(res?.metrics?.gpus) ? res.metrics.gpus : [];
        if (!res?.available || gpus.length === 0) { setGpu(null); return; }
        const util = gpus.reduce((m, g) => Math.max(m, g.util_pct ?? 0), 0);
        const withMem = gpus.filter(g => g.mem_total_mib);
        const vram = withMem.length
          ? Math.round(100 * withMem.reduce((a, g) => a + (g.mem_used_mib || 0), 0)
            / withMem.reduce((a, g) => a + g.mem_total_mib, 0))
          : null;
        setGpu({ util: Math.round(util), vram });
      } catch { /* live usage is best-effort */ }
    };
    sample();
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') sample();
    }, METRICS_POLL_MS);
    return () => { cancelled = true; clearInterval(t); };
  }, [projectId, experimentId, sandboxUid, sandbox?.status]);
  return gpu;
}

// Latest metric curve for the live experiment, from the durable MLflow ledger.
function useLiveMetric(projectId, experimentId) {
  const [metric, setMetric] = useState(null); // {key, values:[…last 30], last}
  useEffect(() => {
    if (!projectId || !experimentId) { setMetric(null); return undefined; }
    let cancelled = false;
    const load = async () => {
      try {
        const res = await api.getResultsMetrics(projectId, experimentId);
        if (cancelled) return;
        const runs = (Array.isArray(res?.experiments) ? res.experiments : [])
          .flatMap(e => (Array.isArray(e.runs) ? e.runs : []));
        // Latest run that actually logged a curve.
        for (let i = runs.length - 1; i >= 0; i--) {
          const history = runs[i]?.history || {};
          const key = pickMetricKey(history);
          const values = key ? curveValues(history[key]) : [];
          if (values.length >= 2) {
            setMetric({ key, values: values.slice(-30) });
            return;
          }
        }
        setMetric(null);
      } catch { if (!cancelled) setMetric(null); }
    };
    load();
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') load();
    }, 30000);
    return () => { cancelled = true; clearInterval(t); };
  }, [projectId, experimentId]);
  return metric;
}

/**
 * Home — the "Instrument snapshot" (design_handoff_mobile_redesign, Home Page
 * option 3a/3b). The supervisor's glance as an instrument: what this project
 * IS (a clamped project.summary — the name's already in the app bar), a
 * one-line standing, a 24h snapshot band, what's live now with real GPU-util
 * + the run's metric curve, then a compact Needs-you. One Surface: hairlines
 * only at section breaks, the 3px orange index the sole rupture.
 */
export default function HomeScreen() {
  const px = useProjectHref();
  const project = useProjectStore(selectProject);
  const projectId = useProjectStore(s => s.projectId);
  const stats = useProjectStore(selectStats);
  const lastSyncError = useProjectStore(s => s.lastSyncError);
  const activeExperiments = useProjectStore(selectActiveExperiments);
  const reviewRequests = useProjectStore(selectReviewRequests);
  const sandboxes = useProjectStore(selectSandboxes);
  const experiments = useProjectStore(selectExperiments);
  const needsRef = useRef(null);
  const summaryRef = useRef(null);
  const [summaryOpen, setSummaryOpen] = useState(false);
  // Whether the clamped summary actually truncates — the toggle only
  // appears "if needed" (a short summary that already fits gets no button).
  const [summaryClamped, setSummaryClamped] = useState(false);
  useLayoutEffect(() => {
    const el = summaryRef.current;
    if (!el) { setSummaryClamped(false); return; }
    setSummaryClamped(el.scrollHeight > el.clientHeight + 1);
  }, [project?.summary]);

  // Minute tick keeps the standing line and elapsed times honest.
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(t);
  }, []);

  const running = sandboxes.filter(s => s.status === 'running');
  const liveSandbox = running[0] || null;
  const liveExp = activeExperiments.find(e => e.status === 'running')
    || (liveSandbox ? experiments.find(e => e.id === liveSandbox.experiment_id) : null)
    || null;
  const gpu = useLiveGpu(projectId, liveSandbox);
  const metric = useLiveMetric(projectId, liveExp?.id);

  // ── 24h snapshot band (derived client-side; approximate by design) ──
  const tiles = useMemo(() => {
    const in24 = experiments.filter(e => now - Date.parse(e.created_at || '') < DAY_MS).length;
    const prior24 = experiments.filter(e => {
      const age = now - Date.parse(e.created_at || '');
      return age >= DAY_MS && age < 2 * DAY_MS;
    }).length;
    return {
      exps24: in24,
      delta: in24 - prior24,
      gpuHours: gpuHours24(sandboxes, now),
      live: running.length,
      gpuLabel: liveSandbox?.gpu || null,
      reviews: stats.open_reviews ?? 0,
    };
  }, [experiments, sandboxes, running.length, liveSandbox, stats.open_reviews, now]);

  // ── Needs-you items, most urgent first (same derivation as before) ──
  const items = [];
  const expById = Object.fromEntries(experiments.map(e => [e.id, e]));
  for (const s of running) {
    if (!s.expires_at) continue;
    const left = Date.parse(s.expires_at) - now;
    if (Number.isFinite(left) && left <= SOON_MS) {
      const exp = expById[s.experiment_id];
      items.push({
        key: `sbx-${s.sandbox_uid || s.experiment_id}`,
        to: px('/sandboxes'),
        title: `Sandbox · ${exp ? expName(exp) : s.experiment_id || 'unassigned'}`,
        sub: `expiring ${left <= 0 ? 'now' : `in ${fmtDuration(left)}`} · release or extend`,
      });
    }
  }
  for (const e of activeExperiments) {
    if (!REVIEW_STATES.has(e.status)) continue;
    items.push({
      key: `rev-${e.id}`,
      to: px(`/experiments/${e.id}`),
      title: expName(e),
      sub: e.status === 'design_review'
        ? 'design review · approve the plan'
        : 'experiment review · read the outcome',
    });
  }
  for (const r of reviewRequests.filter(r => r.status === 'requested' || r.status === 'started')) {
    const exp = r.target_type === 'experiment' ? expById[r.target_id] : null;
    items.push({
      key: `req-${r.id}`,
      to: exp ? px(`/experiments/${exp.id}`) : px('/reviews'),
      title: exp ? expName(exp) : r.target_id,
      sub: `${(r.role || 'review').replace(/_/g, ' ')} · ${r.status}`,
    });
  }

  if (!project) {
    return <div className="page-stage"><div className="empty-state">Loading project…</div></div>;
  }

  const liveElapsed = liveSandbox?.requested_at
    ? now - Date.parse(liveSandbox.requested_at)
    : (liveExp?.updated_at ? now - Date.parse(liveExp.updated_at) : null);
  const dir = metric ? Math.sign(metric.values[metric.values.length - 1] - metric.values[0]) : 0;
  const good = metric ? goodDirection(metric.key) : 0;

  return (
    <div className="mhome">
      {lastSyncError && (
        <div className="mbanner">Backend unreachable — showing last known state. {lastSyncError}</div>
      )}

      {project.summary && (
        <div className="mhome-summary-wrap">
          <p
            ref={summaryRef}
            className={`mhome-summary${summaryOpen ? ' mhome-summary--open' : ''}`}
          >
            {project.summary}
            {summaryOpen && summaryClamped && (
              <button type="button" className="mhome-summary-less" onClick={() => setSummaryOpen(false)}>
                less
              </button>
            )}
          </p>
          {/* Overlaid, not appended: line-clamp truncates wherever the browser
              likes, so this fades over the last visible characters rather than
              trying to land a real inline link exactly at the cut. */}
          {!summaryOpen && summaryClamped && (
            <button type="button" className="mhome-summary-more" onClick={() => setSummaryOpen(true)}>
              … more
            </button>
          )}
        </div>
      )}

      <div className="mstand">
        <span className="mstand-date">{fmtStanding(new Date(now))}</span>
        {items.length > 0 && (
          <button
            type="button"
            className="mstand-need"
            onClick={() => needsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
          >
            {items.length} need{items.length === 1 ? 's' : ''} you →
          </button>
        )}
      </div>

      <div className="mtiles">
        <div className="mtile">
          <div className="mtile-v tabular">
            {tiles.exps24}
            {tiles.delta > 0 && <span className="up">▲{tiles.delta}</span>}
            {tiles.delta < 0 && <span className="down">▼{-tiles.delta}</span>}
          </div>
          <div className="mtile-l">experiments · 24h</div>
        </div>
        <div className="mtile">
          <div className="mtile-v tabular">{tiles.gpuHours}<small>h</small></div>
          <div className="mtile-l">GPU compute · 24h</div>
        </div>
        <div className="mtile">
          <div className="mtile-v tabular">{tiles.live}</div>
          <div className="mtile-l">live now{tiles.gpuLabel ? ` · ${tiles.gpuLabel}` : ''}</div>
        </div>
        <div className="mtile">
          <div className="mtile-v tabular">{tiles.reviews}</div>
          <div className="mtile-l">reviews open</div>
        </div>
      </div>

      <div className="mml" style={{ marginTop: 20 }}>Live now</div>
      {liveExp ? (
        <Link to={px(`/experiments/${liveExp.id}`)} className="mlive">
          <div className="mlivehead">
            <div className="mlive-name">{expName(liveExp)}</div>
            <div className="mlive-sub">running{liveElapsed != null ? ` · ${fmtDuration(liveElapsed)}` : ''}</div>
          </div>
          {gpu && (
            <>
              <div className="mumeter"><i style={{ width: `${Math.min(100, gpu.util)}%` }} /></div>
              <div className="mumlab">
                <span>GPU {gpu.util}%{gpu.vram != null ? ` · VRAM ${gpu.vram}%` : ''}</span>
                {liveSandbox?.gpu && <span>{liveSandbox.gpu}</span>}
              </div>
            </>
          )}
          {metric && (
            <div className="mmetricline">
              <span className="mmetricline-key">{metric.key} · last {metric.values.length} steps</span>
              <span className="mmetricline-val tabular">
                <MiniSpark values={metric.values} />
                {fmtMetric(metric.values[metric.values.length - 1])}
                {dir !== 0 && good !== 0 && (
                  <span className={`tr ${dir === good ? 'tr--good' : 'tr--bad'}`}>
                    {dir < 0 ? '▼' : '▲'}
                  </span>
                )}
              </span>
            </div>
          )}
        </Link>
      ) : (
        <div className="mquiet">nothing running</div>
      )}

      <div className="mbreak" />

      <div className="mml" ref={needsRef}>Needs you</div>
      {items.length === 0 ? (
        <div className="mquiet">nothing needs you</div>
      ) : (
        <>
          {items.slice(0, 2).map(it => (
            <Link key={it.key} to={it.to} className="mprow">
              <span className="mprow-ix" aria-hidden="true" />
              <span>
                <span className="mprow-t">{it.title}</span>
                <span className="mprow-s">{it.sub}</span>
              </span>
            </Link>
          ))}
          {items.length > 2 && (
            <Link to={px('/reviews')} className="mprow mprow--more">
              <span className="mprow-ix mprow-ix--faint" aria-hidden="true" />
              <span className="mprow-s">{items.length - 2} more →</span>
            </Link>
          )}
        </>
      )}
    </div>
  );
}

function fmtMetric(v) {
  if (!Number.isFinite(v)) return '—';
  if (Number.isInteger(v)) return String(v);
  return Number(v.toPrecision(3)).toString();
}

// The instrument's small pulse curve — 50×16, stroke only, green.
function MiniSpark({ values }) {
  if (!values || values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * 50;
    const y = 14 - ((v - min) / span) * 12;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return (
    <svg className="mspk" viewBox="0 0 50 16" fill="none" aria-hidden="true">
      <polyline points={pts} stroke="var(--supports)" strokeWidth="1.6" />
    </svg>
  );
}

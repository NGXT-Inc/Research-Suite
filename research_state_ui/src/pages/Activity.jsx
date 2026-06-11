import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';
import ObjId from '../components/ObjId';
import { tsToTime } from '../utils/format';

/**
 * Activity — live telemetry of HTTP requests and MCP tool calls.
 *
 * Distinct from /events (which shows state-mutation domain events). This is the
 * raw API/MCP traffic monitor: polls /api/activity every 2s while visible,
 * newest-on-top, with source / event-type / status filters and a Pause control
 * for inspecting rows before they age off.
 */
const SOURCE_TABS = ['mcp', 'all', 'http'];
const EVENT_TABS = ['all', 'tool.call', 'http.request'];
const STATUS_TABS = ['all', 'ok', 'error'];

const POLL_MS = 2000;
const DEFAULT_LIMIT = 300;

// Defaults: the user primarily wants MCP visibility (what Codex is calling).
// HTTP traffic is mostly UI polling — kept available but hidden by default.
const DEFAULT_FILTER_SOURCE = 'mcp';

function isOk(ev) {
  if (ev.event === 'http.request') {
    const s = ev.status;
    return typeof s === 'number' ? s < 400 : true;
  }
  return ev.status === 'ok' || ev.status === undefined;
}

function sourceOf(ev) {
  if (ev.event === 'http.request') return 'http';
  return ev.source || 'mcp';
}

function targetFromArgs(args) {
  if (!args || typeof args !== 'object') return null;
  if (args.experiment_id) return { type: 'experiment', id: args.experiment_id };
  if (args.claim_id) return { type: 'claim', id: args.claim_id };
  if (args.resource_id) return { type: 'resource', id: args.resource_id };
  if (args.review_id || args.request_id) return { type: 'review', id: args.review_id || args.request_id };
  if (args.project_id) return { type: 'project', id: args.project_id };
  return null;
}

function targetHref(type, id) {
  switch (type) {
    case 'experiment': return `/experiments/${id}`;
    case 'claim':      return `/claims/${id}`;
    case 'resource':   return `/resources`;
    case 'sandbox':    return `/sandboxes`;
    case 'review':     return `/reviews`;
    case 'project':    return `/projects`;
    default:           return null;
  }
}

export default function Activity() {
  const [data, setData] = useState(null);   // { activity_log, events }
  const [error, setError] = useState(null);
  const [paused, setPaused] = useState(false);
  const [filterSource, setFilterSource] = useState(DEFAULT_FILTER_SOURCE);
  const [filterEvent, setFilterEvent] = useState('all');
  const [filterStatus, setFilterStatus] = useState('all');
  const [expandedKey, setExpandedKey] = useState(null);

  const inFlightRef = useRef(false);

  const fetchNow = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const fresh = await api.listActivity(DEFAULT_LIMIT, filterSource);
      setData(fresh);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      inFlightRef.current = false;
    }
  }, [filterSource]);

  useEffect(() => {
    fetchNow();
  }, [fetchNow]);

  useEffect(() => {
    if (paused) return undefined;
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') fetchNow();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [fetchNow, paused]);

  const events = useMemo(() => {
    const list = data?.events || [];
    // backend returns oldest-first; flip so newest is on top
    return [...list].reverse();
  }, [data]);

  // `events` already reflects the server-side source filter. Event/status
  // pills apply on top of that — so their counts naturally reflect what's in
  // the current view.
  const filtered = useMemo(() => events.filter(e => {
    if (filterEvent !== 'all' && e.event !== filterEvent) return false;
    if (filterStatus === 'ok' && !isOk(e)) return false;
    if (filterStatus === 'error' && isOk(e)) return false;
    return true;
  }), [events, filterEvent, filterStatus]);

  // Source pill counts come from `summary.source_counts` so they STAY STABLE
  // regardless of which source is selected. The "all" total comes from
  // `summary.total`. Event/status counts come from the returned (already
  // source-filtered) events so they reflect the active view.
  const counts = useMemo(() => {
    const sc = data?.summary?.source_counts || {};
    const src = { all: data?.summary?.total ?? events.length, ...sc };
    const evt = { all: events.length };
    const stat = { all: events.length, ok: 0, error: 0 };
    for (const e of events) {
      evt[e.event] = (evt[e.event] || 0) + 1;
      if (isOk(e)) stat.ok++; else stat.error++;
    }
    return { src, evt, stat };
  }, [data, events]);

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <div className="page-eyebrow">Activity</div>
            <h1 className="page-title">Live MCP traffic</h1>
            <p className="page-summary">
              What the agent is calling, in real time. Defaults to <span className="mono">source = mcp</span>{' '}
              so noisy HTTP polling stays out of the way — switch to <span className="mono">all</span> or{' '}
              <span className="mono">http</span> in the source filter to widen the view.
            </p>
          </div>
          <div className="page-actions">
            <span className="cluster" style={{ fontSize: 'var(--text-xs)', color: 'var(--muted)' }}>
              {!paused && <span className="log-tail-live-dot" />}
              {paused ? 'paused' : 'live'} · {filtered.length} of {events.length}
            </span>
            <button className="btn btn--sm btn--ghost" onClick={() => setPaused(p => !p)}>
              {paused ? 'Resume' : 'Pause'}
            </button>
            <button className="btn btn--sm btn--ghost" onClick={fetchNow}>Refresh</button>
          </div>
        </div>

        <div className="events-filter-bar" style={{ marginTop: 14, alignItems: 'center' }}>
          <FilterGroup label="source" tabs={SOURCE_TABS} value={filterSource} onChange={setFilterSource} counts={counts.src} />
          <FilterGroup label="event" tabs={EVENT_TABS} value={filterEvent} onChange={setFilterEvent} counts={counts.evt} />
          <FilterGroup label="status" tabs={STATUS_TABS} value={filterStatus} onChange={setFilterStatus} counts={counts.stat} />
        </div>

        {data?.activity_log && (
          <p className="faint" style={{ fontSize: 10.5, marginTop: 8 }}>
            log: <span className="mono">{data.activity_log}</span>
          </p>
        )}
      </header>

      {error && <div className="error-message">{error}</div>}

      {data == null ? (
        <div className="empty">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="empty">No activity matches these filters.</div>
      ) : (
        <div className="activity-list">
          {filtered.map((ev, i) => {
            const key = `${ev.ts}:${i}`;
            const open = expandedKey === key;
            const ok = isOk(ev);
            const src = sourceOf(ev);
            const target = ev.event === 'tool.call' ? targetFromArgs(ev.args) : null;
            const href = target ? targetHref(target.type, target.id) : null;
            return (
              <div key={key} className={`act-row act-row--${ok ? 'ok' : 'err'}`}>
                <button
                  type="button"
                  className="act-row-main"
                  onClick={() => setExpandedKey(open ? null : key)}
                  title={open ? 'Collapse' : 'Expand'}
                >
                  <span className="act-time">{tsToTime(ev.ts)}</span>
                  <span className={`act-source act-source--${src}`}>{src}</span>
                  {ev.event === 'http.request' ? (
                    <>
                      <span className="act-tool">
                        <span className="act-method">{ev.method}</span>
                        <span className="mono"> {ev.path}</span>
                      </span>
                      <span className={`act-status ${ok ? 'ok' : 'err'}`}>{ev.status}</span>
                    </>
                  ) : (
                    <>
                      <span className="act-tool"><span className="mono">{ev.tool}</span></span>
                      <span className={`act-status ${ok ? 'ok' : 'err'}`}>{ev.status || 'ok'}</span>
                    </>
                  )}
                  <span className="act-dur tabular">{Number(ev.duration_ms ?? 0)} ms</span>
                </button>
                {target && (
                  <span className="act-target" onClick={e => e.stopPropagation()}>
                    {href
                      ? <Link to={href}><ObjId id={target.id} className="timeline-event-target--link" /></Link>
                      : <ObjId id={target.id} />}
                  </span>
                )}
                {open && <ActivityDetail ev={ev} />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function jsonSize(v) {
  if (v == null) return 0;
  try { return JSON.stringify(v).length; } catch { return 0; }
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  return `${(n / 1024).toFixed(1)} KB`;
}

function JsonPane({ value, title, hint, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  const size = jsonSize(value);
  return (
    <div className="act-pane">
      <button
        type="button"
        className="act-pane-head"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        <span className="act-pane-twist" aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="act-pane-title">{title}</span>
        {hint && <span className="act-pane-hint">{hint}</span>}
        {size > 0 && <span className="act-pane-size">{formatBytes(size)}</span>}
      </button>
      {open && (
        <pre className="act-pane-body">{value == null ? '(none)' : JSON.stringify(value, null, 2)}</pre>
      )}
    </div>
  );
}

function ActivityDetail({ ev }) {
  if (ev.event === 'http.request') {
    return (
      <div className="act-detail">
        <div className="act-detail-row">
          <span className="act-detail-key">method</span>
          <span className="mono">{ev.method}</span>
        </div>
        <div className="act-detail-row">
          <span className="act-detail-key">path</span>
          <span className="mono">{ev.path}</span>
        </div>
        <div className="act-detail-row">
          <span className="act-detail-key">status</span>
          <span className="mono">{ev.status}</span>
        </div>
        <div className="act-detail-row">
          <span className="act-detail-key">duration</span>
          <span className="mono">{ev.duration_ms} ms</span>
        </div>
      </div>
    );
  }
  // tool.call — show args (request) and result (response) separately.
  const isError = ev.status === 'error';
  return (
    <div className="act-detail">
      <JsonPane title="Arguments" value={ev.args} defaultOpen={true} />
      {isError ? (
        <div className="act-pane act-pane--error">
          <div className="act-pane-head act-pane-head--static">
            <span className="act-pane-title">Error</span>
            {ev.error_code && <span className="act-pane-hint mono">{ev.error_code}</span>}
          </div>
          <pre className="act-pane-body">{ev.error || '(no message)'}</pre>
        </div>
      ) : (
        <JsonPane
          title="Result"
          value={ev.result}
          hint={ev.result == null ? '(no result captured)' : null}
          defaultOpen={true}
        />
      )}
    </div>
  );
}

function FilterGroup({ label, tabs, value, onChange, counts }) {
  return (
    <div className="cluster" style={{ alignItems: 'center', gap: 4 }}>
      <span className="faint" style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', marginRight: 4 }}>
        {label}
      </span>
      <div className="tab-row">
        {tabs.map(t => (
          <button
            key={t}
            type="button"
            className={`tab${value === t ? ' active' : ''}`}
            onClick={() => onChange(t)}
          >
            {t}
            <span className="tab-count">{counts?.[t] ?? 0}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

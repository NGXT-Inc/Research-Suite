import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';
import JsonView from '../components/JsonView';
import ObjId from '../components/ObjId';
import { tsToTime } from '../utils/format';
import { expName } from '../utils/experiment';
import { useProjectStore, selectExperiments, useProjectHref } from '../store/useProjectStore';

/**
 * Traffic & Tool I/O — the merged MCP tool-call view (route /activity).
 *
 * Two altitudes over one project-scoped data source — the activity ring
 * (/api/activity), which carries every project's tool calls:
 *   - MACRO: a per-tool aggregate (calls, avg/p50/p95/max received, errors,
 *     share) computed client-side, to rank the offenders. Click a tool to
 *     filter the stream.
 *   - MICRO: a live, newest-first call stream (2s auto-refresh, Pause, slow/
 *     heavy coloring, entity chip) — expand any row for its request/response.
 *
 * (tool_calls.sqlite holds fuller payloads but only for the local workspace, so
 * it can't back a cross-project view — hence the ring.)
 */
const POLL_MS = 2000;
const RING_LIMIT = 1000;             // backend caps /api/activity at 1000
const HOT_RECEIVED_CHARS = 20000;    // single heavy/hot threshold (row + aggregate agree)
const SLOW_CALL_MS = 500;
const FILTER_DELAY_MS = 220;

const TIME_PRESETS = [
  { label: 'all', minutes: null },
  { label: '5m', minutes: 5 },
  { label: '15m', minutes: 15 },
  { label: '1h', minutes: 60 },
  { label: '6h', minutes: 360 },
  { label: '24h', minutes: 1440 },
];
const STATUS_PRESETS = ['all', 'ok', 'error'];

// Macro aggregate columns (client-side sort).
const TOOL_COLS = [
  { key: 'tool', label: 'tool', align: 'left' },
  { key: 'calls', label: 'calls' },
  { key: 'received_chars', label: 'total recv' },
  { key: 'avg_received_chars', label: 'avg' },
  { key: 'p95_received_chars', label: 'p95' },
  { key: 'max_received_chars', label: 'max' },
  { key: 'sent_chars', label: 'total sent' },
  { key: 'error_calls', label: 'errors' },
];
// Micro stream columns (act-* single-row design).
const STREAM_COLS = [
  { key: 'ts', label: 'time', align: 'left' },
  { key: 'tool', label: 'tool', align: 'left' },
  { key: 'status', label: 'status', align: 'right' },
  { key: 'duration_ms', label: 'dur', align: 'right' },
  { key: 'received_chars', label: 'result', align: 'right' },
];

function targetFromArgs(args) {
  if (!args || typeof args !== 'object') return [null, null];
  if (args.experiment_id) return ['experiment', String(args.experiment_id)];
  if (args.claim_id) return ['claim', String(args.claim_id)];
  if (args.artifact_id) return ['artifact', String(args.artifact_id)];
  const review = args.review_id || args.request_id;
  if (review) return ['review', String(review)];
  return [null, null];
}

function targetHref(type, id) {
  switch (type) {
    case 'experiment': return `/experiments/${id}`;
    case 'claim':      return `/claims/${id}`;
    case 'artifact':   return `/artifacts/${id}`;
    case 'review':     return `/reviews`;
    default:           return null;
  }
}

function percentile(sorted, p) {
  if (!sorted.length) return 0;
  const idx = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[idx];
}

function tsMs(ts) {
  const v = Date.parse(ts);
  return Number.isFinite(v) ? v : 0;
}

export default function Debug() {
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);
  const expById = useMemo(
    () => Object.fromEntries(experiments.map(e => [e.id, e])),
    [experiments],
  );

  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [paused, setPaused] = useState(false);

  const [minutes, setMinutes] = useState(null);
  const [status, setStatus] = useState('all');
  const [toolInput, setToolInput] = useState('');
  const [toolQuery, setToolQuery] = useState('');
  const [minDurInput, setMinDurInput] = useState('');
  const [minDurMs, setMinDurMs] = useState(0);

  const [toolSort, setToolSort] = useState({ key: 'received_chars', dir: 'desc' });
  const [callSort, setCallSort] = useState({ key: 'ts', order: 'desc' });
  const [expandedKey, setExpandedKey] = useState(null);
  const [aggOpen, setAggOpen] = useState(true);

  const inFlight = useRef(false);

  useEffect(() => {
    const t = setTimeout(() => setToolQuery(toolInput.trim().toLowerCase()), FILTER_DELAY_MS);
    return () => clearTimeout(t);
  }, [toolInput]);

  useEffect(() => {
    const parsed = Number.parseInt(minDurInput, 10);
    setMinDurMs(Number.isFinite(parsed) && parsed > 0 ? parsed : 0);
  }, [minDurInput]);

  const fetchNow = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const fresh = await api.listActivity(RING_LIMIT, 'mcp', projectId);
      setData(fresh);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      inFlight.current = false;
    }
  }, [projectId]);

  useEffect(() => { fetchNow(); }, [fetchNow]);

  // Live auto-refresh, but hold still while a call is expanded for reading.
  useEffect(() => {
    if (paused || expandedKey != null) return undefined;
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') fetchNow();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [fetchNow, paused, expandedKey]);

  // Ring events (oldest-first) -> tool-call rows, newest-first.
  const allCalls = useMemo(() => {
    const events = data?.events || [];
    const rows = [];
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.event !== 'tool.call') continue;
      const [target_type, target_id] = targetFromArgs(e.args);
      rows.push({
        id: `${e.ts}:${i}`,
        ts: e.ts,
        tool: String(e.tool || ''),
        status: e.status || 'ok',
        duration_ms: Number(e.duration_ms || 0),
        sent_chars: Number(e.sent_chars || 0),
        received_chars: Number(e.received_chars || 0),
        error_code: e.error_code || '',
        error: e.error || '',
        args: e.args,
        result: e.result,
        result_truncated: !!(e.result && typeof e.result === 'object' && e.result._truncated),
        target_type,
        target_id,
      });
    }
    return rows;
  }, [data]);

  // The universe for BOTH the aggregate and the stream: time-window + status.
  // (Tool substring + min-duration narrow only the stream, so the macro keeps
  // showing every tool to pick from.)
  const scoped = useMemo(() => {
    const cutoff = minutes ? Date.now() - minutes * 60_000 : 0;
    return allCalls.filter(c => {
      if (cutoff && tsMs(c.ts) < cutoff) return false;
      if (status === 'ok' && c.status === 'error') return false;
      if (status === 'error' && c.status !== 'error') return false;
      return true;
    });
  }, [allCalls, minutes, status]);

  const byTool = useMemo(() => {
    const map = new Map();
    for (const c of scoped) {
      let b = map.get(c.tool);
      if (!b) { b = { tool: c.tool, calls: 0, received_chars: 0, sent_chars: 0, error_calls: 0, max_received_chars: 0, _recv: [] }; map.set(c.tool, b); }
      b.calls += 1;
      b.received_chars += c.received_chars;
      b.sent_chars += c.sent_chars;
      if (c.status === 'error') b.error_calls += 1;
      if (c.received_chars > b.max_received_chars) b.max_received_chars = c.received_chars;
      b._recv.push(c.received_chars);
    }
    const list = [...map.values()].map(b => {
      const s = b._recv.sort((x, y) => x - y);
      return {
        tool: b.tool, calls: b.calls, received_chars: b.received_chars, sent_chars: b.sent_chars,
        error_calls: b.error_calls, max_received_chars: b.max_received_chars,
        avg_received_chars: Math.round(b.received_chars / (b.calls || 1)),
        p50_received_chars: percentile(s, 50),
        p95_received_chars: percentile(s, 95),
      };
    });
    const { key, dir } = toolSort;
    list.sort((a, b) => {
      const av = a[key], bv = b[key];
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : (av - bv);
      return dir === 'asc' ? cmp : -cmp;
    });
    return list;
  }, [scoped, toolSort]);
  const maxToolReceived = useMemo(
    () => Math.max(1, ...byTool.map(t => t.received_chars)),
    [byTool],
  );

  // The stream: scoped + tool substring + min-duration, sorted.
  const visibleCalls = useMemo(() => {
    let list = scoped;
    if (toolQuery) list = list.filter(c => c.tool.toLowerCase().includes(toolQuery));
    if (minDurMs > 0) list = list.filter(c => c.duration_ms >= minDurMs);
    const { key, order } = callSort;
    const dir = order === 'asc' ? 1 : -1;
    const val = (c) => {
      if (key === 'ts') return tsMs(c.ts);
      if (key === 'status') return c.status === 'error' ? 0 : 1;
      if (key === 'tool') return c.tool.toLowerCase();
      return Number(c[key] || 0);
    };
    return [...list].sort((a, b) => {
      const av = val(a), bv = val(b);
      const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv));
      return cmp * dir;
    });
  }, [scoped, toolQuery, minDurMs, callSort]);

  const stats = useMemo(() => {
    let ok = 0, err = 0, slow = 0, heavy = 0, dur = 0, recv = 0, sent = 0;
    for (const c of visibleCalls) {
      if (c.status === 'error') err += 1; else ok += 1;
      dur += c.duration_ms; if (c.duration_ms >= SLOW_CALL_MS) slow += 1;
      recv += c.received_chars; if (c.received_chars >= HOT_RECEIVED_CHARS) heavy += 1;
      sent += c.sent_chars;
    }
    const n = visibleCalls.length;
    return { n, ok, err, slow, heavy, avg: n ? Math.round(dur / n) : 0, recv, sent };
  }, [visibleCalls]);

  const sortTool = (key) => setToolSort(s => ({
    key,
    dir: s.key === key && s.dir === 'desc' ? 'asc' : 'desc',
  }));
  const sortCall = (key) => setCallSort(s => ({
    key,
    order: s.key === key && s.order === 'desc' ? 'asc' : 'desc',
  }));

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <h1 className="page-title">Traffic &amp; Tool I/O</h1>
            <p className="page-summary">Every MCP tool call in this project.</p>
          </div>
          <div className="page-actions">
            <span className="cluster" style={{ fontSize: 'var(--text-xs)', color: 'var(--muted)' }}>
              {!paused && expandedKey == null && <span className="log-tail-live-dot" />}
              {expandedKey != null ? 'holding' : paused ? 'paused' : 'live'}
            </span>
            <button className="btn btn--sm btn--ghost" onClick={() => setPaused(p => !p)}>
              {paused ? 'Resume' : 'Pause'}
            </button>
            <button className="btn btn--sm btn--ghost" onClick={fetchNow}>Refresh</button>
          </div>
        </div>

        {/* Controls */}
        <div className="dbg-controls">
          <Segmented label="period" options={TIME_PRESETS.map(p => p.label)}
            value={TIME_PRESETS.find(p => p.minutes === minutes)?.label || 'all'}
            onChange={(lab) => setMinutes(TIME_PRESETS.find(p => p.label === lab)?.minutes ?? null)} />
          <Segmented label="status" options={STATUS_PRESETS} value={status} onChange={setStatus} />
          <div className="dbg-search">
            <input
              className="dbg-search-input"
              placeholder="filter tool…"
              value={toolInput}
              onChange={(e) => setToolInput(e.target.value)}
            />
            {toolInput && <button className="dbg-search-clear" onClick={() => setToolInput('')}>×</button>}
          </div>
          <div className="dbg-search">
            <input
              className="dbg-search-input"
              placeholder="min duration (ms)"
              value={minDurInput}
              inputMode="numeric"
              onChange={(e) => setMinDurInput(e.target.value)}
            />
            {minDurInput && <button className="dbg-search-clear" onClick={() => setMinDurInput('')}>×</button>}
          </div>
        </div>

        <div className="dbg-totals">
          <Stat label="calls" value={fmtNum(stats.n)} />
          <Stat label="ok / err" value={`${stats.ok}/${stats.err}`} accent={stats.err ? 'err' : null} />
          <Stat label="avg dur" value={formatMs(stats.avg)} accent={stats.slow ? 'warn' : null} />
          <Stat label="slow" value={fmtNum(stats.slow)} accent={stats.slow ? 'err' : null} />
          <Stat label="heavy" value={fmtNum(stats.heavy)} accent={stats.heavy ? 'warn' : null} />
          <Stat label="recv / sent" value={`${fmtChars(stats.recv)} / ${fmtChars(stats.sent)}`} accent="recv" />
        </div>
      </header>

      {error && <div className="error-message">{error}</div>}

      {data == null ? (
        <div className="empty">Loading…</div>
      ) : allCalls.length === 0 ? (
        <div className="empty-state">
          <h2>No tool calls</h2>
        </div>
      ) : (
        <>
          <section className="dbg-section">
            <button
              type="button"
              className="dbg-section-head dbg-section-toggle"
              onClick={() => setAggOpen(o => !o)}
              aria-expanded={aggOpen}
            >
              <span className={`twist${aggOpen ? ' open' : ''}`} aria-hidden="true">▸</span>
              By tool · {byTool.length} tools<span className="dbg-hint"> · click a row to filter the stream</span>
            </button>
            {aggOpen && (
              <div className="dbg-table">
                <div className="dbg-row dbg-row--head con-head">
                  {TOOL_COLS.map(c => (
                    <SortCell key={c.key} align={c.align} active={toolSort.key === c.key}
                      dir={toolSort.dir} onClick={() => sortTool(c.key)}>{c.label}</SortCell>
                  ))}
                  <span className="th th--con dbg-c-bar">share</span>
                </div>
                {byTool.map((t) => {
                  const hot = t.max_received_chars >= HOT_RECEIVED_CHARS;
                  const active = toolQuery === t.tool.toLowerCase();
                  return (
                    <div
                      key={t.tool}
                      className={`dbg-row dbg-row--tool${active ? ' active' : ''}`}
                      onClick={() => setToolInput(active ? '' : t.tool)}
                      title={active ? 'Clear filter' : `Filter calls to ${t.tool}`}
                    >
                      <span className="dbg-c-tool mono">
                        {t.tool}
                        {t.error_calls > 0 && <span className="dbg-err-badge">{t.error_calls} err</span>}
                      </span>
                      <span className="dbg-c-num tabular">{fmtNum(t.calls)}</span>
                      <span className={`dbg-c-num tabular${hot ? ' hot' : ''}`}>{fmtChars(t.received_chars)}</span>
                      <span className="dbg-c-num tabular">{fmtChars(t.avg_received_chars)}</span>
                      <span className="dbg-c-num tabular">{fmtChars(t.p95_received_chars)}</span>
                      <span className={`dbg-c-num tabular${hot ? ' hot' : ''}`}>{fmtChars(t.max_received_chars)}</span>
                      <span className="dbg-c-num tabular faint">{fmtChars(t.sent_chars)}</span>
                      <span className={`dbg-c-num tabular${t.error_calls ? ' err' : ' faint'}`}>{fmtNum(t.error_calls)}</span>
                      <span className="dbg-c-bar">
                        <span className="dbg-bar-track">
                          <span className={`dbg-bar-fill${hot ? ' hot' : ''}`}
                            style={{ width: `${(t.received_chars / maxToolReceived) * 100}%` }} />
                        </span>
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          <section className="dbg-section">
            <div className="dbg-section-head">
              Calls · {fmtNum(visibleCalls.length)}{toolQuery && <> · <span className="mono">{toolQuery}</span></>}<span className="dbg-hint"> · newest first · click to inspect I/O</span>
            </div>
            <div className="activity-list">
              <div className="act-row act-row--head con-head">
                {STREAM_COLS.map(col => (
                  <button
                    key={col.key}
                    type="button"
                    className={`th th--con${col.align === 'right' ? ' th--r' : ''}${col.key === 'tool' ? ' act-head-cell--tool' : ''}${callSort.key === col.key ? ' on' : ''}`}
                    onClick={() => sortCall(col.key)}
                  >
                    {col.label}
                    {callSort.key === col.key && <span className="arr">{callSort.order === 'asc' ? '▲' : '▼'}</span>}
                  </button>
                ))}
              </div>
              {visibleCalls.map(c => (
                <StreamRow
                  key={c.id}
                  call={c}
                  expById={expById}
                  open={expandedKey === c.id}
                  onToggle={() => setExpandedKey(k => k === c.id ? null : c.id)}
                  onFilterTool={() => setToolInput(c.tool)}
                />
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function StreamRow({ call, expById, open, onToggle, onFilterTool }) {
  const px = useProjectHref();
  const ok = call.status !== 'error';
  const slow = call.duration_ms >= SLOW_CALL_MS;
  const heavy = call.received_chars >= HOT_RECEIVED_CHARS;
  const rawHref = call.target_type ? targetHref(call.target_type, call.target_id) : null;
  const href = rawHref ? px(rawHref) : null;
  const exp = call.target_type === 'experiment' ? expById[call.target_id] : null;
  return (
    <div
      className={`act-call act-call--${ok ? 'ok' : 'err'}${open ? ' act-call--open' : ''}`}
      role="button"
      tabIndex={0}
      title={open ? 'Collapse' : 'Expand'}
      onClick={onToggle}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(); }
      }}
    >
      <div className="act-row-main">
        <span className="act-time">{tsToTime(call.ts)}</span>
        <span className="act-tool-cell">
          <span className={`twist${open ? ' open' : ''}`} aria-hidden="true">▸</span>
          <span className="act-tool mono">{call.tool || 'n/a'}</span>
          {call.target_type && (
            <span className="act-target-chip" onClick={(e) => e.stopPropagation()}>
              {href
                ? <Link className="act-target-link" to={href}>{exp ? expName(exp) : <ObjId id={call.target_id} />}</Link>
                : (exp ? expName(exp) : <ObjId id={call.target_id} />)}
            </span>
          )}
        </span>
        <span className={`act-status ${ok ? 'ok' : 'err'}`}>{ok ? 'ok' : (call.error_code || 'error')}</span>
        <span className={`act-dur tabular${slow ? ' act-dur--slow' : ''}`}>{formatMs(call.duration_ms)}</span>
        <span className={`act-size tabular${heavy ? ' act-size--heavy' : ''}`}>{fmtChars(call.received_chars)}</span>
      </div>
      {open && <StreamDetail call={call} onFilterTool={onFilterTool} />}
    </div>
  );
}

function StreamDetail({ call, onFilterTool }) {
  const [copied, setCopied] = useState(false);
  const isError = call.status === 'error';

  const copy = () => {
    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) return;
    try {
      navigator.clipboard.writeText(JSON.stringify({ args: call.args, result: isError ? call.error : call.result }, null, 2));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 900);
    } catch { /* clipboard best-effort */ }
  };

  return (
    <div className="act-detail" onClick={(e) => e.stopPropagation()}>
      <div className="act-detail-bar">
        <span className="act-io">sent {fmtChars(call.sent_chars)} · recv {fmtChars(call.received_chars)}</span>
        <span className="act-detail-bar-spacer" />
        <button type="button" className="act-btn" onClick={onFilterTool}>Filter to this tool</button>
        <button type="button" className="act-btn" onClick={copy}>{copied ? 'Copied' : 'Copy JSON'}</button>
      </div>

      <div className="act-detail-pane">
        <div className="act-detail-label">arguments <span className="act-detail-size">{fmtChars(call.sent_chars)}</span></div>
        {call.args == null
          ? <div className="act-pane-empty">(no arguments)</div>
          : <JsonView data={call.args} initialDepth={3} />}
      </div>

      <div className="act-detail-pane">
        <div className={`act-detail-label${isError ? ' act-detail-label--error' : ''}`}>
          {isError ? 'error' : 'result'} <span className="act-detail-size">{fmtChars(call.received_chars)}</span>
          {call.result_truncated && <span className="dbg-trunc">truncated · stored preview</span>}
        </div>
        {isError
          ? <pre className="act-error-body">{String(call.error || call.result || '(no message)')}{call.error_code ? `\n\ncode: ${call.error_code}` : ''}</pre>
          : call.result == null
            ? <div className="act-pane-empty">(no result captured)</div>
            : <JsonView data={call.result} initialDepth={2} />}
      </div>
    </div>
  );
}

function SortCell({ children, align, active, dir, onClick }) {
  return (
    <span
      className={`th th--con${align === 'left' ? '' : ' th--r'}${active ? ' on' : ''}`}
      onClick={onClick}
      role="button"
    >
      {children}
      {active && <span className="arr">{dir === 'asc' ? '▲' : '▼'}</span>}
    </span>
  );
}

function Segmented({ label, options, value, onChange }) {
  return (
    <div className="dbg-seg">
      <span className="dbg-seg-label">{label}</span>
      <div className="dbg-seg-btns">
        {options.map(o => (
          <button key={o} type="button" className={`dbg-seg-btn${value === o ? ' active' : ''}`} onClick={() => onChange(o)}>
            {o}
          </button>
        ))}
      </div>
    </div>
  );
}

function Stat({ label, value, accent }) {
  return (
    <div className={`dbg-stat${accent ? ` dbg-stat--${accent}` : ''}`}>
      <span className="dbg-stat-value tabular">{value}</span>
      <span className="dbg-stat-label">{label}</span>
    </div>
  );
}

function fmtNum(n) { return Number(n || 0).toLocaleString(); }

function fmtChars(n) {
  const v = Number(n || 0);
  if (v < 1000) return String(v);
  if (v < 1_000_000) return `${(v / 1000).toFixed(v < 10_000 ? 1 : 0)}k`;
  return `${(v / 1_000_000).toFixed(1)}M`;
}

function formatMs(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v)) return '0 ms';
  return v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(1)}s`;
}

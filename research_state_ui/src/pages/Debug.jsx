import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import JsonView from '../components/JsonView';
import { tsToTime } from '../utils/format';

/**
 * Debug — MCP tool I/O analyzer.
 *
 * Answers "which tool calls are flooding the agent's context, and what exactly
 * are they sending back?" The backend keeps the FULL request/response of recent
 * calls; this page lets you:
 *   - filter by time window / source / status / tool, and sort,
 *   - rank tools by data returned (avg / p50 / p95 / max), and
 *   - click any call to read its raw arguments and response in a JSON viewer.
 */
const POLL_MS = 3000;
const HOT_RECEIVED_CHARS = 20000;

const TIME_PRESETS = [
  { label: 'all', minutes: null },
  { label: '5m', minutes: 5 },
  { label: '15m', minutes: 15 },
  { label: '1h', minutes: 60 },
  { label: '6h', minutes: 360 },
  { label: '24h', minutes: 1440 },
];
const SOURCE_PRESETS = ['all', 'mcp', 'http', 'app'];
const STATUS_PRESETS = ['all', 'ok', 'error'];

// Aggregate columns (client-side sort). key matches the backend field.
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
// Per-call columns. `sortKey` set => sortable server-side.
const CALL_COLS = [
  { key: 'ts', label: 'time', sortKey: 'ts', align: 'left' },
  { key: 'tool', label: 'tool', sortKey: 'tool', align: 'left' },
  { key: 'source', label: 'source', align: 'left' },
  { key: 'sent_chars', label: 'sent', sortKey: 'sent_chars' },
  { key: 'received_chars', label: 'received', sortKey: 'received_chars' },
  { key: 'duration_ms', label: 'dur', sortKey: 'duration_ms' },
  { key: 'status', label: 'status' },
];

export default function Debug() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [paused, setPaused] = useState(false);

  const [minutes, setMinutes] = useState(null);
  const [source, setSource] = useState('all');
  const [status, setStatus] = useState('all');
  const [toolInput, setToolInput] = useState('');
  const [toolQuery, setToolQuery] = useState('');

  const [toolSort, setToolSort] = useState({ key: 'received_chars', dir: 'desc' });
  const [callSort, setCallSort] = useState({ key: 'ts', order: 'desc' });
  const [expandedId, setExpandedId] = useState(null);

  const inFlight = useRef(false);

  // Debounce the tool search box.
  useEffect(() => {
    const t = setTimeout(() => setToolQuery(toolInput.trim()), 300);
    return () => clearTimeout(t);
  }, [toolInput]);

  const fetchNow = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const fresh = await api.toolCallStats({
        minutes, source, status, tool: toolQuery,
        limit: 400, sort: callSort.key, order: callSort.order,
      });
      setData(fresh);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      inFlight.current = false;
    }
  }, [minutes, source, status, toolQuery, callSort]);

  useEffect(() => { fetchNow(); }, [fetchNow]);

  // Auto-refresh, but hold still while a call is expanded for reading.
  useEffect(() => {
    if (paused || expandedId != null) return undefined;
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') fetchNow();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [fetchNow, paused, expandedId]);

  const totals = data?.totals || { calls: 0, sent_chars: 0, received_chars: 0, error_calls: 0 };
  const coverage = data?.coverage || {};
  const calls = data?.calls || [];

  const byTool = useMemo(() => {
    const list = [...(data?.by_tool || [])];
    const { key, dir } = toolSort;
    list.sort((a, b) => {
      const av = a[key], bv = b[key];
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : (av - bv);
      return dir === 'asc' ? cmp : -cmp;
    });
    return list;
  }, [data, toolSort]);
  const maxToolReceived = useMemo(
    () => Math.max(1, ...byTool.map(t => t.received_chars)),
    [byTool],
  );

  const onClear = async () => {
    if (!window.confirm('Clear all recorded tool calls?')) return;
    try { await api.clearToolCalls(); setExpandedId(null); await fetchNow(); }
    catch (err) { setError(err.message); }
  };

  const sortTool = (key) => setToolSort(s => ({
    key,
    dir: s.key === key && s.dir === 'desc' ? 'asc' : 'desc',
  }));
  const sortCall = (sortKey) => setCallSort(s => ({
    key: sortKey,
    order: s.key === sortKey && s.order === 'desc' ? 'asc' : 'desc',
  }));

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <div className="page-eyebrow">Debug</div>
            <h1 className="page-title">Tool I/O</h1>
            <p className="page-summary">
              Every MCP tool call with the exact bytes it <strong>sent</strong> and{' '}
              <strong>received</strong>. Rank the offenders, then click any call to read its
              raw request and response.
            </p>
          </div>
          <div className="page-actions">
            <span className="cluster" style={{ fontSize: 'var(--text-xs)', color: 'var(--muted)' }}>
              {!paused && expandedId == null && <span className="log-tail-live-dot" />}
              {expandedId != null ? 'holding' : paused ? 'paused' : 'live'}
            </span>
            <button className="btn btn--sm btn--ghost" onClick={() => setPaused(p => !p)}>
              {paused ? 'Resume' : 'Pause'}
            </button>
            <button className="btn btn--sm btn--ghost" onClick={fetchNow}>Refresh</button>
            <button className="btn btn--sm btn--ghost" onClick={onClear}>Clear</button>
          </div>
        </div>

        {/* Controls */}
        <div className="dbg-controls">
          <Segmented label="period" options={TIME_PRESETS.map(p => p.label)}
            value={TIME_PRESETS.find(p => p.minutes === minutes)?.label || 'all'}
            onChange={(lab) => setMinutes(TIME_PRESETS.find(p => p.label === lab)?.minutes ?? null)} />
          <Segmented label="source" options={SOURCE_PRESETS} value={source} onChange={setSource} />
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
        </div>

        <div className="dbg-totals">
          <Stat label="calls" value={fmtNum(totals.calls)} />
          <Stat label="received" value={fmtChars(totals.received_chars)} accent="recv" />
          <Stat label="sent" value={fmtChars(totals.sent_chars)} />
          <Stat label="errors" value={fmtNum(totals.error_calls)} accent={totals.error_calls ? 'err' : null} />
        </div>
        {coverage.capped && (
          <p className="dbg-coverage-note">
            Showing the {fmtNum(coverage.stored)} most-recent stored calls — older calls in this
            window were evicted from the debug ring.
          </p>
        )}
      </header>

      {error && <div className="error-message">{error}</div>}

      {data == null ? (
        <div className="empty">Loading…</div>
      ) : totals.calls === 0 ? (
        <div className="empty-state">
          <h2>No matching tool calls</h2>
          <p>Adjust the filters, or wait for the agent to make MCP tool calls.</p>
        </div>
      ) : (
        <>
          <section className="dbg-section">
            <div className="dbg-section-head">By tool · click a row to filter calls below</div>
            <div className="dbg-table">
              <div className="dbg-row dbg-row--head">
                {TOOL_COLS.map(c => (
                  <SortCell key={c.key} align={c.align} active={toolSort.key === c.key}
                    dir={toolSort.dir} onClick={() => sortTool(c.key)}>{c.label}</SortCell>
                ))}
                <span className="dbg-c-bar">share</span>
              </div>
              {byTool.map((t) => {
                const hot = t.max_received_chars >= HOT_RECEIVED_CHARS;
                const active = toolQuery === t.tool;
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
          </section>

          <section className="dbg-section">
            <div className="dbg-section-head">
              Calls{toolQuery && <> · <span className="mono">{toolQuery}</span></>} · click to inspect raw I/O
            </div>
            <div className="dbg-table">
              <div className="dbg-row dbg-row--head dbg-row--calls">
                {CALL_COLS.map(c => (
                  c.sortKey
                    ? <SortCell key={c.key} align={c.align} active={callSort.key === c.sortKey}
                        dir={callSort.order} onClick={() => sortCall(c.sortKey)}>{c.label}</SortCell>
                    : <span key={c.key} className={c.align === 'left' ? 'dbg-c-left' : 'dbg-c-num'}>{c.label}</span>
                ))}
              </div>
              {calls.map((c) => (
                <CallRow
                  key={c.id}
                  call={c}
                  open={expandedId === c.id}
                  onToggle={() => setExpandedId(id => id === c.id ? null : c.id)}
                />
              ))}
            </div>
          </section>

          {data?.activity_log && (
            <p className="faint" style={{ fontSize: 10.5, marginTop: 12 }}>
              full payloads stored in <span className="mono">.research_plugin/tool_calls.sqlite</span>
            </p>
          )}
        </>
      )}
    </div>
  );
}

function CallRow({ call, open, onToggle }) {
  const ok = call.status !== 'error';
  const hot = call.received_chars >= HOT_RECEIVED_CHARS;
  return (
    <div className={`dbg-callwrap${open ? ' open' : ''}`}>
      <div className="dbg-row dbg-row--calls dbg-row--clickable" onClick={onToggle}>
        <span className="dbg-c-left mono">
          <span className={`dbg-twist${open ? ' open' : ''}`}>▸</span>
          {tsToTime(call.ts)}
        </span>
        <span className="dbg-c-left mono" title={call.tool}>{call.tool}</span>
        <span className="dbg-c-left">{call.source}</span>
        <span className="dbg-c-num tabular faint">{fmtChars(call.sent_chars)}</span>
        <span className={`dbg-c-num tabular${hot ? ' hot' : ''}`}>{fmtChars(call.received_chars)}</span>
        <span className="dbg-c-num tabular faint">{fmtNum(call.duration_ms)}ms</span>
        <span className={`dbg-c-num ${ok ? 'ok' : 'err'}`}>{ok ? 'ok' : (call.error_code || 'error')}</span>
      </div>
      {open && <CallDetail id={call.id} />}
    </div>
  );
}

function CallDetail({ id }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null); setError(null);
    api.getToolCall(id)
      .then(d => { if (!cancelled) setDetail(d); })
      .catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [id]);

  if (error) return <div className="dbg-detail"><div className="error-message">{error}</div></div>;
  if (!detail) return <div className="dbg-detail"><div className="log-tail-empty">Loading raw call…</div></div>;

  const isError = detail.status === 'error';
  return (
    <div className="dbg-detail">
      <div className="dbg-detail-pane">
        <div className="dbg-detail-label">
          arguments <span className="dbg-detail-size">{fmtChars(detail.sent_chars)}</span>
          {detail.args_truncated && <span className="dbg-trunc">truncated</span>}
        </div>
        <JsonView data={detail.args} initialDepth={3} />
      </div>
      <div className="dbg-detail-pane">
        <div className="dbg-detail-label">
          {isError ? 'error' : 'response'} <span className="dbg-detail-size">{fmtChars(detail.received_chars)}</span>
          {detail.result_truncated && <span className="dbg-trunc">truncated · stored preview</span>}
        </div>
        {isError
          ? <pre className="dbg-error-body">{String(detail.result || '(no message)')}{detail.error_code ? `\n\ncode: ${detail.error_code}` : ''}</pre>
          : <JsonView data={detail.result} initialDepth={2} />}
      </div>
    </div>
  );
}

function SortCell({ children, align, active, dir, onClick }) {
  return (
    <span
      className={`${align === 'left' ? 'dbg-c-left' : 'dbg-c-num'} dbg-sortable${active ? ' active' : ''}`}
      onClick={onClick}
      role="button"
    >
      {children}
      <span className="dbg-sort-caret">{active ? (dir === 'asc' ? '▲' : '▼') : ''}</span>
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


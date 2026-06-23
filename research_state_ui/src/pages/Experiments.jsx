import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useProjectStore, selectExperiments, selectClaims, useProjectHref } from '../store/useProjectStore';
import { api } from '../api';
import ObjId from '../components/ObjId';
import StatusPill from '../components/StatusPill';
import { expName } from '../utils/experiment';
import { fmtDayTime, fmtDuration } from '../utils/format';

const LIFECYCLE = ['planned', 'design_review', 'ready_to_run', 'running', 'experiment_review', 'complete'];
const TERMINAL = ['failed', 'abandoned'];
// Sort rank for the status column: lifecycle order, then terminal states.
const STATUS_ORDER = [...LIFECYCLE, ...TERMINAL];

function isTerminal(status) {
  return status === 'complete' || TERMINAL.includes(status);
}

/**
 * Per-row derived facts the table sorts and renders on. Duration is
 * created→last transition for settled experiments, created→now for ones
 * still in flight (the 3s home poll keeps the live value ticking).
 */
function rowFacts(e, nowMs) {
  const status = (e.status || 'planned').toLowerCase();
  const createdMs = e.created_at ? Date.parse(e.created_at) : NaN;
  const settled = isTerminal(status);
  const endMs = settled && e.updated_at ? Date.parse(e.updated_at) : nowMs;
  const durationMs = Number.isFinite(createdMs) ? Math.max(0, endMs - createdMs) : NaN;
  return { status, createdMs, settled, endMs, durationMs };
}

const SORTS = {
  created: (a, b) => (a.facts.createdMs || 0) - (b.facts.createdMs || 0),
  finished: (a, b) => (a.facts.settled ? a.facts.endMs : 0) - (b.facts.settled ? b.facts.endMs : 0),
  duration: (a, b) => (a.facts.durationMs || 0) - (b.facts.durationMs || 0),
  status: (a, b) => STATUS_ORDER.indexOf(a.facts.status) - STATUS_ORDER.indexOf(b.facts.status),
  title: (a, b) => a.title.localeCompare(b.title),
};

export default function Experiments() {
  const projectId = useProjectStore(s => s.projectId);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const experiments = useProjectStore(selectExperiments);
  const claims = useProjectStore(selectClaims);
  const [showForm, setShowForm] = useState(false);
  const [sortKey, setSortKey] = useState('created');
  const [sortDir, setSortDir] = useState('desc');

  const rows = useMemo(() => {
    const nowMs = Date.now();
    const list = experiments.map(e => ({
      exp: e,
      title: expName(e),
      facts: rowFacts(e, nowMs),
    }));
    const cmp = SORTS[sortKey] || SORTS.created;
    list.sort((a, b) => (sortDir === 'asc' ? cmp(a, b) : cmp(b, a)));
    return list;
  }, [experiments, sortKey, sortDir]);

  function toggleSort(key) {
    if (key === sortKey) {
      setSortDir(d => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      // Time-ish columns read best newest/longest first; title A→Z.
      setSortDir(key === 'title' ? 'asc' : 'desc');
    }
  }

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <h1 className="page-title">What we try</h1>
          </div>
          <div className="page-actions">
            <button className="btn btn--primary" onClick={() => setShowForm(v => !v)}>
              {showForm ? 'Cancel' : 'New experiment'}
            </button>
          </div>
        </div>
      </header>

      {showForm && (
        <NewExperimentForm
          projectId={projectId}
          claims={claims}
          onCancel={() => setShowForm(false)}
          onCreated={async () => { setShowForm(false); await refreshHome(); }}
        />
      )}

      {rows.length === 0 ? (
        <div className="empty-state">
          <h2>No experiments yet</h2>
        </div>
      ) : (
        <ExperimentTable
          rows={rows}
          sortKey={sortKey}
          sortDir={sortDir}
          onSort={toggleSort}
        />
      )}
    </div>
  );
}

const COLUMNS = [
  { key: 'title', label: 'Experiment' },
  { key: 'status', label: 'Status' },
  { key: 'created', label: 'Created' },
  { key: 'finished', label: 'Finished' },
  { key: 'duration', label: 'Duration', right: true },
];

function WhenCell({ parts, title }) {
  if (!parts) return <div className="expt-when expt-when--none">—</div>;
  return (
    <div className="expt-when" title={title}>
      <span className="expt-when-day">{parts.day}</span>
      <span className="expt-when-time">{parts.time}</span>
    </div>
  );
}

function ExperimentTable({ rows, sortKey, sortDir, onSort }) {
  const navigate = useNavigate();
  const px = useProjectHref();
  return (
    <div className="expt-scroll">
      <div className="expt" role="table" aria-label="Experiments">
        <div className="expt-head" role="row">
          {COLUMNS.map(col => (
            <button
              key={col.key}
              type="button"
              role="columnheader"
              aria-sort={sortKey === col.key ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
              className={[
                'expt-th',
                col.right ? 'expt-th--r' : '',
                sortKey === col.key ? 'active' : '',
              ].filter(Boolean).join(' ')}
              onClick={() => onSort(col.key)}
            >
              {col.label}
              <span className="expt-sort" aria-hidden="true">
                {sortKey === col.key ? (sortDir === 'asc' ? '▲' : '▼') : ''}
              </span>
            </button>
          ))}
        </div>
        {rows.map(({ exp: e, title, facts }) => {
          const claimCount = Array.isArray(e.tested_claims) ? e.tested_claims.length : 0;
          const reviewCount = Array.isArray(e.reviews) ? e.reviews.length : 0;
          const created = fmtDayTime(e.created_at);
          const finished = facts.settled ? fmtDayTime(e.updated_at) : null;
          return (
            <div
              key={e.id}
              className="expt-row"
              role="row"
              tabIndex={0}
              onClick={() => navigate(px(`/experiments/${e.id}`))}
              onKeyDown={ev => { if (ev.key === 'Enter') navigate(px(`/experiments/${e.id}`)); }}
            >
              <div className="expt-main">
                <div className="expt-title" title={title}>{title}</div>
                {e.intent && <div className="expt-desc" title={e.intent}>{e.intent}</div>}
                <div className="expt-sub">
                  attempt {e.attempt_index}
                  {claimCount > 0 && <> · tests {claimCount} claim{claimCount === 1 ? '' : 's'}</>}
                  {reviewCount > 0 && <> · {reviewCount} review{reviewCount === 1 ? '' : 's'}</>}
                </div>
              </div>
              <div><StatusPill value={e.status} /></div>
              <WhenCell parts={created} title={e.created_at || ''} />
              {finished ? (
                <WhenCell parts={finished} title={e.updated_at || ''} />
              ) : (
                <div className="expt-when expt-when--none" title="still in progress">—</div>
              )}
              <div
                className={`expt-dur${facts.settled ? '' : ' expt-dur--live'}`}
                title={facts.settled ? 'created → last transition' : 'elapsed since created'}
              >
                {fmtDuration(facts.durationMs)}
                {!facts.settled && <span className="expt-live-dot" aria-hidden="true" />}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Mirrors the backend rule: folder-safe, starts with a letter/digit, ≤48 chars.
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$/;

function NewExperimentForm({ projectId, claims, onCancel, onCreated }) {
  const [name, setName] = useState('');
  const [intent, setIntent] = useState('');
  const [selectedClaims, setSelectedClaims] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const nameOk = NAME_RE.test(name);

  function toggleClaim(id) {
    setSelectedClaims(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function submit(e) {
    e.preventDefault();
    if (!nameOk || !intent.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.createExperiment(projectId, {
        name: name.trim(),
        intent: intent.trim(),
        claim_ids: Array.from(selectedClaims),
      });
      onCreated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-card" onSubmit={submit} style={{ marginBottom: 18 }}>
      <div className="form-row">
        <label className="label">Name</label>
        <input
          className="input"
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder="lora-rank-sweep"
          maxLength={48}
          autoFocus
          required
        />
        <div className="form-hint">
          Becomes the experiment folder <code>experiments/{nameOk ? name : '<name>'}/</code> —
          letters, digits, dots, dashes, underscores; unique within the project.
        </div>
        {name && !nameOk && (
          <div className="error-message">
            Folder-safe names start with a letter or digit and use only letters,
            digits, '.', '_' and '-'.
          </div>
        )}
      </div>
      <div className="form-row">
        <label className="label">Intent</label>
        <textarea
          className="textarea"
          value={intent}
          onChange={e => setIntent(e.target.value)}
          placeholder="Compare threshold rule against majority baseline on toy.csv."
          required
        />
      </div>
      {claims.length > 0 && (
        <div className="form-row">
          <label className="label">Tested claims (optional)</label>
          <div className="stack stack--sm">
            {claims.map(c => (
              <label key={c.id} className="cluster" style={{ cursor: 'pointer', alignItems: 'flex-start' }}>
                <input
                  type="checkbox"
                  checked={selectedClaims.has(c.id)}
                  onChange={() => toggleClaim(c.id)}
                  style={{ marginTop: 4 }}
                />
                <span style={{ fontSize: 'var(--text-base)' }}>
                  {c.statement}
                  <span style={{ marginLeft: 8 }}><ObjId id={c.id} /></span>
                </span>
              </label>
            ))}
          </div>
        </div>
      )}
      {error && <div className="error-message">{error}</div>}
      <div className="form-actions">
        <button type="button" className="btn btn--ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn--primary" disabled={busy || !nameOk || !intent.trim()}>
          {busy ? 'Creating…' : 'Create experiment'}
        </button>
      </div>
    </form>
  );
}

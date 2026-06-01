import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, selectClaims, selectExperiments } from '../store/useProjectStore';
import { api } from '../api';
import { parseIntent } from '../utils/intent';

const TABS = ['all', 'active', 'supported', 'weakened', 'contradicted', 'draft', 'abandoned'];
const CONFIDENCE_LEVELS = { low: 1, medium: 2, high: 3 };

export default function Claims() {
  const projectId = useProjectStore(s => s.projectId);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const claims = useProjectStore(selectClaims);
  const experiments = useProjectStore(selectExperiments);
  const [filter, setFilter] = useState('all');
  const [showForm, setShowForm] = useState(false);

  const counts = useMemo(() => {
    const map = { all: claims.length };
    for (const c of claims) {
      const k = (c.status || 'active').toLowerCase();
      map[k] = (map[k] || 0) + 1;
    }
    return map;
  }, [claims]);

  const filtered = useMemo(() => {
    if (filter === 'all') return claims;
    return claims.filter(c => (c.status || 'active').toLowerCase() === filter);
  }, [claims, filter]);

  const experimentsByClaim = useMemo(() => {
    const map = new Map();
    for (const e of experiments) {
      const linked = Array.isArray(e.tested_claims) ? e.tested_claims : [];
      for (const tc of linked) {
        if (!tc?.id) continue;
        if (!map.has(tc.id)) map.set(tc.id, []);
        map.get(tc.id).push(e);
      }
    }
    return map;
  }, [experiments]);

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <div className="page-eyebrow">Claims</div>
            <h1 className="page-title">What we think</h1>
            <p className="page-summary">
              Claims are the durable statements about your domain. Experiments test them; reviews accept or weaken them.
            </p>
          </div>
          <div className="page-actions">
            <button className="btn btn--primary" onClick={() => setShowForm(v => !v)}>
              {showForm ? 'Cancel' : 'New claim'}
            </button>
          </div>
        </div>
        <div className="tab-row" style={{ marginTop: 14 }}>
          {TABS.map(t => (
            <button
              key={t}
              className={`tab${filter === t ? ' active' : ''}`}
              onClick={() => setFilter(t)}
            >
              {t}
              <span className="tab-count">{counts[t] || 0}</span>
            </button>
          ))}
        </div>
      </header>

      {showForm && (
        <NewClaimForm
          projectId={projectId}
          onCancel={() => setShowForm(false)}
          onCreated={async () => { setShowForm(false); await refreshHome(); }}
        />
      )}

      {filtered.length === 0 ? (
        <div className="empty-state">
          <h2>No claims yet</h2>
          <p>{filter === 'all' ? 'Create one to get started.' : `No claims with status "${filter}".`}</p>
        </div>
      ) : (
        <div className="claim-feed">
          {filtered.map(c => (
            <ClaimEntry
              key={c.id}
              claim={c}
              linkedExperiments={experimentsByClaim.get(c.id) || []}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ClaimEntry({ claim, linkedExperiments }) {
  return (
    <article className="claim-entry">
      <Link to={`/claims/${claim.id}`} className="claim-entry-statement">
        {claim.statement}
      </Link>

      <div className="claim-entry-meta">
        <ConfidenceMark level={claim.confidence} />
        {claim.scope && <span className="claim-entry-scope">scoped to {claim.scope}</span>}
      </div>

      {linkedExperiments.length > 0 && (
        <ul className="claim-entry-tests">
          {linkedExperiments.map(e => {
            const { title } = parseIntent(e.intent);
            const cat = categorize(e.status);
            return (
              <li key={e.id}>
                <Link to={`/experiments/${e.id}`} className="claim-exp-line">
                  <span className={`claim-exp-mark claim-exp-mark--${cat}`} aria-hidden="true">
                    {cat === 'success' ? '✓' : cat === 'against' ? '✗' : '·'}
                  </span>
                  <span className="claim-exp-title">{title || e.id}</span>
                  <span className="claim-exp-status">{(e.status || '').replace(/_/g, ' ')}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </article>
  );
}

function ConfidenceMark({ level }) {
  const n = CONFIDENCE_LEVELS[(level || '').toLowerCase()] || 0;
  const label = level ? `${level} confidence` : 'confidence unset';
  return (
    <span className="claim-conf" title={label} aria-label={label}>
      {[1, 2, 3].map(i => (
        <span key={i} className={`claim-conf-dot${i <= n ? ' is-on' : ''}`} aria-hidden="true" />
      ))}
    </span>
  );
}

const SUPPORT_STATUSES = new Set(['supports', 'supported', 'complete', 'completed', 'pass', 'accepted', 'succeeded']);
const AGAINST_STATUSES = new Set(['refutes', 'contradicted', 'weakened', 'failed', 'fail', 'rejected']);
const LIVE_STATUSES = new Set(['running', 'queued', 'design_review', 'experiment_review', 'needs_changes', 'ready_to_run', 'qualified', 'awaiting']);

function categorize(status) {
  const s = (status || '').toLowerCase();
  if (SUPPORT_STATUSES.has(s)) return 'success';
  if (AGAINST_STATUSES.has(s)) return 'against';
  if (LIVE_STATUSES.has(s)) return 'live';
  return 'idle';
}

function NewClaimForm({ projectId, onCancel, onCreated }) {
  const [statement, setStatement] = useState('');
  const [scope, setScope] = useState('');
  const [confidence, setConfidence] = useState('medium');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    if (!statement.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.createClaim(projectId, { statement: statement.trim(), scope: scope.trim(), confidence });
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
        <label className="label">Statement</label>
        <textarea
          className="textarea"
          value={statement}
          onChange={e => setStatement(e.target.value)}
          placeholder="A length-threshold classifier improves accuracy on toy.csv."
          autoFocus
          required
        />
      </div>
      <div className="form-row">
        <label className="label">Scope</label>
        <input className="input" value={scope} onChange={e => setScope(e.target.value)} placeholder="toy.csv only" />
      </div>
      <div className="form-row">
        <label className="label">Confidence</label>
        <select className="select" value={confidence} onChange={e => setConfidence(e.target.value)}>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
        </select>
      </div>
      {error && <div className="error-message">{error}</div>}
      <div className="form-actions">
        <button type="button" className="btn btn--ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn--primary" disabled={busy || !statement.trim()}>
          {busy ? 'Creating…' : 'Create claim'}
        </button>
      </div>
    </form>
  );
}

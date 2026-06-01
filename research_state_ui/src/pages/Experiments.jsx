import { Fragment, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, selectExperiments, selectClaims } from '../store/useProjectStore';
import { api } from '../api';
import ObjId from '../components/ObjId';
import StatusPill from '../components/StatusPill';
import { parseIntent } from '../utils/intent';

const LIFECYCLE = ['planned', 'design_review', 'ready_to_run', 'running', 'experiment_review', 'complete'];
const TERMINAL = ['failed', 'abandoned'];

export default function Experiments() {
  const projectId = useProjectStore(s => s.projectId);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const experiments = useProjectStore(selectExperiments);
  const claims = useProjectStore(selectClaims);
  const [filter, setFilter] = useState('all');
  const [showForm, setShowForm] = useState(false);

  const counts = useMemo(() => {
    const map = { all: experiments.length };
    for (const e of experiments) {
      const k = (e.status || 'planned').toLowerCase();
      map[k] = (map[k] || 0) + 1;
    }
    return map;
  }, [experiments]);

  const filtered = useMemo(() => {
    if (filter === 'all') return experiments;
    return experiments.filter(e => (e.status || 'planned').toLowerCase() === filter);
  }, [experiments, filter]);

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            <div className="page-eyebrow">Experiments</div>
            <h1 className="page-title">What we try</h1>
          </div>
          <div className="page-actions">
            <button className="btn btn--primary" onClick={() => setShowForm(v => !v)}>
              {showForm ? 'Cancel' : 'New experiment'}
            </button>
          </div>
        </div>
        <div className="tab-row lifecycle-row">
          <button
            className={`tab${filter === 'all' ? ' active' : ''}`}
            onClick={() => setFilter('all')}
          >
            all
            <span className="tab-count">{counts.all || 0}</span>
          </button>
          <span className="lc-divider" aria-hidden="true" />
          {LIFECYCLE.map((s, i) => (
            <Fragment key={s}>
              <button
                className={`tab${filter === s ? ' active' : ''}`}
                onClick={() => setFilter(s)}
              >
                {s.replace(/_/g, ' ')}
                <span className="tab-count">{counts[s] || 0}</span>
              </button>
              {i < LIFECYCLE.length - 1 && (
                <span className="lc-arrow" aria-hidden="true">→</span>
              )}
            </Fragment>
          ))}
          <span className="lc-divider" aria-hidden="true" />
          {TERMINAL.map(s => (
            <button
              key={s}
              className={`tab tab--terminal${filter === s ? ' active' : ''}`}
              onClick={() => setFilter(s)}
            >
              {s}
              <span className="tab-count">{counts[s] || 0}</span>
            </button>
          ))}
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

      {filtered.length === 0 ? (
        <div className="empty-state">
          <h2>No experiments yet</h2>
          <p>{filter === 'all' ? 'Create one to test a claim.' : `No experiments with status "${filter.replace(/_/g, ' ')}".`}</p>
        </div>
      ) : (
        <div className="list card card--flush" style={{ marginTop: 16 }}>
          {filtered.map(e => (
            <Link key={e.id} to={`/experiments/${e.id}`} className="list-row">
              <div className="list-row-main">
                <div className="list-row-title">{parseIntent(e.intent).title || e.id}</div>
                <div className="list-row-sub">
                  <ObjId id={e.id} />
                  {' · attempt '}{e.attempt_index}
                  {Array.isArray(e.tested_claims) && e.tested_claims.length > 0 && (
                    <> · tests {e.tested_claims.length} claim{e.tested_claims.length === 1 ? '' : 's'}</>
                  )}
                </div>
              </div>
              <div className="list-row-aside">
                <StatusPill value={e.status} />
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function NewExperimentForm({ projectId, claims, onCancel, onCreated }) {
  const [intent, setIntent] = useState('');
  const [selectedClaims, setSelectedClaims] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  function toggleClaim(id) {
    setSelectedClaims(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function submit(e) {
    e.preventDefault();
    if (!intent.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.createExperiment(projectId, {
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
        <label className="label">Intent</label>
        <textarea
          className="textarea"
          value={intent}
          onChange={e => setIntent(e.target.value)}
          placeholder="Compare threshold rule against majority baseline on toy.csv."
          autoFocus
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
        <button type="submit" className="btn btn--primary" disabled={busy || !intent.trim()}>
          {busy ? 'Creating…' : 'Create experiment'}
        </button>
      </div>
    </form>
  );
}

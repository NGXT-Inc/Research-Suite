import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, selectExperiments } from '../store/useProjectStore';
import { api } from '../api';
import JobCard from '../components/JobCard';
import ObjId from '../components/ObjId';

const STATUS_TABS = ['all', 'queued', 'running', 'succeeded', 'failed', 'cancelled'];

/**
 * Jobs index — every Ray-backed run for the current project.
 *
 * Refreshes every 3s while at least one job is still queued/running, then
 * falls back to 10s heartbeat. Filter by experiment or status using the tab
 * row + experiment selector.
 */
export default function Jobs() {
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);
  const [jobs, setJobs] = useState(null);
  const [error, setError] = useState(null);
  const [filterStatus, setFilterStatus] = useState('all');
  const [filterExp, setFilterExp] = useState('all');

  const fetchJobs = useCallback(async () => {
    try {
      const data = await api.listJobs(projectId);
      setJobs(data.jobs || []);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [projectId]);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  // Active-aware polling: 3s while any job is active, 10s otherwise.
  useEffect(() => {
    const anyActive = (jobs || []).some(j => ['queued', 'running', 'submitting'].includes(j.status));
    const interval = anyActive ? 3000 : 10000;
    const t = setInterval(fetchJobs, interval);
    return () => clearInterval(t);
  }, [fetchJobs, jobs]);

  const expById = useMemo(() => Object.fromEntries(experiments.map(e => [e.id, e])), [experiments]);

  const counts = useMemo(() => {
    const map = { all: (jobs || []).length };
    for (const j of (jobs || [])) {
      map[j.status] = (map[j.status] || 0) + 1;
    }
    return map;
  }, [jobs]);

  const filtered = useMemo(() => {
    let list = jobs || [];
    if (filterStatus !== 'all') list = list.filter(j => j.status === filterStatus);
    if (filterExp !== 'all') list = list.filter(j => j.experiment_id === filterExp);
    return list;
  }, [jobs, filterStatus, filterExp]);

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-eyebrow">Jobs</div>
        <h1 className="page-title">Background runs</h1>
        <p className="page-summary">
          Ray-backed jobs submitted by experiments. Status, logs, and outputs reflect the
          current state of the Ray cluster — see <span className="mono">jobs.py</span> for
          the policy layer (allowed executables, sensitive-env filtering, terminal reconciliation).
        </p>
        <div className="cluster" style={{ marginTop: 14, gap: 8 }}>
          <div className="tab-row">
            {STATUS_TABS.map(s => (
              <button key={s} className={`tab${filterStatus === s ? ' active' : ''}`} onClick={() => setFilterStatus(s)}>
                {s}
                <span className="tab-count">{counts[s] || 0}</span>
              </button>
            ))}
          </div>
          {experiments.length > 0 && (
            <select className="select" value={filterExp} onChange={e => setFilterExp(e.target.value)} style={{ maxWidth: 360 }}>
              <option value="all">All experiments</option>
              {experiments.map(e => (
                <option key={e.id} value={e.id}>{e.id} — {(e.intent || '').slice(0, 60)}</option>
              ))}
            </select>
          )}
        </div>
      </header>

      {error && <div className="error-message">{error}</div>}

      {jobs == null ? (
        <div className="empty">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="empty-state">
          <h2>No jobs match these filters</h2>
          <p>
            {jobs.length === 0
              ? 'Submit a job from an experiment in ready_to_run status.'
              : 'Try a different status or experiment filter.'}
          </p>
        </div>
      ) : (
        <div className="stack stack--lg">
          {filtered.map((j, idx) => {
            const exp = expById[j.experiment_id];
            return (
              <div key={j.id}>
                {exp && (
                  <div className="cluster" style={{ marginBottom: 6, fontSize: 'var(--text-xs)', color: 'var(--muted)' }}>
                    <Link to={`/experiments/${exp.id}`} style={{ color: 'var(--active)' }}>{exp.intent}</Link>
                    <ObjId id={exp.id} />
                  </div>
                )}
                <JobCard
                  projectId={projectId}
                  job={j}
                  onChanged={fetchJobs}
                  defaultOpen={idx === 0 && ['queued', 'running'].includes(j.status)}
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

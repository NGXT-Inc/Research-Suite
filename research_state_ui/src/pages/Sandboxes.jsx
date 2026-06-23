import { useCallback, useEffect, useMemo, useState } from 'react';
import { useProjectStore, selectExperiments, selectEventsAll } from '../store/useProjectStore';
import { api } from '../api';
import SandboxTable from '../components/SandboxTable';

const STATUS_TABS = ['all', 'running', 'provisioning', 'terminated'];

/**
 * Sandboxes index — the compute fleet as an infra table.
 *
 * One sandbox per experiment; the agent procures them over MCP and drives them
 * over SSH. This page is the instance console: status, hardware, lifetime, and
 * endpoint per row, with an expand-to-terminal drawer. The row UI lives in the
 * shared SandboxTable (also used on Home); this page owns the live fetch and
 * the status-filter tabs.
 */
export default function Sandboxes() {
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);
  const events = useProjectStore(selectEventsAll);
  const [sandboxes, setSandboxes] = useState(null);
  const [error, setError] = useState(null);
  const [filterStatus, setFilterStatus] = useState('all');

  const fetchSandboxes = useCallback(async () => {
    try {
      const data = await api.listSandboxes(projectId);
      setSandboxes(data.sandboxes || []);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [projectId]);

  useEffect(() => { fetchSandboxes(); }, [fetchSandboxes]);

  useEffect(() => {
    const anyActive = (sandboxes || []).some(
      s => s.status === 'running' || s.status === 'provisioning',
    );
    const t = setInterval(fetchSandboxes, anyActive ? 3000 : 10000);
    return () => clearInterval(t);
  }, [fetchSandboxes, sandboxes]);

  const counts = useMemo(() => {
    const map = { all: (sandboxes || []).length };
    for (const s of (sandboxes || [])) map[s.status] = (map[s.status] || 0) + 1;
    return map;
  }, [sandboxes]);

  const filtered = useMemo(() => {
    if (filterStatus === 'all') return sandboxes || [];
    return (sandboxes || []).filter(s => s.status === filterStatus);
  }, [sandboxes, filterStatus]);

  return (
    <div className="page-stage">
      <header className="page-header">
        <h1 className="page-title">Compute fleet</h1>
        <div className="tab-row" style={{ marginTop: 12 }}>
          {STATUS_TABS.map(s => (
            <button key={s} className={`tab${filterStatus === s ? ' active' : ''}`} onClick={() => setFilterStatus(s)}>
              {s === 'running' && (counts.running || 0) > 0 && <span className="sbxt-tab-dot" />}
              {s}
              <span className="tab-count">{counts[s] || 0}</span>
            </button>
          ))}
        </div>
      </header>

      {error && <div className="error-message">{error}</div>}

      {sandboxes == null ? (
        <div className="empty">Loading…</div>
      ) : (
        <SandboxTable
          sandboxes={filtered}
          experiments={experiments}
          events={events}
          projectId={projectId}
          empty={(
            <div className="empty-state">
              <h2>No sandboxes</h2>
              {sandboxes.length > 0 && <p>{`No ${filterStatus} sandboxes.`}</p>}
            </div>
          )}
        />
      )}
    </div>
  );
}

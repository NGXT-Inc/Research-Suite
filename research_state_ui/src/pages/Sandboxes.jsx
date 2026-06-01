import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, selectExperiments } from '../store/useProjectStore';
import { api } from '../api';
import ObjId from '../components/ObjId';
import StatusPill from '../components/StatusPill';
import SandboxTerminal from '../components/SandboxTerminal';
import { parseIntent } from '../utils/intent';

const STATUS_TABS = ['all', 'running', 'provisioning', 'terminated'];

/**
 * Sandboxes index — one Modal sandbox per experiment.
 *
 * The agent procures sandboxes over MCP (sandbox.request) and runs commands on
 * them over SSH; this page makes the fleet visible: how many are running, and a
 * drill-in terminal for each one.
 */
export default function Sandboxes() {
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);
  const [sandboxes, setSandboxes] = useState(null);
  const [error, setError] = useState(null);
  const [filterStatus, setFilterStatus] = useState('all');
  const [expanded, setExpanded] = useState(null);
  const [now, setNow] = useState(Date.now());

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

  // 1Hz tick for live uptime / "expires in" labels.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const expById = useMemo(() => Object.fromEntries(experiments.map(e => [e.id, e])), [experiments]);

  const counts = useMemo(() => {
    const map = { all: (sandboxes || []).length };
    for (const s of (sandboxes || [])) map[s.status] = (map[s.status] || 0) + 1;
    return map;
  }, [sandboxes]);

  const runningCount = counts.running || 0;
  const provisioningCount = counts.provisioning || 0;
  const totalCount = counts.all || 0;

  const filtered = useMemo(() => {
    let list = sandboxes || [];
    if (filterStatus !== 'all') list = list.filter(s => s.status === filterStatus);
    // running first, then provisioning, then most-recently-updated.
    const rank = (st) => (st === 'running' ? 0 : st === 'provisioning' ? 1 : 2);
    return list.slice().sort((a, b) => {
      const ar = rank(a.status);
      const br = rank(b.status);
      if (ar !== br) return ar - br;
      return String(b.updated_at || '').localeCompare(String(a.updated_at || ''));
    });
  }, [sandboxes, filterStatus]);

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-eyebrow">Sandboxes</div>
        <h1 className="page-title">Experiment sandboxes</h1>
        <div className="sbx-fleet" aria-label="running sandbox count">
          <span className={`sbx-fleet-dot${runningCount > 0 ? ' live' : ''}`} />
          <span className="sbx-fleet-count tabular">{runningCount}</span>
          <span className="sbx-fleet-label">running</span>
          {provisioningCount > 0 && (
            <>
              <span className="sbx-fleet-sep">·</span>
              <span className="sbx-fleet-total tabular">{provisioningCount}</span>
              <span className="sbx-fleet-label">provisioning</span>
            </>
          )}
          <span className="sbx-fleet-sep">·</span>
          <span className="sbx-fleet-total tabular">{totalCount}</span>
          <span className="sbx-fleet-label">total</span>
        </div>
        <p className="page-summary">
          One Modal sandbox per experiment, procured by the agent and accessed over SSH.
          Expand a sandbox to watch its live terminal.
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
        </div>
      </header>

      {error && <div className="error-message">{error}</div>}

      {sandboxes == null ? (
        <div className="empty">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="empty-state">
          <h2>No sandboxes</h2>
          <p>
            {(sandboxes.length === 0)
              ? 'The agent provisions a sandbox with sandbox.request once an experiment is ready_to_run.'
              : 'Try a different status filter.'}
          </p>
        </div>
      ) : (
        <div className="stack">
          {filtered.map((s) => (
            <SandboxRow
              key={s.experiment_id}
              sandbox={s}
              experiment={expById[s.experiment_id]}
              projectId={projectId}
              now={now}
              open={expanded === s.experiment_id}
              onToggle={() => setExpanded(expanded === s.experiment_id ? null : s.experiment_id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function SandboxRow({ sandbox, experiment, projectId, now, open, onToggle }) {
  const s = sandbox;
  const live = s.status === 'running';
  const title = experiment ? (parseIntent(experiment.intent).title || experiment.intent) : s.experiment_id;
  const resources = [
    s.gpu && `gpu ${s.gpu}`,
    s.cpu && `${s.cpu} cpu`,
    s.memory && `${Math.round(s.memory / 1024)} GiB`,
  ].filter(Boolean).join(' · ');
  const endpoint = s.ssh_host && s.ssh_port ? `${s.ssh_user || 'root'}@${s.ssh_host}:${s.ssh_port}` : null;

  return (
    <div className={`sbx-card${open ? ' sbx-card--open' : ''}`}>
      <button type="button" className="sbx-card-head" onClick={onToggle} aria-expanded={open}>
        <span className={`sbx-card-twist${open ? ' open' : ''}`} aria-hidden="true">▸</span>
        <StatusPill value={s.status} />
        {live && <span className="log-tail-live-dot" title="live" />}
        <span className="sbx-card-title">{title}</span>
        <span className="sbx-card-spacer" />
        <span className="sbx-card-meta mono">
          {resources && <span>{resources}</span>}
          {live && s.requested_at && <span>· up {fmtSince(now, s.requested_at)}</span>}
          {live && s.expires_at && <span>· expires {fmtUntil(now, s.expires_at)}</span>}
        </span>
      </button>
      <div className="sbx-card-sub">
        <span className="mono">{s.sandbox_id || '—'}</span>
        {endpoint && <><span className="sbx-fleet-sep">·</span><span className="mono">{endpoint}</span></>}
        <span className="sbx-fleet-sep">·</span>
        <Link to={`/experiments/${s.experiment_id}#execution`} className="sbx-card-link">open experiment</Link>
        <span className="sbx-fleet-sep">·</span>
        <ObjId id={s.experiment_id} />
      </div>
      {open && (
        <div className="sbx-card-body">
          <SandboxTerminal projectId={projectId} experimentId={s.experiment_id} />
        </div>
      )}
    </div>
  );
}

function fmtSince(now, iso) {
  const ms = now - Date.parse(iso);
  return fmtDur(ms);
}

function fmtUntil(now, iso) {
  const ms = Date.parse(iso) - now;
  if (ms <= 0) return 'soon';
  return 'in ' + fmtDur(ms);
}

function fmtDur(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60 ? ` ${m % 60}m` : ''}`;
}

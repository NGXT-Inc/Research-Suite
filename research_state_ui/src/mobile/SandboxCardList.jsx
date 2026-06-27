import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, useProjectHref, selectExperiments, selectSandboxes, selectEventsAll } from '../store/useProjectStore';
import { api } from '../api';
import ObjId from '../components/ObjId';
import StatusPill from '../components/StatusPill';
import SandboxTerminal from '../components/SandboxTerminal';
import SlideToConfirm from './SlideToConfirm';
import { SkeletonCards } from './Skeleton';
import { toast } from './toastStore';
import { expName } from '../utils/experiment';
import { fmtDuration } from '../utils/format';
import { PARACHUTE_CHIPS, latestParachute } from '../utils/parachute';

const sandboxRowId = (s) => s.sandbox_uid || s.sandbox_id || s.experiment_id;
const primaryExperimentId = (s) => (
  s.experiment_id
  || (Array.isArray(s.active_experiment_ids) ? s.active_experiment_ids[0] : '')
  || ''
);

/**
 * Mobile replacement for the 840px Sandboxes infra table: one card per
 * sandbox from the store's already-polled list. Release is the single
 * sanctioned mobile mutation — two-step inline confirm, with an explicit
 * escalation when a live experiment is attached.
 */
export default function SandboxCardList() {
  const sandboxes = useProjectStore(selectSandboxes);
  const experiments = useProjectStore(selectExperiments);
  const events = useProjectStore(selectEventsAll);
  const home = useProjectStore(s => s.home);
  const expById = Object.fromEntries(experiments.map(e => [e.id, e]));
  // One drawer open at a time (mirrors the desktop table) — the panel polls
  // sandbox/metrics/terminal while mounted, so don't stack them.
  const [expandedId, setExpandedId] = useState(null);

  if (!home) {
    return (
      <div className="page-stage">
        <header className="page-header"><h1 className="page-title">Compute fleet</h1></header>
        <SkeletonCards count={2} />
      </div>
    );
  }

  const rank = (st) => (st === 'running' ? 0 : st === 'provisioning' ? 1 : 2);
  const rows = sandboxes.slice().sort((a, b) => {
    const d = rank(a.status) - rank(b.status);
    if (d !== 0) return d;
    return String(b.updated_at || '').localeCompare(String(a.updated_at || ''));
  });

  return (
    <div className="page-stage">
      <header className="page-header">
        <h1 className="page-title">Compute fleet</h1>
      </header>

      {rows.length === 0 ? (
        <div className="empty-state">
          <h2>No sandboxes</h2>
        </div>
      ) : (
        <div className="mcard-list">
          {rows.map(s => {
            const rowId = sandboxRowId(s);
            const experimentId = primaryExperimentId(s);
            return (
              <SandboxCard
                key={rowId}
                sandbox={s}
                experiment={expById[experimentId]}
                experimentId={experimentId}
                parachute={latestParachute(events, experimentId, s.sandbox_id)}
                open={expandedId === rowId}
                onToggle={() => setExpandedId(prev => (prev === rowId ? null : rowId))}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function SandboxCard({ sandbox: s, experiment, experimentId, parachute, open, onToggle }) {
  const px = useProjectHref();
  const projectId = useProjectStore(st => st.projectId);
  const chip = parachute ? PARACHUTE_CHIPS[parachute] : null;
  const refreshHome = useProjectStore(st => st.refreshHome);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const live = s.status === 'running';
  const now = Date.now();
  const up = live && s.requested_at ? now - Date.parse(s.requested_at) : null;
  const left = live && s.expires_at ? Date.parse(s.expires_at) - now : null;
  const hardware = [
    s.gpu,
    s.cpu && `${s.cpu} cpu`,
    s.memory && `${Math.round(s.memory / 1024)} GiB`,
  ].filter(Boolean).join(' · ');
  const endpoint = s.ssh_host && s.ssh_port ? `${s.ssh_user || 'root'}@${s.ssh_host}:${s.ssh_port}` : null;
  const expRunning = experiment && experiment.status === 'running';

  async function release() {
    setBusy(true);
    setError(null);
    try {
      await api.releaseSandbox(projectId, experimentId, { sandboxUid: s.sandbox_uid });
      setConfirming(false);
      toast('Sandbox released', { variant: 'success' });
      await refreshHome();
    } catch (err) {
      setError(err.message);
      toast(`Release failed: ${err.message}`, { variant: 'error' });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`mcard${left != null && left < 30 * 60 * 1000 ? ' mcard--attn' : ''}`}>
      <div className="mcard-head">
        <div className="mcard-title">{experiment ? expName(experiment) : experimentId || s.sandbox_uid || s.sandbox_id}</div>
        {chip && <span className={`parachute-chip parachute-chip--${chip.variant}`}>{chip.short}</span>}
        <StatusPill value={s.status} />
      </div>
      <div className="mcard-meta">
        {hardware && <span className="mono">{hardware}</span>}
        {up != null && <span>up {fmtDuration(up)}</span>}
        {left != null && <span>expires in {left <= 0 ? 'soon' : fmtDuration(left)}</span>}
        {s.sandbox_id && <span><ObjId id={s.sandbox_id} /></span>}
      </div>
      {endpoint && <div className="mcard-meta"><span className="mono">{endpoint}</span></div>}

      <div className="mcard-actions">
        <button type="button" className="btn btn--sm" onClick={onToggle} aria-expanded={open}>
          {open ? 'Hide details ▾' : 'Details & terminal ▸'}
        </button>
        {experimentId && (
          <Link to={px(`/experiments/${experimentId}`)} className="btn btn--sm btn--ghost">
            Open experiment →
          </Link>
        )}
        {live && !confirming && (
          <button type="button" className="btn btn--sm btn--ghost" onClick={() => setConfirming(true)}>
            Release…
          </button>
        )}
      </div>

      {open && (
        <div className="mcard-drawer">
          <SandboxTerminal projectId={projectId} experimentId={experimentId} sandboxUid={s.sandbox_uid} readOnly />
        </div>
      )}

      {confirming && (
        <div className="mconfirm">
          {expRunning && (
            <div className="mconfirm-warn">
              ⚠ {expName(experiment)} is RUNNING on this sandbox — releasing terminates the VM under it.
            </div>
          )}
          <div style={{ marginBottom: 10 }}>Terminate this sandbox?</div>
          <SlideToConfirm busy={busy} onConfirm={release} label="Slide to release" busyLabel="Releasing…" />
          <div className="mconfirm-actions" style={{ marginTop: 8 }}>
            <button type="button" className="btn btn--sm btn--ghost" onClick={() => setConfirming(false)} disabled={busy}>
              Keep it
            </button>
          </div>
          {error && <div className="error-message" style={{ marginTop: 8 }}>{error}</div>}
        </div>
      )}
    </div>
  );
}

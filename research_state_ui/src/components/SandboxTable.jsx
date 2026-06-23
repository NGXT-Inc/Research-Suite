import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectHref } from '../store/useProjectStore';
import SandboxTerminal from './SandboxTerminal';
import { expName } from '../utils/experiment';
import { fmtDuration } from '../utils/format';
import { PARACHUTE_CHIPS, latestParachute } from '../utils/parachute';

// Column template (chevron · status · experiment · hardware · uptime · expires
// · endpoint · links) lives in CSS as --sbxt-cols so the head and every row
// share one source of truth and stay aligned.

const rank = (st) => (st === 'running' ? 0 : st === 'provisioning' ? 1 : 2);

/**
 * SandboxTable — the compute fleet as an infra table.
 *
 * One sandbox per experiment; status, hardware, lifetime, and endpoint per row,
 * with an expand-to-terminal drawer (the live terminal UI is unchanged — see
 * SandboxTerminal). Shared between the Sandboxes index (full fleet, with its own
 * status-filter tabs) and the Home dashboard (current project at a glance) so
 * both surfaces render the identical row UI over the same /sandboxes payload.
 *
 * Rows are sorted running → provisioning → terminated, then newest first; the
 * caller passes whatever subset it wants shown. Live uptime / "expires in"
 * labels tick at 1Hz only while something is actually running.
 */
export default function SandboxTable({ sandboxes, experiments, events, projectId, empty = null }) {
  const [expanded, setExpanded] = useState(null);
  const [now, setNow] = useState(Date.now());

  const rows = useMemo(() => (
    (sandboxes || []).slice().sort((a, b) => {
      const ar = rank(a.status);
      const br = rank(b.status);
      if (ar !== br) return ar - br;
      return String(b.updated_at || '').localeCompare(String(a.updated_at || ''));
    })
  ), [sandboxes]);

  const anyLive = rows.some(s => s.status === 'running' || s.status === 'provisioning');
  useEffect(() => {
    if (!anyLive) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [anyLive]);

  const expById = useMemo(
    () => Object.fromEntries((experiments || []).map(e => [e.id, e])),
    [experiments],
  );

  if (rows.length === 0) return empty;

  return (
    <div className="sbxt-scroll">
      <div className="sbxt">
        <div className="sbxt-head">
          <span aria-hidden="true" />
          <span className="sbxt-th">Status</span>
          <span className="sbxt-th">Experiment</span>
          <span className="sbxt-th">Hardware</span>
          <span className="sbxt-th sbxt-th--r">Uptime</span>
          <span className="sbxt-th sbxt-th--r">Expires</span>
          <span className="sbxt-th">SSH endpoint</span>
          <span className="sbxt-th sbxt-th--r">Links</span>
        </div>
        {rows.map(s => (
          <SandboxRow
            key={s.experiment_id}
            sandbox={s}
            experiment={expById[s.experiment_id]}
            projectId={projectId}
            now={now}
            parachute={latestParachute(events, s.experiment_id, s.sandbox_id)}
            open={expanded === s.experiment_id}
            onToggle={() => setExpanded(expanded === s.experiment_id ? null : s.experiment_id)}
          />
        ))}
      </div>
    </div>
  );
}

function SandboxRow({ sandbox, experiment, projectId, now, parachute, open, onToggle }) {
  const px = useProjectHref();
  const s = sandbox;
  const live = s.status === 'running';
  const chip = parachute ? PARACHUTE_CHIPS[parachute] : null;
  const title = experiment ? expName(experiment) : s.experiment_id;
  const hardware = [
    s.gpu,
    s.cpu && `${s.cpu} cpu`,
    s.memory && `${Math.round(s.memory / 1024)} GiB`,
  ].filter(Boolean).join(' · ');
  const endpoint = s.ssh_host && s.ssh_port ? `${s.ssh_user || 'root'}@${s.ssh_host}:${s.ssh_port}` : null;

  const expiresMs = live && s.expires_at ? Date.parse(s.expires_at) - now : null;
  const expiresCls = expiresMs == null ? '' : expiresMs < 120000 ? ' sbxt-warn--hot' : expiresMs < 600000 ? ' sbxt-warn' : '';

  const onKey = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(); }
  };

  return (
    <div className={`sbxt-rowgroup${open ? ' open' : ''}`}>
      <div
        className="sbxt-row"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={onToggle}
        onKeyDown={onKey}
      >
        <span className={`sbxt-twist${open ? ' open' : ''}`} aria-hidden="true">▸</span>
        <span className="sbxt-status">
          <span className={`sbxt-dot sbxt-dot--${s.status}`} />
          <span className="sbxt-status-label">{s.status}</span>
          {chip && <span className={`parachute-chip parachute-chip--${chip.variant}`}>{chip.short}</span>}
        </span>
        <span className="sbxt-exp">
          <span className="sbxt-exp-title">{title}</span>
        </span>
        <span className="sbxt-hw mono" title={hardware}>{hardware || '—'}</span>
        <span className="sbxt-num">{live && s.requested_at ? fmtDuration(now - Date.parse(s.requested_at)) : '—'}</span>
        <span className={`sbxt-num${expiresCls}`}>
          {expiresMs == null ? '—' : expiresMs <= 0 ? 'soon' : fmtDuration(expiresMs)}
        </span>
        <span className="sbxt-ep mono" title={endpoint || ''}>{endpoint || '—'}</span>
        <span className="sbxt-links" onClick={(e) => e.stopPropagation()}>
          <Link to={px(`/experiments/${s.experiment_id}#execution`)} className="sbxt-link">open ↗</Link>
          <DashboardChips dashboards={s.dashboards} mlflow={s.mlflow} />
        </span>
      </div>
      {open && (
        <div className="sbxt-drawer">
          <SandboxTerminal projectId={projectId} experimentId={s.experiment_id} />
        </div>
      )}
    </div>
  );
}

function DashboardChips({ dashboards, mlflow }) {
  const centralMlflowUrl = mlflow?.configured
    ? (mlflow.dashboard_url || mlflow.tracking_uri)
    : '';
  if (!dashboards && !centralMlflowUrl) return null;
  const entries = [
    (centralMlflowUrl || dashboards?.mlflow) && {
      key: 'mlflow',
      label: 'MLflow',
      url: centralMlflowUrl || dashboards?.mlflow,
    },
    dashboards?.tensorboard && { key: 'tensorboard', label: 'TB', url: dashboards.tensorboard },
  ].filter(Boolean);
  if (entries.length === 0) return null;
  return (
    <>
      {entries.map((e) => (
        <a
          key={e.key}
          href={e.url}
          target="_blank"
          rel="noreferrer noopener"
          className="sbxt-link sbxt-link--muted"
          title={`Open ${e.label} for this sandbox in a new tab`}
        >
          {e.label} ↗
        </a>
      ))}
    </>
  );
}

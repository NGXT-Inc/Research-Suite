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
const sandboxRowId = (s) => s.sandbox_uid || s.sandbox_id || s.experiment_id;
const primaryExperimentId = (s) => (
  s.experiment_id
  || (Array.isArray(s.active_experiment_ids) ? s.active_experiment_ids[0] : '')
  || ''
);

/**
 * SandboxTable — the compute fleet as an infra table.
 *
 * One row per sandbox; experiment relationships come from the attachments
 * ledger. Status, hardware, lifetime, and endpoint stay per row, with an
 * expand-to-terminal drawer (the live terminal UI is unchanged — see
 * SandboxTerminal). Shared between the Sandboxes index (full fleet, with its own
 * status-filter tabs) and Home (current project at a glance) so both surfaces
 * render the identical row UI over the same /sandboxes payload.
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
        <div className="sbxt-head con-head">
          <span aria-hidden="true" />
          <span className="th th--con">Status</span>
          <span className="th th--con">Experiment</span>
          <span className="th th--con">Hardware</span>
          <span className="th th--con th--r">Uptime</span>
          <span className="th th--con th--r">Expires</span>
          <span className="th th--con">SSH endpoint</span>
          <span className="th th--con th--r">Links</span>
        </div>
        {rows.map(s => {
          const rowId = sandboxRowId(s);
          const experimentId = primaryExperimentId(s);
          return (
            <SandboxRow
              key={rowId}
              sandbox={s}
              experiment={expById[experimentId]}
              experimentId={experimentId}
              projectId={projectId}
              now={now}
              parachute={latestParachute(events, experimentId, s.sandbox_id)}
              open={expanded === rowId}
              onToggle={() => setExpanded(expanded === rowId ? null : rowId)}
            />
          );
        })}
      </div>
    </div>
  );
}

function SandboxRow({ sandbox, experiment, experimentId, projectId, now, parachute, open, onToggle }) {
  const px = useProjectHref();
  const s = sandbox;
  const live = s.status === 'running';
  const chip = parachute ? PARACHUTE_CHIPS[parachute] : null;
  const title = experiment ? expName(experiment) : experimentId || s.sandbox_uid || s.sandbox_id;
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
        <span className={`twist${open ? ' open' : ''}`} aria-hidden="true">▸</span>
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
          {experimentId && (
            <Link to={px(`/experiments/${experimentId}#execution`)} className="sbxt-link">open ↗</Link>
          )}
        </span>
      </div>
      {open && (
        <div className="sbxt-drawer">
          <SandboxTerminal projectId={projectId} experimentId={experimentId} sandboxUid={s.sandbox_uid} />
        </div>
      )}
    </div>
  );
}

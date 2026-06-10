import { useEffect } from 'react';
import { createPortal } from 'react-dom';

/**
 * ExperimentSyncDetailsModal — minimal drill-in for one experiment's sandbox
 * rsync. Signal only: status, what maps where, last-sync state, errors, action.
 *
 * Sync model (services/sandboxes.py + execution/ssh_rsync.py): local files are
 * pushed once at provision, then the remote sync dir is pulled back every ~5s
 * (remote wins, --delete mirrors). Derived entirely from data the store already
 * polls — the sandbox row (dir paths, status) and this experiment's `sandbox.*`
 * rsync events.
 */

const PULL_EVENTS = new Set(['sandbox.rsynced', 'sandbox.synced']);

const STATUS_KIND = {
  running: 'active',
  provisioning: 'pending',
  terminated: 'idle',
  failed: 'error',
  none: 'pending',
};

// Friendly dir label → existing chip color (pull=green, del=red, push=blue).
const DIR_CHIP = { synced: 'pull', unsynced: 'del', keep: 'push' };

function evType(e) {
  return e.event_type || e.type;
}

export default function ExperimentSyncDetailsModal({
  open,
  onClose,
  title,
  sandbox,
  events = [],
  intervalSec = 5,
  onSyncNow,
  syncing = false,
  now = Date.now(),
}) {
  // Close on Escape while open.
  useEffect(() => {
    if (!open) return undefined;
    function onKey(e) {
      if (e.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open || !sandbox) return null;

  const status = sandbox.status || 'none';
  const statusKind = STATUS_KIND[status] || 'pending';

  // `events` arrive newest-first.
  const lastPull = events.find((e) => PULL_EVENTS.has(evType(e))) || null;
  const lastError = events.find((e) => evType(e) === 'sandbox.rsync_error') || null;
  const errorActive = Boolean(lastError && (!events[0] || events[0] === lastError));

  const syncDir = stripSlash(sandbox.sync_dir || sandbox.workdir || '/workspace/synced');
  const unsyncedDir = stripSlash(sandbox.unsynced_dir || sandbox.sandbox_data_dir || '/workspace/unsynced');
  const localSyncDir = stripSlash(sandbox.local_sync_dir || '');

  const lastPullLabel = lastPull
    ? `↓${num(lastPull.payload?.pulled)} · ${fmtAgo(now - Date.parse(lastPull.created_at))}`
    : 'no pull yet';

  const body = (
    <div className="vsdm-overlay" onMouseDown={onClose}>
      <div
        className="vsdm vsdm--min"
        role="dialog"
        aria-modal="true"
        aria-label="Experiment sync"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="vsdm-head">
          <div className="vsdm-head-main">
            <span className={`vsdm-pill vsdm-pill--${statusKind}`}>{status}</span>
            <h2 className="vsdm-title">Sync</h2>
          </div>
          <button type="button" className="vsdm-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        {title && (
          <p className="vsdm-sub" title={title}>
            {title}
          </p>
        )}

        <div className="vsdm-dirs">
          <DirRow label="synced" remote={syncDir} local={localSyncDir} />
          <DirRow label="unsynced" remote={unsyncedDir} />
          <DirRow label="keep" remote={`${syncDir}/artifacts_to_keep`} local={localSyncDir ? `${localSyncDir}/artifacts_to_keep` : ''} />
        </div>

        <div className="vsdm-status">
          <span>
            pull every {intervalSec}s
            <span className="vsdm-dot-sep">·</span>
            {lastPullLabel}
          </span>
          {onSyncNow && (
            <button
              type="button"
              className="btn btn--ghost btn--sm vsdm-status-btn"
              disabled={syncing || status !== 'running'}
              onClick={onSyncNow}
            >
              {syncing ? 'Syncing…' : 'Sync now'}
            </button>
          )}
        </div>

        {errorActive && (
          <div className="vsdm-err" title={lastError.payload?.error || 'sync error'}>
            <span className="vsdm-err-tag">error</span>
            <span className="vsdm-err-msg">{shortError(lastError.payload?.error)}</span>
          </div>
        )}

        <div className="vsdm-min-foot">
          <span className="mono">{sandbox.sandbox_id || '—'}</span>
        </div>
      </div>
    </div>
  );

  return createPortal(body, document.body);
}

// --- presentational --------------------------------------------------------

function DirRow({ label, remote, local }) {
  return (
    <div className="vsdm-dir">
      <span className={`vsdm-chip vsdm-chip--${DIR_CHIP[label]}`}>{label}</span>
      <div className="vsdm-dir-paths">
        <span className="vsdm-dir-remote" title={remote}>
          {remote}
        </span>
        {local ? (
          <span className="vsdm-dir-local" title={local}>
            {shortenPath(local)}
          </span>
        ) : null}
      </div>
    </div>
  );
}

// --- value helpers ---------------------------------------------------------

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function stripSlash(s) {
  return String(s || '').replace(/\/+$/, '');
}

// Show only the meaningful tail of a long absolute path (full path on hover).
function shortenPath(p, segs = 3) {
  const parts = stripSlash(p).split('/').filter(Boolean);
  if (parts.length <= segs) return p;
  return '…/' + parts.slice(-segs).join('/');
}

function shortError(raw) {
  const s = String(raw || 'sync error').replace(/\s+/g, ' ').trim();
  return s.length > 80 ? s.slice(0, 79) + '…' : s;
}

function fmtAgo(ms) {
  if (ms == null || !Number.isFinite(ms)) return '—';
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

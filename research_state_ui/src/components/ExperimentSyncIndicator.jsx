import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import {
  useProjectStore,
  selectSandboxes,
  selectEventsAll,
  selectExperiments,
} from '../store/useProjectStore';
import { parseIntent } from '../utils/intent';
import ExperimentSyncDetailsModal from './ExperimentSyncDetailsModal';

/**
 * ExperimentSyncIndicator — ambient, per-experiment sandbox sync status.
 *
 * Replaces the retired VolumeSyncIndicator (which tracked a single project-level
 * repo ↔ Modal Volume sync that no longer exists). Sync is now per-experiment
 * SSH rsync owned by SandboxService: push-once at provision, pull every ~5s
 * while the sandbox runs. This card lists each active sandbox and its last pull,
 * and opens a drill-in for the full picture.
 *
 * Pure-client derivation from data the store already polls — the sandbox list
 * and the project event window (filtered to `sandbox.*` rsync events). No new
 * polling; only the optional "Sync now" action hits the network.
 */

// Mirrors DEFAULT_AUTO_RSYNC_INTERVAL_SECONDS in services/sandboxes.py.
const PULL_INTERVAL_SEC = 5;
const ACTIVE_STATUSES = new Set(['running', 'provisioning']);
const PULL_EVENTS = new Set(['sandbox.rsynced', 'sandbox.synced']);
const SYNC_EVENTS = new Set([
  'sandbox.rsynced',
  'sandbox.synced',
  'sandbox.initial_rsynchronized',
  'sandbox.rsync_error',
]);

function evType(e) {
  return e.event_type || e.type;
}

export default function ExperimentSyncIndicator() {
  const sandboxes = useProjectStore(selectSandboxes);
  const events = useProjectStore(selectEventsAll);
  const experiments = useProjectStore(selectExperiments);
  const projectId = useProjectStore((s) => s.projectId);
  const refreshHome = useProjectStore((s) => s.refreshHome);

  const [now, setNow] = useState(Date.now());
  const [detailExp, setDetailExp] = useState(null);
  const [syncingId, setSyncingId] = useState(null);

  // 1Hz tick for "Ns ago" labels.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const titleFor = useMemo(() => {
    const map = {};
    for (const e of experiments) map[e.id] = parseIntent(e.intent).title || e.intent || e.id;
    return (eid) => map[eid] || eid;
  }, [experiments]);

  // Group this project's sync events by experiment (target_id), newest-first
  // (preserved from the store's id-DESC fetch).
  const eventsByExp = useMemo(() => {
    const byExp = {};
    for (const e of events) {
      if (!SYNC_EVENTS.has(evType(e))) continue;
      const eid = e.target_id;
      if (!eid) continue;
      (byExp[eid] = byExp[eid] || []).push(e);
    }
    return byExp;
  }, [events]);

  // Active sandboxes (running first, then provisioning), most-recent first within.
  const rows = useMemo(() => {
    const active = (sandboxes || []).filter((s) => ACTIVE_STATUSES.has(s.status));
    const rank = (st) => (st === 'running' ? 0 : st === 'provisioning' ? 1 : 2);
    return active
      .slice()
      .sort(
        (a, b) =>
          rank(a.status) - rank(b.status) ||
          String(b.updated_at || '').localeCompare(String(a.updated_at || '')),
      )
      .map((s) =>
        deriveRow(s, eventsByExp[s.experiment_id] || [], titleFor(s.experiment_id), now),
      );
  }, [sandboxes, eventsByExp, titleFor, now]);

  const runningCount = rows.filter((r) => r.status === 'running').length;

  const detailSandbox = detailExp
    ? (sandboxes || []).find((s) => s.experiment_id === detailExp) || null
    : null;

  async function onSyncNow(eid) {
    if (!projectId || !eid || syncingId) return;
    setSyncingId(eid);
    try {
      await api.syncSandbox(projectId, eid);
      await refreshHome();
    } catch {
      // The failure surfaces as the row's error state on the next poll.
    } finally {
      setSyncingId(null);
    }
  }

  return (
    <div className="vsync" aria-label="Experiment sync status">
      <div className="vsync-row vsync-row--head">
        <span className="vsync-title">sync</span>
        <span className="vsync-status">{rows.length === 0 ? 'idle' : `${runningCount} active`}</span>
      </div>

      {rows.length === 0 ? (
        <div className="vsync-row vsync-row--last vsync-row--faint">no active sandboxes</div>
      ) : (
        <div className="vsync-exp-list">
          {rows.map((r) => (
            <button
              key={r.experimentId}
              type="button"
              className="vsync-exp-row"
              onClick={() => setDetailExp(r.experimentId)}
              title={`${r.title} — view sync details`}
            >
              <span className={r.dotClass} aria-hidden="true" />
              <span className="vsync-exp-title">{r.title}</span>
              <span className="vsync-exp-meta">{r.metaLabel}</span>
            </button>
          ))}
        </div>
      )}

      <div className="vsync-row vsync-row--hint">push once · pull every {PULL_INTERVAL_SEC}s</div>

      <ExperimentSyncDetailsModal
        open={Boolean(detailExp && detailSandbox)}
        onClose={() => setDetailExp(null)}
        title={detailExp ? titleFor(detailExp) : ''}
        sandbox={detailSandbox}
        events={detailExp ? eventsByExp[detailExp] || [] : []}
        intervalSec={PULL_INTERVAL_SEC}
        onSyncNow={() => onSyncNow(detailExp)}
        syncing={syncingId === detailExp}
        now={now}
      />
    </div>
  );
}

// --- helpers ---------------------------------------------------------------

function deriveRow(sandbox, evs, title, now) {
  const status = sandbox.status;
  const last = evs[0] || null;
  const lastPull = evs.find((e) => PULL_EVENTS.has(evType(e))) || null;
  const lastPush = evs.find((e) => evType(e) === 'sandbox.initial_rsynchronized') || null;
  const lastError = evs.find((e) => evType(e) === 'sandbox.rsync_error') || null;
  const errorActive = Boolean(lastError && (!last || lastError === last));

  let dotClass = 'vsync-dot vsync-dot--idle';
  let metaLabel = 'awaiting first pull';

  if (status === 'provisioning') {
    dotClass = 'vsync-dot vsync-dot--pending';
    metaLabel = 'provisioning…';
  } else if (errorActive) {
    dotClass = 'vsync-dot vsync-dot--error';
    metaLabel = 'sync error';
  } else if (lastPull) {
    dotClass = 'vsync-dot vsync-dot--idle';
    metaLabel = `↓${num(lastPull.payload?.pulled)} · ${fmtAgo(now - Date.parse(lastPull.created_at))}`;
  } else if (lastPush) {
    dotClass = 'vsync-dot vsync-dot--idle';
    metaLabel = `↑${num(lastPush.payload?.pushed)} pushed`;
  }

  return { experimentId: sandbox.experiment_id, status, title, dotClass, metaLabel };
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function fmtAgo(ms) {
  if (ms == null || !Number.isFinite(ms)) return '—';
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

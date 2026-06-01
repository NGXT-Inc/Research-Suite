import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

/**
 * VolumeSyncIndicator — ambient status of the backend's repo ↔ Modal Volume sync.
 *
 * Distinct from the existing "UI ↔ backend" sync indicator that sits next to it.
 * This one reports on what the Modal sync engine is doing on the server side:
 * how often it runs, when it last finished, what it pushed/pulled, and whether
 * the last pass produced any conflicts or errors.
 *
 * Pure-client derivation: polls /api/activity, filters to `modal.sync.*` events
 * for the active project, and computes state from the most recent ones. No
 * backend changes required.
 *
 * Caveat: the engine doesn't emit a "started" event, so "syncing now" is
 * inferred from (now ≥ last_pass_ts + POLL_INTERVAL_SEC). For coalesced/skipped
 * callers and submit-time syncs the inference is loose — that's fine for an
 * ambient indicator.
 */

// Mirrors the SyncPoller default in research_plugin (sync/poller.py).
// Kept in sync manually; the UI doesn't currently fetch it from the backend.
const POLL_INTERVAL_SEC = 60;
const REFRESH_MS = 3000;
const ACTIVITY_LIMIT = 200;

export default function VolumeSyncIndicator({ projectId }) {
  const [events, setEvents] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [now, setNow] = useState(Date.now());
  const inFlightRef = useRef(false);

  // 1Hz tick for the "next in Xs" countdown and "Ns ago" labels.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // Poll the activity feed for modal.sync.* events.
  useEffect(() => {
    if (!projectId) return undefined;
    let cancelled = false;

    async function fetchOnce() {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      try {
        // source=mcp is where sync events land (they have no explicit source,
        // and the backend treats null-source events as mcp). Asking for mcp
        // events directly is cheaper than the all-sources view.
        const data = await api.listActivity(ACTIVITY_LIMIT, 'mcp');
        if (cancelled) return;
        const filtered = (data?.events || []).filter(
          (ev) =>
            typeof ev.event === 'string' &&
            ev.event.startsWith('modal.sync.') &&
            ev.project_id === projectId,
        );
        setEvents(filtered);
        setLoaded(true);
      } catch {
        if (!cancelled) setLoaded(true); // mark loaded so we don't sit on "loading…"
      } finally {
        inFlightRef.current = false;
      }
    }

    fetchOnce();
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') fetchOnce();
    }, REFRESH_MS);

    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [projectId]);

  if (!projectId) return null;

  // Locate the most recent event of each interesting type. Activity events
  // arrive oldest-first, so iterate in reverse.
  const lastPass = findLast(events, (e) => e.event === 'modal.sync.pass');
  const lastError = findLast(events, (e) => e.event === 'modal.sync.error');
  const lastVolumeReady = findLast(events, (e) => e.event === 'modal.sync.volume_ready');
  const lastSkipped = findLast(events, (e) => e.event === 'modal.sync.skipped_busy');
  const lastCoalesced = findLast(events, (e) => e.event === 'modal.sync.coalesced');
  // Emitted by the poller every tick that an active job is using the project
  // volume — sync is intentionally deferred until the job clears. During a
  // long training run this dominates the activity feed and can push every
  // `modal.sync.pass` event outside the polled window, leaving the bootstrap
  // "awaiting volume" branch as the only visible state. Treat it as a
  // distinct, calm "paused" state instead.
  const lastPausedByJob = findLast(events, (e) => e.event === 'modal.sync.skipped_active_jobs');

  const lastPassTs = lastPass ? Date.parse(lastPass.ts) : 0;
  const lastErrorTs = lastError ? Date.parse(lastError.ts) : 0;
  const lastPausedByJobTs = lastPausedByJob ? Date.parse(lastPausedByJob.ts) : 0;
  const isErrorState = lastErrorTs > lastPassTs;
  // True when the poller is currently being held back by an active job — i.e.
  // the most recent thing we heard about this project's sync is a skip-because-
  // active-job, more recent than any pass or error.
  const isPausedByJob =
    lastPausedByJobTs > lastPassTs && lastPausedByJobTs > lastErrorTs;

  const conflicts = lastPass ? Number(lastPass.conflicts || 0) : 0;

  const sinceLastMs = lastPass ? now - lastPassTs : null;
  // Countdown only renders when we have a baseline (a recorded last pass).
  const nextInSec =
    sinceLastMs != null
      ? Math.max(0, POLL_INTERVAL_SEC - Math.floor(sinceLastMs / 1000))
      : null;
  // Heuristic: if we're past the next-due tick and within ~3s of it, assume a
  // sync is firing right now. The poller's real cadence drifts slightly because
  // its sleep starts after each pass completes (so the next pass is 60s after
  // the previous one ended, not started). This is close enough for an LED.
  const probablyRunning = sinceLastMs != null && sinceLastMs >= POLL_INTERVAL_SEC * 1000;

  // Compose the headline state. Order matters — error overrides conflict
  // overrides "paused by job" overrides "syncing now" overrides "idle".
  // "paused by job" sits above the !lastPass branch because during a long
  // run we may have no pass in the polled window even though the volume is
  // healthy; the active-job skip event is the more reliable signal.
  let dotClass = 'vsync-dot vsync-dot--idle';
  let statusLabel = 'idle';
  if (isErrorState) {
    dotClass = 'vsync-dot vsync-dot--error';
    statusLabel = 'error';
  } else if (conflicts > 0) {
    dotClass = 'vsync-dot vsync-dot--conflict';
    statusLabel = `${conflicts} conflict${conflicts === 1 ? '' : 's'}`;
  } else if (isPausedByJob) {
    dotClass = 'vsync-dot vsync-dot--paused';
    statusLabel = 'paused · active job';
  } else if (!lastPass) {
    dotClass = 'vsync-dot vsync-dot--pending';
    statusLabel = lastVolumeReady ? 'first sync pending' : 'awaiting volume';
  } else if (probablyRunning) {
    dotClass = 'vsync-dot vsync-dot--active';
    statusLabel = 'syncing…';
  }

  // Hide entirely if no sync activity has ever been observed for this project
  // and we've polled at least once. Most likely: the backend is not the Modal
  // backend (Ray/fake), or this project's volume hasn't been registered yet.
  // Hiding prevents a confusing "awaiting volume" forever for non-Modal users.
  if (loaded && events.length === 0) {
    return null;
  }

  return (
    <div className="vsync" aria-label="Volume sync status">
      <div className="vsync-row vsync-row--head">
        <span className={dotClass} aria-hidden="true" />
        <span className="vsync-title">volume sync</span>
        <span className="vsync-status">{statusLabel}</span>
      </div>
      <div className="vsync-row vsync-row--sched">
        <span>every {POLL_INTERVAL_SEC}s</span>
        {nextInSec != null && (
          <>
            <span className="vsync-sep">·</span>
            <span>
              {probablyRunning ? 'due now' : `next in ${nextInSec}s`}
            </span>
          </>
        )}
      </div>
      {lastPass ? (
        <div className="vsync-row vsync-row--last">
          <span title={lastPass.ts}>{fmtSinceLast(sinceLastMs)}</span>
          <span className="vsync-sep">·</span>
          <span className="vsync-counts">{formatCounts(lastPass)}</span>
          {Number.isFinite(lastPass.duration_ms) && (
            <>
              <span className="vsync-sep">·</span>
              <span>{lastPass.duration_ms}ms</span>
            </>
          )}
        </div>
      ) : isPausedByJob ? (
        <div className="vsync-row vsync-row--last vsync-row--faint">
          deferred while a job uses the volume
        </div>
      ) : (
        <div className="vsync-row vsync-row--last vsync-row--faint">
          no sync recorded yet
        </div>
      )}
      {isErrorState && lastError && (
        <div
          className="vsync-row vsync-row--err"
          title={lastError.message || ''}
        >
          {(lastError.phase || 'sync error') + ': '}
          <span className="vsync-err-msg">
            {truncate(String(lastError.message || ''), 48)}
          </span>
        </div>
      )}
      {(lastSkipped || lastCoalesced) && !probablyRunning && !isErrorState && !isPausedByJob && (
        <div className="vsync-row vsync-row--hint" title={lastSkipped ? 'A sync request hit a full queue and was skipped' : 'A sync request was merged with a pending one'}>
          {lastSkipped && lastCoalesced
            ? mostRecent([lastSkipped, lastCoalesced])
            : lastSkipped
            ? hintFromEvent(lastSkipped)
            : hintFromEvent(lastCoalesced)}
        </div>
      )}
    </div>
  );
}

// --- helpers ---------------------------------------------------------------

function findLast(arr, pred) {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (pred(arr[i])) return arr[i];
  }
  return null;
}

function fmtSinceLast(ms) {
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

function formatCounts(pass) {
  const pushed = Number(pass.pushed || 0);
  const pulled = Number(pass.pulled || 0);
  const delR = Number(pass.deleted_remote || 0);
  const delL = Number(pass.deleted_local || 0);
  const parts = [];
  parts.push(`↑${pushed}`);
  parts.push(`↓${pulled}`);
  if (delR > 0) parts.push(`−${delR}r`);
  if (delL > 0) parts.push(`−${delL}l`);
  return parts.join(' ');
}

function truncate(s, n) {
  if (!s) return '';
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

function hintFromEvent(ev) {
  if (!ev) return '';
  if (ev.event === 'modal.sync.skipped_busy') return 'last attempt skipped (busy)';
  if (ev.event === 'modal.sync.coalesced') return 'last attempt coalesced';
  return '';
}

function mostRecent(evs) {
  let best = null;
  for (const ev of evs) {
    if (!ev) continue;
    if (!best || Date.parse(ev.ts) > Date.parse(best.ts)) best = ev;
  }
  return hintFromEvent(best);
}

import { useEffect, useRef } from 'react';
import { api } from '../api';
import { useProjectStore } from './useProjectStore';

/**
 * SSE client for GET /api/projects/:id/events/stream.
 *
 * One EventSource per active project, owned by the app shell. Server `state`
 * signals coalesce into a single refreshHome() (which is conditional, so a
 * no-op change costs a 304). `append` rows fan out to subscribers so detail
 * pages can refetch only when their record moved.
 *
 * Fallback contract: streamHealthy in the store gates the polling loops —
 * while the stream is down (backend restart, hosted-control 401, no
 * EventSource) EventSource retries per the server's `retry:` hint and the
 * existing pollers carry the load unchanged.
 */

// Module-level pub/sub: subscribers receive each `append` event row.
const listeners = new Set();
export function subscribeProjectEvents(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function useEventStream() {
  const projectId = useProjectStore(s => s.projectId);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const setStreamHealthy = useProjectStore(s => s.setStreamHealthy);

  useEffect(() => {
    if (!projectId || typeof EventSource === 'undefined') return undefined;
    let es = null;
    let refreshTimer = null;
    let lastBeat = Date.now();

    // Coalesce a burst of events into one refresh.
    const scheduleRefresh = () => {
      if (refreshTimer) return;
      refreshTimer = setTimeout(() => { refreshTimer = null; refreshHome(); }, 250);
    };

    const connect = () => {
      es = new EventSource(api.eventStreamUrl(projectId));
      const beat = () => { lastBeat = Date.now(); };
      // On (re)connect, resync once — anything missed while disconnected.
      es.onopen = () => { beat(); setStreamHealthy(true); refreshHome(); };
      es.onerror = () => { setStreamHealthy(false); };
      es.addEventListener('ping', beat);
      es.addEventListener('hello', beat);
      es.addEventListener('state', () => { beat(); scheduleRefresh(); });
      es.addEventListener('append', (e) => {
        beat();
        let row = null;
        try { row = JSON.parse(e.data); } catch { /* skip malformed frame */ }
        if (row) listeners.forEach(fn => { try { fn(row); } catch { /* subscriber's problem */ } });
      });
    };
    connect();

    // Liveness watchdog. onerror alone is not enough: a dev/reverse proxy can
    // hold the browser side half-open after the upstream dies, so a stream
    // that outlives 3 missed server heartbeats (15s cadence) is declared dead,
    // polling takes back over, and we reconnect from scratch (also the only
    // retry path once EventSource goes CLOSED on a non-200, e.g. hosted 401).
    const STALL_MS = 45000;
    const watchdog = setInterval(() => {
      if (Date.now() - lastBeat <= STALL_MS) return;
      setStreamHealthy(false);
      es.close();
      lastBeat = Date.now();
      connect();
    }, 15000);

    return () => {
      clearInterval(watchdog);
      if (refreshTimer) clearTimeout(refreshTimer);
      setStreamHealthy(false);
      es.close();
    };
  }, [projectId, refreshHome, setStreamHealthy]);
}

/**
 * Poll `fetchFn` every `fastMs` while the stream is down (today's behavior);
 * while the stream is healthy, refetch on matching event rows instead, with
 * a `slowMs` safety poll for changes that never record an event.
 *
 * `enabled: false` fetches once and stops (the terminal-experiment case);
 * `refetchKey` forces an immediate refetch when it changes (status/attempt
 * bumps). `fetchFn` must be memoized by the caller; `matches` may be inline.
 */
export function useStreamAwarePoll(
  fetchFn,
  { fastMs = 3000, slowMs = 30000, matches = null, enabled = true, refetchKey = null } = {},
) {
  const streamHealthy = useProjectStore(s => s.streamHealthy);
  const matchesRef = useRef(matches);
  matchesRef.current = matches;

  useEffect(() => {
    let cancelled = false;
    let debounce = null;
    fetchFn();
    if (!enabled) return () => { cancelled = true; };
    const t = setInterval(() => { if (!cancelled) fetchFn(); }, streamHealthy ? slowMs : fastMs);
    const unsub = streamHealthy
      ? subscribeProjectEvents((row) => {
          if (cancelled || (matchesRef.current && !matchesRef.current(row))) return;
          if (debounce) return;
          debounce = setTimeout(() => { debounce = null; if (!cancelled) fetchFn(); }, 250);
        })
      : null;
    const onVis = () => { if (document.visibilityState === 'visible' && !cancelled) fetchFn(); };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      cancelled = true;
      clearInterval(t);
      if (debounce) clearTimeout(debounce);
      if (unsub) unsub();
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [fetchFn, streamHealthy, fastMs, slowMs, enabled, refetchKey]);
}

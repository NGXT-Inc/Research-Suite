import { useEffect, useRef } from 'react';
import { useProjectStore } from './useProjectStore';

/**
 * Poll GET /home every `intervalMs` while:
 *   - a projectId is set
 *   - `enabled` (the SSE stream is not covering us)
 *   - document.visibilityState === 'visible'
 *
 * Pauses on tab-hide, resumes on tab-show (with an immediate refresh so the
 * user never sees stale state right after returning to the tab). Flipping
 * `enabled` back on also refreshes immediately — it means the stream just
 * dropped, so the poller must catch whatever the stream would have pushed.
 */
export function usePolling(intervalMs = 3000, { enabled = true } = {}) {
  const projectId = useProjectStore(s => s.projectId);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const setPolling = useProjectStore(s => s.setPolling);
  const intervalRef = useRef(null);

  useEffect(() => {
    if (!projectId || !enabled) {
      setPolling(false);
      return undefined;
    }

    let cancelled = false;

    const start = () => {
      if (intervalRef.current) return;
      setPolling(true);
      intervalRef.current = setInterval(() => {
        if (!cancelled) refreshHome();
      }, intervalMs);
    };
    const stop = () => {
      if (!intervalRef.current) return;
      clearInterval(intervalRef.current);
      intervalRef.current = null;
      setPolling(false);
    };

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        refreshHome();
        start();
      } else {
        stop();
      }
    };

    // Kick once immediately + start polling if visible.
    refreshHome();
    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', onVisibility);
      stop();
    };
  }, [projectId, intervalMs, enabled, refreshHome, setPolling]);
}

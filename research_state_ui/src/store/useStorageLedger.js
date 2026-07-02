import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';

const POLL_MS = 60000;

/**
 * Self-contained loader for the long-term storage ledger.
 *
 * Deliberately NOT part of the project store: storage is an architecturally
 * separate feature, so its page owns its own fetch and degrades gracefully when
 * the backend storage API isn't present yet (a 404 → `unsupported`, not an error
 * banner). Expired objects are always fetched — the page renders them as ghosts
 * instead of hiding them behind a filter. Re-polls quietly once a minute while
 * the tab is visible; there is no refresh chrome.
 */
export function useStorageLedger(projectId) {
  const [objects, setObjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [unsupported, setUnsupported] = useState(false);

  const reload = useCallback(async () => {
    if (!projectId) return;
    setError(null);
    try {
      const data = await api.listStorage(projectId, { includeExpired: true });
      setObjects(data?.objects || []);
      setUnsupported(false);
    } catch (err) {
      if (err.status === 404) { setUnsupported(true); setObjects([]); }
      else setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    reload();
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') reload();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [reload]);

  return { objects, loading, error, unsupported, reload };
}

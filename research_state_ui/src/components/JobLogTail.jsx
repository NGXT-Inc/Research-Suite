import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

const ACTIVE_STATUSES = new Set(['queued', 'running', 'submitting']);

/**
 * Live log tail for one job.
 *
 * Polls /jobs/{id}/logs?tail=200 every 1.5s while status is non-terminal.
 * Stops polling on terminal statuses and does one final fetch to grab
 * the post-mortem log line. Auto-scrolls to bottom on each update unless
 * the user has scrolled away.
 */
export default function JobLogTail({ projectId, jobId, status, tail = 200 }) {
  const [logs, setLogs] = useState(null);
  const [error, setError] = useState(null);
  const boxRef = useRef(null);
  const stickyRef = useRef(true); // user sticks to bottom by default

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    async function fetchOnce() {
      try {
        const data = await api.getJobLogs(projectId, jobId, tail);
        if (cancelled) return;
        setLogs(data.logs || '');
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err.message);
      }
    }

    fetchOnce();
    if (ACTIVE_STATUSES.has(status)) {
      timer = setInterval(fetchOnce, 1500);
    }
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [projectId, jobId, status, tail]);

  // Auto-scroll when logs grow, but only if user is near the bottom.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    if (stickyRef.current) el.scrollTop = el.scrollHeight;
  }, [logs]);

  function onScroll() {
    const el = boxRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickyRef.current = dist < 24;
  }

  const live = ACTIVE_STATUSES.has(status);
  const lineCount = logs ? logs.split('\n').length : 0;

  return (
    <div>
      <div className="log-tail-head">
        <span>
          {live && <span className="log-tail-live-dot" />}
          logs · tail {tail} · {lineCount} lines
        </span>
        {error && <span style={{ color: 'var(--refutes)' }}>{error}</span>}
      </div>
      {logs == null ? (
        <div className="log-tail-empty">Loading logs…</div>
      ) : logs.trim() === '' ? (
        <div className="log-tail-empty">No log output yet.</div>
      ) : (
        <pre ref={boxRef} className="log-tail" onScroll={onScroll}>{logs}</pre>
      )}
    </div>
  );
}

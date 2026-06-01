import { useState } from 'react';
import { api } from '../api';
import ObjId from './ObjId';
import StatusPill from './StatusPill';
import JobLogTail from './JobLogTail';
import SubmitPipelineStrip from './SubmitPipelineStrip';

const ACTIVE_STATUSES = new Set(['queued', 'running', 'submitting']);
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);

/**
 * One job in a list. Expandable to show live log tail + expected outputs.
 *
 * Props:
 *   projectId
 *   job              hydrated job row from /jobs (see jobs.py:_hydrate_job)
 *   onChanged        called after a cancel succeeds (parent re-fetches list)
 *   defaultOpen      auto-expand on mount (used for the most recent job)
 */
export default function JobCard({ projectId, job, onChanged, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState(null);

  async function onCancel(e) {
    e.stopPropagation();
    if (!confirm(`Cancel job ${job.id}?`)) return;
    setCancelling(true);
    setError(null);
    try {
      await api.cancelJob(projectId, job.id);
      if (onChanged) await onChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setCancelling(false);
    }
  }

  const isActive = ACTIVE_STATUSES.has(job.status);
  const isTerminal = TERMINAL_STATUSES.has(job.status);
  const outputs = job.outputs || job.expected_outputs?.map(p => ({ path: p, exists: false })) || [];

  return (
    <div className="job-card">
      <div className="job-card-head" onClick={() => setOpen(o => !o)} style={{ cursor: 'pointer' }}>
        <div className="cluster">
          <ObjId id={job.id} accent />
          <StatusPill value={job.nested_status || job.status} />
          <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>attempt {job.attempt_index}</span>
          {job.progress_message && isActive && (
            <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
              {job.progress_message}
            </span>
          )}
        </div>
        <div className="cluster">
          {isActive && (
            <button
              type="button"
              className="btn btn--sm btn--danger"
              onClick={onCancel}
              disabled={cancelling}
            >
              {cancelling ? '…' : 'Cancel'}
            </button>
          )}
          <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>{open ? '−' : '+'}</span>
        </div>
      </div>

      <div className="job-card-meta">
        <span>
          <span className="job-card-meta-key">cmd</span>
          <span className="mono">{job.command}</span>
        </span>
        <span>
          <span className="job-card-meta-key">cwd</span>
          <span className="mono">{job.cwd || '.'}</span>
        </span>
        {job.submitted_at && (
          <span>
            <span className="job-card-meta-key">submitted</span>
            <span className="mono">{shortTime(job.submitted_at)}</span>
          </span>
        )}
        {job.started_at && (
          <span>
            <span className="job-card-meta-key">started</span>
            <span className="mono">{shortTime(job.started_at)}</span>
          </span>
        )}
        {job.finished_at && (
          <span>
            <span className="job-card-meta-key">finished</span>
            <span className="mono">{shortTime(job.finished_at)}</span>
          </span>
        )}
        {job.gpu && (
          <span>
            <span className="job-card-meta-key">gpu</span>
            <span className="mono">{job.gpu}</span>
          </span>
        )}
        {job.sandbox_id && (
          <span>
            <span className="job-card-meta-key">sandbox</span>
            <span className="mono">{job.sandbox_id.slice(0, 18)}</span>
          </span>
        )}
        {job.ssh_address && (
          <span
            className="job-card-ssh"
            onClick={(e) => {
              e.stopPropagation();
              if (navigator.clipboard) navigator.clipboard.writeText(job.ssh_address).catch(() => {});
            }}
            title="Click to copy SSH address"
          >
            <span className="job-card-meta-key">ssh</span>
            <span className="mono">{job.ssh_address}</span>
          </span>
        )}
        {job.ray_job_id && !job.sandbox_id && (
          <span>
            <span className="job-card-meta-key">ray</span>
            <span className="mono">{job.ray_job_id.slice(0, 12)}</span>
          </span>
        )}
      </div>

      {job.error && (
        <div className="error-message" style={{ marginBottom: 8 }}>
          {job.error}
        </div>
      )}
      {error && <div className="error-message" style={{ marginBottom: 8 }}>{error}</div>}

      {open && (
        <>
          {job.status === 'submitting' && (
            <SubmitPipelineStrip nested={job.nested_status} />
          )}
          <JobLogTail projectId={projectId} jobId={job.id} status={job.status} tail={200} />
          {outputs.length > 0 && (
            <div className="job-outputs">
              <div className="section-title" style={{ marginTop: 12, marginBottom: 6 }}>Expected outputs</div>
              {outputs.map((o, i) => {
                // Until the job reaches a terminal status, "absent" = "not produced yet"
                // and should read as muted "pending", not alarming red "missing".
                let cls = 'job-output-status--pending';
                let label = 'pending';
                if (o.exists) {
                  cls = 'job-output-status--exists';
                  label = 'exists';
                } else if (isTerminal) {
                  cls = 'job-output-status--missing';
                  label = 'missing';
                }
                return (
                  <div key={i} className="job-output-row">
                    <span className="job-output-path">{o.path}</span>
                    <span className={cls}>{label}</span>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function shortTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return iso; }
}

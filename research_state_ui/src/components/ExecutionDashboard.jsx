import { useEffect, useState } from 'react';
import { api } from '../api';
import ObjId from './ObjId';
import StatusPill from './StatusPill';
import JobLogTail from './JobLogTail';
import SubmitJobForm from './SubmitJobForm';
import SubmitPipelineStrip from './SubmitPipelineStrip';

const ACTIVE_STATUSES = new Set(['queued', 'running', 'submitting']);
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);

function bytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function fmtDuration(ms) {
  if (ms == null || ms < 0) return '—';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

function fmtTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

function runtimeName(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const normalized = raw.toLowerCase();
  if (normalized === 'modal') return 'Modal';
  if (normalized === 'ray') return 'Ray';
  if (normalized === 'local_subprocess') return 'Local subprocess';
  return raw.replace(/_/g, ' ').replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function shortId(value) {
  if (!value) return '';
  const id = String(value);
  return `${id.slice(0, 18)}${id.length > 18 ? '…' : ''}`;
}

/**
 * Does the nested_status already carry the progress phase the spotlight bar
 * would otherwise repeat next to the pill? If yes, suppress the trailing
 * `progress_message` text so we don't say the same thing twice.
 */
function pillCarriesPhase(job) {
  const nested = String(job?.nested_status || '');
  const top = String(job?.status || '');
  return nested && nested !== top && nested.startsWith(`${top}.`);
}

function describeRuntime(job) {
  const backend = String(job.backend || (job.ray_job_id ? 'ray' : '')).toLowerCase();
  const hints = job.backend_hints || {};
  const env = job.runtime_env || {};
  const parts = [];

  const name = runtimeName(backend);
  if (name) parts.push(name);

  if (backend === 'modal') {
    parts.push(job.gpu || hints.gpu || 'H100');
    if (hints.compute_tier) parts.push(String(hints.compute_tier).replace(/_/g, ' '));
    // Prefer sandbox_id (human-meaningful, points at a real Modal resource)
    // over runtime_job_id (opaque base64 envelope) when present.
    const handle = job.sandbox_id || job.runtime_job_id;
    if (handle) parts.push(shortId(handle));
    return parts.join(' · ');
  }

  if (job.ray_job_id || job.runtime_job_id) {
    parts.push(shortId(job.ray_job_id || job.runtime_job_id));
  }
  if (env.num_cpus) parts.push(`${env.num_cpus} CPU`);
  if (env.num_gpus) parts.push(`${env.num_gpus} GPU`);
  if (env.working_dir && !/^https?:\/\//.test(String(env.working_dir))) {
    const wd = String(env.working_dir);
    parts.push(wd.length > 28 ? `…${wd.slice(-28)}` : wd);
  }
  if (env.pip || env.conda) parts.push('custom env');
  return parts.length ? parts.join(' · ') : 'Modal · H100';
}

/**
 * ExecutionDashboard — the run.
 *
 * The latest job is the primary one and gets a stats dashboard + I/O + live
 * log. Previous runs collapse behind a single line.
 */
export default function ExecutionDashboard({
  projectId,
  experimentId,
  experimentStatus,
  jobs,
  execResources,
  onRefresh,
}) {
  const sortedJobs = [...(jobs || [])].sort((a, b) =>
    (b.created_at || '').localeCompare(a.created_at || ''),
  );
  const primaryJob = sortedJobs[0] || null;
  const previousJobs = sortedJobs.slice(1);

  const [showSubmit, setShowSubmit] = useState(false);
  const [showPrev, setShowPrev] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState(null);

  async function cancel() {
    if (!primaryJob) return;
    if (!confirm(`Cancel job ${primaryJob.id}?`)) return;
    setCancelling(true);
    setError(null);
    try {
      await api.cancelJob(projectId, primaryJob.id);
      if (onRefresh) await onRefresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setCancelling(false);
    }
  }

  return (
    <section id="execution" className="spotlight exec-dash">
      <header className="spotlight-head">
        <div className="spotlight-eyebrow">Execution</div>
        <div className="spotlight-bar">
          {primaryJob ? (
            <>
              <ObjId id={primaryJob.id} accent />
              <span className="spotlight-bar-sep">·</span>
              <StatusPill value={primaryJob.nested_status || primaryJob.status} />
              {primaryJob.progress_message
                && ACTIVE_STATUSES.has(primaryJob.status)
                && !pillCarriesPhase(primaryJob) && (
                <>
                  <span className="spotlight-bar-sep">·</span>
                  <span className="faint">{primaryJob.progress_message}</span>
                </>
              )}
              <span style={{ marginLeft: 'auto' }}>
                {ACTIVE_STATUSES.has(primaryJob.status) && (
                  <button
                    type="button"
                    className="btn btn--sm btn--danger"
                    onClick={cancel}
                    disabled={cancelling}
                  >
                    {cancelling ? '…' : 'Cancel'}
                  </button>
                )}
                {['ready_to_run', 'running'].includes(experimentStatus) && !ACTIVE_STATUSES.has(primaryJob.status) && (
                  <button
                    type="button"
                    className="btn btn--sm btn--primary"
                    onClick={() => setShowSubmit(v => !v)}
                  >
                    {showSubmit ? 'Cancel' : '+ Submit another'}
                  </button>
                )}
              </span>
            </>
          ) : (
            <>
              <span className="faint">No job submitted for this attempt yet.</span>
              {['ready_to_run', 'running'].includes(experimentStatus) && (
                <span style={{ marginLeft: 'auto' }}>
                  <button
                    type="button"
                    className="btn btn--sm btn--primary"
                    onClick={() => setShowSubmit(v => !v)}
                  >
                    {showSubmit ? 'Cancel' : '+ Submit job'}
                  </button>
                </span>
              )}
            </>
          )}
        </div>
      </header>

      {showSubmit && (
        <div style={{ marginTop: 12 }}>
          <SubmitJobForm
            projectId={projectId}
            experimentId={experimentId}
            onCancel={() => setShowSubmit(false)}
            onSubmitted={async () => { setShowSubmit(false); if (onRefresh) await onRefresh(); }}
          />
        </div>
      )}

      {primaryJob && primaryJob.status === 'submitting' && (
        <SubmitPipelineStrip nested={primaryJob.nested_status} />
      )}

      {primaryJob && (
        <>
          <RunStats job={primaryJob} />

          <ExecutionIO
            execResources={execResources}
            outputs={primaryJob.outputs || (primaryJob.expected_outputs || []).map(p => ({ path: p, exists: false }))}
            jobStatus={primaryJob.status}
          />

          {primaryJob.error && (
            <div className="error-message" style={{ marginTop: 10 }}>
              <span className="faint" style={{ marginRight: 6, fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em' }}>job error</span>
              {primaryJob.error}
            </div>
          )}

          <div className="exec-dash-log">
            <JobLogTail
              projectId={projectId}
              jobId={primaryJob.id}
              status={primaryJob.status}
              tail={200}
            />
          </div>
        </>
      )}

      {error && <div className="error-message">{error}</div>}

      {previousJobs.length > 0 && (
        <div className="exec-dash-prev">
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => setShowPrev(v => !v)}
          >
            {showPrev ? `Hide previous runs (${previousJobs.length})` : `Previous runs (${previousJobs.length})`}
          </button>
          {showPrev && (
            <div className="stack stack--sm" style={{ marginTop: 8 }}>
              {previousJobs.map(j => <PreviousRunRow key={j.id} job={j} />)}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function RunStats({ job }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ACTIVE_STATUSES.has(job.status)) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [job.status]);

  const startedTs = job.started_at ? new Date(job.started_at).getTime() : null;
  const finishedTs = job.finished_at ? new Date(job.finished_at).getTime() : null;
  let elapsedMs = null;
  if (startedTs) {
    elapsedMs = (finishedTs || now) - startedTs;
  }

  return (
    <div className="run-stats">
      <div className="run-stat">
        <div className="run-stat-key">Elapsed</div>
        <div className="run-stat-value run-stat-value--lg tabular">{fmtDuration(elapsedMs)}</div>
      </div>
      <div className="run-stat">
        <div className="run-stat-key">Started</div>
        <div className="run-stat-value">
          {fmtTime(job.started_at) || <span className="faint">not yet</span>}
          {finishedTs ? <> <span className="faint">→</span> {fmtTime(job.finished_at)}</>
            : startedTs ? <> <span className="faint">→</span> <span className="faint">running</span></> : null}
        </div>
      </div>
      <div className="run-stat">
        <div className="run-stat-key">Hardware / runtime</div>
        <div className="run-stat-value mono">{describeRuntime(job)}</div>
        {job.ssh_address && <SshLine address={job.ssh_address} />}
      </div>
      <div className="run-stat run-stat--wide">
        <div className="run-stat-key">Command</div>
        <div className="run-stat-value mono run-stat-command">{job.command}</div>
      </div>
    </div>
  );
}

function ExecutionIO({ execResources, outputs, jobStatus }) {
  return (
    <div className="exec-io">
      <div className="exec-io-col">
        <div className="exec-io-col-head">
          Inputs
          <span className="tab-count">{execResources.length}</span>
        </div>
        {execResources.length === 0 ? (
          <div className="empty" style={{ fontSize: 'var(--text-sm)' }}>None registered.</div>
        ) : (
          <ul className="exec-io-list">
            {execResources.map(r => {
              const pinned = r.association_version_id;
              const liveAdvanced = pinned && r.current_version_id && pinned !== r.current_version_id;
              return (
                <li key={`${r.id}:${r.association_role || ''}`}>
                  <span className="mono exec-io-path">{r.path}</span>
                  <span className="exec-io-meta">
                    {bytes(r.size_bytes)}
                    {r.association_role && <> · {r.association_role}</>}
                    {liveAdvanced && (
                      <span className="plan-version-tag" style={{ marginLeft: 6 }} title="The live file has advanced past the version pinned to this attempt">
                        live file has advanced
                      </span>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="exec-io-col">
        <div className="exec-io-col-head">
          Outputs
          <span className="tab-count">{outputs.length}</span>
        </div>
        {outputs.length === 0 ? (
          <div className="empty" style={{ fontSize: 'var(--text-sm)' }}>None declared.</div>
        ) : (
          <ul className="exec-io-list">
            {outputs.map((o, i) => {
              let label = 'pending';
              let cls = 'pending';
              if (o.exists) { label = 'exists'; cls = 'exists'; }
              else if (TERMINAL_STATUSES.has(jobStatus)) { label = 'missing'; cls = 'missing'; }
              return (
                <li key={i}>
                  <span className="mono exec-io-path">{o.path}</span>
                  <span className={`io-pill io-pill--${cls}`}>{label}</span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

/**
 * Faint click-to-copy line for an SSH address. Rendered inside the runtime
 * tile only when the backend produced an address — keeps the tile single-row
 * for everyone else.
 */
function SshLine({ address }) {
  const [copied, setCopied] = useState(false);
  async function copy(e) {
    e.preventDefault();
    try {
      await navigator.clipboard.writeText(address);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* noop — clipboard unavailable, the visible text is the fallback */
    }
  }
  return (
    <button
      type="button"
      className="ssh-line mono"
      onClick={copy}
      title="Copy SSH address"
    >
      <span className="ssh-line-key">ssh</span>
      <span className="ssh-line-addr">{address}</span>
      <span className="ssh-line-copy">{copied ? 'copied' : 'copy'}</span>
    </button>
  );
}

function PreviousRunRow({ job }) {
  return (
    <div className="prev-run">
      <ObjId id={job.id} />
      <StatusPill value={job.status} />
      <span className="faint prev-run-time">{fmtTime(job.created_at) || ''}</span>
      <span className="mono prev-run-cmd" title={job.command}>{job.command}</span>
    </div>
  );
}

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import ObjId from './ObjId';
import { parseIntent } from '../utils/intent';

/**
 * ProjectPulse — the project-wide "what's going on now" panel.
 *
 * Three bands, each a compact live list:
 *   1. Experiments  — non-terminal experiments, grouped from the top of the
 *                     attention queue (running first, then awaiting review).
 *   2. Processes    — Ray jobs currently submitting / queued / running.
 *   3. Reviews      — open review requests awaiting a reviewer or response.
 *
 * Each row:
 *   - status dot (tinted; subtle pulse on truly live items)
 *   - title / id / linked deep-link target
 *   - meta line with secondary context
 *   - live-ticking elapsed time pinned to the right
 *
 * Bands with zero items hide themselves. If everything is empty, the panel
 * renders a single calm "idle" line — the project really has nothing in
 * flight, which is itself useful information.
 */

const TICK_MS = 1000;
const OPEN_REVIEW_STATUSES = new Set(['requested', 'started']);

function parseTs(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

// Granularity scales with magnitude so the rightmost number visibly ticks for
// fresh items but stays calm for long-running ones.
function fmtRel(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 10) return `${m}m ${s % 60}s`;
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

function prettyGate(g) {
  if (!g) return '';
  return String(g).replace(/_/g, ' ');
}

function prettyRole(role) {
  if (!role) return 'review';
  if (role === 'design_reviewer') return 'design review';
  if (role === 'experiment_reviewer') return 'experiment review';
  return String(role).replace(/_/g, ' ');
}

export default function ProjectPulse({ activeExperiments = [], activeProcesses = [], reviewRequests = [] }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);

  const pendingReviews = reviewRequests.filter((r) => OPEN_REVIEW_STATUSES.has(r?.status));
  const allEmpty =
    activeExperiments.length === 0 && activeProcesses.length === 0 && pendingReviews.length === 0;

  if (allEmpty) {
    return (
      <div className="pulse pulse--idle">
        <div className="pulse-idle-dot" aria-hidden="true" />
        <div className="pulse-idle-text">
          Nothing in flight. The project is idle.
        </div>
      </div>
    );
  }

  return (
    <div className="pulse">
      <Band kind="experiments" title="Experiments" count={activeExperiments.length}>
        {activeExperiments.map((exp) => (
          <ExperimentRow key={exp.id} exp={exp} now={now} />
        ))}
      </Band>
      <Band kind="processes" title="Processes" count={activeProcesses.length}>
        {activeProcesses.map((proc) => (
          <ProcessRow key={proc.id} proc={proc} now={now} />
        ))}
      </Band>
      <Band kind="reviews" title="Reviews" count={pendingReviews.length}>
        {pendingReviews.map((req) => (
          <ReviewRow key={req.id} req={req} now={now} />
        ))}
      </Band>
    </div>
  );
}

function Band({ kind, title, count, children }) {
  if (count === 0) return null;
  return (
    <div className={`pulse-band pulse-band--${kind}`}>
      <div className="pulse-band-head">
        <span className="pulse-band-title">{title}</span>
        <span className="pulse-band-count tabular">{count}</span>
      </div>
      <div className="pulse-band-body">{children}</div>
    </div>
  );
}

function ExperimentRow({ exp, now }) {
  const { title } = parseIntent(exp.intent);
  const status = String(exp.status || '').toLowerCase();
  const updatedMs = parseTs(exp.updated_at) || parseTs(exp.created_at);
  const elapsed = updatedMs ? now - updatedMs : null;
  const gateLabel = prettyGate(exp.workflow?.current_gate || status);
  // Pulse the dot only when the experiment is actively in motion. Awaiting
  // review reads as "waiting on a human" — calm, not blinking.
  const isLive = status === 'running';

  return (
    <Link to={`/experiments/${exp.id}`} className="pulse-row">
      <span
        className={`pulse-row-dot pulse-row-dot--${status}${isLive ? ' pulse-row-dot--pulse' : ''}`}
        aria-hidden="true"
      />
      <div className="pulse-row-main">
        <div className="pulse-row-title">{title || exp.id}</div>
        <div className="pulse-row-meta">
          <ObjId id={exp.id} />
          <span className="pulse-row-sep">·</span>
          <span>{gateLabel}</span>
          {exp.attempt_index > 1 && (
            <>
              <span className="pulse-row-sep">·</span>
              <span>attempt {exp.attempt_index}</span>
            </>
          )}
        </div>
      </div>
      {elapsed != null && (
        <div className="pulse-row-elapsed tabular" title="time since last update">
          {fmtRel(elapsed)}
        </div>
      )}
    </Link>
  );
}

function ProcessRow({ proc, now }) {
  const status = String(proc.status || '').toLowerCase();
  const startedMs = parseTs(proc.started_at) || parseTs(proc.submitted_at);
  const elapsed = startedMs ? now - startedMs : null;
  const isLive = status === 'running';
  const cmd = String(proc.command || '').trim();
  // Job rows are useful when you can tell *which* experiment they belong to
  // and roughly what they're doing — both compact.
  return (
    <Link to={`/jobs#${proc.id}`} className="pulse-row">
      <span
        className={`pulse-row-dot pulse-row-dot--${status}${isLive ? ' pulse-row-dot--pulse' : ''}`}
        aria-hidden="true"
      />
      <div className="pulse-row-main">
        <div className="pulse-row-title pulse-row-title--mono">
          <ObjId id={proc.id} />
          {proc.experiment_id && (
            <>
              <span className="pulse-row-arrow">→</span>
              <ObjId id={proc.experiment_id} />
            </>
          )}
        </div>
        {cmd && (
          <div className="pulse-row-meta">
            <span className="mono pulse-row-cmd">{cmd}</span>
          </div>
        )}
      </div>
      {elapsed != null && (
        <div className="pulse-row-elapsed tabular" title="time since started/submitted">
          {fmtRel(elapsed)}
        </div>
      )}
    </Link>
  );
}

function ReviewRow({ req, now }) {
  const createdMs = parseTs(req.created_at);
  const age = createdMs ? now - createdMs : null;
  const status = String(req.status || '').toLowerCase();
  const role = prettyRole(req.role);
  const targetType = req.target_type || 'target';
  // Reviews link to the target — for an experiment review, that's the
  // experiment detail page where the gate banner + reviewer affordances live.
  const to =
    req.target_type === 'experiment' && req.target_id
      ? `/experiments/${req.target_id}#design`
      : '/reviews';

  return (
    <Link to={to} className="pulse-row">
      <span
        className={`pulse-row-dot pulse-row-dot--review pulse-row-dot--${status}`}
        aria-hidden="true"
      />
      <div className="pulse-row-main">
        <div className="pulse-row-title">
          {role} of {targetType} <ObjId id={req.target_id} />
        </div>
        <div className="pulse-row-meta">
          <span>{status === 'requested' ? 'awaiting reviewer' : status}</span>
          {req.reason && (
            <>
              <span className="pulse-row-sep">·</span>
              <span className="pulse-row-reason">{req.reason}</span>
            </>
          )}
        </div>
      </div>
      {age != null && (
        <div className="pulse-row-elapsed tabular" title="time since request">
          {fmtRel(age)}
        </div>
      )}
    </Link>
  );
}

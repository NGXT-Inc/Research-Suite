import { Link } from 'react-router-dom';
import StatusPill from './StatusPill';

/**
 * GateBanner — surfaces the server's `workflow` block.
 *
 * The UI never computes the state machine — it renders what the server says
 * is the current_gate + next_action and offers transition buttons derived
 * from the same authority (see deriveActionButtons in ExperimentDetail).
 *
 * Props:
 *   workflow:           { current_gate, next_action, allowed_actions, blocked_actions, missing_evidence, revision_context }
 *   experimentStatus:   to flag terminal/failed banner variants
 *   primaryAction:      { transition, label } | null  (the main button — fires onAction(transition))
 *   secondaryActions:   Array<{transition, label}>    (subtle "abandon" / "mark failed" buttons)
 *   onAction:           (transition) => void
 *   actionsBusy:        Set<string>  (transition currently in flight → disable + show "…")
 */
export default function GateBanner({
  workflow,
  experimentStatus,
  closedStatus = null,
  primaryAction = null,
  secondaryActions = [],
  onAction,
  actionsBusy = new Set(),
  linkTo = null,
}) {
  if (!workflow) return null;
  const { current_gate, next_action, blocked_actions = [], missing_evidence = [] } = workflow;

  // Closed / terminal experiment: there is no gate and nothing the user can
  // do. The backend still reports current_gate="terminal" plus an internal
  // blocked entry (e.g. `mutate_experiment — experiment complete`). Rendering
  // those raw — the bare word "terminal" next to a red "blocked:" line — reads
  // like an error on a successful run, so render a calm closure note instead
  // and skip the gate/next/blocked machinery entirely.
  const closed = ['complete', 'failed', 'abandoned'].includes(closedStatus)
    ? closedStatus
    : (current_gate === 'terminal' ? 'closed' : null);
  if (closed) {
    const CLOSED = {
      complete:  { cls: 'gate-banner--terminal', glyph: '✓', label: 'Experiment complete' },
      failed:    { cls: 'gate-banner--failed',   glyph: '✕', label: 'Experiment failed' },
      abandoned: { cls: 'gate-banner--failed',   glyph: '✕', label: 'Experiment abandoned' },
      closed:    { cls: 'gate-banner--terminal', glyph: '✓', label: 'Experiment closed' },
    }[closed];
    return (
      <div className={`gate-banner gate-banner--closed ${CLOSED.cls}`}>
        <div className="gate-banner-body gate-banner-body--closed">
          <span className="gate-closed-glyph" aria-hidden="true">{CLOSED.glyph}</span>
          <span className="gate-banner-title">{CLOSED.label}</span>
        </div>
      </div>
    );
  }

  const isTerminal = experimentStatus === 'complete' || current_gate === 'terminal';
  const isFailed = experimentStatus === 'failed' || experimentStatus === 'abandoned';
  // Pulse when the system is actively waiting on something external — a
  // sandbox provisioning, a reviewer that's been launched, etc. The `wait_`
  // prefix is the backend's signal for "you don't need to do anything; we're
  // in motion".
  const isWaiting = !isTerminal && !isFailed && /^wait[_-]/.test(next_action || '');

  const cls = ['gate-banner'];
  if (isTerminal && !isFailed) cls.push('gate-banner--terminal');
  if (isFailed) cls.push('gate-banner--failed');
  if (isWaiting) cls.push('gate-banner--live');

  const hasButtons = (primaryAction && onAction) || (secondaryActions.length > 0 && onAction);

  const titleInner = (
    <>
      {isWaiting && <span className="gate-live-dot" aria-hidden="true" />}
      {prettyGate(current_gate)}
    </>
  );
  // If linkTo is just a hash, we're already on the target page — scroll
  // in place instead of navigating (a no-op hash change wouldn't re-trigger
  // useScrollToHash on the destination).
  const handleTitleClick = (e) => {
    if (typeof linkTo === 'string' && linkTo.startsWith('#')) {
      e.preventDefault();
      const id = linkTo.slice(1);
      document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  };

  return (
    <div className={cls.join(' ')}>
      <div className="gate-banner-body">
        {linkTo ? (
          <Link to={linkTo} onClick={handleTitleClick} className="gate-banner-title gate-banner-title--link">
            {titleInner}
          </Link>
        ) : (
          <div className="gate-banner-title">{titleInner}</div>
        )}
        {next_action && next_action !== 'none' && (
          <div className="gate-banner-action">
            <span className="gate-banner-meta-key">next:</span>
            {next_action}
          </div>
        )}
        {experimentStatus && (
          <div className="gate-banner-meta">
            <span><span className="gate-banner-meta-key">status</span><StatusPill value={experimentStatus} pill={false} /></span>
          </div>
        )}

        {missing_evidence.length > 0 && (
          <div className="gate-banner-missing">
            {missing_evidence.map((m, i) => (
              <div key={i}>
                <span className="gate-banner-missing-key">missing</span>
                {m}
              </div>
            ))}
          </div>
        )}

        {hasButtons && (
          <div className="gate-banner-actions">
            {primaryAction && onAction && (
              <button
                className="btn btn--primary"
                disabled={actionsBusy.has(primaryAction.transition)}
                onClick={() => onAction(primaryAction.transition)}
              >
                {actionsBusy.has(primaryAction.transition) ? '…' : primaryAction.label}
              </button>
            )}
            {secondaryActions.map(a => (
              <button
                key={a.transition}
                className="btn btn--sm btn--ghost"
                disabled={actionsBusy.has(a.transition)}
                onClick={() => onAction(a.transition)}
              >
                {actionsBusy.has(a.transition) ? '…' : a.label}
              </button>
            ))}
          </div>
        )}

        {blocked_actions.length > 0 && (
          <div className="gate-banner-blocked">
            {blocked_actions.map((b, i) => (
              <div key={i} className="gate-banner-blocked-item">
                blocked: {b.action} — {b.reason}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function prettyGate(g) {
  if (!g) return 'No gate';
  return String(g).replace(/_/g, ' ');
}

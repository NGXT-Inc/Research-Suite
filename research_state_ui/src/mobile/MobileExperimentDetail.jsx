import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api';
import { useProjectStore, selectResources, useProjectHref } from '../store/useProjectStore';
import ExperimentMetrics from '../components/ExperimentMetrics';
import SandboxTerminal from '../components/SandboxTerminal';
import MobileGraphSection from './MobileGraphSection';
import MobileDoc from './MobileDoc';
import { Skeleton } from './Skeleton';
import { expName, statusColor, statusLine, TERMINAL_STATUSES } from '../utils/experiment';

/**
 * Mobile experiment detail — one continuous scroll (design handoff, sketch
 * 2b): Status → Plan → Run → Outcomes flow down a single surface, each
 * introduced by a small label and separated by a hairline. No section
 * navigator — just scroll.
 *
 * design_philosophy.md is the law here: the artifacts (intent, plan,
 * terminal, report, curves) are the content; everything about the workflow
 * collapses into ONE color-indexed status statement — no FSM enumeration,
 * no gate card, no counts. Heavy panes attach on tap: the terminal (its
 * poller) and the graph mount only when opened, so a long scroll never
 * stacks pollers. Read-only: reviews and transitions are the agent's job.
 */
export default function MobileExperimentDetail() {
  const { experimentId } = useParams();
  const px = useProjectHref();
  const projectId = useProjectStore(s => s.projectId);
  const allProjectResources = useProjectStore(selectResources);

  const [statusData, setStatusData] = useState(null);
  const [error, setError] = useState(null);
  const [termOpen, setTermOpen] = useState(false);
  const [graphOpen, setGraphOpen] = useState(false);

  // Run only exists while a sandbox is attached — a terminal with nothing
  // to attach to is dead chrome.
  const hasSandbox = (statusData?.sandboxes || []).length > 0;

  const fetchStatus = useCallback(async () => {
    try {
      const data = await api.getExperimentStatus(projectId, experimentId);
      setStatusData(data);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [projectId, experimentId]);

  // Navigating experiment→experiment keeps this component mounted; reset so
  // the old experiment never flashes and heavy panes fold back shut.
  useEffect(() => {
    setStatusData(null);
    setError(null);
    setTermOpen(false);
    setGraphOpen(false);
  }, [experimentId]);

  useEffect(() => {
    let cancelled = false;
    fetchStatus();
    const t = setInterval(() => {
      if (!cancelled && document.visibilityState === 'visible') fetchStatus();
    }, 5000);
    const onVis = () => { if (document.visibilityState === 'visible') fetchStatus(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { cancelled = true; clearInterval(t); document.removeEventListener('visibilitychange', onVis); };
  }, [fetchStatus]);

  const experiment = statusData?.experiment;
  const workflow = statusData?.workflow;

  if (error) {
    return (
      <div className="mxd">
        <div className="error-message">{error}</div>
        <Link className="btn" to={px('/experiments')} style={{ marginTop: 12 }}>← Experiments</Link>
      </div>
    );
  }
  if (!experiment) {
    return (
      <div className="mxd">
        <header className="page-header"><Skeleton lines={1} /></header>
        <Skeleton lines={5} />
      </div>
    );
  }

  const currentAttempt = experiment.attempt_index;
  const isClosed = TERMINAL_STATUSES.includes(experiment.status);

  // ── Resource partition (same derivation as the desktop detail page) ──
  const currentRes = (experiment.current_attempt_resources || [])
    .slice()
    .sort((a, b) => (a.association_role || '').localeCompare(b.association_role || ''));
  const enrich = (bare) =>
    bare ? (allProjectResources.find(r => r.id === bare.id) || bare) : null;
  const planResBare = currentRes.find(r => r.association_role === 'plan') || null;
  const planRes = enrich(planResBare)
    || allProjectResources.find(r => (r.associations || []).some(
      a => a.target_type === 'experiment' && a.target_id === experimentId && a.role === 'plan',
    )) || null;
  const reportRes = enrich(currentRes.find(r => r.association_role === 'report') || null);

  const allReviews = (experiment.reviews || []).slice().sort((a, b) =>
    (a.created_at || '').localeCompare(b.created_at || ''),
  );
  const designReviews = allReviews.filter(r => (r.role || '').toLowerCase().includes('design'));
  const experimentReviews = allReviews.filter(r => !(r.role || '').toLowerCase().includes('design'));

  return (
    <div className="mxd">
      <header className="page-header">
        <div className="page-eyebrow">
          <Link to={px('/experiments')}>‹ Experiments</Link>
          {' · '}attempt {currentAttempt}
        </div>
        <h1 className="page-title">{expName(experiment)}</h1>
      </header>

      {/* ── Status ─────────────────────────────────────────────────── */}
      <section className="mxd-section">
        <div className="mml">Status</div>
        <StatusStatement experiment={experiment} workflow={isClosed ? null : workflow} />
        {experiment.intent && <p className="mxd-intent">{experiment.intent}</p>}
        <LazyRow open={graphOpen} onOpen={() => setGraphOpen(true)} label="graph">
          <MobileGraphSection
            projectId={projectId}
            experimentId={experimentId}
            experimentStatus={experiment.status}
            attemptIndex={currentAttempt}
          />
        </LazyRow>
      </section>

      <div className="mbreak" />

      {/* ── Plan ───────────────────────────────────────────────────── */}
      <section className="mxd-section">
        <div className="mml">Plan</div>
        {planRes ? (
          <MobileDoc
            projectId={projectId}
            resource={planRes}
            reviews={designReviews}
            kind="plan"
            experimentStatus={experiment.status}
            attemptIndex={currentAttempt}
          />
        ) : (
          <div className="mquiet">no plan synced yet</div>
        )}
      </section>

      <div className="mbreak" />

      {/* ── Run — only while a sandbox is attached; terminal attaches
             (and starts polling) on tap ── */}
      {hasSandbox && (
        <>
          <section className="mxd-section">
            <div className="mml">Run</div>
            <LazyRow open={termOpen} onOpen={() => setTermOpen(true)} label="terminal">
              <SandboxTerminal projectId={projectId} experimentId={experimentId} readOnly />
            </LazyRow>
          </section>

          <div className="mbreak" />
        </>
      )}

      {/* ── Outcomes ───────────────────────────────────────────────── */}
      <section className="mxd-section">
        <div className="mml">Outcomes</div>
        {reportRes && (
          <MobileDoc
            projectId={projectId}
            resource={reportRes}
            reviews={experimentReviews}
            kind="report"
            experimentStatus={experiment.status}
          />
        )}
        <ExperimentMetrics
          projectId={projectId}
          experimentId={experimentId}
          refreshKey={`${experiment.status}:${currentAttempt}`}
          dense
        />
      </section>
    </div>
  );
}

// The entire workflow apparatus as one statement: a 3px index in the state's
// color (the same facet language as the list rows), the state sentence, and
// — only while live — the server's next move, humanized from its snake_case.
// FSM enumeration, gate cards, and counts are struck (design_philosophy §II).
function StatusStatement({ experiment, workflow }) {
  const status = (experiment.status || 'planned').toLowerCase();
  const color = statusColor(status);
  const next = workflow?.next_action && workflow.next_action !== 'none'
    ? humanizeAction(workflow.next_action)
    : null;
  const missing = workflow?.missing_evidence || [];

  return (
    <div className="mxd-status">
      <span className="mxd-status-ix" style={{ background: color }} aria-hidden="true" />
      <div className="mxd-status-body">
        <div className="mxd-status-line" style={{ color }}>
          {statusLine(experiment, status, Date.now())}
        </div>
        {next && <div className="mxd-status-next">{next}</div>}
        {missing.map((m, i) => (
          <div key={i} className="mxd-status-next">missing · {String(m).replace(/_/g, ' ')}</div>
        ))}
      </div>
    </div>
  );
}

// "wait_for_reviewer" → "waiting on reviewer"; "launch_design_reviewer" →
// "next · launch design reviewer". The wait_ prefix is the backend's "in
// motion, nothing needed from you".
function humanizeAction(action) {
  const a = String(action);
  if (/^wait[_-]/.test(a)) return `waiting on ${a.replace(/^wait[_-](for[_-])?/, '').replace(/_/g, ' ')}`;
  return `next · ${a.replace(/_/g, ' ')}`;
}

// A heavy pane folded into the surface: a quiet disclosure row that mounts
// its children only once opened (preserves the "polls only when open" rule).
function LazyRow({ open, onOpen, label, children }) {
  if (open) return children;
  return (
    <button type="button" className="mterm-row" onClick={onOpen}>
      <span className="mterm-twist" aria-hidden="true">▸</span>
      {label}
    </button>
  );
}

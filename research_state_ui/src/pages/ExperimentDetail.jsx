import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api';
import { useProjectStore, selectResources } from '../store/useProjectStore';
import FSMStrip from '../components/FSMStrip';
import GateBanner from '../components/GateBanner';
import PlanSpotlight from '../components/PlanSpotlight';
import ReportSpotlight from '../components/ReportSpotlight';
import ExperimentGraphs from '../components/ExperimentGraphs';
import SandboxTerminal from '../components/SandboxTerminal';
import OutcomesSection from '../components/OutcomesSection';
import ResultsMetricsPanel from '../components/ResultsMetricsPanel';
import ResourceList from '../components/ResourceList';
import AddResourceToExperiment from '../components/AddResourceToExperiment';
import { expName } from '../utils/experiment';
import { gateToSectionId, useScrollToHash } from '../utils/useScrollToHash';

const NEXT_ACTION_TO_TRANSITION = {
  submit_design_for_review:  { transition: 'submit_design',     label: 'Submit for design review' },
  mark_ready_to_run:         { transition: 'mark_ready_to_run', label: 'Mark ready to run' },
  start_running:             { transition: 'start_running',     label: 'Start running' },
  submit_results_for_review: { transition: 'submit_results',    label: 'Submit results for review' },
  complete_experiment:       { transition: 'complete',          label: 'Complete experiment' },
};
const SECONDARY_TRANSITIONS = [
  { transition: 'mark_failed', label: 'Mark failed' },
  { transition: 'abandon',     label: 'Abandon' },
];

function deriveActionButtons(workflow) {
  if (!workflow) return { primary: null, secondary: [] };
  const allowsTransition = (workflow.allowed_actions || []).some(a => a === 'experiment.transition' || (a && !a.includes('.')));
  if (!allowsTransition) return { primary: null, secondary: [] };
  // next_action may carry inline guidance after the verb (e.g.
  // "submit_results_for_review (call only once …)") — match on the verb.
  const actionKey = String(workflow.next_action || '').split(/[\s(]/)[0];
  const primary = NEXT_ACTION_TO_TRANSITION[actionKey] || null;
  const inFlight = !['complete', 'failed', 'abandoned', 'terminal'].includes(workflow.current_gate);
  return { primary, secondary: inFlight ? SECONDARY_TRANSITIONS : [] };
}


export default function ExperimentDetail() {
  const { experimentId } = useParams();
  const projectId = useProjectStore(s => s.projectId);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const allProjectResources = useProjectStore(selectResources);

  const [statusData, setStatusData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(new Set());
  const [actionError, setActionError] = useState(null);
  const [gateOpen, setGateOpen] = useState(false);
  const [showAddPlan, setShowAddPlan] = useState(false);
  const [showAddInput, setShowAddInput] = useState(false);
  const [showAddOutcome, setShowAddOutcome] = useState(false);
  const [showAddReport, setShowAddReport] = useState(false);

  // Cross-page deep links (e.g. /experiments/:id#execution) — once the
  // experiment has loaded and its sections rendered, scroll the matching id
  // into view.
  useScrollToHash([statusData]);

  const fetchStatus = useCallback(async () => {
    try {
      const data = await api.getExperimentStatus(projectId, experimentId);
      setStatusData(data);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [projectId, experimentId]);

  useEffect(() => {
    let cancelled = false;
    fetchStatus();
    const t = setInterval(() => { if (!cancelled) fetchStatus(); }, 3000);
    const onVis = () => { if (document.visibilityState === 'visible') fetchStatus(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { cancelled = true; clearInterval(t); document.removeEventListener('visibilitychange', onVis); };
  }, [fetchStatus]);

  const experiment = statusData?.experiment;
  const workflow = statusData?.workflow;

  const { primary, secondary } = useMemo(() => deriveActionButtons(workflow), [workflow]);

  const onAction = useCallback(async (transition) => {
    setBusy(prev => { const n = new Set(prev); n.add(transition); return n; });
    setActionError(null);
    try {
      await api.transitionExperiment(projectId, experimentId, transition);
      await Promise.all([fetchStatus(), refreshHome()]);
    } catch (err) {
      setActionError(`${transition}: ${err.message}`);
    } finally {
      setBusy(prev => { const n = new Set(prev); n.delete(transition); return n; });
    }
  }, [projectId, experimentId, fetchStatus, refreshHome]);

  if (error) {
    return (
      <div className="page-stage">
        <div className="error-message">{error}</div>
        <Link className="btn" to="/experiments" style={{ marginTop: 12 }}>← Experiments</Link>
      </div>
    );
  }
  if (!experiment) {
    return <div className="page-stage"><div className="empty">Loading…</div></div>;
  }

  const currentAttempt = experiment.attempt_index;
  const isClosed = ['complete', 'failed', 'abandoned'].includes(experiment.status);

  // Partition resources by role.
  const currentRes = (experiment.current_attempt_resources || [])
    .slice()
    .sort((a, b) => (a.association_role || '').localeCompare(b.association_role || ''));
  const currentIds = new Set(currentRes.map(r => r.id));
  // The status endpoint's current_attempt_resources gives us per-attempt
  // association metadata (role / attempt), but not the richer /home shape
  // (version_token, associations[]) — so we look the resource up in the
  // project store, which carries it.
  //
  // Fallback: if the current attempt hasn't associated a plan yet (e.g. just
  // bumped to a new attempt), find the experiment's plan resource via its
  // full associations history so PlanSpotlight can still render it.
  const planResBare = currentRes.find(r => r.association_role === 'plan') || null;
  const planResFromCurrent = planResBare
    ? (allProjectResources.find(r => r.id === planResBare.id) || planResBare)
    : null;
  const planResFromHistory = planResFromCurrent
    ? null
    : allProjectResources.find(r => (r.associations || []).some(
        a => a.target_type === 'experiment' && a.target_id === experimentId && a.role === 'plan'
      )) || null;
  const planRes = planResFromCurrent || planResFromHistory;
  // The results report (role 'report') mirrors the plan: current attempt only
  // (a prior attempt's report is history, not the face of this attempt).
  const reportResBare = currentRes.find(r => r.association_role === 'report') || null;
  const reportRes = reportResBare
    ? (allProjectResources.find(r => r.id === reportResBare.id) || reportResBare)
    : null;
  const execRes    = currentRes.filter(r => ['code', 'config', 'input'].includes(r.association_role));
  const outcomeRes = currentRes.filter(r => ['result', 'model'].includes(r.association_role));
  const otherRes   = currentRes.filter(r => !['plan', 'report', 'code', 'config', 'input', 'result', 'model'].includes(r.association_role));

  // Historical (deduped by id).
  const historicalRes = (experiment.resources || [])
    .filter(r => r.association_attempt_index !== currentAttempt)
    .filter(r => !currentIds.has(r.id));

  // Reviews — split by role, ascending by created_at so the stepper reads
  // left-to-right as the timeline.
  const allReviews = (experiment.reviews || []).slice().sort((a, b) =>
    (a.created_at || '').localeCompare(b.created_at || ''),
  );
  const designReviews = allReviews.filter(r => (r.role || '').toLowerCase().includes('design'));
  const experimentReviews = allReviews.filter(r => !(r.role || '').toLowerCase().includes('design'));

  const refresh = async () => { await Promise.all([fetchStatus(), refreshHome()]); };

  return (
    <div className="page-stage">
      {/* ─────────────  STAGE  ──────────────────────────────────────── */}
      {/* The strip is the page's status truth. For a live experiment the
          current step discloses the gate panel (details + transitions);
          closed experiments need no panel — the strip already says it. */}
      <section className="exp-fsm">
        <FSMStrip
          status={experiment.status}
          badge={!isClosed && primary ? 'action' : null}
          expanded={!isClosed && gateOpen}
          onToggle={isClosed ? null : () => setGateOpen(v => !v)}
        >
          <div className="fsm-gate-panel">
            <GateBanner
              workflow={workflow}
              primaryAction={primary}
              secondaryActions={secondary}
              actionsBusy={busy}
              onAction={onAction}
              linkTo={(() => {
                const section = gateToSectionId(workflow?.current_gate);
                return section ? `#${section}` : null;
              })()}
            />
          </div>
        </FSMStrip>
        {actionError && <div className="error-message">{actionError}</div>}
      </section>

      {/* ─────────────  ORIENTATION  ────────────────────────────────── */}
      <header className="exp-orient">
        <div className="page-eyebrow">
          <Link to="/experiments">Experiments</Link>
          {' · '}<span className="exp-orient-attempt">attempt {currentAttempt}</span>
        </div>
        <h1 className="page-title exp-title-name">{expName(experiment)}</h1>
        {experiment.intent && <p className="exp-intent">{experiment.intent}</p>}
      </header>

      {/* ─────────────  MAP (pinned overview: figure ⇄ logic graph)  ── */}
      <ExperimentGraphs
        projectId={projectId}
        experimentId={experimentId}
        experimentStatus={experiment.status}
        attemptIndex={currentAttempt}
      />

      {/* ═════════════  RESULTS  ════════════════════════════════════════
          Newest-first: the executed experiment's output leads the page. The
          report (with its experiment review behind a "Show review" disclosure)
          comes first, then durable metrics and result files. Each piece is
          simply absent until it exists — the order itself never changes. */}
      {reportRes && (
        <ReportSpotlight
          projectId={projectId}
          reportResource={reportRes}
          experimentReviews={experimentReviews}
          experimentStatus={experiment.status}
        />
      )}

      {!reportRes && experiment.status === 'running' && (
        <div id="report" className="spotlight-followup">
          <button
            type="button"
            className="btn btn--sm btn--primary"
            onClick={() => setShowAddReport(v => !v)}
          >
            {showAddReport ? 'Cancel' : '+ Add results report'}
          </button>
          {showAddReport && (
            <div style={{ marginTop: 10 }}>
              <AddResourceToExperiment
                projectId={projectId}
                experimentId={experimentId}
                attemptIndex={currentAttempt}
                currentResources={currentRes}
                allResources={allProjectResources}
                defaultRole="report"
                onCancel={() => setShowAddReport(false)}
                onDone={async () => { setShowAddReport(false); await refresh(); }}
              />
            </div>
          )}
        </div>
      )}

      {/* Durable archived metrics — final MLflow numbers that survive sandbox
          teardown (renders nothing until something has been recorded). */}
      <ResultsMetricsPanel projectId={projectId} experimentId={experimentId} />

      {/* Result files. The experiment review is suppressed here when a report
          hosts it in its "Show review" disclosure (no double-render). */}
      <OutcomesSection
        projectId={projectId}
        outcomeResources={outcomeRes}
        experimentReviews={experimentReviews}
        experimentStatus={experiment.status}
        hideReviews={!!reportRes}
      />

      {!['complete', 'failed', 'abandoned'].includes(experiment.status) && experiment.status !== 'planned' && (
        <div className="spotlight-followup">
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => setShowAddOutcome(v => !v)}
          >
            {showAddOutcome ? 'Cancel' : '+ Add result resource'}
          </button>
          {showAddOutcome && (
            <div style={{ marginTop: 10 }}>
              <AddResourceToExperiment
                projectId={projectId}
                experimentId={experimentId}
                attemptIndex={currentAttempt}
                currentResources={currentRes}
                allResources={allProjectResources}
                defaultRole="result"
                onCancel={() => setShowAddOutcome(false)}
                onDone={async () => { setShowAddOutcome(false); await refresh(); }}
              />
            </div>
          )}
        </div>
      )}

      {/* ═════════════  EXECUTION  ══════════════════════════════════════
          The sandbox: expanded while a run is live/provisioning, collapsed to
          its header once the run has ended (collapsible). */}
      <SandboxTerminal
        projectId={projectId}
        experimentId={experimentId}
        collapsible
      />

      {!['complete', 'failed', 'abandoned'].includes(experiment.status) && (
        <div className="spotlight-followup">
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => setShowAddInput(v => !v)}
          >
            {showAddInput ? 'Cancel' : '+ Add code / config / input'}
          </button>
          {showAddInput && (
            <div style={{ marginTop: 10 }}>
              <AddResourceToExperiment
                projectId={projectId}
                experimentId={experimentId}
                attemptIndex={currentAttempt}
                currentResources={currentRes}
                allResources={allProjectResources}
                defaultRole="code"
                onCancel={() => setShowAddInput(false)}
                onDone={async () => { setShowAddInput(false); await refresh(); }}
              />
            </div>
          )}
        </div>
      )}

      {/* ═════════════  DESIGN  ═════════════════════════════════════════
          The framing document, oldest so it anchors the bottom. Its design
          review lives behind a "Show review" disclosure on the header. */}
      <PlanSpotlight
        projectId={projectId}
        planResource={planRes}
        designReviews={designReviews}
        attemptIndex={currentAttempt}
        experimentStatus={experiment.status}
        defaultOpen={!reportRes}
      />

      {!planRes && !['complete', 'failed', 'abandoned'].includes(experiment.status) && (
        <div className="spotlight-followup">
          <button
            type="button"
            className="btn btn--sm btn--primary"
            onClick={() => setShowAddPlan(v => !v)}
          >
            {showAddPlan ? 'Cancel' : '+ Register plan resource'}
          </button>
          {showAddPlan && (
            <div style={{ marginTop: 10 }}>
              <AddResourceToExperiment
                projectId={projectId}
                experimentId={experimentId}
                attemptIndex={currentAttempt}
                currentResources={currentRes}
                allResources={allProjectResources}
                defaultRole="plan"
                onCancel={() => setShowAddPlan(false)}
                onDone={async () => { setShowAddPlan(false); await refresh(); }}
              />
            </div>
          )}
        </div>
      )}

      {(otherRes.length > 0 || historicalRes.length > 0) && (
        <FooterMisc
          projectId={projectId}
          otherRes={otherRes}
          historicalRes={historicalRes}
        />
      )}
    </div>
  );
}

function FooterMisc({ projectId, otherRes, historicalRes }) {
  const [showHist, setShowHist] = useState(false);
  return (
    <section className="exp-footer">
      {otherRes.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="outcomes-subhead">Other resources</div>
          <ResourceList projectId={projectId} resources={otherRes} />
        </div>
      )}
      {historicalRes.length > 0 && (
        <div>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => setShowHist(v => !v)}
          >
            {showHist
              ? `Hide earlier-attempt resources (${historicalRes.length})`
              : `Carried forward from earlier attempts (${historicalRes.length})`}
          </button>
          {showHist && (
            <div style={{ marginTop: 10 }}>
              <ResourceList projectId={projectId} resources={historicalRes} historical />
            </div>
          )}
        </div>
      )}
    </section>
  );
}

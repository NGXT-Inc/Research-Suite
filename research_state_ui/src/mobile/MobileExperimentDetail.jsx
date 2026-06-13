import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api';
import { useProjectStore, selectResources } from '../store/useProjectStore';
import FSMStrip from '../components/FSMStrip';
import GateBanner from '../components/GateBanner';
import PlanSpotlight from '../components/PlanSpotlight';
import ReportSpotlight from '../components/ReportSpotlight';
import OutcomesSection from '../components/OutcomesSection';
import SandboxTerminal from '../components/SandboxTerminal';
import MobileGraphSection from './MobileGraphSection';
import MobileMetricsPanel from './MobileMetricsPanel';
import { expName } from '../utils/experiment';

const SEGMENTS = [
  { id: 'status',   label: 'Status' },
  { id: 'graph',    label: 'Graph' },
  { id: 'plan',     label: 'Plan' },
  { id: 'run',      label: 'Run' },
  { id: 'outcomes', label: 'Outcomes' },
];

// Where a supervisor most likely wants to land, by lifecycle position.
function defaultSegment(status) {
  if (status === 'planned' || status === 'design_review') return 'plan';
  if (status === 'ready_to_run' || status === 'running') return 'run';
  if (status === 'experiment_review' || status === 'complete' || status === 'failed' || status === 'abandoned') return 'outcomes';
  return 'status';
}

/**
 * Mobile experiment detail: FSM strip + segmented control where ONLY the
 * active segment mounts — so the terminal polls only on Run, the plan
 * fetches only on Plan, and the page never stacks five pollers the way the
 * desktop detail does. Read-only: the gate panel shows the server's
 * workflow state without transition buttons (reviews and transitions are
 * the agent's job; sandbox release lives on the Sandboxes screen).
 */
export default function MobileExperimentDetail() {
  const { experimentId } = useParams();
  const projectId = useProjectStore(s => s.projectId);
  const allProjectResources = useProjectStore(selectResources);

  const [statusData, setStatusData] = useState(null);
  const [error, setError] = useState(null);
  const [segment, setSegment] = useState(null); // null until first load picks a default
  const [gateOpen, setGateOpen] = useState(false);

  const fetchStatus = useCallback(async () => {
    try {
      const data = await api.getExperimentStatus(projectId, experimentId);
      setStatusData(data);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [projectId, experimentId]);

  // Navigating experiment→experiment keeps this component mounted; without a
  // reset the old experiment flashes for a tick and the landing segment never
  // re-defaults to the new one's lifecycle (docs/MOBILE_UX_REVIEW.md §1.8).
  useEffect(() => {
    setStatusData(null);
    setSegment(null);
    setError(null);
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

  // Pick the landing segment once, from the first loaded status.
  useEffect(() => {
    if (experiment && segment == null) setSegment(defaultSegment(experiment.status));
  }, [experiment, segment]);

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
  const seg = segment || defaultSegment(experiment.status);

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
  const outcomeRes = currentRes.filter(r => ['result', 'model'].includes(r.association_role));

  const allReviews = (experiment.reviews || []).slice().sort((a, b) =>
    (a.created_at || '').localeCompare(b.created_at || ''),
  );
  const designReviews = allReviews.filter(r => (r.role || '').toLowerCase().includes('design'));
  const experimentReviews = allReviews.filter(r => !(r.role || '').toLowerCase().includes('design'));

  return (
    <div className="page-stage">
      <header className="page-header">
        <div className="page-eyebrow">
          <Link to="/experiments">Experiments</Link>
          {' · '}attempt {currentAttempt}
        </div>
        <h1 className="page-title">{expName(experiment)}</h1>
      </header>

      <FSMStrip
        status={experiment.status}
        expanded={!isClosed && gateOpen}
        onToggle={isClosed ? null : () => setGateOpen(v => !v)}
      >
        <div className="fsm-gate-panel">
          {/* Read-only: no transition buttons on mobile — the strip shows
              where it stands, the banner shows what the server wants next. */}
          <GateBanner workflow={workflow} />
        </div>
      </FSMStrip>

      <div className="mseg" role="tablist" aria-label="Experiment sections">
        {SEGMENTS.map(s => (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={seg === s.id}
            className={`mseg-btn${seg === s.id ? ' active' : ''}`}
            onClick={() => setSegment(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      {seg === 'status' && (
        <StatusSegment
          experiment={experiment}
          workflow={workflow}
          currentRes={currentRes}
          reviewCount={allReviews.length}
        />
      )}

      {seg === 'graph' && (
        <MobileGraphSection
          projectId={projectId}
          experimentId={experimentId}
          experimentStatus={experiment.status}
          attemptIndex={currentAttempt}
        />
      )}

      {seg === 'plan' && (
        <>
          <PlanSpotlight
            projectId={projectId}
            planResource={planRes}
            designReviews={designReviews}
            attemptIndex={currentAttempt}
            experimentStatus={experiment.status}
            defaultOpen
          />
          {!planRes && (
            <div className="empty-state empty-state--compact">
              <p>No plan synced yet — the agent registers <span className="mono">plan.md</span> before design review.</p>
            </div>
          )}
        </>
      )}

      {seg === 'run' && (
        <SandboxTerminal projectId={projectId} experimentId={experimentId} readOnly />
      )}

      {seg === 'outcomes' && (
        <>
          {reportRes && (
            <ReportSpotlight
              projectId={projectId}
              reportResource={reportRes}
              experimentReviews={experimentReviews}
              experimentStatus={experiment.status}
            />
          )}
          <OutcomesSection
            projectId={projectId}
            outcomeResources={outcomeRes}
            experimentReviews={experimentReviews}
            experimentStatus={experiment.status}
          />
          <MobileMetricsPanel projectId={projectId} experimentId={experimentId} />
        </>
      )}
    </div>
  );
}

function StatusSegment({ experiment, workflow, currentRes, reviewCount }) {
  const claimCount = Array.isArray(experiment.tested_claims) ? experiment.tested_claims.length : 0;
  return (
    <>
      {experiment.intent && (
        <section className="section">
          <div className="section-title">Intent</div>
          <p style={{ fontSize: 'var(--text-md)' }}>{experiment.intent}</p>
        </section>
      )}
      {workflow && (
        <section className="section">
          <div className="section-title">Workflow</div>
          <GateBanner workflow={workflow} />
        </section>
      )}
      <section className="section">
        <div className="section-title">At a glance</div>
        <div className="mcard">
          <div className="mcard-meta">
            <span>{currentRes.length} current-attempt resource{currentRes.length === 1 ? '' : 's'}</span>
            {claimCount > 0 && <span>tests {claimCount} claim{claimCount === 1 ? '' : 's'}</span>}
            {reviewCount > 0 && <span>{reviewCount} review{reviewCount === 1 ? '' : 's'}</span>}
          </div>
        </div>
      </section>
    </>
  );
}

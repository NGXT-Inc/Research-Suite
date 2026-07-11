import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useProjectStore,
  useProjectHref,
  selectProject,
  selectStats,
  selectActiveExperiments,
  selectClaims,
  selectExperiments,
  selectSandboxes,
  selectEventsAll,
} from '../store/useProjectStore';
import FSMStrip from '../components/FSMStrip';
import SandboxTable from '../components/SandboxTable';
import ActiveExperimentPager from '../components/ActiveExperimentPager';
import ProjectSynthesisPanel from '../components/ProjectSynthesisPanel';
import ResearchStory from '../components/story/ResearchStory';
import { expName } from '../utils/experiment';

export default function Home() {
  const px = useProjectHref();
  const project = useProjectStore(selectProject);
  const stats = useProjectStore(selectStats);
  const activeExperiments = useProjectStore(selectActiveExperiments);
  const claims = useProjectStore(selectClaims);
  const experiments = useProjectStore(selectExperiments);
  const sandboxes = useProjectStore(selectSandboxes);
  const events = useProjectStore(selectEventsAll);
  const runningSandboxes = sandboxes.filter(s => s.status === 'running').length;

  // Pager state for the spotlight. Clamp on list shrink (e.g. an experiment
  // just completed and dropped out of active_experiments).
  const [activeIdx, setActiveIdx] = useState(0);
  useEffect(() => {
    if (activeIdx > 0 && activeIdx >= activeExperiments.length) {
      setActiveIdx(Math.max(0, activeExperiments.length - 1));
    }
  }, [activeExperiments.length, activeIdx]);

  // Keyboard ← / → page through active experiments while Home is mounted.
  useEffect(() => {
    if (activeExperiments.length <= 1) return undefined;
    const onKey = (e) => {
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      // Focus resting on any interactive control (story ribbon segments,
      // chapter toggles, chips…) keeps its own arrow-key meaning — don't
      // flip the spotlight underneath it.
      if (e.target && typeof e.target.closest === 'function' && e.target.closest('a, button')) return;
      if (e.key === 'ArrowLeft') {
        setActiveIdx((i) => Math.max(0, i - 1));
      } else if (e.key === 'ArrowRight') {
        setActiveIdx((i) => Math.min(activeExperiments.length - 1, i + 1));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [activeExperiments.length]);

  if (!project) {
    return <div className="page-stage"><div className="empty-state">Loading project…</div></div>;
  }

  const safeIdx = Math.min(activeIdx, Math.max(0, activeExperiments.length - 1));
  const activeExp = activeExperiments[safeIdx] || null;
  const workflow = activeExp?.workflow || null;

  return (
    <div className="page-stage">
      {/* The project name is always in the sidebar's project chip — repeating it
          as the page title is noise. Lead with the summary (real content) when
          there is one; otherwise go straight to the work below. */}
      {project.summary && (
        <header className="page-header page-header--lg">
          <p className="page-summary page-summary--lead">{project.summary}</p>
        </header>
      )}

      {/* The narrative arc first: what happened, chapter by chapter, live. */}
      <ResearchStory />

      {workflow && (
        <section className="section section--focused-exp">
          <div className="cluster--between" style={{ marginBottom: 12 }}>
            <div className="section-title" style={{ marginBottom: 0 }}>Focused experiment</div>
            <ActiveExperimentPager
              items={activeExperiments}
              index={safeIdx}
              onChange={setActiveIdx}
            />
          </div>
          {activeExp && (
            <Link
              to={px(`/experiments/${activeExp.id}`)}
              className="active-exp-card active-exp-card--bounded"
            >
              <div className="intent-lead">{expName(activeExp)}</div>
              {activeExp.intent && <p className="active-exp-intent">{activeExp.intent}</p>}
              <FSMStrip status={activeExp.status} />
            </Link>
          )}
        </section>
      )}

      <ProjectSynthesisPanel projectId={project.id} />

      <section className="section">
        <div className="section-title">
          Sandboxes
          {runningSandboxes > 0 && (
            <span className="section-title-badge">
              <span className="sidebar-live-dot" />{runningSandboxes} running
            </span>
          )}
        </div>
        <SandboxTable
          sandboxes={sandboxes}
          experiments={experiments}
          events={events}
          projectId={project.id}
          empty={(
            <div className="empty-state empty-state--compact">
              <p>No sandboxes yet.</p>
            </div>
          )}
        />
      </section>

      <section className="section">
        <div className="section-title">Counts</div>
        <div className="stat-grid">
          <StatCard label="Claims" value={stats.claims ?? claims.length} sub={countOf(claims, 'status', 'active') + ' active'} />
          <StatCard label="Experiments" value={stats.experiments ?? experiments.length} sub={countOf(experiments, 'status', 'running') + ' running'} />
          <StatCard label="Resources" value={stats.resources ?? 0} />
          <StatCard label="Sandboxes" value={runningSandboxes} sub="running" />
          <StatCard label="Open reviews" value={stats.open_reviews ?? stats.reviews ?? 0} />
        </div>
      </section>
    </div>
  );
}

function StatCard({ label, value, sub }) {
  return (
    <div className="stat-card">
      <div className="stat-card-key">{label}</div>
      <div className="stat-card-value tabular">{value}</div>
      {sub && <div className="stat-card-sub">{sub}</div>}
    </div>
  );
}

function countOf(arr, key, val) {
  return (arr || []).filter(x => x && x[key] === val).length;
}

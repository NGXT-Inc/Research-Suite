import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useProjectStore,
  selectProject,
  selectStats,
  selectActiveExperiments,
  selectActiveProcesses,
  selectClaims,
  selectExperiments,
  selectEvents,
  selectEventsAll,
  selectJobs,
  selectReviewRequests,
} from '../store/useProjectStore';
import EventTimeline from '../components/EventTimeline';
import ObjId from '../components/ObjId';
import FSMStrip from '../components/FSMStrip';
import ActiveExperimentPager from '../components/ActiveExperimentPager';
import ProjectDashboard from '../components/ProjectDashboard';
import { parseIntent } from '../utils/intent';

export default function Home() {
  const project = useProjectStore(selectProject);
  const stats = useProjectStore(selectStats);
  const activeExperiments = useProjectStore(selectActiveExperiments);
  const activeProcesses = useProjectStore(selectActiveProcesses);
  const reviewRequests = useProjectStore(selectReviewRequests);
  const claims = useProjectStore(selectClaims);
  const experiments = useProjectStore(selectExperiments);
  const events = useProjectStore(selectEvents);
  const eventsAll = useProjectStore(selectEventsAll);
  const jobs = useProjectStore(selectJobs);

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
      <header className="page-header page-header--lg">
        <div className="page-eyebrow">
          Project <ObjId id={project.id} className="page-eyebrow-id" />
        </div>
        <h1 className="page-title">{project.name}</h1>
        {project.summary && <p className="page-summary">{project.summary}</p>}
      </header>

      <section className="section">
        <ProjectDashboard jobs={jobs} experiments={experiments} />
      </section>

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
          {activeExp && (() => {
            const { title, brief } = parseIntent(activeExp.intent);
            return (
              <Link
                to={`/experiments/${activeExp.id}`}
                className="active-exp-card active-exp-card--bounded"
              >
                <div className="active-exp-title">{title || activeExp.id}</div>
                {brief && <div className="active-exp-brief">{brief}</div>}
                <FSMStrip status={activeExp.status} attemptIndex={activeExp.attempt_index} />
              </Link>
            );
          })()}
        </section>
      )}

      <section className="section">
        <div className="section-title">Counts</div>
        <div className="stat-grid">
          <StatCard label="Claims" value={stats.claims ?? claims.length} sub={countOf(claims, 'status', 'active') + ' active'} />
          <StatCard label="Experiments" value={stats.experiments ?? experiments.length} sub={countOf(experiments, 'status', 'running') + ' running'} />
          <StatCard label="Resources" value={stats.resources ?? 0} />
          <StatCard label="Open reviews" value={stats.open_reviews ?? stats.reviews ?? 0} />
        </div>
      </section>

      <section className="section">
        <div className="cluster--between" style={{ marginBottom: 12 }}>
          <div className="section-title" style={{ marginBottom: 0 }}>Recent events</div>
          <Link to="/events" className="btn btn--sm btn--ghost">Full timeline →</Link>
        </div>
        <EventTimeline events={events} limit={15} />
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

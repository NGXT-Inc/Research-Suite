import { useEffect } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useProjectStore } from './store/useProjectStore';
import { usePolling } from './store/usePolling';
import Sidebar from './components/Sidebar';
import Home from './pages/Home';
import CreateProject from './pages/CreateProject';
import Projects from './pages/Projects';
import Claims from './pages/Claims';
import ClaimDetail from './pages/ClaimDetail';
import Experiments from './pages/Experiments';
import ExperimentDetail from './pages/ExperimentDetail';
import Resources from './pages/Resources';
import Reviews from './pages/Reviews';
import Events from './pages/Events';
import Jobs from './pages/Jobs';
import Activity from './pages/Activity';
import VisualDag from './pages/VisualDag';

export default function App() {
  const projectId = useProjectStore(s => s.projectId);
  const projects = useProjectStore(s => s.projects);
  const projectsLoaded = useProjectStore(s => s.projectsLoaded);
  const bootError = useProjectStore(s => s.bootError);
  const loadProjects = useProjectStore(s => s.loadProjects);
  const refreshHome = useProjectStore(s => s.refreshHome);
  usePolling(3000);

  useEffect(() => { loadProjects(); }, [loadProjects]);

  if (!projectsLoaded) {
    return <FullPageStatus>Loading…</FullPageStatus>;
  }
  if (bootError) {
    return (
      <FullPageStatus>
        <h2>Backend not reachable</h2>
        <p>Is the research_plugin HTTP server running on <code>127.0.0.1:8787</code>?</p>
        <p className="mono" style={{ fontSize: 'var(--text-xs)', marginTop: 8 }}>
          python3 scripts/dev_http_reload.py --host 127.0.0.1 --port 8787
        </p>
        <div style={{ marginTop: 18 }}>
          <button className="btn" onClick={() => loadProjects()}>Retry</button>
        </div>
        <div className="error-message" style={{ marginTop: 10 }}>{bootError}</div>
      </FullPageStatus>
    );
  }
  // Bootstrap: no projects yet → render bare CreateProject without the shell.
  if (projects.length === 0) {
    return <CreateProject bootstrap />;
  }
  // Have projects but no active selection (race during setProjectId clearing) → wait.
  if (!projectId) {
    return <FullPageStatus>Selecting project…</FullPageStatus>;
  }

  return (
    <div className="shell">
      <Sidebar onRefresh={refreshHome} />
      <main className="shell-main">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/new" element={<CreateProject />} />
          <Route path="/claims" element={<Claims />} />
          <Route path="/claims/:claimId" element={<ClaimDetail />} />
          <Route path="/experiments" element={<Experiments />} />
          <Route path="/experiments/:experimentId" element={<ExperimentDetail />} />
          <Route path="/resources" element={<Resources />} />
          <Route path="/resources/:resourceId" element={<Resources />} />
          <Route path="/reviews" element={<Reviews />} />
          <Route path="/events" element={<Events />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/visual/dag" element={<VisualDag />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function FullPageStatus({ children }) {
  return (
    <div className="page-stage" style={{ display: 'flex', alignItems: 'center', minHeight: '80vh' }}>
      <div className="empty-state" style={{ textAlign: 'left' }}>{children}</div>
    </div>
  );
}

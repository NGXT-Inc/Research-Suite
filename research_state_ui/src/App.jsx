import { useEffect, useState } from 'react';
import { Routes, Route, Navigate, Outlet, useParams, useSearchParams } from 'react-router-dom';
import { useProjectStore, projectPath, useProjectHref, selectActiveExperiments, selectSandboxes } from './store/useProjectStore';
import { usePolling } from './store/usePolling';
import { useEventStream } from './store/useEventStream';
import { useViewport } from './store/useViewport';
import Sidebar, { SIDEBAR_KB, IconSidebar } from './components/Sidebar';
import CompatBanner from './components/CompatBanner';
import AppBackdrop from './bg/AppBackdrop';
import MobileShell from './mobile/MobileShell';
import HomeScreen from './mobile/HomeScreen';
import ExperimentCardList from './mobile/ExperimentCardList';
import MobileExperimentDetail from './mobile/MobileExperimentDetail';
import SandboxCardList from './mobile/SandboxCardList';
import MobileArtifacts from './mobile/MobileArtifacts';
import MobileClaims from './mobile/MobileClaims';
import MobileClaimDetail from './mobile/MobileClaimDetail';
import MobileReviews from './mobile/MobileReviews';
import MobileProjects from './mobile/MobileProjects';
import MobileProjectCreateNotice from './mobile/MobileProjectCreateNotice';
import MobileReflectionScreen from './mobile/MobileReflectionScreen';
import MobileMlflow from './mobile/MobileMlflow';
import Home from './pages/Home';
import Feed from './feed/Feed';
import CreateProject from './pages/CreateProject';
import Projects from './pages/Projects';
import Claims from './pages/Claims';
import ClaimDetail from './pages/ClaimDetail';
import LitReview from './pages/LitReview';
import Experiments from './pages/Experiments';
import ExperimentDetail from './pages/ExperimentDetail';
import Artifacts from './pages/Artifacts';
import Storage from './pages/Storage';
import Reviews from './pages/Reviews';
import Events from './pages/Events';
import Sandboxes from './pages/Sandboxes';
import Debug from './pages/Debug';
import MlflowDashboard from './pages/MlflowDashboard';
import Settings from './pages/Settings';

// /debug merged into /activity. Preserve ?tool= (v6 <Navigate> drops search).
// Lives under /p/:projectId, so redirect into the same project's /activity.
function DebugRedirect() {
  const px = useProjectHref();
  const [sp] = useSearchParams();
  const q = sp.toString();
  return <Navigate to={`${px('/activity')}${q ? `?${q}` : ''}`} replace />;
}

/**
 * Layout for the /p/:projectId subtree. The URL is the source of truth for the
 * active project: mirror the route param into the store (so every consumer that
 * reads `projectId` keeps working untouched), and bounce unknown/stale ids to
 * the active project. Holds a frame while syncing so children never fetch the
 * previous project.
 */
function ProjectScope() {
  const { projectId: routePid } = useParams();
  const projects = useProjectStore(s => s.projects);
  const storePid = useProjectStore(s => s.projectId);
  const setProjectId = useProjectStore(s => s.setProjectId);
  const known = projects.some(p => p.id === routePid);

  // Mirror the URL's project into the store. usePolling re-kicks an immediate
  // refresh whenever projectId changes, so we deliberately don't fetch here —
  // that avoids a double load, and refreshHome's identity guard drops any poll
  // still in flight for the project we just left.
  useEffect(() => {
    if (known && routePid && routePid !== storePid) {
      setProjectId(routePid);
    }
  }, [known, routePid, storePid, setProjectId]);

  if (!known) {
    const fallback = storePid || projects[0]?.id;
    return fallback
      ? <Navigate to={projectPath(fallback)} replace />
      : <FullPageStatus>Selecting project…</FullPageStatus>;
  }
  if (routePid !== storePid) return <FullPageStatus>Loading project…</FullPageStatus>;
  return <Outlet />;
}

// Desktop sidebar visibility — survives reloads, default open.
const SIDEBAR_KEY = 'rsui:sidebar';
function readSidebarOpen() {
  try { return localStorage.getItem(SIDEBAR_KEY) !== 'closed'; } catch { return true; }
}
function writeSidebarOpen(open) {
  try { localStorage.setItem(SIDEBAR_KEY, open ? 'open' : 'closed'); } catch { /* best-effort */ }
  return open;
}

// Root ("/") and anything unmatched land on the active project's home.
function RootRedirect() {
  const storePid = useProjectStore(s => s.projectId);
  const projects = useProjectStore(s => s.projects);
  const target = storePid || projects[0]?.id;
  return target
    ? <Navigate to={projectPath(target)} replace />
    : <FullPageStatus>Selecting project…</FullPageStatus>;
}

export default function App() {
  const projectId = useProjectStore(s => s.projectId);
  const projects = useProjectStore(s => s.projects);
  const projectsLoaded = useProjectStore(s => s.projectsLoaded);
  const bootError = useProjectStore(s => s.bootError);
  const loadProjects = useProjectStore(s => s.loadProjects);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const isMobile = useViewport();
  const activeExperiments = useProjectStore(selectActiveExperiments);
  const sandboxes = useProjectStore(selectSandboxes);
  // Adaptive cadence on mobile: poll fast only while something is live (a
  // running experiment / sandbox), and decay to 30s
  // on a quiet Now screen where each cellular radio wakeup is the dominant
  // battery cost. Pull-to-refresh is the instant override. Desktop stays 3s.
  const somethingLive =
    activeExperiments.some(e => e.status === 'running') ||
    sandboxes.some(s => s.status === 'running' || s.status === 'provisioning');
  const interval = isMobile ? (somethingLive ? 5000 : 30000) : 3000;
  // Server push first: while the SSE stream is open it triggers refreshHome
  // on demand and the interval poller stands down (it remains the fallback —
  // stream drop → streamHealthy flips → polling resumes at today's cadence).
  useEventStream();
  const streamHealthy = useProjectStore(s => s.streamHealthy);
  usePolling(interval, { enabled: !streamHealthy });
  const [sidebarOpen, setSidebarOpen] = useState(readSidebarOpen);
  const toggleSidebar = () => setSidebarOpen(v => writeSidebarOpen(!v));

  useEffect(() => { loadProjects(); }, [loadProjects]);

  // ⌘B / Ctrl+B toggles the sidebar (desktop shell only) — skipped while
  // typing so contenteditable bold and terminal input stay untouched.
  useEffect(() => {
    if (isMobile) return undefined;
    const onKey = (e) => {
      if (e.key !== 'b' || !(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      e.preventDefault();
      setSidebarOpen(v => writeSidebarOpen(!v));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isMobile]);

  if (!projectsLoaded) {
    return <FullPageStatus>Loading…</FullPageStatus>;
  }
  if (bootError) {
    return (
      <FullPageStatus>
        <h2>Backend not reachable</h2>
        <p>Is the Merv HTTP server running on <code>127.0.0.1:8787</code>?</p>
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
  // On mobile the directory-path form is unfillable, so show an honest notice.
  if (projects.length === 0) {
    return isMobile ? <MobileProjectCreateNotice bootstrap /> : <CreateProject bootstrap />;
  }
  // Have projects but no active selection (race during setProjectId clearing) → wait.
  if (!projectId) {
    return <FullPageStatus>Selecting project…</FullPageStatus>;
  }

  // Mobile surface: same router, same store — different shell and landing,
  // with card/segment screens replacing the desktop-physics pages
  // (min-width tables, hover tooltips, side panels). Desktop is untouched.
  if (isMobile) {
    return (
      <MobileShell onRefresh={refreshHome}>
        <CompatBanner />
        <Routes>
          {/* Global project picker (unscoped) */}
          <Route path="/projects" element={<MobileProjects />} />
          <Route path="/projects/new" element={<MobileProjectCreateNotice />} />
          {/* Project-scoped surface */}
          <Route path="/p/:projectId" element={<ProjectScope />}>
            <Route index element={<HomeScreen />} />
            <Route path="feed" element={<Feed />} />
            <Route path="claims" element={<MobileClaims />} />
            <Route path="claims/:claimId" element={<MobileClaimDetail />} />
            <Route path="litreview" element={<LitReview />} />
            <Route path="experiments" element={<ExperimentCardList />} />
            <Route path="experiments/:experimentId" element={<MobileExperimentDetail />} />
            <Route path="reflection" element={<MobileReflectionScreen />} />
            <Route path="artifacts" element={<MobileArtifacts />} />
            <Route path="artifacts/:artifactId" element={<MobileArtifacts />} />
            <Route path="storage" element={<Storage />} />
            <Route path="storage/:objectId" element={<Storage />} />
            <Route path="reviews" element={<MobileReviews />} />
            <Route path="events" element={<Events />} />
            <Route path="sandboxes" element={<SandboxCardList />} />
            <Route path="mlflow" element={<MobileMlflow />} />
            <Route path="activity" element={<Debug />} />
            <Route path="debug" element={<DebugRedirect />} />
          </Route>
          <Route path="/" element={<RootRedirect />} />
          <Route path="*" element={<RootRedirect />} />
        </Routes>
      </MobileShell>
    );
  }

  return (
    <>
      <AppBackdrop />
      <div className={'shell' + (sidebarOpen ? '' : ' shell--nosb')}>
        <Sidebar onRefresh={refreshHome} onHide={toggleSidebar} />
        {!sidebarOpen && (
          <button
            type="button"
            className="sb-edge"
            onClick={toggleSidebar}
            title={`Show sidebar (${SIDEBAR_KB})`}
            aria-label="Show sidebar"
          >
            <span className="sb-edge-glyph" aria-hidden="true"><IconSidebar /></span>
          </button>
        )}
        <main className="shell-main">
          <CompatBanner />
        <Routes>
          {/* Global project picker (unscoped) */}
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/new" element={<CreateProject />} />
          {/* Project-scoped surface */}
          <Route path="/p/:projectId" element={<ProjectScope />}>
            <Route index element={<Home />} />
            <Route path="feed" element={<Feed />} />
            <Route path="claims" element={<Claims />} />
            <Route path="claims/:claimId" element={<ClaimDetail />} />
            <Route path="litreview" element={<LitReview />} />
            <Route path="experiments" element={<Experiments />} />
            <Route path="experiments/:experimentId" element={<ExperimentDetail />} />
            <Route path="artifacts" element={<Artifacts />} />
            <Route path="artifacts/:artifactId" element={<Artifacts />} />
            <Route path="storage" element={<Storage />} />
            <Route path="storage/:objectId" element={<Storage />} />
            <Route path="reviews" element={<Reviews />} />
            <Route path="events" element={<Events />} />
            <Route path="sandboxes" element={<Sandboxes />} />
            <Route path="mlflow" element={<MlflowDashboard />} />
            <Route path="settings" element={<Settings />} />
            <Route path="activity" element={<Debug />} />
            <Route path="debug" element={<DebugRedirect />} />
          </Route>
          <Route path="/" element={<RootRedirect />} />
          <Route path="*" element={<RootRedirect />} />
        </Routes>
        </main>
      </div>
    </>
  );
}

function FullPageStatus({ children }) {
  return (
    <div className="page-stage" style={{ display: 'flex', alignItems: 'center', minHeight: '80vh' }}>
      <div className="empty-state" style={{ textAlign: 'left' }}>{children}</div>
    </div>
  );
}

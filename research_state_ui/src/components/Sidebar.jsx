import { useEffect, useState } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { useProjectStore, useProjectHref, selectStats, selectResources, selectSandboxes } from '../store/useProjectStore';
import { CLIENT_VERSION } from '../api';
import { useTheme } from '../store/useTheme';
import { setSurfaceOverride } from '../store/useViewport';
import ProjectSwitcher from './ProjectSwitcher';
import FileTree from './FileTree';
import SandboxRetentionIndicator from './SandboxRetentionIndicator';

function fmtUpdatedAgo(ms) {
  if (!ms) return 'never';
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

// Cycle order for the theme button: explicit choices first, then back to
// following the OS.
const NEXT_THEME_MODE = { light: 'dark', dark: 'system', system: 'light' };

// Platform-correct label for the sidebar toggle shortcut (also shown on the
// shell's reveal button, so exported).
export const SIDEBAR_KB = /Mac|iP/.test(navigator.platform || '') ? '⌘B' : 'Ctrl+B';

// The conventional toggle-sidebar glyph: a frame with the panel marked off.
// Same 24-grid stroke language as the mobile nav icons; used by both the
// brand-row hide button and the shell's edge-reveal.
export function IconSidebar(props) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.2" />
      <path d="M9.5 4.5v15" />
    </svg>
  );
}

export default function Sidebar({ onRefresh, onHide }) {
  const { mode: themeMode, theme, setMode: setThemeMode } = useTheme();
  const home = useProjectStore(s => s.home);
  const stats = useProjectStore(selectStats);
  const lastSyncedAt = useProjectStore(s => s.lastSyncedAt);
  const isPolling = useProjectStore(s => s.isPolling);
  const lastSyncError = useProjectStore(s => s.lastSyncError);
  const resources = useProjectStore(selectResources);
  const sandboxes = useProjectStore(selectSandboxes);
  const runningSandboxes = sandboxes.filter(s => s.status === 'running').length;
  // Central MLflow spans every experiment, so its entry point is project-level.
  // The /home payload's `mlflow` health block gates the nav row; the dedicated
  // page (/p/<id>/mlflow) renders the project's runs, curves, and embedded UI.
  const mlflowConfigured = home?.mlflow?.configured;
  const location = useLocation();
  const navigate = useNavigate();
  const px = useProjectHref();
  // Sidebar lives outside the <Routes> tree, so useParams() returns {}.
  // Parse the resourceId out of the path ourselves so deep-links highlight
  // the selected file in the tree. Paths are project-scoped: /p/<id>/resources/<rid>.
  const resMatch = location.pathname.match(/\/resources\/([^/]+)\/?$/);
  const resourceId = resMatch ? resMatch[1] : null;
  const onResourcesPath = /\/resources(\/|$)/.test(location.pathname);

  // Sidebar tree state: which top-level "drawer" sections are expanded.
  // For now only Resources is expandable, but the pattern leaves room for
  // similar nested sections later (e.g., Claims expanded into their files).
  const [resourcesOpen, setResourcesOpen] = useState(onResourcesPath);

  // Auto-open the Resources drawer whenever the user navigates into a
  // resource URL (e.g., from a deep link), so the file tree is visible.
  useEffect(() => {
    if (onResourcesPath) setResourcesOpen(true);
  }, [onResourcesPath]);

  const dotClass = lastSyncError ? 'sync-dot stale' : (isPolling ? 'sync-dot' : 'sync-dot paused');
  const pollLabel = lastSyncError ? 'stale' : (isPolling ? 'live' : 'paused');

  const resourcesCount = stats.resources ?? home?.resources?.length ?? 0;
  // Live backend version from the /api/meta handshake; fall back to the UI's
  // own build version before the first handshake lands.
  const serverVersion = useProjectStore(s => s.serverMeta?.server_version);

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div>
          Research State
          <small>research_plugin · v{serverVersion || CLIENT_VERSION}</small>
        </div>
        {onHide && (
          <button
            type="button"
            className="sidebar-hide"
            onClick={onHide}
            title={`Hide sidebar (${SIDEBAR_KB})`}
            aria-label="Hide sidebar"
          ><IconSidebar /></button>
        )}
      </div>

      <ProjectSwitcher />

      <nav className="sidebar-nav">
        <NavLink to={px('')} end className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          Home
        </NavLink>
        <NavLink to={px('/feed')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          Feed
        </NavLink>
        <NavLink to={px('/claims')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          <span>Claims</span>
          <span className="sidebar-link-count">{stats.claims ?? home?.claims?.length ?? 0}</span>
        </NavLink>
        <NavLink to={px('/experiments')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          <span>Experiments</span>
          <span className="sidebar-link-count">{stats.experiments ?? home?.experiments?.length ?? 0}</span>
        </NavLink>

        {/* Resources — expands inline; clicking does not change route.
            Chevron sits on the LEFT (VSCode-style) — `>` collapsed, `v`
            expanded, rendered as a single rotating glyph. */}
        <button
          type="button"
          className={`sidebar-link sidebar-link--expandable${onResourcesPath ? ' active' : ''}`}
          onClick={() => setResourcesOpen(v => !v)}
          aria-expanded={resourcesOpen}
        >
          <span
            className={`sidebar-link-chevron${resourcesOpen ? ' sidebar-link-chevron--open' : ''}`}
            aria-hidden="true"
          >▸</span>
          <span className="sidebar-link-label">Resources</span>
          <span className="sidebar-link-count">{resourcesCount}</span>
        </button>
        {resourcesOpen && (
          <div className="sidebar-subtree">
            {resources.length === 0 ? (
              <div className="sidebar-subtree-empty">No files registered.</div>
            ) : (
              <FileTree
                resources={resources}
                selectedId={resourceId || null}
                onSelect={(r) => navigate(px(`/resources/${r.id}`))}
              />
            )}
          </div>
        )}

        <NavLink to={px('/storage')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          Storage
        </NavLink>

        <NavLink to={px('/reviews')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          <span>Reviews</span>
          <span className="sidebar-link-count">{stats.open_reviews ?? stats.reviews ?? 0}</span>
        </NavLink>
        <NavLink to={px('/sandboxes')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          <span>Sandboxes</span>
          {runningSandboxes > 0 && (
            <span className="sidebar-link-count sidebar-link-count--live" title={`${runningSandboxes} running`}>
              <span className="sidebar-live-dot" />{runningSandboxes}
            </span>
          )}
        </NavLink>
        {mlflowConfigured && (
          <NavLink to={px('/mlflow')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
            MLflow
          </NavLink>
        )}
      </nav>

      <NavLink to={px('/visual/dag')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Logic DAG
      </NavLink>

      <div className="sidebar-section">Activity</div>
      <NavLink to={px('/events')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Events
      </NavLink>
      <NavLink to={px('/activity')} className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Traffic &amp; Tool I/O
      </NavLink>
      <NavLink to="/projects" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Projects
      </NavLink>

      <div className="sidebar-foot">
        <SandboxRetentionIndicator />
        <div className="sync-indicator">
          <span className={dotClass} />
          <span>ui {pollLabel} · updated {fmtUpdatedAgo(lastSyncedAt)}</span>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <button className="btn btn--ghost btn--sm" onClick={onRefresh}>Refresh now</button>
          <button
            className="btn btn--ghost btn--sm"
            onClick={() => setThemeMode(NEXT_THEME_MODE[themeMode])}
            title={`Theme follows ${themeMode === 'system' ? 'the OS' : 'your choice'} — click to switch to ${NEXT_THEME_MODE[themeMode]}`}
          >
            <span aria-hidden="true">{theme === 'dark' ? '◑' : '◐'}</span>
            {themeMode === 'system' ? `auto · ${theme}` : themeMode}
          </button>
          <button className="btn btn--ghost btn--sm" onClick={() => setSurfaceOverride('mobile')}>
            Switch to mobile
          </button>
        </div>
        {lastSyncError && <div className="error-message" style={{ fontSize: 11 }}>{lastSyncError}</div>}
      </div>
    </aside>
  );
}

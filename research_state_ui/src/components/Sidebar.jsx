import { useEffect, useState } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { useProjectStore, selectStats, selectResources, selectSandboxes } from '../store/useProjectStore';
import ProjectSwitcher from './ProjectSwitcher';
import FileTree from './FileTree';
import ExperimentSyncIndicator from './ExperimentSyncIndicator';

function fmtSyncedAgo(ms) {
  if (!ms) return 'never';
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

export default function Sidebar({ onRefresh }) {
  const home = useProjectStore(s => s.home);
  const stats = useProjectStore(selectStats);
  const lastSyncedAt = useProjectStore(s => s.lastSyncedAt);
  const isPolling = useProjectStore(s => s.isPolling);
  const lastSyncError = useProjectStore(s => s.lastSyncError);
  const resources = useProjectStore(selectResources);
  const sandboxes = useProjectStore(selectSandboxes);
  const runningSandboxes = sandboxes.filter(s => s.status === 'running').length;
  const location = useLocation();
  const navigate = useNavigate();
  // Sidebar lives outside the <Routes> tree, so useParams() returns {}.
  // Parse the resourceId out of the path ourselves so deep-links highlight
  // the selected file in the tree.
  const resMatch = location.pathname.match(/^\/resources\/(.+?)\/?$/);
  const resourceId = resMatch ? resMatch[1] : null;
  const onResourcesPath = location.pathname.startsWith('/resources');

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
  const syncLabel = lastSyncError ? 'stale' : (isPolling ? 'live' : 'paused');

  const resourcesCount = stats.resources ?? home?.resources?.length ?? 0;

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        Research State
        <small>research_plugin · v0.0001</small>
      </div>

      <ProjectSwitcher />

      <nav className="sidebar-nav">
        <NavLink to="/" end className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          Home
        </NavLink>
        <NavLink to="/claims" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          <span>Claims</span>
          <span className="sidebar-link-count">{stats.claims ?? home?.claims?.length ?? 0}</span>
        </NavLink>
        <NavLink to="/experiments" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
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
                onSelect={(r) => navigate(`/resources/${r.id}`)}
              />
            )}
          </div>
        )}

        <NavLink to="/reviews" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          <span>Reviews</span>
          <span className="sidebar-link-count">{stats.open_reviews ?? stats.reviews ?? 0}</span>
        </NavLink>
        <NavLink to="/sandboxes" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
          <span>Sandboxes</span>
          {runningSandboxes > 0 && (
            <span className="sidebar-link-count sidebar-link-count--live" title={`${runningSandboxes} running`}>
              <span className="sidebar-live-dot" />{runningSandboxes}
            </span>
          )}
        </NavLink>
      </nav>

      <NavLink to="/visual/dag" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Logic DAG
      </NavLink>

      <div className="sidebar-section">Activity</div>
      <NavLink to="/events" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Events
      </NavLink>
      <NavLink to="/activity" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Live traffic
      </NavLink>
      <NavLink to="/debug" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Tool I/O
      </NavLink>
      <NavLink to="/projects" className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}>
        Projects
      </NavLink>

      <div className="sidebar-foot">
        <ExperimentSyncIndicator />
        <div className="sync-indicator">
          <span className={dotClass} />
          <span>ui {syncLabel} · synced {fmtSyncedAgo(lastSyncedAt)}</span>
        </div>
        <button className="btn btn--ghost btn--sm" onClick={onRefresh}>Refresh now</button>
        {lastSyncError && <div className="error-message" style={{ fontSize: 11 }}>{lastSyncError}</div>}
      </div>
    </aside>
  );
}

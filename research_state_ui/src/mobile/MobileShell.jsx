import { useEffect, useState } from 'react';
import { NavLink, Link, useLocation } from 'react-router-dom';
import { useProjectStore, useProjectHref, selectStats, selectSandboxes } from '../store/useProjectStore';
import { useTheme } from '../store/useTheme';
import ProjectSwitcher from '../components/ProjectSwitcher';
import { setSurfaceOverride } from '../store/useViewport';
import BottomSheet from './BottomSheet';
import ToastHost from './Toast';
import { usePullToRefresh } from './usePullToRefresh';
import { IconFeed, IconHome, IconExperiments, IconActivity, IconMore } from './icons';

const NEXT_THEME_MODE = { light: 'dark', dark: 'system', system: 'light' };

function fmtSyncedAgo(ms, now) {
  if (!ms) return 'never';
  const s = Math.max(0, Math.floor((now - ms) / 1000));
  if (s < 5) return 'now';
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h`;
}

/**
 * Mobile app shell: top bar (project · freshness · theme), pull-to-refresh,
 * routed content, 5-tab bottom nav, and a More sheet hosting everything that
 * lives in the desktop sidebar. While mounted it tags <html
 * data-surface="mobile"> so mobile.css applies — desktop styling is untouched
 * by construction.
 */
export default function MobileShell({ children, onRefresh }) {
  const location = useLocation();
  const home = useProjectStore(s => s.home);
  const lastSyncedAt = useProjectStore(s => s.lastSyncedAt);
  const lastSyncError = useProjectStore(s => s.lastSyncError);
  const isPolling = useProjectStore(s => s.isPolling);
  const [sheetOpen, setSheetOpen] = useState(false);
  const { distance, refreshing } = usePullToRefresh(onRefresh);
  const px = useProjectHref();
  // 10s tick so the "synced Xs" label and staleness stay honest even when
  // polling has stopped delivering new store state (unreachable daemon).
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 10000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.surface = 'mobile';
    return () => { delete document.documentElement.dataset.surface; };
  }, []);

  // Any navigation closes the sheet.
  useEffect(() => { setSheetOpen(false); }, [location.pathname]);

  const projectName = home?.project?.name || 'Merv';
  const stale = lastSyncError || (lastSyncedAt && now - lastSyncedAt > 30000);
  const dotClass = stale ? 'sync-dot stale' : (isPolling ? 'sync-dot' : 'sync-dot paused');

  return (
    <div className="mshell">
      <header className="mbar">
        <div className="mbar-title">{projectName}</div>
        <div className="mbar-sync" aria-label={stale ? 'data stale' : 'data live'}>
          <span className={dotClass} />
          synced {fmtSyncedAgo(lastSyncedAt, now)}
        </div>
        <ThemeButton />
      </header>

      {(distance > 0 || refreshing) && (
        <div className="mptr" style={{ height: refreshing ? 44 : distance }} aria-hidden="true">
          <span className={`mptr-icon${refreshing ? ' is-spinning' : ''}`}>↻</span>
        </div>
      )}

      <main className="mshell-main">{children}</main>

      <nav className="mnav" aria-label="Primary">
        <NavLink to={px('')} end className={({ isActive }) => 'mnav-tab' + (isActive ? ' active' : '')}>
          <IconHome className="mnav-glyph" />
          Home
        </NavLink>
        <NavLink to={px('/feed')} className={({ isActive }) => 'mnav-tab' + (isActive ? ' active' : '')}>
          <IconFeed className="mnav-glyph" />
          Feed
        </NavLink>
        <NavLink to={px('/experiments')} className={({ isActive }) => 'mnav-tab' + (isActive ? ' active' : '')}>
          <IconExperiments className="mnav-glyph" />
          Exps
        </NavLink>
        <NavLink to={px('/mlflow')} className={({ isActive }) => 'mnav-tab' + (isActive ? ' active' : '')}>
          <IconActivity className="mnav-glyph" />
          MLflow
        </NavLink>
        <button
          type="button"
          className={'mnav-tab' + (sheetOpen ? ' active' : '')}
          onClick={() => setSheetOpen(v => !v)}
          aria-expanded={sheetOpen}
        >
          <IconMore className="mnav-glyph" />
          More
        </button>
      </nav>

      <MoreSheet open={sheetOpen} onClose={() => setSheetOpen(false)} onRefresh={onRefresh} />
      <ToastHost />
    </div>
  );
}

function ThemeButton() {
  const { mode, theme, setMode } = useTheme();
  return (
    <button
      type="button"
      className="mbar-btn"
      onClick={() => setMode(NEXT_THEME_MODE[mode])}
      aria-label={`Theme: ${mode}. Tap to switch.`}
    >
      <span aria-hidden="true">{theme === 'dark' ? '◑' : '◐'}</span>
    </button>
  );
}

function MoreSheet({ open, onClose, onRefresh }) {
  const stats = useProjectStore(selectStats);
  const home = useProjectStore(s => s.home);
  const lastSyncError = useProjectStore(s => s.lastSyncError);
  const sandboxes = useProjectStore(selectSandboxes);
  const runningSandboxes = sandboxes.filter(s => s.status === 'running').length;
  const px = useProjectHref();

  const footer = (
    <>
      <button type="button" className="btn btn--ghost btn--sm" onClick={() => { onRefresh?.(); onClose(); }}>
        Refresh now
      </button>
      <button type="button" className="btn btn--ghost btn--sm" onClick={() => setSurfaceOverride('desktop')}>
        Use desktop layout
      </button>
    </>
  );

  return (
    <BottomSheet open={open} onClose={onClose} label="More" footer={footer}>
      <ProjectSwitcher />

      <div className="msheet-section">Browse</div>
      <SheetLink to={px('/claims')} label="Claims" count={stats.claims ?? home?.claims?.length ?? 0} />
      <SheetLink to={px('/reviews')} label="Reviews" count={stats.open_reviews ?? stats.reviews ?? 0} />
      <SheetLink to={px('/litreview')} label="Lit Review" />
      <SheetLink to={px('/reflection')} label="Reflection" />
      <SheetLink to={px('/artifacts')} label="Artifacts" count={stats.resources ?? 0} />
      <SheetLink to={px('/storage')} label="Storage" />
      <SheetLink to={px('/sandboxes')} label="Sandboxes" count={runningSandboxes ? `${runningSandboxes} running` : null} />
      <SheetLink to="/projects" label="Projects" />

      <div className="msheet-section">Forensics</div>
      <SheetLink to={px('/events')} label="Events" />
      <SheetLink to={px('/activity')} label="Traffic & Tool I/O" />

      {lastSyncError && (
        <div className="error-message" style={{ marginTop: 10, fontSize: 11 }}>{lastSyncError}</div>
      )}
    </BottomSheet>
  );
}

function SheetLink({ to, label, count = null, note = null }) {
  return (
    <Link to={to} className="msheet-link">
      <span>{label}</span>
      <span className="msheet-count">
        {note && <span className="msheet-link-note">{note} </span>}
        {count != null && count !== 0 ? count : ''}
      </span>
    </Link>
  );
}

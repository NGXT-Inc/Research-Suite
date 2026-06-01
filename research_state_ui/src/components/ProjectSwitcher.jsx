import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useProjectStore } from '../store/useProjectStore';
import ObjId from './ObjId';

/**
 * Sidebar project chip + popover.
 *
 * Always visible (even with one project) so the multi-project nature of the
 * backend is discoverable. The popover lists projects, links to /projects,
 * and offers "+ New project".
 */
export default function ProjectSwitcher() {
  const navigate = useNavigate();
  const projects = useProjectStore(s => s.projects);
  const projectId = useProjectStore(s => s.projectId);
  const setProjectId = useProjectStore(s => s.setProjectId);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    function onClick(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false); }
    function onKey(e) { if (e.key === 'Escape') setOpen(false); }
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const current = projects.find(p => p.id === projectId);

  function pick(pid) {
    setOpen(false);
    if (pid === projectId) return;
    setProjectId(pid);
    refreshHome();
  }

  return (
    <div className="proj-switcher" ref={ref}>
      <button
        type="button"
        className="proj-chip"
        onClick={() => setOpen(v => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <div className="proj-chip-body">
          <div className="proj-chip-name">{current?.name || 'No project'}</div>
          {current && <ObjId id={current.id} className="proj-chip-id" />}
        </div>
        <span className={`proj-chip-caret${open ? ' open' : ''}`}>▾</span>
      </button>
      {open && (
        <div className="proj-pop" role="listbox">
          <div className="proj-pop-head">Switch project</div>
          <div className="proj-pop-list">
            {projects.length === 0 ? (
              <div className="proj-pop-empty">No projects yet.</div>
            ) : projects.map(p => (
              <button
                key={p.id}
                type="button"
                className={`proj-pop-row${p.id === projectId ? ' active' : ''}`}
                onClick={() => pick(p.id)}
              >
                <span className="proj-pop-row-tick">{p.id === projectId ? '●' : ''}</span>
                <span className="proj-pop-row-body">
                  <span className="proj-pop-row-name">{p.name || p.id}</span>
                  {p.summary && <span className="proj-pop-row-sum">{p.summary}</span>}
                </span>
                <ObjId id={p.id} />
              </button>
            ))}
          </div>
          <div className="proj-pop-foot">
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => { setOpen(false); navigate('/projects'); }}
            >
              Manage projects →
            </button>
            <button
              type="button"
              className="btn btn--sm btn--primary"
              onClick={() => { setOpen(false); navigate('/projects/new'); }}
            >
              + New project
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

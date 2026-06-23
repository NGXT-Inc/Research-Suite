import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useProjectStore, projectPath } from '../store/useProjectStore';
import ObjId from '../components/ObjId';

/**
 * Projects index — every project in the workspace, with switch + inline rename.
 * The active project is highlighted; the page is reachable from the sidebar
 * switcher's "Manage projects →".
 */
export default function Projects() {
  const navigate = useNavigate();
  const projects = useProjectStore(s => s.projects);
  const projectId = useProjectStore(s => s.projectId);
  const patchProject = useProjectStore(s => s.patchProject);
  const loadProjects = useProjectStore(s => s.loadProjects);

  // URL drives the active project; <ProjectScope> mirrors it into the store.
  function switchTo(pid) {
    navigate(projectPath(pid));
  }

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-head-row">
          <div>
            {/* "Projects" already labels the active sidebar nav item — lead with
                the description instead of echoing it. */}
            <p className="page-summary page-summary--lead">Every project on this backend.</p>
          </div>
          <div className="page-actions">
            <button className="btn btn--ghost" onClick={() => loadProjects()}>Refresh</button>
            <Link className="btn btn--primary" to="/projects/new">+ New project</Link>
          </div>
        </div>
      </header>

      {projects.length === 0 ? (
        <div className="empty-state">
          <h2>No projects yet</h2>
          <div style={{ marginTop: 18 }}>
            <Link className="btn btn--primary" to="/projects/new">+ New project</Link>
          </div>
        </div>
      ) : (
        <div className="stack stack--lg">
          {projects.map(p => (
            <ProjectCard
              key={p.id}
              project={p}
              isActive={p.id === projectId}
              onSwitch={() => switchTo(p.id)}
              onRename={(name, summary) => patchProject(p.id, { name, summary })}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ProjectCard({ project, isActive, onSwitch, onRename }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(project.name || '');
  const [summary, setSummary] = useState(project.summary || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function save(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onRename(name.trim(), summary.trim());
      setEditing(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function cancel() {
    setName(project.name || '');
    setSummary(project.summary || '');
    setError(null);
    setEditing(false);
  }

  function startEditing() {
    setName(project.name || '');
    setSummary(project.summary || '');
    setError(null);
    setEditing(true);
  }

  return (
    <div className={`proj-card${isActive ? ' proj-card--active' : ''}`}>
      {editing ? (
        <form onSubmit={save} className="stack stack--sm">
          <div className="form-row">
            <label className="label">Name</label>
            <input className="input" value={name} onChange={e => setName(e.target.value)} autoFocus required />
          </div>
          <div className="form-row">
            <label className="label">Summary</label>
            <textarea className="textarea" value={summary} onChange={e => setSummary(e.target.value)} />
          </div>
          {error && <div className="error-message">{error}</div>}
          <div className="form-actions">
            <button type="button" className="btn btn--ghost btn--sm" onClick={cancel}>Cancel</button>
            <button type="submit" className="btn btn--primary btn--sm" disabled={busy || !name.trim()}>
              {busy ? 'Saving…' : 'Save'}
            </button>
          </div>
        </form>
      ) : (
        <>
          <div className="cluster--between" style={{ alignItems: 'flex-start' }}>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="cluster" style={{ marginBottom: 4 }}>
                <h2 className="proj-card-name">{project.name || 'Untitled'}</h2>
                {isActive && <span className="proj-active-tag">Active</span>}
              </div>
              {project.summary
                ? <p className="proj-card-sum">{project.summary}</p>
                : <p className="proj-card-sum faint">No summary yet.</p>}
              {project.repo_root && (
                <div className="mono" style={{ marginTop: 8, fontSize: 'var(--text-xs)', color: 'var(--faint)', overflowWrap: 'anywhere' }}>
                  {project.repo_root}
                </div>
              )}
              <div className="cluster" style={{ marginTop: 10, fontSize: 'var(--text-xs)', color: 'var(--faint)' }}>
                <ObjId id={project.id} strong />
                {project.created_at && <span className="mono">· created {fmtDate(project.created_at)}</span>}
              </div>
            </div>
            <div className="cluster" style={{ flexShrink: 0 }}>
              <button className="btn btn--sm btn--ghost" onClick={startEditing}>Edit</button>
              {isActive
                ? <Link to={projectPath(project.id)} className="btn btn--sm">Open →</Link>
                : <button className="btn btn--sm btn--primary" onClick={onSwitch}>Switch →</button>}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function fmtDate(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
  } catch { return iso; }
}

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useProjectStore } from '../store/useProjectStore';

/**
 * CreateProject — usable both as bootstrap (no projects yet) and as an
 * in-shell route at /projects/new.
 *
 * Props:
 *   bootstrap: bool. When true, omits the "← Projects" back link and the
 *              sidebar isn't there anyway; the layout adapts.
 */
export default function CreateProject({ bootstrap = false }) {
  const navigate = useNavigate();
  const createProject = useProjectStore(s => s.createProject);
  const projects = useProjectStore(s => s.projects);
  const [name, setName] = useState('');
  const [summary, setSummary] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await createProject({ name: name.trim(), summary: summary.trim() });
      navigate('/');
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page-stage" style={{ maxWidth: 560 }}>
      <header className="page-header page-header--lg">
        <div className="page-eyebrow">
          {!bootstrap && <Link to="/projects">Projects</Link>}
          {!bootstrap && ' · '}
          {bootstrap ? 'Research State · research_plugin v0.0001' : 'New'}
        </div>
        <h1 className="page-title">{bootstrap ? 'No project yet' : 'New project'}</h1>
        <p className="page-summary">
          {bootstrap
            ? <>Create the first project to start tracking claims, experiments, resources, and review gates. Projects are local to this repo and stored in SQLite at <code className="mono">.research_plugin/state.sqlite</code>.</>
            : <>Add another research project to this workspace. Each project has its own claims, experiments, resources, and review history.</>}
        </p>
      </header>

      <form className="form-card" onSubmit={submit}>
        <div className="form-row">
          <label className="label" htmlFor="proj-name">Name</label>
          <input
            id="proj-name"
            className="input"
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="Toy Length Classifier"
            autoFocus
            required
          />
        </div>
        <div className="form-row">
          <label className="label" htmlFor="proj-summary">Summary</label>
          <textarea
            id="proj-summary"
            className="textarea"
            value={summary}
            onChange={e => setSummary(e.target.value)}
            placeholder="What is this project about? (optional)"
          />
        </div>
        {error && <div className="error-message">{error}</div>}
        <div className="form-actions">
          {!bootstrap && (
            <Link to="/projects" className="btn btn--ghost">Cancel</Link>
          )}
          <button type="submit" className="btn btn--primary" disabled={busy || !name.trim()}>
            {busy ? 'Creating…' : 'Create project'}
          </button>
        </div>
      </form>

      {!bootstrap && projects.length > 0 && (
        <p className="faint" style={{ marginTop: 18, fontSize: 'var(--text-xs)' }}>
          You have {projects.length} project{projects.length === 1 ? '' : 's'} already.
          Use the sidebar to switch between them.
        </p>
      )}
    </div>
  );
}

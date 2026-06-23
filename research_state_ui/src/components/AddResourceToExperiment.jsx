import { useMemo, useState } from 'react';
import { api } from '../api';
import { useProjectStore, selectHasLocalDataPlaneHttp } from '../store/useProjectStore';

const KINDS = ['plan', 'code', 'config', 'input', 'dataset', 'result', 'note', 'model', 'other'];
const ROLES = ['plan', 'code', 'config', 'input', 'result', 'report', 'note', 'model'];

/**
 * Two flows for getting a resource into an experiment:
 *   - register:  enter a repo-relative path + role → POST /resources, then
 *                POST /resources/{id}/associate.
 *   - existing:  pick an already-registered resource not yet associated with
 *                this experiment (in any role on the current attempt).
 *
 * The role defaults smartly based on the workflow's next_action so the user
 * usually doesn't need to change it.
 */
export default function AddResourceToExperiment({
  projectId,
  experimentId,
  attemptIndex,
  currentResources,
  allResources,
  defaultRole = 'input',
  onCancel,
  onDone,
}) {
  const [mode, setMode] = useState('register'); // 'register' | 'existing'
  const hasLocalDataPlane = useProjectStore(selectHasLocalDataPlaneHttp);

  // Resources already associated with this attempt — we hide them from "existing".
  const alreadyAssociated = useMemo(() => {
    const seen = new Set();
    for (const r of currentResources || []) {
      if (r.association_attempt_index === attemptIndex) seen.add(r.id);
    }
    return seen;
  }, [currentResources, attemptIndex]);

  const candidates = useMemo(() => {
    return (allResources || []).filter(r => !alreadyAssociated.has(r.id));
  }, [allResources, alreadyAssociated]);

  if (!hasLocalDataPlane) {
    return (
      <div className="form-card" style={{ marginBottom: 14 }}>
        <div className="empty">Registration unavailable in this mode.</div>
        <div className="form-actions" style={{ marginTop: 10 }}>
          <button type="button" className="btn btn--ghost btn--sm" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    );
  }

  return (
    <div className="form-card" style={{ marginBottom: 14 }}>
      <div className="tab-row" style={{ marginBottom: 12 }}>
        <button type="button" className={`tab${mode === 'register' ? ' active' : ''}`} onClick={() => setMode('register')}>
          Register new file
        </button>
        <button type="button" className={`tab${mode === 'existing' ? ' active' : ''}`} onClick={() => setMode('existing')}>
          Use existing
          <span className="tab-count">{candidates.length}</span>
        </button>
      </div>
      {mode === 'register' ? (
        <RegisterAndAssociate
          projectId={projectId}
          experimentId={experimentId}
          defaultRole={defaultRole}
          onCancel={onCancel}
          onDone={onDone}
        />
      ) : (
        <AssociateExisting
          projectId={projectId}
          experimentId={experimentId}
          candidates={candidates}
          defaultRole={defaultRole}
          onCancel={onCancel}
          onDone={onDone}
        />
      )}
    </div>
  );
}

function RegisterAndAssociate({ projectId, experimentId, defaultRole, onCancel, onDone }) {
  const [path, setPath] = useState('');
  const [kind, setKind] = useState(defaultRole === 'note' ? 'note' : defaultRole === 'plan' ? 'plan' : 'result');
  const [role, setRole] = useState(defaultRole);
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    if (!path.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const registered = await api.registerResource(projectId, {
        path: path.trim(),
        kind,
        title: title.trim() || undefined,
      });
      const resourceRow = registered.resource || registered;
      const rid = resourceRow.id;
      if (!rid) throw new Error('registration did not return an id');
      await api.associateResource(projectId, rid, {
        target_type: 'experiment',
        target_id: experimentId,
        role,
      });
      if (onDone) await onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <div className="form-row">
        <label className="label">Path (repo-relative)</label>
        <input
          className="input mono"
          value={path}
          onChange={e => setPath(e.target.value)}
          placeholder="experiments/e001/results.json"
          autoFocus
          required
        />
      </div>
      <div className="cluster" style={{ gap: 12, flexWrap: 'wrap' }}>
        <div className="form-row" style={{ flex: 1, minWidth: 160 }}>
          <label className="label">Role</label>
          <select className="select" value={role} onChange={e => setRole(e.target.value)}>
            {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
        <div className="form-row" style={{ flex: 1, minWidth: 160 }}>
          <label className="label">Kind</label>
          <select className="select" value={kind} onChange={e => setKind(e.target.value)}>
            {KINDS.map(k => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
      </div>
      <div className="form-row">
        <label className="label">Title (optional)</label>
        <input className="input" value={title} onChange={e => setTitle(e.target.value)} placeholder="Attempt 3 results" />
      </div>
      {error && <div className="error-message">{error}</div>}
      <div className="form-actions">
        <button type="button" className="btn btn--ghost btn--sm" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn--primary btn--sm" disabled={busy || !path.trim()}>
          {busy ? 'Registering…' : 'Register & associate'}
        </button>
      </div>
    </form>
  );
}

function AssociateExisting({ projectId, experimentId, candidates, defaultRole, onCancel, onDone }) {
  const [resourceId, setResourceId] = useState(candidates[0]?.id || '');
  const [role, setRole] = useState(defaultRole);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    if (!resourceId) return;
    setBusy(true);
    setError(null);
    try {
      await api.associateResource(projectId, resourceId, {
        target_type: 'experiment',
        target_id: experimentId,
        role,
      });
      if (onDone) await onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (candidates.length === 0) {
    return (
      <div className="empty">
        No unassociated resources.
        <div className="form-actions" style={{ marginTop: 10 }}>
          <button type="button" className="btn btn--ghost btn--sm" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={submit}>
      <div className="form-row">
        <label className="label">Resource</label>
        <select className="select mono" value={resourceId} onChange={e => setResourceId(e.target.value)}>
          {candidates.map(r => (
            <option key={r.id} value={r.id}>
              {r.path} {r.kind ? `· ${r.kind}` : ''}
            </option>
          ))}
        </select>
      </div>
      <div className="form-row">
        <label className="label">Role</label>
        <select className="select" value={role} onChange={e => setRole(e.target.value)}>
          {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>
      {error && <div className="error-message">{error}</div>}
      <div className="form-actions">
        <button type="button" className="btn btn--ghost btn--sm" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn--primary btn--sm" disabled={busy || !resourceId}>
          {busy ? 'Associating…' : 'Associate'}
        </button>
      </div>
    </form>
  );
}

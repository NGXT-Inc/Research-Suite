import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useProjectStore, selectResources, selectExperiments } from '../store/useProjectStore';
import { api } from '../api';
import ObjId from '../components/ObjId';
import ResourceContentView from '../components/ResourceContentView';
import { formatBytes } from '../utils/format';

const KINDS = ['plan', 'code', 'config', 'input', 'dataset', 'result', 'note', 'model', 'other'];
const ROLES = ['plan', 'code', 'config', 'input', 'result', 'note', 'model'];

export default function Resources() {
  const { resourceId } = useParams();
  const navigate = useNavigate();
  const projectId = useProjectStore(s => s.projectId);
  const refreshHome = useProjectStore(s => s.refreshHome);
  const resources = useProjectStore(selectResources);
  const experiments = useProjectStore(selectExperiments);

  const [showRegister, setShowRegister] = useState(false);

  const selected = useMemo(
    () => (resourceId ? resources.find(r => r.id === resourceId) : null) || null,
    [resources, resourceId],
  );

  return (
    <div className="page-stage page-stage--explorer">
      {selected ? (
        <PreviewPanel
          projectId={projectId}
          resource={selected}
          experiments={experiments}
          onAssociated={refreshHome}
          onDeleted={async () => {
            await refreshHome();
            navigate('/resources');
          }}
        />
      ) : (
        <div className="page-stage--explorer-empty">
          <header className="page-header page-header--lg">
            <div className="page-head-row">
              <div>
                <div className="page-eyebrow">Resources</div>
                <h1 className="page-title">Files we use or produce</h1>
                <p className="page-summary">
                  Pick a file from the sidebar to preview it here. A resource is a regular file in
                  the local repo — the backend stores a pointer + observed version token
                  (<span className="mono">path + mtime_ns + size_bytes</span>) and serves content
                  directly from disk.
                </p>
              </div>
              <div className="page-actions">
                <button className="btn btn--primary" onClick={() => setShowRegister(v => !v)}>
                  {showRegister ? 'Cancel' : 'Register file'}
                </button>
              </div>
            </div>
          </header>

          {showRegister && (
            <RegisterForm
              projectId={projectId}
              onCancel={() => setShowRegister(false)}
              onCreated={async () => { setShowRegister(false); await refreshHome(); }}
            />
          )}

          {resources.length === 0 ? (
            <div className="empty-state">
              <h2>No resources registered yet</h2>
              <p>Register a repo file to associate it with experiments and reviews.</p>
            </div>
          ) : (
            <div className="explorer-hint">
              <div className="explorer-hint-title">Select a file from the sidebar</div>
              <div className="explorer-hint-sub">
                {resources.length} {resources.length === 1 ? 'file is' : 'files are'} registered.
                The folder tree on the left lets you drill in and open any of them in this panel.
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function PreviewPanel({ projectId, resource, experiments, onAssociated, onDeleted }) {
  const [associating, setAssociating] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState(null);

  useEffect(() => {
    setAssociating(false);
    setDetailsOpen(false);
    setDeleteBusy(false);
    setDeleteError(null);
  }, [resource.id]);

  async function deleteResource() {
    if (!window.confirm(`Delete resource registration for ${resource.path}?`)) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await api.deleteResource(projectId, resource.id);
      await onDeleted();
    } catch (err) {
      setDeleteError(err.message);
      setDeleteBusy(false);
    }
  }

  return (
    <div className="file-view">
      <header className="file-strip">
        <button
          type="button"
          className="file-strip-head"
          onClick={() => setDetailsOpen(v => !v)}
          aria-expanded={detailsOpen}
        >
          <span className="file-strip-path mono">{resource.path}</span>
          {resource.size_bytes != null && (
            <span className="file-strip-size">{formatBytes(resource.size_bytes)}</span>
          )}
          {resource.missing ? <span className="ft-tag ft-tag--missing">missing</span> : null}
          <span className="file-strip-twist" aria-hidden="true">{detailsOpen ? '▾' : '▸'}</span>
        </button>
        {detailsOpen && (
          <div className="file-strip-details">
            <div className="file-strip-meta">
              <ObjId id={resource.id} />
              {resource.kind && <span>kind: <span className="mono">{resource.kind}</span></span>}
              {resource.git_commit && <span>git <span className="mono">{String(resource.git_commit).slice(0, 7)}</span></span>}
            </div>
            {resource.title && <div className="file-strip-title">{resource.title}</div>}
            <div className="file-strip-actions">
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => setAssociating(v => !v)}
              >
                {associating ? 'Cancel' : 'Associate with experiment'}
              </button>
              <a
                className="btn btn--sm btn--ghost"
                href={api.resourceFileUrl(projectId, resource.id)}
                target="_blank"
                rel="noreferrer"
              >
                Open raw
              </a>
              <button
                type="button"
                className="btn btn--sm btn--danger"
                disabled={deleteBusy}
                onClick={deleteResource}
              >
                {deleteBusy ? 'Deleting…' : 'Delete'}
              </button>
            </div>
            {deleteError && <div className="error-message">{deleteError}</div>}
            {associating && (
              <div className="file-strip-associate">
                <AssociateForm
                  projectId={projectId}
                  resourceId={resource.id}
                  experiments={experiments}
                  onCancel={() => setAssociating(false)}
                  onDone={async () => { setAssociating(false); await onAssociated(); }}
                />
              </div>
            )}
            <VersionHistory
              projectId={projectId}
              resourceId={resource.id}
              currentVersionId={resource.current_version_id}
            />
          </div>
        )}
      </header>
      <div className="file-body">
        <ResourceContentView
          projectId={projectId}
          resourceId={resource.id}
          size={resource.size_bytes}
          path={resource.path}
        />
      </div>
    </div>
  );
}

function VersionHistory({ projectId, resourceId, currentVersionId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    if (data) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.getResourceHistory(projectId, resourceId)
      .then(d => { if (!cancelled) setData(d); })
      .catch(e => { if (!cancelled) setError(e.message); })
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [open, projectId, resourceId, data]);

  // Reset cached history when the user navigates to a different resource.
  useEffect(() => {
    setData(null);
    setOpen(false);
  }, [resourceId]);

  const versions = (data?.versions || []).slice().sort((a, b) =>
    (b.observed_at || '').localeCompare(a.observed_at || ''),
  );

  return (
    <div className="version-history">
      <button
        type="button"
        className="version-history-toggle"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        <span className="version-history-twist" aria-hidden="true">{open ? '▾' : '▸'}</span>
        Version history
        {data && <span className="version-history-count">{versions.length}</span>}
      </button>
      {open && (
        <div className="version-history-body">
          {loading && <div className="empty">Loading…</div>}
          {error && <div className="error-message">{error}</div>}
          {!loading && !error && versions.length === 0 && (
            <div className="empty">No versions recorded yet.</div>
          )}
          {!loading && !error && versions.map(v => {
            const isCurrent = v.id === currentVersionId;
            return (
              <div key={v.id} className="version-row-wrap">
                <div className={`version-row${isCurrent ? ' is-viewing' : ''}`}>
                  <div className="version-row-main">
                    <div className="version-row-line">
                      <span className="mono version-row-id">{v.id.slice(0, 16)}…</span>
                      {isCurrent && <span className="version-row-tag version-row-tag--current">current</span>}
                    </div>
                    <div className="version-row-sub">
                      {formatObserved(v.observed_at)}
                      {' · '}
                      {formatBytes(v.size_bytes)}
                      {Array.isArray(v.associations) && v.associations.length > 0 && (
                        <> · {associationsSummary(v.associations)}</>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function formatObserved(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch { return iso; }
}

function associationsSummary(assocs) {
  // Compact rollup like "attempt 5 (plan)" or "attempts 4–5".
  const labels = assocs.map(a => {
    const role = a.role ? ` (${a.role})` : '';
    if (a.target_type === 'experiment' && a.attempt_index != null) {
      return `attempt ${a.attempt_index}${role}`;
    }
    return a.target_type || 'assoc';
  });
  return labels.join(', ');
}

function RegisterForm({ projectId, onCancel, onCreated }) {
  const [path, setPath] = useState('');
  const [kind, setKind] = useState('result');
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    if (!path.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.registerResource(projectId, { path: path.trim(), kind, title: title.trim() || undefined });
      onCreated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-card" onSubmit={submit} style={{ marginBottom: 18 }}>
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
      <div className="form-row">
        <label className="label">Kind</label>
        <select className="select" value={kind} onChange={e => setKind(e.target.value)}>
          {KINDS.map(k => <option key={k} value={k}>{k}</option>)}
        </select>
      </div>
      <div className="form-row">
        <label className="label">Title (optional)</label>
        <input className="input" value={title} onChange={e => setTitle(e.target.value)} placeholder="Attempt 3 results" />
      </div>
      {error && <div className="error-message">{error}</div>}
      <div className="form-actions">
        <button type="button" className="btn btn--ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn--primary" disabled={busy || !path.trim()}>
          {busy ? 'Registering…' : 'Register'}
        </button>
      </div>
    </form>
  );
}

function AssociateForm({ projectId, resourceId, experiments, onCancel, onDone }) {
  const [targetId, setTargetId] = useState(experiments[0]?.id || '');
  const [role, setRole] = useState('result');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    if (!targetId) return;
    setBusy(true);
    setError(null);
    try {
      await api.associateResource(projectId, resourceId, {
        target_type: 'experiment',
        target_id: targetId,
        role,
      });
      onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (experiments.length === 0) {
    return <div className="empty">Create an experiment first to associate this resource.</div>;
  }

  return (
    <form onSubmit={submit} className="cluster" style={{ flexWrap: 'wrap', gap: 8 }}>
      <select className="select" value={targetId} onChange={e => setTargetId(e.target.value)} style={{ minWidth: 280, flex: 1 }}>
        {experiments.map(e => <option key={e.id} value={e.id}>{e.id} — {e.intent.slice(0, 70)}</option>)}
      </select>
      <select className="select" value={role} onChange={e => setRole(e.target.value)} style={{ width: 140 }}>
        {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
      </select>
      <button type="submit" className="btn btn--primary btn--sm" disabled={busy}>
        {busy ? '…' : 'Associate'}
      </button>
      <button type="button" className="btn btn--ghost btn--sm" onClick={onCancel}>Cancel</button>
      {error && <div className="error-message" style={{ width: '100%' }}>{error}</div>}
    </form>
  );
}


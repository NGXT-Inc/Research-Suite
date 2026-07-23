import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useProjectStore, useProjectHref, selectExperiments } from '../store/useProjectStore';
import { api } from '../api';
import ArtifactContentView from '../components/ArtifactContentView';
import ObjId from '../components/ObjId';
import { formatBytes } from '../utils/format';
import { expName } from '../utils/experiment';

const basename = (p) => (p || '').split('/').filter(Boolean).pop() || p || '';

/**
 * Mobile Artifacts: the same flat per-target ledger as desktop, restacked as
 * tappable card rows; tapping opens the artifact as pure content.
 */
export default function MobileArtifacts() {
  const { artifactId } = useParams();
  const px = useProjectHref();
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);

  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const fetchArtifacts = useCallback(async () => {
    try {
      const d = await api.listArtifacts(projectId);
      setData(prev => (JSON.stringify(prev) === JSON.stringify(d) ? prev : d));
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [projectId]);

  useEffect(() => {
    setData(null);
    fetchArtifacts();
  }, [fetchArtifacts]);

  // Pending rows are half-born (upload token outstanding); show complete only.
  const artifacts = useMemo(
    () => (data?.artifacts || []).filter(a => a.status === 'complete'),
    [data],
  );
  const selected = artifactId ? artifacts.find(a => a.id === artifactId) : null;

  if (selected) {
    return (
      <div className="page-stage">
        <header className="page-header">
          <div className="page-eyebrow">
            <Link to={px('/artifacts')}>‹ Artifacts</Link>
            {' · '}<span className="mono">{selected.role}</span>
          </div>
          <h1 className="page-title">{selected.title || basename(selected.path)}</h1>
        </header>
        <div className="mcard" style={{ marginBottom: 14 }}>
          <div className="mcard-meta">
            <span><ObjId id={selected.id} /></span>
            {selected.size_bytes != null && <span>{formatBytes(selected.size_bytes)}</span>}
          </div>
        </div>
        <ArtifactContentView
          projectId={projectId}
          artifactId={selected.id}
          size={selected.size_bytes}
          path={selected.path}
        />
      </div>
    );
  }

  // Group by target, experiments first in home order.
  const expOrder = new Map(experiments.map((e, i) => [e.id, i]));
  const groups = new Map();
  for (const a of artifacts) {
    const key = `${a.target_type}:${a.target_id}`;
    if (!groups.has(key)) groups.set(key, { target_type: a.target_type, target_id: a.target_id, rows: [] });
    groups.get(key).rows.push(a);
  }
  const rank = (g) => g.target_type === 'experiment' ? (expOrder.get(g.target_id) ?? 1e6)
    : g.target_type === 'reflection' ? 2e6 : 3e6;
  const ordered = Array.from(groups.values()).sort((a, b) => rank(a) - rank(b));

  return (
    <div className="page-stage">
      <header className="page-header">
        <h1 className="page-title">Artifacts</h1>
      </header>

      {error && <div className="error-message">{error}</div>}
      {!data && !error && <div className="mquiet">loading…</div>}
      {data && artifacts.length === 0 && (
        <div className="empty-state empty-state--compact">
          <p>No artifacts submitted yet.</p>
        </div>
      )}

      {ordered.map(g => {
        const e = g.target_type === 'experiment' ? experiments.find(x => x.id === g.target_id) : null;
        const heading = e ? expName(e)
          : `${(g.target_type || 'target').replace(/_/g, ' ')} ${g.target_id}`;
        return (
          <section key={`${g.target_type}:${g.target_id}`} style={{ marginBottom: 16 }}>
            <div className="mml">{heading}</div>
            <div className="mcard-list">
              {g.rows.map(a => (
                <Link key={a.id} to={px(`/artifacts/${a.id}`)} className="mcard">
                  <div className="mcard-title">
                    <span className="artifact-role mono">{a.role}</span>
                    {a.title || basename(a.path)}
                  </div>
                  <div className="mcard-meta">
                    {a.attempt_index != null && <span>attempt {a.attempt_index}</span>}
                    {a.lens_id && <span>lens {a.lens_id}</span>}
                    {a.size_bytes != null && <span>{formatBytes(a.size_bytes)}</span>}
                  </div>
                </Link>
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

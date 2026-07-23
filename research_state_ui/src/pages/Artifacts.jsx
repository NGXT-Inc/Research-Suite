import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useProjectStore, useProjectHref, selectExperiments } from '../store/useProjectStore';
import { api } from '../api';
import { RawLink } from '../components/AuthedMedia';
import ObjId from '../components/ObjId';
import ArtifactContentView from '../components/ArtifactContentView';
import { formatBytes, fmtStamp } from '../utils/format';
import { expName } from '../utils/experiment';

/**
 * Artifacts — the flat ledger of what the agents submitted.
 *
 * One list, grouped by workflow target (experiment / reflection / …); each row
 * is role + title + size + time. No tree, no folders: submission is agent-only
 * (backend-mandated typed artifacts), so this page only shows and opens them.
 */

const basename = (p) => (p || '').split('/').filter(Boolean).pop() || p || '';

// Group order mirrors the workflow's gravity: experiments first (home order),
// then reflections, then anything else, each keyed `${target_type}:${target_id}`.
function groupByTarget(artifacts, experiments) {
  const expOrder = new Map(experiments.map((e, i) => [e.id, i]));
  const groups = new Map();
  for (const a of artifacts) {
    const key = `${a.target_type}:${a.target_id}`;
    if (!groups.has(key)) groups.set(key, { target_type: a.target_type, target_id: a.target_id, rows: [] });
    groups.get(key).rows.push(a);
  }
  const rank = (g) => {
    if (g.target_type === 'experiment') return expOrder.get(g.target_id) ?? 1e6;
    if (g.target_type === 'reflection') return 2e6;
    return 3e6;
  };
  const out = Array.from(groups.values()).sort((a, b) => rank(a) - rank(b));
  for (const g of out) {
    g.rows.sort((a, b) =>
      (b.attempt_index ?? 0) - (a.attempt_index ?? 0)
      || (a.role || '').localeCompare(b.role || '')
      || (a.created_at || '').localeCompare(b.created_at || ''));
  }
  return out;
}

function targetHeading(group, experiments, px) {
  if (group.target_type === 'experiment') {
    const e = experiments.find(x => x.id === group.target_id);
    return (
      <Link to={px(`/experiments/${group.target_id}`)} className="artifact-group-link">
        {e ? expName(e) : group.target_id}
      </Link>
    );
  }
  const label = (group.target_type || 'target').replace(/_/g, ' ');
  return <>{label} <span className="mono">{group.target_id}</span></>;
}

export default function Artifacts() {
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
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') fetchArtifacts();
    }, 10000);
    return () => clearInterval(t);
  }, [fetchArtifacts]);

  // Pending rows are half-born (upload token outstanding); show complete only.
  const artifacts = useMemo(
    () => (data?.artifacts || []).filter(a => a.status === 'complete'),
    [data],
  );
  const groups = useMemo(() => groupByTarget(artifacts, experiments), [artifacts, experiments]);
  const selected = artifactId ? artifacts.find(a => a.id === artifactId) : null;

  if (selected) {
    return (
      <div className="page-stage">
        <ArtifactViewer projectId={projectId} artifact={selected} px={px} />
      </div>
    );
  }

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <h1 className="page-title">Artifacts</h1>
        <p className="page-summary">What the agents submitted, by experiment and reflection.</p>
      </header>

      {error && <div className="error-message">{error}</div>}

      {!error && data && artifacts.length === 0 && (
        <div className="empty-state">
          <h2>No artifacts submitted yet</h2>
        </div>
      )}
      {!data && !error && <div className="empty">Loading…</div>}

      {groups.map(g => (
        <section key={`${g.target_type}:${g.target_id}`} className="artifact-group">
          <div className="outcomes-subhead">{targetHeading(g, experiments, px)}</div>
          <div className="list card card--flush">
            {g.rows.map(a => (
              <Link
                key={a.id}
                to={px(`/artifacts/${a.id}`)}
                className="list-row list-row--link"
              >
                <div className="list-row-main">
                  <div className="res-path">
                    <span className="artifact-role mono">{a.role}</span>
                    {a.title || basename(a.path)}
                  </div>
                  <div className="list-row-sub">
                    {a.attempt_index != null && <>attempt {a.attempt_index} · </>}
                    {a.lens_id && <>lens {a.lens_id} · </>}
                    {formatBytes(a.size_bytes)}
                    {a.created_at && <> · {fmtStamp(Date.parse(a.created_at))}</>}
                  </div>
                </div>
                <div className="list-row-aside">
                  <span className="twist" aria-hidden="true">▸</span>
                </div>
              </Link>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function ArtifactViewer({ projectId, artifact, px }) {
  return (
    <div className="file-view">
      <header className="page-header">
        <div className="page-eyebrow">
          <Link to={px('/artifacts')}>Artifacts</Link>
          {' · '}<span className="mono">{artifact.role}</span>
        </div>
        <h1 className="page-title">{artifact.title || basename(artifact.path)}</h1>
        <div className="list-row-sub" style={{ marginTop: 4 }}>
          <ObjId id={artifact.id} />
          {artifact.path && <> · <span className="mono">{artifact.path}</span></>}
          {artifact.size_bytes != null && <> · {formatBytes(artifact.size_bytes)}</>}
          {' · '}
          <RawLink href={api.artifactFileUrl(projectId, artifact.id)}>Open raw</RawLink>
        </div>
      </header>
      <div className="file-body">
        <ArtifactContentView
          projectId={projectId}
          artifactId={artifact.id}
          size={artifact.size_bytes}
          path={artifact.path}
        />
      </div>
    </div>
  );
}

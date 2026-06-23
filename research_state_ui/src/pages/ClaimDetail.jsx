import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api';
import { useProjectStore, selectExperiments, useProjectHref } from '../store/useProjectStore';
import ObjId from '../components/ObjId';
import StatusPill from '../components/StatusPill';
import KvList from '../components/KvList';
import { expName } from '../utils/experiment';

export default function ClaimDetail() {
  const { claimId } = useParams();
  const px = useProjectHref();
  const projectId = useProjectStore(s => s.projectId);
  const experiments = useProjectStore(selectExperiments);
  const [claim, setClaim] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setClaim(null);
    setError(null);
    api.getClaim(projectId, claimId)
      .then(c => !cancelled && setClaim(c))
      .catch(err => !cancelled && setError(err.message));
    return () => { cancelled = true; };
  }, [projectId, claimId]);

  const linkedExperiments = experiments.filter(e =>
    Array.isArray(e.tested_claims) && e.tested_claims.some(c => c.id === claimId),
  );

  if (error) {
    return (
      <div className="page-stage">
        <div className="error-message">{error}</div>
        <Link className="btn" to={px('/claims')} style={{ marginTop: 12 }}>← Claims</Link>
      </div>
    );
  }
  if (!claim) {
    return <div className="page-stage"><div className="empty">Loading…</div></div>;
  }

  return (
    <div className="page-stage">
      <header className="page-header page-header--lg">
        <div className="page-eyebrow">
          <Link to={px('/claims')}>Claims</Link> · <ObjId id={claim.id} className="page-eyebrow-id" />
        </div>
        <h1 className="page-title page-title--statement">{claim.statement}</h1>
        <div className="cluster">
          <StatusPill value={claim.status} />
          <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>{claim.confidence}</span>
        </div>
      </header>

      {/* Status + confidence already sit in the header cluster above — the
          KvList carries only what isn't shown there. */}
      <KvList
        rows={[
          { key: 'Scope', value: claim.scope || <span className="faint">—</span> },
          { key: 'Created', value: <span className="mono" style={{ fontSize: 'var(--text-xs)' }}>{claim.created_at}</span> },
        ]}
      />

      <section className="section" style={{ marginTop: 32 }}>
        <div className="section-title">Experiments testing this claim</div>
        {linkedExperiments.length === 0 ? (
          <div className="empty">No experiments link to this claim yet.</div>
        ) : (
          <div className="list card card--flush">
            {linkedExperiments.map(e => (
              <Link key={e.id} to={px(`/experiments/${e.id}`)} className="list-row">
                <div className="list-row-main">
                  <div className="list-row-title">{e.intent}</div>
                  <div className="list-row-sub">
                    {expName(e)} · attempt {e.attempt_index}
                  </div>
                </div>
                <div className="list-row-aside">
                  <StatusPill value={e.status} />
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

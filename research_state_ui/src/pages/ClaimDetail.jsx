import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api';
import { useProjectStore, selectExperiments, useProjectHref } from '../store/useProjectStore';
import ObjId from '../components/ObjId';
import StatusPill from '../components/StatusPill';
import { ConfidenceDots, ClaimExperimentList } from '../components/ClaimEvidence';
import { fmtStamp } from '../utils/format';

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
        <div className="claim-entry-meta">
          <StatusPill value={claim.status} />
          <ConfidenceDots level={claim.confidence} />
          {claim.scope && <span className="claim-entry-scope">scoped to {claim.scope}</span>}
          {claim.created_at && <span>created {fmtStamp(Date.parse(claim.created_at))}</span>}
        </div>
      </header>

      <section className="section" style={{ marginTop: 32 }}>
        <div className="section-title">Experiments testing this claim</div>
        {linkedExperiments.length === 0 ? (
          <div className="empty">No experiments link to this claim yet.</div>
        ) : (
          <ClaimExperimentList experiments={linkedExperiments} />
        )}
      </section>
    </div>
  );
}

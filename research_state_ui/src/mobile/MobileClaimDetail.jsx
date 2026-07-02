import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api';
import { useProjectStore, selectExperiments, useProjectHref } from '../store/useProjectStore';
import { claimStatusColor } from '../utils/evidence';
import { ConfidenceDots, ClaimExperimentList } from '../components/ClaimEvidence';
import { fmtStamp } from '../utils/format';
import { Skeleton } from './Skeleton';

/**
 * Mobile claim detail — one continuous flush surface, the same grammar as
 * the mobile experiment detail (design_philosophy.md): the statement IS the
 * page; status, confidence, scope, and created date collapse into ONE
 * colored status block — no KvList box, no status pill, no separate cards.
 * Read-only — recording claims is the agent's / desktop's job.
 */
export default function MobileClaimDetail() {
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
      <div className="mdetail">
        <div className="error-message">{error}</div>
        <Link className="btn" to={px('/claims')} style={{ marginTop: 12 }}>← Claims</Link>
      </div>
    );
  }
  if (!claim) {
    return (
      <div className="mdetail">
        <header className="page-header"><Skeleton lines={1} /></header>
        <Skeleton lines={4} />
      </div>
    );
  }

  const status = (claim.status || 'active').toLowerCase();
  const color = claimStatusColor(status);

  return (
    <div className="mdetail">
      <header className="page-header">
        <div className="page-eyebrow">
          <Link to={px('/claims')}>‹ Claims</Link>
        </div>
        <h1 className="page-title page-title--statement">{claim.statement}</h1>
      </header>

      <div className="mstatus">
        <span className="mstatus-ix" style={{ background: color }} aria-hidden="true" />
        <div className="mstatus-body">
          <div className="mstatus-line" style={{ color }}>{status}</div>
          <div className="mstatus-next">
            <ConfidenceDots level={claim.confidence} />
            {claim.scope && <span> scoped: {claim.scope}</span>}
          </div>
          {claim.created_at && (
            <div className="mstatus-next">created {fmtStamp(Date.parse(claim.created_at))}</div>
          )}
        </div>
      </div>

      <div className="mbreak" />

      <section className="mdetail-section">
        <div className="mml">Evidence</div>
        {linkedExperiments.length === 0 ? (
          <div className="mquiet">no experiments test this claim yet</div>
        ) : (
          <ClaimExperimentList experiments={linkedExperiments} />
        )}
      </section>
    </div>
  );
}

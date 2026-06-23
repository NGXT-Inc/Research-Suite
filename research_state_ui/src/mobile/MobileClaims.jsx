import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, useProjectHref, selectClaims, selectExperiments } from '../store/useProjectStore';
import { expName } from '../utils/experiment';
import { SkeletonCards } from './Skeleton';

// Lifecycle order for the filter chips.
const STATUS_ORDER = ['active', 'supported', 'weakened', 'contradicted', 'draft', 'abandoned'];
const CONFIDENCE = { low: 1, medium: 2, high: 3 };
const SUPPORT = new Set(['supports', 'supported', 'complete', 'completed', 'pass', 'accepted', 'succeeded']);
const AGAINST = new Set(['refutes', 'contradicted', 'weakened', 'failed', 'fail', 'rejected']);

function categorize(status) {
  const s = (status || '').toLowerCase();
  if (SUPPORT.has(s)) return 'success';
  if (AGAINST.has(s)) return 'against';
  return 'idle';
}

/**
 * MobileClaims — read-only replacement for the desktop Claims page (which
 * exposes a "New claim" form). Recording claims is the agent's / desktop's
 * job; the phone reads. docs/MOBILE_UX_REVIEW.md §2.6.
 */
export default function MobileClaims() {
  const claims = useProjectStore(selectClaims);
  const experiments = useProjectStore(selectExperiments);
  const home = useProjectStore(s => s.home);
  const [filter, setFilter] = useState('all');

  const byClaim = useMemo(() => {
    const m = new Map();
    for (const e of experiments) {
      for (const tc of (Array.isArray(e.tested_claims) ? e.tested_claims : [])) {
        if (!tc?.id) continue;
        if (!m.has(tc.id)) m.set(tc.id, []);
        m.get(tc.id).push(e);
      }
    }
    return m;
  }, [experiments]);

  const counts = useMemo(() => {
    const map = { all: claims.length };
    for (const c of claims) {
      const k = (c.status || 'active').toLowerCase();
      map[k] = (map[k] || 0) + 1;
    }
    return map;
  }, [claims]);

  const rows = useMemo(
    () => (filter === 'all' ? claims : claims.filter(c => (c.status || 'active').toLowerCase() === filter)),
    [claims, filter],
  );
  const chips = ['all', ...STATUS_ORDER.filter(s => counts[s])];

  if (!home) {
    return (
      <div className="page-stage">
        <header className="page-header"><h1 className="page-title">What we think</h1></header>
        <SkeletonCards />
      </div>
    );
  }

  return (
    <div className="page-stage">
      <header className="page-header"><h1 className="page-title">What we think</h1></header>

      <div className="mchips" role="tablist" aria-label="Filter by status">
        {chips.map(s => (
          <button
            key={s}
            type="button"
            role="tab"
            aria-selected={filter === s}
            className={`mchip${filter === s ? ' active' : ''}`}
            onClick={() => setFilter(s)}
          >
            {s}
            <span className="mchip-count">{counts[s] || 0}</span>
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <div className="empty-state">
          <h2>No claims{filter !== 'all' ? ` in ${filter}` : ' yet'}</h2>
        </div>
      ) : (
        <div className="mcard-list">
          {rows.map(c => <ClaimCard key={c.id} claim={c} linked={byClaim.get(c.id) || []} />)}
        </div>
      )}
    </div>
  );
}

function ConfidenceDots({ level }) {
  const n = CONFIDENCE[(level || '').toLowerCase()] || 0;
  return (
    <span className="mconf" aria-label={level ? `${level} confidence` : 'confidence unset'}>
      {[1, 2, 3].map(i => <span key={i} className={`mconf-dot${i <= n ? ' on' : ''}`} aria-hidden="true" />)}
    </span>
  );
}

function ClaimCard({ claim, linked }) {
  const px = useProjectHref();
  return (
    <Link to={px(`/claims/${claim.id}`)} className="mcard">
      <div className="mcard-title" style={{ marginBottom: 6 }}>{claim.statement}</div>
      <div className="mcard-meta">
        <ConfidenceDots level={claim.confidence} />
        <span>{(claim.status || 'active')}</span>
        {claim.scope && <span>scoped: {claim.scope}</span>}
      </div>
      {linked.length > 0 && (
        <div className="mclaim-tests">
          {linked.map(e => {
            const cat = categorize(e.status);
            return (
              <span key={e.id} className="mclaim-test">
                <span className={`mclaim-mark mclaim-mark--${cat}`} aria-hidden="true">
                  {cat === 'success' ? '✓' : cat === 'against' ? '✗' : '·'}
                </span>
                {expName(e)}
              </span>
            );
          })}
        </div>
      )}
    </Link>
  );
}

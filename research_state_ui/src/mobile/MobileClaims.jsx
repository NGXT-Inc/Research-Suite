import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, useProjectHref, selectClaims, selectExperiments } from '../store/useProjectStore';
import { claimStatusColor } from '../utils/evidence';
import { ConfidenceDots, ClaimExperimentList } from '../components/ClaimEvidence';
import { SkeletonCards } from './Skeleton';

// Lifecycle order for the filter, same vocabulary as the desktop tabs.
const STATUS_ORDER = ['active', 'supported', 'weakened', 'contradicted', 'draft', 'abandoned'];

/**
 * Claims — the flush lifecycle-row language established by Experiments
 * (design_philosophy.md): an underline filter instead of boxed chips, then
 * hairline-separated rows whose 3px left index carries the claim's own
 * status. Read-only — recording claims is the agent's / desktop's job.
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
      <div className="mlist">
        <h1 className="mtitle-lg">What we think</h1>
        <SkeletonCards />
      </div>
    );
  }

  return (
    <div className="mlist">
      <h1 className="mtitle-lg">What we think</h1>

      <div className="mefilt" role="tablist" aria-label="Filter by status">
        {chips.map(s => (
          <button
            key={s}
            type="button"
            role="tab"
            aria-selected={filter === s}
            className={filter === s ? 'on' : ''}
            onClick={() => setFilter(s)}
          >
            {s}<span className="c tabular">{counts[s] || 0}</span>
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <div className="mquiet">no claims{filter !== 'all' ? ` in ${filter}` : ' yet'}</div>
      ) : (
        rows.map(c => <ClaimRow key={c.id} claim={c} linked={byClaim.get(c.id) || []} />)
      )}
    </div>
  );
}

function ClaimRow({ claim, linked }) {
  const px = useProjectHref();
  const status = (claim.status || 'active').toLowerCase();
  const color = claimStatusColor(status);

  return (
    <Link to={px(`/claims/${claim.id}`)} className="merow">
      <span className="merow-ix" style={{ background: color }} aria-hidden="true" />
      <span className="merow-main">
        <span className="merow-name">{claim.statement}</span>
        <span className="merow-status" style={{ color }}>{status}</span>
        <span className="merow-meta">
          <ConfidenceDots level={claim.confidence} />
          {claim.scope && <span>scoped: {claim.scope}</span>}
        </span>
        <ClaimExperimentList experiments={linked} dense />
      </span>
    </Link>
  );
}

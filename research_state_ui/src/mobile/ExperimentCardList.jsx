import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, selectExperiments, useProjectHref } from '../store/useProjectStore';
import { SkeletonCards } from './Skeleton';
import { expName, statusColor, statusLine } from '../utils/experiment';
import { fmtDuration } from '../utils/format';

// Lifecycle groups for the underline text filter, in FSM order.
const GROUPS = [
  { id: 'running', label: 'Running', statuses: ['running'] },
  { id: 'review', label: 'Review', statuses: ['design_review', 'experiment_review'] },
  { id: 'ready', label: 'Ready', statuses: ['planned', 'ready_to_run'] },
  { id: 'done', label: 'Done', statuses: ['complete'] },
  { id: 'failed', label: 'Failed', statuses: ['failed'] },
  { id: 'abandoned', label: 'Abandoned', statuses: ['abandoned'] },
];

/**
 * Experiments — the lifecycle list in the One-Surface language
 * (design_handoff_mobile_redesign): an underline text filter instead of
 * boxed chips, then flush hairline-separated rows whose 3px left index is
 * colored by state. Read-only — creating experiments is the agent's job.
 */
export default function ExperimentCardList() {
  const experiments = useProjectStore(selectExperiments);
  const home = useProjectStore(s => s.home);
  const [filter, setFilter] = useState('all');
  const now = Date.now();

  const counts = useMemo(() => {
    const by = { all: experiments.length };
    for (const g of GROUPS) {
      by[g.id] = experiments.filter(e => g.statuses.includes((e.status || 'planned').toLowerCase())).length;
    }
    return by;
  }, [experiments]);

  const rows = useMemo(() => {
    const group = GROUPS.find(g => g.id === filter);
    const list = group
      ? experiments.filter(e => group.statuses.includes((e.status || 'planned').toLowerCase()))
      : experiments;
    return list.slice().sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  }, [experiments, filter]);

  if (!home) {
    return (
      <div className="mlist">
        <h1 className="mtitle-lg">Experiments</h1>
        <SkeletonCards />
      </div>
    );
  }

  return (
    <div className="mlist">
      <h1 className="mtitle-lg">Experiments</h1>

      <div className="mefilt" role="tablist" aria-label="Filter by state">
        {[{ id: 'all', label: 'All' }, ...GROUPS.filter(g => counts[g.id])].map(g => (
          <button
            key={g.id}
            type="button"
            role="tab"
            aria-selected={filter === g.id}
            className={filter === g.id ? 'on' : ''}
            onClick={() => setFilter(g.id)}
          >
            {g.label}<span className="c tabular">{counts[g.id]}</span>
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <div className="mquiet">no experiments{filter !== 'all' ? ' in this state' : ' yet'}</div>
      ) : (
        rows.map(e => <ExperimentRow key={e.id} experiment={e} now={now} />)
      )}
    </div>
  );
}

function ExperimentRow({ experiment: e, now }) {
  const px = useProjectHref();
  const status = (e.status || 'planned').toLowerCase();
  const color = statusColor(status);
  const settled = ['complete', 'failed', 'abandoned'].includes(status);
  const endMs = settled && e.updated_at ? Date.parse(e.updated_at) : now;
  const durationMs = e.created_at ? Math.max(0, endMs - Date.parse(e.created_at)) : NaN;
  const claimCount = Array.isArray(e.tested_claims) ? e.tested_claims.length : 0;

  return (
    <Link to={px(`/experiments/${e.id}`)} className="merow">
      <span className="merow-ix" style={{ background: color }} aria-hidden="true" />
      <span className="merow-main">
        <span className="merow-name">{expName(e)}</span>
        <span className="merow-status" style={{ color }}>{statusLine(e, status, now)}</span>
        <span className="merow-meta">
          <span>attempt {e.attempt_index}</span>
          {claimCount > 0 && <span>tests {claimCount} claim{claimCount === 1 ? '' : 's'}</span>}
          {Number.isFinite(durationMs) && <span>{fmtDuration(durationMs)}</span>}
        </span>
      </span>
    </Link>
  );
}

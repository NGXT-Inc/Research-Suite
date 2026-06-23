import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useProjectStore, selectExperiments, useProjectHref } from '../store/useProjectStore';
import StatusPill from '../components/StatusPill';
import { SkeletonCards } from './Skeleton';
import { expName } from '../utils/experiment';
import { fmtDayTime, fmtDuration } from '../utils/format';
import { classifyExperiment, outcomeColor, outcomeGlyph } from '../utils/evidence';

// Chip order mirrors the FSM so the filter row reads as the lifecycle.
const STATUS_ORDER = [
  'planned', 'design_review', 'ready_to_run', 'running',
  'experiment_review', 'complete', 'failed', 'abandoned',
];

function isTerminal(status) {
  return status === 'complete' || status === 'failed' || status === 'abandoned';
}

/**
 * Mobile replacement for the Experiments grid table: filter chips by FSM
 * state on top, one card per experiment below. Read-only — creating
 * experiments is the agent's (or desktop's) job.
 */
export default function ExperimentCardList() {
  const experiments = useProjectStore(selectExperiments);
  const home = useProjectStore(s => s.home);
  const [filter, setFilter] = useState('all');

  const counts = useMemo(() => {
    const map = { all: experiments.length };
    for (const e of experiments) {
      const s = (e.status || 'planned').toLowerCase();
      map[s] = (map[s] || 0) + 1;
    }
    return map;
  }, [experiments]);

  const rows = useMemo(() => {
    let list = experiments;
    if (filter !== 'all') list = list.filter(e => (e.status || '').toLowerCase() === filter);
    return list.slice().sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  }, [experiments, filter]);

  const chips = ['all', ...STATUS_ORDER.filter(s => counts[s])];

  if (!home) {
    return (
      <div className="page-stage">
        <header className="page-header"><h1 className="page-title">What we try</h1></header>
        <SkeletonCards />
      </div>
    );
  }

  return (
    <div className="page-stage">
      <header className="page-header">
        <h1 className="page-title">What we try</h1>
      </header>

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
            {s.replace(/_/g, ' ')}
            <span className="mchip-count">{counts[s] || 0}</span>
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <div className="empty-state">
          <h2>No experiments{filter !== 'all' ? ` in ${filter.replace(/_/g, ' ')}` : ' yet'}</h2>
        </div>
      ) : (
        <div className="mcard-list">
          {rows.map(e => <ExperimentCard key={e.id} experiment={e} />)}
        </div>
      )}
    </div>
  );
}

function ExperimentCard({ experiment: e }) {
  const px = useProjectHref();
  const status = (e.status || 'planned').toLowerCase();
  const created = fmtDayTime(e.created_at);
  const settled = isTerminal(status);
  const endMs = settled && e.updated_at ? Date.parse(e.updated_at) : Date.now();
  const durationMs = e.created_at ? Math.max(0, endMs - Date.parse(e.created_at)) : NaN;
  const outcome = classifyExperiment(e);
  const claimCount = Array.isArray(e.tested_claims) ? e.tested_claims.length : 0;

  return (
    <Link to={px(`/experiments/${e.id}`)} className="mcard">
      <div className="mcard-head">
        <span className="mcard-glyph" style={{ color: outcomeColor(outcome) }} aria-hidden="true">
          {outcomeGlyph(outcome)}
        </span>
        <div className="mcard-title">{expName(e)}</div>
        <StatusPill value={e.status} />
      </div>
      {e.intent && <div className="mcard-sub">{e.intent}</div>}
      <div className="mcard-meta">
        <span>attempt {e.attempt_index}</span>
        {claimCount > 0 && <span>tests {claimCount} claim{claimCount === 1 ? '' : 's'}</span>}
        {created && <span>{created.day} · {created.time}</span>}
        <span>{settled ? fmtDuration(durationMs) : `${fmtDuration(durationMs)} elapsed`}</span>
      </div>
    </Link>
  );
}

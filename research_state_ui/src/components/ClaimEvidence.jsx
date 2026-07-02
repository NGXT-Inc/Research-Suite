import { Link } from 'react-router-dom';
import { useProjectHref } from '../store/useProjectStore';
import { expName } from '../utils/experiment';
import { classifyExperiment, outcomeColor, outcomeLabel, outcomeGlyph } from '../utils/evidence';

const CONFIDENCE_LEVELS = { low: 1, medium: 2, high: 3 };

/**
 * The single renderer for "what does the evidence say about this claim" —
 * shared by the desktop and mobile list/detail pages so a claim looks the
 * same everywhere instead of drifting into per-page chrome.
 */
export function ConfidenceDots({ level }) {
  const n = CONFIDENCE_LEVELS[(level || '').toLowerCase()] || 0;
  const label = level ? `${level} confidence` : 'confidence unset';
  return (
    <span className="claim-conf" title={label} aria-label={label}>
      {[1, 2, 3].map(i => (
        <span key={i} className={`claim-conf-dot${i <= n ? ' is-on' : ''}`} aria-hidden="true" />
      ))}
    </span>
  );
}

// Every experiment that tested this claim, marked with the same outcome
// glyph/color the Logic DAG already trusts (classifyExperiment) — not a
// re-derived heuristic per page. `dense` (mobile list rows, tight space)
// collapses the full line down to a wrapping row of glyph+name chips — same
// data, no separate outcome-label text, and NOT individually navigable:
// dense only ever renders inside the claim row's own <Link>, and a nested
// <a> is invalid HTML that breaks the outer row's tap target.
export function ClaimExperimentList({ experiments, dense = false }) {
  const px = useProjectHref();
  if (!experiments || experiments.length === 0) return null;

  if (dense) {
    return (
      <div className="claim-exp-inline">
        {experiments.map(e => {
          const outcome = classifyExperiment(e);
          return (
            <span key={e.id} className="claim-exp-chip">
              <span style={{ color: outcomeColor(outcome) }} aria-hidden="true">{outcomeGlyph(outcome)}</span>
              {expName(e)}
            </span>
          );
        })}
      </div>
    );
  }

  return (
    <ul className="claim-entry-tests">
      {experiments.map(e => {
        const outcome = classifyExperiment(e);
        return (
          <li key={e.id}>
            <Link to={px(`/experiments/${e.id}`)} className="claim-exp-line">
              <span className="claim-exp-mark" style={{ color: outcomeColor(outcome) }} aria-hidden="true">
                {outcomeGlyph(outcome)}
              </span>
              <span className="claim-exp-title">{expName(e)}</span>
              <span className="claim-exp-status">{outcomeLabel(outcome)}</span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

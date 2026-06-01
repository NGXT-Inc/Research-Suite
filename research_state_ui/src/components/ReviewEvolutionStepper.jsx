import { useState } from 'react';
import ReviewCard from './ReviewCard';

/**
 * Horizontal stepper showing how the plan evolved.
 *
 *   [v1] ─[needs_changes · 4]─→ [v2] ─[pass · 1]─→ ✓ accepted
 *
 * Two interactions, independent:
 *   - Click a v_k pill → tells the parent to render the plan AT that version
 *     (via onSelectVersion). The active version pill is outlined.
 *   - Click a verdict pill → expands the ReviewCard for that review inline.
 *     Only one verdict card is expanded at a time so the section stays quiet.
 */
export default function ReviewEvolutionStepper({
  reviews,
  currentAttempt,
  experimentStatus,
  selectedAttempt = null,
  onSelectVersion,
  versionAvailability = {},
}) {
  const [expandedIdx, setExpandedIdx] = useState(null);

  const segments = [];
  for (let v = 1; v <= currentAttempt; v++) {
    segments.push({ version: v, review: reviews[v - 1] || null });
  }

  const lastReview = reviews[reviews.length - 1];
  let terminus = null;
  if (lastReview?.verdict === 'pass') {
    terminus = 'accepted';
  } else if (experimentStatus === 'design_review' && reviews.length < currentAttempt) {
    terminus = 'awaiting';
  } else if (experimentStatus === 'planned' && reviews.length === currentAttempt && lastReview && lastReview.verdict !== 'pass') {
    terminus = 'revising';
  }

  return (
    <div className="review-stepper-wrap">
      <div className="review-stepper">
        {segments.map((seg, i) => {
          const last = i === segments.length - 1;
          const showTerminus = last ? terminus : null;
          const isSelected = selectedAttempt === seg.version;
          const avail = versionAvailability[seg.version];
          const versionBtnClasses = [
            'review-stepper-version',
            isSelected ? 'review-stepper-version--active' : '',
            avail === false ? 'review-stepper-version--unavailable' : '',
          ].filter(Boolean).join(' ');
          return (
            <span key={seg.version} className="review-stepper-seg">
              {onSelectVersion && avail !== false ? (
                <button
                  type="button"
                  className={versionBtnClasses}
                  onClick={() => onSelectVersion(seg.version)}
                  title={`View plan at v${seg.version}`}
                >
                  v{seg.version}
                </button>
              ) : (
                <span
                  className={versionBtnClasses}
                  title={
                    avail === false
                      ? `v${seg.version}: historical snapshot not available`
                      : undefined
                  }
                >
                  v{seg.version}
                </span>
              )}
              {seg.review ? (
                <>
                  <span className="review-stepper-arrow">→</span>
                  <button
                    type="button"
                    className={`review-stepper-pill review-stepper-pill--${seg.review.verdict || 'unknown'}${expandedIdx === i ? ' expanded' : ''}`}
                    onClick={() => setExpandedIdx(expandedIdx === i ? null : i)}
                  >
                    <span>{(seg.review.verdict || 'review').replace(/_/g, ' ')}</span>
                    {Array.isArray(seg.review.findings) && seg.review.findings.length > 0 && (
                      <span className="review-stepper-pill-count">{seg.review.findings.length}</span>
                    )}
                  </button>
                </>
              ) : last && (experimentStatus === 'design_review') ? (
                <>
                  <span className="review-stepper-arrow">→</span>
                  <span className="review-stepper-await">awaiting review</span>
                </>
              ) : null}
              {showTerminus === 'accepted' && (
                <>
                  <span className="review-stepper-arrow">→</span>
                  <span className="review-stepper-terminus review-stepper-terminus--accepted">✓ accepted</span>
                </>
              )}
              {showTerminus === 'revising' && (
                <>
                  <span className="review-stepper-arrow">→</span>
                  <span className="review-stepper-await">revising</span>
                </>
              )}
            </span>
          );
        })}
      </div>
      {expandedIdx != null && segments[expandedIdx]?.review && (
        <div className="review-stepper-expanded">
          <ReviewCard review={segments[expandedIdx].review} />
          <div style={{ marginTop: 6 }}>
            <button className="btn btn--sm btn--ghost" onClick={() => setExpandedIdx(null)}>
              Collapse review
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

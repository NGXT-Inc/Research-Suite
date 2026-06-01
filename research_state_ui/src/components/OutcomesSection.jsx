import { useState } from 'react';
import ReviewCard from './ReviewCard';
import ResourceList from './ResourceList';

/**
 * OutcomesSection — result files + experiment review verdict.
 *
 * Only renders when there's something to show. The experiment review uses
 * the same review-stepper visual language as the design reviews so the
 * page reads coherently across stages.
 */
export default function OutcomesSection({
  projectId,
  outcomeResources,
  experimentReviews,
  experimentStatus,
}) {
  const hasResults = outcomeResources.length > 0;
  const hasReviews = experimentReviews.length > 0;
  const inExpReview = experimentStatus === 'experiment_review';

  if (!hasResults && !hasReviews && !inExpReview) {
    return null;
  }

  return (
    <section id="outcomes" className="spotlight outcomes">
      <header className="spotlight-head">
        <div className="spotlight-eyebrow">Outcomes &amp; review</div>
      </header>

      {hasResults ? (
        <div style={{ marginTop: 6 }}>
          <div className="outcomes-subhead">
            Result files
            <span className="tab-count">{outcomeResources.length}</span>
          </div>
          <ResourceList projectId={projectId} resources={outcomeResources} />
        </div>
      ) : inExpReview ? (
        <div className="empty" style={{ marginTop: 6 }}>Results submitted for review.</div>
      ) : null}

      {(hasReviews || inExpReview) && (
        <div style={{ marginTop: hasResults ? 18 : 6 }}>
          <div className="outcomes-subhead">
            Experiment reviews
            <span className="tab-count">{experimentReviews.length}</span>
          </div>
          {hasReviews ? (
            <ExperimentReviewStepper reviews={experimentReviews} />
          ) : (
            <div className="empty" style={{ fontSize: 'var(--text-sm)' }}>
              Awaiting reviewer.
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function ExperimentReviewStepper({ reviews }) {
  const sorted = [...reviews].sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
  const [expandedIdx, setExpandedIdx] = useState(sorted.length === 1 ? 0 : null);
  return (
    <div className="review-stepper-wrap">
      <div className="review-stepper">
        {sorted.map((r, i) => (
          <span key={r.id || i} className="review-stepper-seg">
            {i > 0 && <span className="review-stepper-arrow">→</span>}
            <button
              type="button"
              className={`review-stepper-pill review-stepper-pill--${r.verdict || 'unknown'}${expandedIdx === i ? ' expanded' : ''}`}
              onClick={() => setExpandedIdx(expandedIdx === i ? null : i)}
            >
              <span>{(r.verdict || 'review').replace(/_/g, ' ')}</span>
              {Array.isArray(r.findings) && r.findings.length > 0 && (
                <span className="review-stepper-pill-count">{r.findings.length}</span>
              )}
            </button>
          </span>
        ))}
      </div>
      {expandedIdx != null && sorted[expandedIdx] && (
        <div className="review-stepper-expanded">
          <ReviewCard review={sorted[expandedIdx]} />
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

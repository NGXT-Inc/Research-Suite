import { useState } from 'react';
import ReviewCard from './ReviewCard';

/**
 * Horizontal stepper showing how the plan evolved.
 *
 *   [v1] ─[needs_changes · 4]─→ [v2] ─[pass · 1]─→ ✓ accepted
 *
 * One interaction: click a verdict pill → expands the ReviewCard for that
 * review inline. Only one verdict card is expanded at a time so the section
 * stays quiet.
 */
export default function ReviewEvolutionStepper({
  reviews,
  currentAttempt,
  experimentStatus,
}) {
  const [expandedIdx, setExpandedIdx] = useState(null);

  // A single review on the first attempt has no progression to tell — the
  // verdict is already the plan's status badge, so render just the reasoning.
  if (reviews.length <= 1 && currentAttempt <= 1) {
    return reviews[0] ? <ReviewCard review={reviews[0]} bare /> : null;
  }

  const segments = [];
  for (let v = 1; v <= currentAttempt; v++) {
    segments.push({ version: v, review: reviews[v - 1] || null });
  }

  return (
    <div className="review-stepper-wrap">
      <div className="review-stepper">
        {segments.map((seg, i) => {
          const last = i === segments.length - 1;
          return (
            <span key={seg.version} className="review-stepper-seg">
              <span className="review-stepper-version">v{seg.version}</span>
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
            </span>
          );
        })}
      </div>
      {expandedIdx != null && segments[expandedIdx]?.review && (
        <div className="review-stepper-expanded">
          <ReviewCard review={segments[expandedIdx].review} bare />
        </div>
      )}
    </div>
  );
}

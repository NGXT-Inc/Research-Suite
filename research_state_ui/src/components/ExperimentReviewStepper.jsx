import { useState } from 'react';
import ReviewCard from './ReviewCard';

/**
 * ExperimentReviewStepper — the experiment (results) review timeline, in the
 * same horizontal verdict-pill language as the design ReviewEvolutionStepper.
 *
 *   [needs_changes · 4] → [pass · 1]
 *
 * One interaction: click a verdict pill → expand its ReviewCard inline. A lone
 * review starts expanded; with several, all start collapsed so the section
 * stays quiet. Shared by OutcomesSection and the ReportSpotlight "Show review"
 * disclosure so the results review reads the same wherever it surfaces.
 */
export default function ExperimentReviewStepper({ reviews }) {
  const sorted = [...reviews].sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
  const [expandedIdx, setExpandedIdx] = useState(null);

  // A lone review needs no stepper — the pill would just duplicate the verdict
  // already in the card head. Render the content directly.
  if (sorted.length === 1) {
    return <ReviewCard review={sorted[0]} bare />;
  }

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
          <ReviewCard review={sorted[expandedIdx]} bare />
        </div>
      )}
    </div>
  );
}

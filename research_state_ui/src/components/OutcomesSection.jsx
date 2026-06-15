import ResourceList from './ResourceList';
import ExperimentReviewStepper from './ExperimentReviewStepper';

/**
 * OutcomesSection — result files + experiment review verdict.
 *
 * Only renders when there's something to show. The experiment review uses
 * the same review-stepper visual language as the design reviews so the
 * page reads coherently across stages.
 *
 * `hideReviews` lets a host that already shows the experiment review elsewhere
 * (the ReportSpotlight "Show review" disclosure) suppress the review block here
 * so it isn't rendered twice. When set, this section is purely result files.
 */
export default function OutcomesSection({
  projectId,
  outcomeResources,
  experimentReviews,
  experimentStatus,
  hideReviews = false,
}) {
  const hasResults = outcomeResources.length > 0;
  const hasReviews = experimentReviews.length > 0;
  const inExpReview = experimentStatus === 'experiment_review';
  const showReviewBlock = !hideReviews && (hasReviews || inExpReview);

  if (!hasResults && !showReviewBlock) {
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
      ) : showReviewBlock && inExpReview ? (
        <div className="empty" style={{ marginTop: 6 }}>Results submitted for review.</div>
      ) : null}

      {showReviewBlock && (
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

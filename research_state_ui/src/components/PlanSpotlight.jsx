import { useEffect, useState } from 'react';
import { api } from '../api';
import PlanBody from './PlanBody';
import ReviewEvolutionStepper from './ReviewEvolutionStepper';
import ContentUnavailable from './ContentUnavailable';

/**
 * PlanSpotlight — the design artifact.
 *
 * The plan resource is the framing document of the experiment, so we render
 * its content inline (always the live file — the backend stores version
 * metadata only, not historical content). Above the body, a compact header
 * bar (path + size + status), and the ReviewEvolutionStepper showing
 * v1→v2→…→accepted.
 */
export default function PlanSpotlight({
  projectId,
  planResource,
  designReviews,
  attemptIndex,
  experimentStatus,
  defaultOpen = true,
}) {
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showBody, setShowBody] = useState(defaultOpen);
  const [showReview, setShowReview] = useState(false);

  useEffect(() => {
    if (!planResource) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setContent(null);
    api.getResourceContent(projectId, planResource.id)
      .then(d => { if (!cancelled) setContent(d); })
      .catch(e => { if (!cancelled) setError(e.message); })
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [projectId, planResource?.id, planResource?.version_token]);

  if (!planResource) {
    return (
      <section id="design" className="spotlight">
        <div className="spotlight-eyebrow">Plan</div>
        <div className="spotlight-empty">
          No plan resource registered for this attempt yet.
        </div>
      </section>
    );
  }

  const latestReview = designReviews[designReviews.length - 1];
  let planStatus = 'drafting';
  if (latestReview?.verdict === 'pass') planStatus = 'accepted';
  else if (experimentStatus === 'design_review') planStatus = 'under review';
  else if (designReviews.length > 0) planStatus = 'revising';

  const reviewAvailable = designReviews.length > 0 || experimentStatus === 'design_review';

  return (
    <section id="design" className="spotlight">
      <header className="spotlight-head spotlight-head--row">
        <div className="spotlight-head-left">
          <span className="spotlight-eyebrow">Plan</span>
          <span className={`plan-status plan-status--${planStatus.replace(/\s+/g, '_')}`}>{planStatus}</span>
        </div>
        <div className="spotlight-head-right">
          <span className="mono spotlight-bar-path">{planResource.path}</span>
          {reviewAvailable && (
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => setShowReview(v => !v)}
            >
              <span className="toggle-verb">{showReview ? 'Hide' : 'Show'}</span>{' review'}
            </button>
          )}
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => setShowBody(v => !v)}
          >
            <span className="toggle-verb">{showBody ? 'Hide' : 'Show'}</span>{' plan'}
          </button>
        </div>
      </header>

      {showReview && reviewAvailable && (
        <div className="spotlight-review">
          <ReviewEvolutionStepper
            reviews={designReviews}
            currentAttempt={attemptIndex}
            experimentStatus={experimentStatus}
          />
        </div>
      )}

      {showBody && (
        <div className="spotlight-body">
          {loading ? (
            <div className="empty">Loading plan…</div>
          ) : error ? (
            <div className="error-message">{error}</div>
          ) : content ? (
            content.available === false ? (
              <ContentUnavailable content={content} />
            ) : content.is_binary ? (
              <div className="empty">Binary plan file</div>
            ) : (
              <PlanBody text={content.content ?? ''} path={planResource.path} />
            )
          ) : null}
        </div>
      )}
    </section>
  );
}
